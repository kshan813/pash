import json
import logging
import os
import subprocess
import math

from util import *

## Global
__version__ = "0.12.2" # FIXME add libdash version
GIT_TOP_CMD = [ 'git', 'rev-parse', '--show-toplevel', '--show-superproject-working-tree']
if 'DISH_TOP' in os.environ:
    DISH_TOP = os.environ['DISH_TOP']
if 'PASH_TOP' in os.environ:
    PASH_TOP = os.environ['PASH_TOP']
else:
    PASH_TOP = subprocess.run(GIT_TOP_CMD, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True).stdout.rstrip()

PYTHON_VERSION = "python3"
PLANNER_EXECUTABLE = os.path.join(PASH_TOP, "compiler/pash_compiler.py")
RUNTIME_EXECUTABLE = os.path.join(PASH_TOP, "compiler/pash_runtime.sh")
SAVE_ARGS_EXECUTABLE = os.path.join(PASH_TOP, "runtime/save_args.sh")
SAVE_SHELL_STATE_EXECUTABLE = os.path.join(PASH_TOP, "compiler/orchestrator_runtime/save_shell_state.sh")

# Ensure that PASH_TMP_PREFIX is set by pa.sh
assert (not os.getenv('PASH_TMP_PREFIX') is None)
PASH_TMP_PREFIX = os.getenv('PASH_TMP_PREFIX')

SOCKET_BUF_SIZE = 8192


##
## Global configuration used by all pash components
##
LOGGING_PREFIX = ""
OUTPUT_TIME = False
DEBUG_LEVEL = 0
LOG_FILE = ""


HDFS_PREFIX = "$HDFS_DATANODE_DIR/"


config = {}
pash_args = None

# Contains a bash subprocess that is used for expanding
bash_mirror = None

# A cache containing variable values since variables are not meant to change while we compile one region
variable_cache = {}

## This function sets the global configuration
##
## TODO: Actually move everything outside of pash_args to configuration.
def set_config_globals_from_pash_args(given_pash_args):
    global pash_args, OUTPUT_TIME, DEBUG_LEVEL, LOG_FILE
    pash_args = given_pash_args
    OUTPUT_TIME = pash_args.output_time
    DEBUG_LEVEL = pash_args.debug
    LOG_FILE = pash_args.log_file

    ## Also set logging here
    # Format logging
    # ref: https://docs.python.org/3/library/logging.html#formatter-objects
    ## TODO: When we add more logging levels bring back the levelname+time
    if given_pash_args.log_file == "":
        logging.basicConfig(format="%(message)s")
    else:
        logging.basicConfig(format="%(message)s", 
                            filename=f"{os.path.abspath(given_pash_args.log_file)}", 
                            filemode="w")

    # Set debug level
    if given_pash_args.debug == 1:
        logging.getLogger().setLevel(logging.INFO)
    elif given_pash_args.debug >= 2:
        logging.getLogger().setLevel(logging.DEBUG)

# Increase the recursion limit (it seems that the parser/unparser needs it for bigger graphs)
sys.setrecursionlimit(10000)


def load_config(config_file_path=""):
    global config
    pash_config = {}
    CONFIG_KEY = 'distr_planner'

    if (config_file_path == ""):
        config_file_path = '{}/compiler/config.json'.format(PASH_TOP)
    with open(config_file_path) as config_file:
        pash_config = json.load(config_file)

    if not pash_config:
        raise Exception(
            'No valid configuration could be loaded from {}'.format(config_file_path))

    if CONFIG_KEY not in pash_config:
        raise Exception('Missing `{}` config in {}'.format(
            CONFIG_KEY, config_file_path))

    config = pash_config


def getWidth():
    cpus = os.cpu_count()
    return math.floor(cpus / 8) if cpus >= 16 else 2

def add_general_config_arguments(parser):
    ## TODO: Delete that at some point, or make it have a different use (e.g., outputting time even without -d 1).
    parser.add_argument("-t", "--output_time", #FIXME: --time
                        help="(obsolete, time is always logged now) output the time it took for every step",
                        action="store_true")
    parser.add_argument("-d", "--debug",
                        type=int,
                        help="configure debug level; defaults to 0",
                        default=0)
    parser.add_argument("--log_file",
                        help="configure where to write the log; defaults to stderr.",
                        default="")

