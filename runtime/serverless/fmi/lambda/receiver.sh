#!/bin/bash

cd "$(dirname $0)"

num=$1
data=$2
id=$3

rm_pash_fifos() {
	rm -f /tmp/fifo
}
mkfifo_pash_fifos() {
	mkfifo /tmp/fifo
}

rm_pash_fifos
mkfifo_pash_fifos
pids_to_kill=""

python sendrecv.py 1 /tmp/fifo &
pids_to_kill="${!} ${pids_to_kill}"

cat /tmp/fifo >/dev/null &
pids_to_kill="${!} ${pids_to_kill}"

source wait_for_output_and_sigpipe_rest.sh ${!}

rm_pash_fifos

( exit "${internal_exec_status}" )
