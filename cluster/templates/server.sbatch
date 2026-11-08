#!/bin/bash
#SBATCH -p {partition}
#SBATCH -N {server_nodes}
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task={cpus}
#SBATCH --gres={gres}
#SBATCH --mem={mem}
#SBATCH -t {time_limit}
#SBATCH -J {name}
#SBATCH -o {jobs}/server-%j.log
#
# The model servers ({model}) of evaluate job {eval_job}, written by
# cluster/submit.py at {stamp}: one per node, one node per node of that job,
# so each of its nodes reaches a server of its own over SSH (as many agents
# per server as on a GPU node). That job cancels this one when its workers
# are done. The account runs two jobs at once, so this
# job may start while {eval_job} still waits for a slot: it then serves for
# at most {grace} minutes after the model answers, and ends, rather than
# hold a slot that the evaluate job needs. ({eval_job} then finds no server
# and leaves its instances to do; submit it again.)

set -u
cd {source}
# each server its own compile cache, a copy of the warm shared one if there is
# one: servers compiling into one cache at once break each other on NFS
srun -N {server_nodes} -n {server_nodes} --ntasks-per-node=1 /bin/bash -c \
    'C="$EMBEDEVAL_VLLM_CACHE/node$SLURM_PROCID"; mkdir -p "$C"
     [ -e {warm_cache}/.warm ] && cp -a {warm_cache}/. "$C"/ && rm -f "$C/.fill.lock"
     EMBEDEVAL_VLLM_CACHE="$C" EMBEDEVAL_SCRATCH="$EMBEDEVAL_SCRATCH/node$SLURM_PROCID" \
         exec bash scripts/serve_triage_model.sh {model}' &
SERVER=$!
READY=""
while kill -0 "$SERVER" 2>/dev/null; do
    STATE=$(squeue -h -j {eval_job} -o %T 2>/dev/null)
    if [ -z "$STATE" ]; then
        echo "[$(date +%T)] evaluate job {eval_job} is gone: stopping"
        break
    fi
    if [ "$STATE" != RUNNING ] && curl -sf 127.0.0.1:8000/v1/models >/dev/null; then
        READY=${{READY:-$(date +%s)}}
        if (( $(date +%s) - READY > {grace} * 60 )); then
            echo "[$(date +%T)] evaluate job {eval_job} still $STATE {grace} min after the model answered: freeing the slot"
            break
        fi
    fi
    [ "$STATE" = RUNNING ] && READY=""
    sleep 60
done
kill "$SERVER" 2>/dev/null
wait "$SERVER" 2>/dev/null
echo "[$(date +%T)] server job done"