## These are arguments that are common to pash.py and pash_compiler.py
def add_common_arguments(parser):
    add_general_config_arguments(parser)

    parser.add_argument("-w", "--width",
                        type=int,
                        default=getWidth(),
                        help="set data-parallelism factor")
    parser.add_argument("--no_optimize",
                        help="not apply transformations over the DFG",
                        action="store_true")
    parser.add_argument("--dry_run_compiler",
                        help="not execute the compiled script, even if the compiler succeeded",
                        action="store_true")
    parser.add_argument("--assert_compiler_success",
                        help="assert that the compiler succeeded (used to make tests more robust)",
                        action="store_true")
    parser.add_argument("--avoid_pash_runtime_completion",
                        help="avoid the pash_runtime execution completion (only relevant when --debug > 0)",
                        action="store_true")
    parser.add_argument("--profile_driven",
                        help="(experimental) use profiling information when optimizing",
                        action="store_true")
    parser.add_argument("-p", "--output_optimized", # FIXME: --print
                        help="output the parallel shell script for inspection",
                        action="store_true")
    parser.add_argument("--graphviz",
                        help="generates graphical representations of the dataflow graphs. The option argument corresponds to the format. PaSh stores them in a timestamped directory in the argument of --graphviz_dir",
                        choices=["no", "dot", "svg", "pdf", "png"],
                        default="no")
    # TODO: To discuss: Do we maybe want to have graphviz to always be included
    # in the temp directory (under a graphviz subdirectory) instead of in its own?
    # kk: I think that ideally we want a log-directory where we can put logs, graphviz,
    # and other observability and monitoring info (instead of putting them in the temp).
    parser.add_argument("--graphviz_dir",
                        help="the directory in which to store graphical representations",
                        default="/tmp")
    parser.add_argument("--no_eager",
                        help="(experimental) disable eager nodes before merging nodes",
                        action="store_true")
    parser.add_argument("--no_daemon",
                        help="(obsolete) does nothing -- Run the compiler everytime we need a compilation instead of using the daemon",
                        action="store_true",
                        default=False)
    parser.add_argument("--parallel_pipelines",
                        help="Run multiple pipelines in parallel if they are safe to run",
                        action="store_true",
                        default=False)
    parser.add_argument("--parallel_pipelines_limit",
                        type=int,
                        help="Configure the limit for the number of parallel pipelines at one time (default: cpu count, 0 is turns parallel pipelines off)",
                        default=os.cpu_count())
    parser.add_argument("--r_split_batch_size",
                        type=int,
                        help="configure the batch size of r_split (default: 1MB)",
                        default=1000000)
    parser.add_argument("--r_split",
                        help="(obsolete) does nothing -- only here for old interfaces (not used anywhere in the code)",
                        action="store_true")
    parser.add_argument("--dgsh_tee",
                        help="(obsolete) does nothing -- only here for old interfaces (not used anywhere in the code)",
                        action="store_true")
    parser.add_argument("--speculative",
                        help="(experimental) use the speculative execution preprocessing and runtime (NOTE: this has nothing to do with --speculation, which is actually misnamed, and should be named concurrent compilation/execution and is now obsolete)",
                        action="store_true",
                        default=False)
    ## This is misnamed, it should be named concurrent compilation/execution
    parser.add_argument("--speculation",
                        help="(obsolete) does nothing -- run the original script during compilation; if compilation succeeds, abort the original and run only the parallel (quick_abort) (Default: no_spec)",
                        choices=['no_spec', 'quick_abort'],
                        default='no_spec')
    parser.add_argument("--termination",
                        help="(experimental) determine the termination behavior of the DFG. Defaults to cleanup after the last process dies, but can drain all streams until depletion",
                        choices=['clean_up_graph', 'drain_stream'],
                        default="clean_up_graph")
    parser.add_argument("--daemon_communicates_through_unix_pipes",
                        help="(experimental) the daemon communicates through unix pipes instead of sockets",
                        action="store_true")
    parser.add_argument("--distributed_exec",
                        help="(experimental) execute the script in a distributed environment. Remote machines should be configured and ready",
                        action="store_true",
                        default=False)
    parser.add_argument("--kill",
                        help="determines which node is going to be killed. Address or conatiner name must follow this parameter, like \"--kill datanode1\". Only works if distributed_exec is used.",
                        default="")
    parser.add_argument("--config_path",
                        help="determines the config file path. By default it is 'PASH_TOP/compiler/config.yaml'.",
                        default="")
    parser.add_argument("--version",
                        action='version',
                        version='%(prog)s {version}'.format(version=__version__))
    parser.add_argument("--worker_timeout",
                        help="determines if we will mock a timeout for worker node.",
                        default="")
    parser.add_argument("--worker_timeout_choice",
                        help="determines which worker node will be timed out.",
                        default="")
    return


