#!/bin/bash
#SBATCH -p {partition}
#SBATCH -N {nodes}
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task={cpus}
#SBATCH --gres={gres}
#SBATCH --mem={mem}
#SBATCH -t {time_limit}
#SBATCH -J embedeval-{name}
#SBATCH -o {run}/jobs/job-%j.out
#
# EmbedEval pipeline job for run "{name}", written by cluster/submit.py at
# {stamp}. To run again (e.g. to resume), submit again rather than edit this.
#
#   phase 1  triage       node_worker.py on every node, each its own share  (cap {triage_phase} min)
#   phase 2  validation   node_worker.py on every node, each its own share  (cap {validate_phase} min)
#   phase 3  report       report.py
#
# Each phase starts when every node has finished the one before (srun
# returns). The job waits only for the workers, never for a model server, and
# never submits other jobs.

set -u
cd {repo}
RUN={run}
CONFIG={config}
NN=$SLURM_JOB_NUM_NODES
log() {{ echo "[$(date +%T)] $*"; }}
log "job $SLURM_JOB_ID, run {name}, $NN nodes: $SLURM_JOB_NODELIST"

log "phase 1: triage"
timeout -k 2m {triage_phase}m srun -N $NN -n $NN --ntasks-per-node=1 -c {cpus} --kill-on-bad-exit=0 \
    {python} cluster/node_worker.py --run "$RUN" --config "$CONFIG" --phase triage
log "phase 1 done (rc=$?)"

log "phase 2: validation"
timeout -k 2m {validate_phase}m srun -N $NN -n $NN --ntasks-per-node=1 -c {cpus} --kill-on-bad-exit=0 \
    {python} cluster/node_worker.py --run "$RUN" --config "$CONFIG" --phase validate
log "phase 2 done (rc=$?)"

log "phase 3: report"
{python} cluster/report.py --run "$RUN"
log "JOB DONE"
