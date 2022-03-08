import argparse
import sys
import pickle
import traceback
from datetime import datetime
from typing import List, Set, Tuple, Dict
sys.path.append("/pash/compiler")

import config
from ir import *
from ast_to_ir import compile_asts
from json_ast import *
from ir_to_ast import to_shell
from util import *

from definitions.ir.aggregator_node import *

from definitions.ir.nodes.eager import *
from definitions.ir.nodes.pash_split import *

import definitions.ir.nodes.r_merge as r_merge
import definitions.ir.nodes.r_split as r_split
import definitions.ir.nodes.r_unwrap as r_unwrap
import definitions.ir.nodes.dgsh_tee as dgsh_tee
import definitions.ir.nodes.remote_exec as remote_exec
import definitions.ir.nodes.remote_pipe as remote_pipe
import shlex
import subprocess
import pash_runtime
from collections import deque, defaultdict

HOST = socket.gethostbyname(socket.gethostname())
NEXT_PORT = 58000

def get_available_port():
    # There is a possible race condition using the returned port as it could be opened by a different process
    global NEXT_PORT
    port = NEXT_PORT
    NEXT_PORT += 1
    return port

def read_graph(filename):
    with open(filename, "rb") as ir_file:
        ir, shell_vars = pickle.load(ir_file)
    return ir, shell_vars
            
def to_shell_file(graph, args):
    _, filename = ptempfile()
    
    dirs = set()
    for edge in graph.all_fids():
        directory = os.path.join(config.PASH_TMP_PREFIX, edge.prefix)
        dirs.add(directory)
    for directory in dirs:
        os.makedirs(directory, exist_ok=True)

    if not args.no_eager:
        graph = pash_runtime.add_eager_nodes(graph, args.dgsh_tee)

    script = to_shell(graph, args)
    with open(filename, "w") as f:
        f.write(script)
    return filename

def split_ir(graph: IR):
    source_node_ids = graph.source_nodes()
    input_fifo_map = defaultdict(list)
    
    graphs = []
    queue = deque([(source, IR({}, {})) for source in source_node_ids])

    # Graph is a DAG so we need to keep track of traversed edges
    visited_edges = set(graph.all_input_fids())
    visited_nodes = set()

    while queue:
        old_node_id, sub_graph = queue.popleft()
        input_fids = graph.get_node_input_fids(old_node_id)
        output_fids = graph.get_node_output_fids(old_node_id)

        if(any(map(lambda fid:fid not in visited_edges, input_fids))):
            if sub_graph.source_nodes():
                graphs.append(sub_graph)
            continue
        
        # Second condition makes sure we don't add empty graphs
        if len(input_fids) > 1 and sub_graph.source_nodes(): # merger node
            if sub_graph not in graphs:
                graphs.append(sub_graph)
            sub_graph = IR({}, {})

        if old_node_id in visited_nodes:
            continue
        else:
            visited_nodes.add(old_node_id)
        
        node = graph.get_node(old_node_id).copy()
        node_id = node.get_id()

        for idx, input_fid in enumerate(input_fids):
            input_edge_id = None
            # If subgraph is empty and edge isn't ephemeral the edge needs to be added
            if not input_fid.get_ident() in sub_graph.edges:
                new_fid = input_fid
                sub_graph.add_to_edge(new_fid, node_id)
                input_edge_id = new_fid.get_ident()
            else:
                input_edge_id = input_fid.get_ident()
                sub_graph.set_edge_to(input_edge_id, node_id)
            # keep track  
            input_fifo_map[input_edge_id].append(sub_graph)

        # Add edges coming out of the node
        for output_fid in output_fids:
            sub_graph.add_from_edge(node_id, output_fid)
            visited_edges.add(output_fid)

        # Add edges coming into the node
        for input_fid in input_fids:
            if input_fid.get_ident() not in sub_graph.edges:
                sub_graph.add_to_edge(input_fid, node_id) 

        # Add the node
        sub_graph.add_node(node)

        next_ids = graph.get_next_nodes(old_node_id)
        # Second condition makes sure we are not stepping into merger by mistake (eg set-diff)
        if (len(input_fids) == len(next_ids) == 1) and (not output_fid.get_ident() in input_fifo_map):
            queue.append((next_ids[0], sub_graph))
        else:
            graphs.append(sub_graph)
            for next_id in next_ids:
                queue.append((next_id, IR({}, {})))
        
    # print(list(map(lambda k : k.all_fids(), graphs)))
    return graphs, input_fifo_map

def add_stdout_fid(graph : IR, file_id_gen: FileIdGen) -> FileId:
    stdout = file_id_gen.next_file_id()
    stdout.set_resource(FileDescriptorResource(('fd', 1)))
    graph.add_edge(stdout)
    return stdout