def pass_common_arguments(pash_arguments):
    arguments = []
    if (pash_arguments.no_optimize):
        arguments.append("--no_optimize")
    if (pash_arguments.dry_run_compiler):
        arguments.append("--dry_run_compiler")
    if (pash_arguments.assert_compiler_success):
        arguments.append("--assert_compiler_success")
    if (pash_arguments.avoid_pash_runtime_completion):
        arguments.append("--avoid_pash_runtime_completion")
    if (pash_arguments.profile_driven):
        arguments.append("--profile_driven")
    if (pash_arguments.output_time):
        arguments.append("--output_time")
    if (pash_arguments.output_optimized):
        arguments.append("--output_optimized")
    arguments.append("--graphviz")
    arguments.append(pash_arguments.graphviz)
    arguments.append("--graphviz_dir")
    arguments.append(pash_arguments.graphviz_dir)
    if(not pash_arguments.log_file == ""):
        arguments.append("--log_file")
        arguments.append(pash_arguments.log_file)
    if (pash_arguments.no_eager):
        arguments.append("--no_eager")
    if (pash_arguments.distributed_exec):
        arguments.append("--distributed_exec")
    if (pash_arguments.speculative):
        arguments.append("--speculative")
    if (pash_arguments.parallel_pipelines):
        arguments.append("--parallel_pipelines")
        arguments.append("--parallel_pipelines_limit")
    if (pash_arguments.daemon_communicates_through_unix_pipes):
        arguments.append("--daemon_communicates_through_unix_pipes")
    arguments.append("--r_split_batch_size")
    arguments.append(str(pash_arguments.r_split_batch_size))
    arguments.append("--debug")
    arguments.append(str(pash_arguments.debug))
    arguments.append("--termination")
    arguments.append(pash_arguments.termination)
    arguments.append("--width")
    arguments.append(str(pash_arguments.width))
    if(not pash_arguments.config_path == ""):
        arguments.append("--config_path")
        arguments.append(pash_arguments.config_path)
    return arguments


def init_log_file():
    global LOG_FILE
    if(not LOG_FILE == ""):
        with open(LOG_FILE, "w") as f:
            pass


def wait_bash_mirror(bash_mirror):
    r = bash_mirror.expect(r'EXPECT\$ ')
    assert (r == 0)
    output = bash_mirror.before

    # I am not sure why, but \r s are added before \n s
    output = output.replace('\r\n', '\n')

    log("Before the prompt!")
    log(output)
    return output


def query_expand_variable_bash_mirror(variable):
    global bash_mirror

    command = f'if [ -z ${{{variable}+foo}} ]; then echo -n "PASH_VAR_UNSET"; else echo -n "${variable}"; fi'
    data = sync_run_line_command_mirror(command)

    if data == "PASH_VAR_UNSET":
        return None
    else:
        # This is here because we haven't specified utf encoding when spawning bash mirror
        # return data.decode('ascii')
        return data


def query_expand_bash_mirror(string):
    global bash_mirror

    command = f'echo -n "{string}"'
    return sync_run_line_command_mirror(command)


