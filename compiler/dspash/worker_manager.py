from collections import defaultdict
import socket
import os
from threading import Event, Thread, Lock
import time
import queue
import pickle
import json
import traceback
from typing import Dict, List, Tuple
from uuid import UUID

from dspash.socket_utils import SocketManager, encode_request, decode_request, send_msg, recv_msg
from ir import IR
from definitions.ir.file_id import FileId
from util import log
from ir_to_ast import to_shell
from dspash.ir_helper import prepare_graph_for_remote_exec, to_shell_file, split_main_graph
from dspash.utils import read_file
from dspash.hdfs_utils import start_hdfs_daemon, stop_hdfs_daemon
import config 
import copy
import requests

import grpc
import dspash.proto.data_stream_pb2 as data_stream_pb2
import dspash.proto.data_stream_pb2_grpc as data_stream_pb2_grpc

# For profiling
import cProfile
import pstats
import io


HOST = socket.gethostbyname(socket.gethostname())
PORT = 65425        # Port to listen on (non-privileged ports are > 1023)
PORT_CLIENT = 65432
# TODO: get the url from the environment or config
DEBUG_URL = f'http://{socket.getfqdn()}:5001' 
KILL_WITNESS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'kill_witness.log')

class WorkerConnection:
    def __init__(self, name, host, port, args):
        self.name = name
        self._host = socket.gethostbyaddr(host)[2][0] # get ip address in case host needs resolving
        self._port = port
        self.args = args
        self._running_processes = 0
        self._online = True
        # assume client service is running, can add a way to activate later
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.connect((self._host, self._port))
        except Exception as e:
            log(f"Failed to connect to {self._host}:{self._port} with error {e}")
            self._online = False

    def is_online(self):
        # TODO: create a ping to confirm is online
        return self._online

    def get_running_processes(self):
        # request_dict = { 'type': 'query',
        #                 'fields': ['running_processes']
        # }
        # with self.socket.connect((self.host, self.port)):
        #     self.socket.send(request)
        #     # TODO wait until the command exec finishes and run this in parallel?
        #     answer = self.socket.recv(1024)
        return self._running_processes

    def send_request(self, request_dict: dict, wait_ack=True):
        request = encode_request(request_dict)
        send_msg(self._socket, request)
        if wait_ack:
            self.handle_response()

    def handle_response(self):
        response_data = recv_msg(self._socket)
        if not response_data:
            raise Exception(f"didn't recieved ack on request {response_data}")

    def send_setup_request(self):
        request_dict = { 
            'type': 'Setup',
            'debug': self.args.debug,
            'pool_size': self.args.pool,
            'ft': self.args.ft,
            'script_name': self.args.script_name,
            # here kill is either merger or regular, we send it to not update execution times
            'kill_target': self.args.kill,
        }
        # we no longer push logs to flask app
        # if self.args.debug:
        #     request_dict['debug'] = {'name': self.name, 'url': f'{DEBUG_URL}/putlog'}
        self.send_request(request_dict)

    def send_kill_node_request(self):
        request_dict = {
            'type': 'Kill-Node', 
            'kill_target': self._host, 
            'kill_delay': self.args.kill_delay, 
        }
        self.send_request(request_dict)

    def send_kill_subgraphs_request(self, merger_id: int):
        # if merger_id is -1, it kills everything
        request_dict = { 'type': 'Kill-Subgraphs', 'merger_id': merger_id }
        self.send_request(request_dict)

    def send_batch_graph_exec_request(self, shell_vars, functions, merger_id, regulars, mergers, wait_ack=False):
        request_dict = { 
            'type': 'Batch-Exec-Graph',
            'regulars': regulars,
            'mergers': mergers,
            'shell_variables': None, # Doesn't seem needed for now
            'functions': functions,
            'merger_id': merger_id,
        }
        self.send_request(request_dict, wait_ack=wait_ack)

    def send_graph_exec_request(self, graph, shell_vars, functions, merger_id) -> bool:
        request_dict = { 
            'type': 'Exec-Graph',
            'graph': graph,
            'shell_variables': None, # Doesn't seem needed for now
            'functions': functions,
            'merger_id': merger_id,
        }

        self.send_request(request_dict)

    def close(self):
        self._socket.send("Done")
        self._socket.close()

    def _wait_ack(self):
        confirmation = self._socket.recv(4096)
        if not confirmation or decode_request(confirmation).status != "OK":
            return False
        else:
            return True

    def __str__(self):
        return f"Worker {self._host}:{self._port}"

    def host(self):
        return self._host