# def add_remote_pipes(sub_graph:IR, file_id_gen: FileIdGen, mapping: Dict, worker, final_subgraph)
def add_remote_pipes(graphs:List[IR], file_id_gen: FileIdGen, mapping:Dict, get_worker) -> IR:
    write_port = -1
    # The graph to execute in the main pash_runtime
    final_subgraph = IR({}, {})
    worker_graph_pairs = []

    # Replace output edges and corrosponding input edges with remote read/write
    for sub_graph in graphs:
        worker = get_worker([])
        worker._running_processes += 1
        worker_graph_pairs.append((worker, sub_graph))
        sink_nodes = sub_graph.sink_nodes()
        assert(len(sink_nodes) == 1)
        
        for out_edge in sub_graph.get_node_output_fids(sink_nodes[0]):
            stdout = add_stdout_fid(sub_graph, file_id_gen)
            write_port = get_available_port()
            out_edge_id = out_edge.get_ident()
            # Replace the old edge with an ephemeral edge in case it isn't and
            # to avoid modifying the edge in case it's used in some other subgraph
            ephemeral_edge = file_id_gen.next_ephemeral_file_id()
            sub_graph.replace_edge(out_edge_id, ephemeral_edge)

            # Add remote-write node at the end of the subgraph
            remote_write = remote_pipe.make_remote_pipe([ephemeral_edge.get_ident()], [stdout.get_ident()], worker.host(), write_port, False, True)
            sub_graph.add_node(remote_write)
            
            # Copy the old output edge resource
            new_edge = file_id_gen.next_file_id()
            new_edge.set_resource(out_edge.get_resource())
            # Get the subgraph which "edge" writes to
            if out_edge_id in mapping and out_edge.is_ephemeral():
                # Copy the old output edge resource
                matching_subgraph = mapping[out_edge_id][0]
                matching_subgraph.replace_edge(out_edge.get_ident(), new_edge)
            else:
                matching_subgraph = final_subgraph
                matching_subgraph.add_edge(new_edge)
    
            remote_read = remote_pipe.make_remote_pipe([], [new_edge.get_ident()], worker.host(), write_port, True, False)
            matching_subgraph.add_node(remote_read)

    # Replace non ephemeral input edges with remote read/write
    for sub_graph in graphs:
        source_nodes = sub_graph.source_nodes()
        for source in source_nodes:
            for in_edge in sub_graph.get_node_input_fids(source):
                if in_edge.has_file_resource() or in_edge.has_file_descriptor_resource():
                    # setup
                    write_port = get_available_port()
                    stdout = add_stdout_fid(final_subgraph, file_id_gen)

                    # Copy the old input edge resource
                    new_edge = file_id_gen.next_file_id()
                    new_edge.set_resource(in_edge.get_resource())
                    final_subgraph.add_edge(new_edge)

                    # Add remote write to main subgraph
                    remote_write = remote_pipe.make_remote_pipe([new_edge.get_ident()], [stdout.get_ident()], HOST, write_port, False, True)
                    final_subgraph.add_node(remote_write)

                    # Add remote read to current subgraph
                    ephemeral_edge = file_id_gen.next_ephemeral_file_id()
                    sub_graph.replace_edge(in_edge.get_ident(), ephemeral_edge)

                    remote_read = remote_pipe.make_remote_pipe([], [ephemeral_edge.get_ident()], HOST, write_port, True, False)
                    sub_graph.add_node(remote_read)
                else:
                    # sometimes a command can have both a file resource and an ephemeral resources (example: spell oneliner)
                    continue

    return final_subgraph, worker_graph_pairs

def prepare_graph_for_remote_exec(filename, get_worker):
    """
    Reads the complete ir from filename and splits it
    into subgraphs where ony the first subgraph represent a continues
    segment (merger segment or branched segment) in the graph. 
    Note: All subgraphs(except first one) read and write from remote pipes.
        However, we had to add a fake stdout to avoid some problems when converting to shell code.

    Returns: 
        worker_graph_pairs: List of (worker, subgraph)
        shell_vars: shell variables
        final_graph: The ir we need to execute on the main shell. 
            This graph contains edges to correctly redirect the following to remote workers
            - special pipes (stdin/stdout)
            - named pipes reading and writing
            - files reading and writing
    """
    ir, shell_vars = read_graph(filename)
    file_id_gen = ir.get_file_id_gen()
    graphs, mapping = split_ir(ir)
    main_graph, worker_graph_pairs = add_remote_pipes(graphs, file_id_gen, mapping, get_worker)
    return worker_graph_pairs, shell_vars, main_graph