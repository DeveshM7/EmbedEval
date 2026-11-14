#!/bin/bash
#SBATCH -p {partition}
#SBATCH -N {nodes}
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task={cpus}
{gres_line}#SBATCH --mem={mem}
#SBATCH -t {time_limit}
#SBATCH -J {name}
#SBATCH -o {run}/jobs/job-%j.out
#
# EmbedEval {phase} job for run {run}, written by cluster/submit.py at {stamp}.
# One phase on every node (node_worker.py, each node its own share), then the
# report. It runs the frozen code in {repo}, whose files' sha256 are in its
# source-manifest.json. To run again (e.g. to resume), submit again rather
# than edit this.

set -u
cd {repo}
sha256sum --quiet -c source-sha256.txt || {{ echo "frozen source changed"; exit 1; }}
echo "[$(date +%T)] {phase} job $SLURM_JOB_ID on $SLURM_JOB_NODELIST"
# The phase's end, so a worker can tell how long it has left (evaluate).
export PHASE_END=$(( $(date +%s) + {phase_min} * 60 ))
# A clean environment for the workers: nothing of the submitting shell (a
# login node's PATH can hold tools of another architecture).
timeout -k 2m {phase_min}m srun -N "$SLURM_JOB_NUM_NODES" -n "$SLURM_JOB_NUM_NODES" \
    --ntasks-per-node=1 -c {cpus} --kill-on-bad-exit=0 \
    /bin/bash -c 'exec /usr/bin/env -i PATH="{path_prefix}$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        HOME="$HOME" USER="$USER" LANG=C.UTF-8 SLURM_JOB_ID="$SLURM_JOB_ID" \
        SLURM_PROCID="$SLURM_PROCID" SLURM_NTASKS="$SLURM_NTASKS" EMBEDEVAL_PHASE_END="$PHASE_END" \
        {python} cluster/node_worker.py --run "{run}" --config "{config}" --phase {phase}'
echo "[$(date +%T)] {phase} done (rc=$?)"
{end_server}{python} cluster/report.py --run "{run}"
echo "[$(date +%T)] JOB DONE"