class WorkersManager():    
    def __init__(self, workers: List[WorkerConnection] = []):
        self.start_time = time.time()

        self.workers = workers
        self.host = socket.gethostbyname(socket.gethostname())
        self.args = copy.copy(config.pash_args)
        # Required to create a correct multi sink graph
        self.args.termination = "" 
        # NOTE: right now worker_manager node and client node are the same node, so we know the host/port
        #       for the client node. When we de-couple client node from worker_manager node, we will get this
        #       information from the Exec-Graph request from teh client node.
        self.client_worker = WorkerConnection("client_worker", self.host, PORT_CLIENT, self.args)
        self.kill_node_req_sent = False

        if self.args.ft != "disabled":
            self.all_worker_subgraph_pairs: List[Tuple[WorkerConnection, IR]] = []
            self.all_merger_to_shell_vars = {}
            self.all_merger_to_declared_functions = {}
            self.all_uuid_to_graphs = {}
            self.all_graph_to_uuid = defaultdict(list)
            # self.graph_to_uuid = defaultdict(list)
            self.all_merger_to_subgraph = {}
            self.all_subgraph_to_merger = {}

            # Rescheduling must never happen with scheduling
            # This can happen with dependency untangling and lots of small inputs (e.g. nlp 1000 books)
            self.reschedule_lock = Lock()

            self.daemon_quit = Event()
            self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.s.bind((HOST, PORT))
            self.s.listen()
            self.wm_log(f"Worker manager on {HOST}:{PORT}")
            Thread(target=self.__daemon, daemon=True).start()

    def get_worker(self, fids: List[FileId] = None) -> WorkerConnection:
        if not fids:
            fids = []

        best_worker = None  # Online worker with least work
        for worker in self.workers:
            if not worker.is_online():
                continue
            
            # Skip if any provided fid isn't available on the worker machine
            if any(map(lambda fid: not fid.is_available_on(worker.host()), fids)):
                continue

            if best_worker is None or best_worker.get_running_processes() > worker.get_running_processes():
                best_worker = worker

        if best_worker == None:
            raise Exception("no workers online where the date is stored")

        return best_worker

    def add_worker(self, name, host, port):
        self.workers.append(WorkerConnection(name, host, port, self.args))

    def add_workers_from_cluster_config(self, config_path):
        with open(config_path, 'r') as f:
            cluster_config = json.load(f)

        workers = cluster_config["workers"]
        for name, worker in workers.items():
            host = worker['host']
            port = worker['port']
            self.add_worker(name, host, port)

        addrs = {conn.host() for conn in self.workers}
        if self.args.ft != "disabled":
            start_hdfs_daemon(10, addrs, self.addr_added, self.addr_removed)
            self.wm_log(f"Started HDFS daemon")

    def addr_added(self, addr: str):
        self.wm_log(f"Added {addr} to active nodes")
        for worker in self.workers:
            if worker.host() == addr:
                worker._online = True

    def addr_removed(self, addr: str):
        self.wm_log("Fault detected!!!!")
        self.wm_log(f"Removed {addr} from active nodes")
        for worker in self.workers:
            if worker.host() == addr:
                worker._online = False
        
        if self.args.ft != "disabled":
            try:
                self.wm_log(f"Crash handling started")
                self.reschedule_lock.acquire()
                if self.args.ft == "naive":
                    self.handle_naive_crash(addr)
                else:
                    self.handle_crash(addr)
                self.wm_log(f"Crash handling finished")
            except Exception as e:
                error_trace = traceback.format_exc()
                self.wm_log(f"Failed to handle ft re-execution with error {e}\n{error_trace}")
            finally:
                self.reschedule_lock.release()

    # pip install grpcio-tools
    # python -m grpc_tools.protoc -Idspash/proto=. --python_out=/home/ramiz/dish/pash/compiler --grpc_python_out=/home/ramiz/dish/pash/compiler *.proto
    # go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
    # go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
    # GOPATH=$HOME/go; PATH=$PATH:$GOPATH/bin; ~/protoc/bin/protoc --go_out=. --go_opt=paths=source_relative --go-grpc_out=. --go-grpc_opt=paths=source_relative *.proto
    def check_persisted_discovery(self, addr, uuids):
        with grpc.insecure_channel('localhost:50052') as channel:
            stub = data_stream_pb2_grpc.DiscoveryStub(channel)

            # Create an FPMessage object
            fp_message = data_stream_pb2.FPMessage(uuids=uuids, addr=addr)

            # Call the gRPC method
            response = stub.FindPersistedOptimized(fp_message)

            self.wm_log("Received reply from discovery service")

            return response.indexes

    def handle_crash(self, addr: str):
        ft = self.args.ft
        self.wm_log("Node crashed, handling it, ft mode is", ft)
        subgraphs_to_reexecute = set()

        # handle crashed subgraphs
        for worker, subgraph in self.all_worker_subgraph_pairs:
            if worker.host() == addr and self.all_graph_to_uuid[subgraph.id]:
                subgraphs_to_reexecute.add(subgraph.id)
                # handle the dependencies of the merger subgraphs
                if subgraph.merger:
                    merger_id = subgraph.id
                    # no need to kill existing sgs for optimized
                    if ft == "base":
                        for worker in self.all_workers:
                            worker.send_kill_subgraphs_request(merger_id)
                            self.wm_log(f"Sent kill all subgraphs request to {worker}")
                    subgraphs_to_reexecute.update(self.all_merger_to_subgraph[merger_id])
        self.wm_log(f"Subgraphs to re-execute: {len(subgraphs_to_reexecute)}")

        # clear and update the tracked subgraph state
        for subgraph in subgraphs_to_reexecute:
            self.all_graph_to_uuid[subgraph].clear()
        for uuid, (from_graph, _) in self.all_uuid_to_graphs.copy().items():
            if from_graph in subgraphs_to_reexecute:
                self.all_graph_to_uuid[from_graph].append(uuid)
                # self.wm_log(f"Re-added {uuid} to all_graph_to_uuid[{from_graph}]")

        # remove executions if they are already persisted
        # this may be redundant
        if ft == "optimized":
            uuids = []
            uuid_objs = []
            for uuid, (from_graph, _) in self.all_uuid_to_graphs.items():
                if from_graph in subgraphs_to_reexecute:
                    uuids.append(str(uuid))
                    uuid_objs.append(uuid)
            indexes = self.check_persisted_discovery(addr, uuids)

            # Remove elements by index in reverse order
            for index in indexes:
                uu = uuid_objs[index]
                id = self.all_uuid_to_graphs[uu][0]
                # self.wm_log(f"Subgraph {id} is already persisted, discarding it")
                subgraphs_to_reexecute.remove(id)

            self.wm_log(f"Subgraphs to re-execute reduced by: {len(indexes)}")

        if ft == "optimized":
            # Function to return a defaultdict of list
            def default_to_list():
                return defaultdict(list)
            worker_to_batches: Dict[WorkerConnection, Dict[int, List[IR]]] = defaultdict(default_to_list)

        # iterate over the copy of the list to avoid modifying it while iterating
        for worker, subgraph in self.all_worker_subgraph_pairs[:]:
            if subgraph.id in subgraphs_to_reexecute:
                if worker.host() == addr:
                    self.all_worker_subgraph_pairs.remove((worker, subgraph))
                    subgraph_critical_fids = list(filter(lambda fid: fid.has_remote_file_resource(), subgraph.all_fids()))
                    new_worker = self.get_worker(subgraph_critical_fids)
                    new_worker._running_processes += 1
                    self.all_worker_subgraph_pairs.append((new_worker, subgraph))
                    worker = new_worker
                    if ft == "optimized":
                        worker_to_batches[worker][self.all_subgraph_to_merger[subgraph.id]].append(subgraph)
                        # self.wm_log(f"Re-execute req: subgraph {subgraph.id} belongs to merger {self.all_subgraph_to_merger[subgraph.id]}")

                if ft == "base":
                    merger_id = self.all_subgraph_to_merger[subgraph.id]
                    self.wm_log(f"Re-execute req: Sending subgraph {subgraph.id} to {worker}")
                    
                    worker.send_graph_exec_request(
                        subgraph,
                        self.all_merger_to_shell_vars[merger_id],
                        self.all_merger_to_declared_functions[merger_id],
                        merger_id,
                    )
                    self.wm_log(f"Re-execute req: Sent subgraph {subgraph.id} to {worker}")

        if ft == "optimized":
            self.wm_log(f"Re-execute requests are being prepared...")
            for worker in self.all_workers:
                merger_id_to_batch = worker_to_batches[worker]
                for merger_id, subgraphs in merger_id_to_batch.items():
                    mergers = []
                    regulars = []
                    for s in subgraphs:
                        if s.merger:
                            mergers.append(s)
                        else:
                            regulars.append(s)
                    
                    self.wm_log(f"Re-execute req: Sending {len(regulars)} regulars and {len(mergers)} mergers to {worker}")
                    worker.send_batch_graph_exec_request(
                        self.all_merger_to_shell_vars[merger_id],
                        self.all_merger_to_declared_functions[merger_id],
                        merger_id,
                        regulars,
                        mergers,
                        wait_ack=True
                    )
                    self.wm_log(f"Re-execute req: Sent {len(regulars)} regulars and {len(mergers)} mergers to {worker}")
            self.wm_log(f"Re-execute requests are sent")

    def handle_naive_crash(self, addr):
        self.wm_log("Node crashed while in naive ft, killing all subgraphs")
        for worker in self.all_workers:
            worker.send_kill_subgraphs_request(-1)
            self.wm_log(f"Sent kill all subgraphs request to {worker}")
        self.wm_log("Killed all subgraphs, will send all subgraphs again")

        # if a subgraph is finished in main, we need to not re-execute that even with naive
        # since only merger can send output to main, here we search for such mergers
        completely_finished_subgraphs = set()
        for _, subgraph in self.all_worker_subgraph_pairs:
            if subgraph.merger and not self.all_graph_to_uuid[subgraph.id]:
                completely_finished_subgraphs.add(subgraph.id)
                completely_finished_subgraphs.update(self.all_merger_to_subgraph[subgraph.id])
        self.wm_log("Completely finished subgraphs", completely_finished_subgraphs)

        # clear and update the tracked subgraph state
        for _, subgraph in self.all_worker_subgraph_pairs:
            if subgraph not in completely_finished_subgraphs:
                self.all_graph_to_uuid[subgraph].clear()
        for uuid, (from_graph, _) in self.all_uuid_to_graphs.items():
            if from_graph not in completely_finished_subgraphs:
                self.all_graph_to_uuid[from_graph].append(uuid)

        self.wm_log("Re-execute all subgraphs")
        # iterate over the copy of the list to avoid modifying it while iterating
        for worker, subgraph in self.all_worker_subgraph_pairs[:]:
            if subgraph.id in completely_finished_subgraphs:
                continue

            if worker.host() == addr:
                self.all_worker_subgraph_pairs.remove((worker, subgraph))
                subgraph_critical_fids = list(filter(lambda fid: fid.has_remote_file_resource(), subgraph.all_fids()))
                new_worker = self.get_worker(subgraph_critical_fids)
                new_worker._running_processes += 1
                self.all_worker_subgraph_pairs.append((new_worker, subgraph))
                worker = new_worker

            merger_id = self.all_subgraph_to_merger[subgraph.id]
            worker.send_graph_exec_request(
                subgraph,
                self.all_merger_to_shell_vars[merger_id],
                self.all_merger_to_declared_functions[merger_id],
                merger_id,
            )
            self.wm_log(f"Re-execute req: Sent subgraph {subgraph.id} to {worker}")
        self.wm_log("Sent all subgraphs again")

    def __daemon(self):
        self.s.settimeout(1)  # Set a timeout of 1 second
        while not self.daemon_quit.is_set():
            try:
                conn, addr = self.s.accept()
                Thread(target=self.__manage_connection, args=[conn, addr]).start()
            except socket.timeout:
                continue

    def __manage_connection(self, conn: socket, addr):
        # 1 byte for read or write and 16 byte for uuid
        data = conn.recv(17)

        # it's extemely unlikely that the data will be split
        # even so at most we duplicate execution in case of failure by returning here
        if len(data) != 17:
            self.wm_log(f"MCE Expected 17 bytes, got {len(data)} bytes")
            return

        # Read the first byte to get if the request is from read or write client
        read_client = True if data[0] == 0 else False

        uuid = UUID(bytes=data[1:])

        if read_client:
            if uuid not in self.all_uuid_to_graphs:
                self.wm_log(f"MCE UUID {uuid} not found in all_uuid_to_graphs")
                return

            responsible_graph = self.all_uuid_to_graphs[uuid][0]

            if responsible_graph not in self.all_graph_to_uuid:
                self.wm_log(f"MCE Responsible graph {responsible_graph} not found in all_graph_to_uuid")
                return

            if uuid not in self.all_graph_to_uuid[responsible_graph]:
                self.wm_log(f"MCE UUID {uuid} not found in all_graph_to_uuid[{responsible_graph}]")
                return

            self.all_graph_to_uuid[responsible_graph].remove(uuid)

    def handle_kill(self, worker_subgraph_pairs: List[Tuple[WorkerConnection, IR]]):
        for worker, subgraph in worker_subgraph_pairs:
            if subgraph.merger:
                merger_worker = worker
                break
        if self.args.kill == "merger":
            kill_target = merger_worker
        elif self.args.kill == "regular":
            for worker in self.workers:
                if worker != merger_worker:
                    kill_target = worker
                    break
        else:
            raise Exception(f"Invalid kill target {self.args.kill}. It must be either 'merger' or 'regular'")

        # Record the kill target so we can resurrect that node later
        log(KILL_WITNESS_PATH)
        with open(KILL_WITNESS_PATH, 'w') as witness_file:
            witness_file.write(kill_target.host())
        kill_target.send_kill_node_request()
        self.kill_node_req_sent = True
        self.wm_log(f"Sent kill node request to {kill_target}")

    def log_node_ip(self, worker_subgraph_pairs: List[Tuple[WorkerConnection, IR]]):
        # pick a merger worker and pick a regular worker
        selected_merger_worker = None
        for worker, subgraph in worker_subgraph_pairs:
            if subgraph.merger:
                selected_merger_worker = worker
                break
        selected_regular_worker = None
        for worker in self.workers:
            if worker != selected_merger_worker:
                selected_regular_worker = worker
                break

        # write out the ip to file
        log(KILL_WITNESS_PATH)
        with open(KILL_WITNESS_PATH, 'w') as witness_file:
            witness_file.write(selected_merger_worker.host() + '\n')
            witness_file.write(selected_regular_worker.host() + '\n')


    def handle_exec_graph(self, request, dspash_socket, conn):
        args = request.split(':', 1)[1].strip()
        filename, declared_functions_file = args.split()

        worker_subgraph_pairs, shell_vars, main_graph, uuid_to_graphs = prepare_graph_for_remote_exec(filename, self.get_worker)
        worker_subgraph_pairs: List[Tuple[WorkerConnection, IR]]
        self.log_node_ip(worker_subgraph_pairs)
        if self.args.kill and not self.kill_node_req_sent:
            self.handle_kill(worker_subgraph_pairs)

        self.wm_log(f"Will split graph")
        # Split main_graph
        main_reader_graph, main_writer_graphs = split_main_graph(main_graph, uuid_to_graphs)
        self.wm_log(f"Graph split")

        # If main_writer_graphs, add (client_worker, main_writer_graphs) tp worker_subgraph_pairs
        for main_writer_graph in main_writer_graphs:                    
            worker_subgraph_pairs.append((self.client_worker, main_writer_graph))
        script_fname = to_shell_file(main_reader_graph, self.args)
        self.wm_log(f"Master node graph stored in {script_fname}")

        # Read functions
        self.wm_log(f"Functions stored in {declared_functions_file}")
        declared_functions = read_file(declared_functions_file)
        self.wm_log("hi")
        for worker, _ in worker_subgraph_pairs:
            self.wm_log(worker.host())
        merger_id = -1
        if self.args.ft != "disabled":
            # Update the Worker Manager state for fault tolerance
            for _, subgraph in worker_subgraph_pairs:                        
                if subgraph.merger:
                    merger_id = subgraph.id
                    break                  
            assert merger_id != -1, "No merger found in the subgraphs"
            self.all_worker_subgraph_pairs.extend(worker_subgraph_pairs)
            self.all_merger_to_shell_vars[merger_id] = shell_vars
            self.all_merger_to_declared_functions[merger_id] = declared_functions
            self.all_uuid_to_graphs.update(uuid_to_graphs)
            for uuid, (from_graph, _) in uuid_to_graphs.items():
                self.all_graph_to_uuid[from_graph].append(uuid)
            self.all_merger_to_subgraph[merger_id] = [subgraph.id for _, subgraph in worker_subgraph_pairs]
            self.all_subgraph_to_merger.update({subgraph.id: merger_id for _, subgraph in worker_subgraph_pairs})
            self.wm_log(f"Worker Manager state updated for merger {merger_id}")

        # Report to main shell a script to execute
        response_msg = f"OK {script_fname}"
        dspash_socket.respond(response_msg, conn)

        if self.args.ft == "optimized":
            worker_to_regulars = defaultdict(list)
            worker_to_mergers = defaultdict(list)
            for worker, subgraph in worker_subgraph_pairs:
                if subgraph.merger:
                    worker_to_mergers[worker].append(subgraph)
                else:
                    worker_to_regulars[worker].append(subgraph)

            response_worker_list: List[WorkerConnection] = []
            for worker in self.all_workers:
                if worker_to_regulars[worker] or worker_to_mergers[worker]:
                    worker.send_batch_graph_exec_request(
                        shell_vars,
                        declared_functions,
                        merger_id,
                        worker_to_regulars[worker],
                        worker_to_mergers[worker]
                    )

                    response_worker_list.append(worker)

            self.wm_log(f"Sent async batch graph exec requests (optimized)")
            for worker in response_worker_list:
                    worker.handle_response()
            for worker, subgraph in worker_subgraph_pairs:
                self.wm_log(f"Sent subgraph {subgraph.id} to {worker}, online is {worker.is_online()}")
        else:
            # Execute subgraphs on workers
            for worker, subgraph in worker_subgraph_pairs:
                if worker.is_online():
                    worker.send_graph_exec_request(subgraph, shell_vars, declared_functions, merger_id)
                    self.wm_log(f"Sent subgraph {subgraph.id} to {worker}, online is {worker.is_online()}")

        self.wm_log(f"Sent all graph exec requests")

    def run(self):
        if self.args.debug and self.args.debug > 2:
            profiler = cProfile.Profile()
            profiler.enable()

        dspash_socket = SocketManager(os.getenv('DSPASH_SOCKET'))
        self.wm_log(f"Created dspash_socket at {os.getenv('DSPASH_SOCKET')}")

        self.add_workers_from_cluster_config(os.path.join(config.PASH_TOP, 'cluster.json'))
        self.all_workers = self.workers.copy()
        self.all_workers.append(self.client_worker)
        self.wm_log(f"All workers are online")

        for worker in self.all_workers:
            worker.send_setup_request()
        self.wm_log(f"All setup requests are sent")

        while True:
            request, conn = dspash_socket.get_next_cmd()
            self.wm_log(f"Received request: {request} from {conn}")
            if request.startswith("Done"):
                dspash_socket.close()
                if self.args.ft != "disabled":
                    stop_hdfs_daemon()
                    self.daemon_quit.set()
                self.wm_log(f"Done")
                if self.args.debug and self.args.debug > 2:
                    profiler.disable()
                    s = io.StringIO()
                    ps = pstats.Stats(profiler, stream=s).sort_stats('cumulative')
                    ps.print_stats()
                    with open(f"WM_profile.log", "w") as f:
                        f.write(s.getvalue())
                break
            elif request.startswith("Exec-Graph"):
                try:
                    if self.args.ft != "disabled":
                        self.reschedule_lock.acquire()
                    self.handle_exec_graph(request, dspash_socket, conn)
                finally:
                    if self.args.ft != "disabled":
                        self.reschedule_lock.release()
            else:
                if self.args.ft != "disabled":
                    stop_hdfs_daemon()
                    self.daemon_quit.set()
                raise Exception(f"Unknown request: {request}")

    def wm_log(self, *args):
        log(f"WM {(time.time() - self.start_time):10.6f}:", *args)

if __name__ == "__main__":
    WorkersManager().run()