def sync_run_line_command_mirror(command):
    bash_command = f'{command}'
    log("Executing bash command in mirror:", bash_command)

    bash_mirror.sendline(bash_command)

    data = wait_bash_mirror(bash_mirror)
    log("mirror done!")

    return data


def update_bash_mirror_vars(var_file_path):
    global bash_mirror

    assert (var_file_path != "" and not var_file_path is None)

    bash_mirror.sendline(f'PS1="EXPECT\$ "')
    wait_bash_mirror(bash_mirror)
    log("PS1 set!")

    # TODO: There is unnecessary write/read to this var file now.
    bash_mirror.sendline(f'source {var_file_path}')
    log("sent source to mirror")
    wait_bash_mirror(bash_mirror)
    log("mirror done!")


def add_to_variable_cache(variable_name, value):
    global variable_cache
    variable_cache[variable_name] = value


def get_from_variable_cache(variable_name):
    global variable_cache
    try:
        return variable_cache[variable_name]
    except:
        return None


def reset_variable_cache():
    global variable_cache

    variable_cache = {}


# This finds the end of this variable/function
def find_next_delimiter(tokens, i):
    if (tokens[i] == "declare"):
        return i + 3
    else:
        j = i + 1
        while j < len(tokens) and (tokens[j] != "declare"):
            j += 1
        return j

##
# Read a shell variables file
##


def read_vars_file(var_file_path):
    global config

    log("Reading variables from:", var_file_path)

    config['shell_variables'] = None
    config['shell_variables_file_path'] = var_file_path
    if (not var_file_path is None):
        vars_dict = {}
        # with open(var_file_path) as f:
        #     lines = [line.rstrip() for line in f.readlines()]

        with open(var_file_path) as f:
            variable_reading_start_time = datetime.now()
            data = f.read()
            variable_reading_end_time = datetime.now()
            print_time_delta(
                "Variable Reading", variable_reading_start_time, variable_reading_end_time)

            variable_tokenizing_start_time = datetime.now()
            # TODO: Can we replace this tokenizing process with our own code? This is very slow :'(
            # It takes about 15ms on deathstar.
            tokens = shlex.split(data)
            variable_tokenizing_end_time = datetime.now()
            print_time_delta(
                "Variable Tokenizing", variable_tokenizing_start_time, variable_tokenizing_end_time)
            # log(tokens)

        # MMG 2021-03-09 definitively breaking on newlines (e.g., IFS) and function outputs (i.e., `declare -f`)
        # KK  2021-10-26 no longer breaking on newlines (probably)

        # At the start of each iteration token_i should point to a 'declare'
        token_i = 0
        while token_i < len(tokens):
            # FIXME is this assignment needed?
            _export_or_typeset = tokens[token_i]

            new_token_i = find_next_delimiter(tokens, token_i)
            rest = " ".join(tokens[(token_i+1):new_token_i])
            # log("Rest:", rest)
            token_i = new_token_i

            space_index = rest.find(' ')
            eq_index = rest.find('=')
            var_type = None

            # Declared but unset?
            if eq_index == -1:
                if space_index != -1:
                    var_name = rest[(space_index+1):]
                    var_type = rest[:space_index]
                else:
                    var_name = rest
                var_value = ""
            # Set, with type
            elif (space_index < eq_index and not space_index == -1):
                var_type = rest[:space_index]

                if var_type == "--":
                    var_type = None

                var_name = rest[(space_index+1):eq_index]
                var_value = rest[(eq_index+1):]
            # Set, without type
            else:
                var_name = rest[:eq_index]
                var_value = rest[(eq_index+1):]

            # Strip quotes
            if var_value is not None and len(var_value) >= 2 and \
               var_value[0] == "\"" and var_value[-1] == "\"":
                var_value = var_value[1:-1]

            vars_dict[var_name] = (var_type, var_value)

        config['shell_variables'] = vars_dict


##
## Set the shell variables
##

def set_vars_file(var_file_path: str, var_dict: dict):
    global config    
    config['shell_variables'] = var_dict
    config['shell_variables_file_path'] = var_file_path
