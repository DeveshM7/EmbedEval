#!/bin/bash
#SBATCH -p spark-batch
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem=120500M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-verify
#
# The statement verification's review step (scripts/verify_statements.py), across the
# Spark nodes given: each node serves the model (scripts/verify_statements_node.sh) and
# reviews its share of the tasks (prepared first with `verify_statements.py prepare`).
#
#     sbatch -N 19 --export=NONE -o /scratch/scholar/$USER/embedeval/verify/logs/%x-%j.out \
#         cluster/templates/verify_statements.sbatch
#
# A cut-off job leaves every finished call's result in place; the job submits itself
# again while tasks are left, up to VERIFY_MAX_JOBS (3) jobs. Then run
# `verify_statements.py combine`. Settings in --export: VERIFY_PARALLEL, VERIFY_GPU_MEMORY,
# VERIFY_MAX_JOBS, EMBEDEVAL_VERIFY_DIR. Submit from the repository.
set -u
cd "${SLURM_SUBMIT_DIR:?}"
export EMBEDEVAL_VERIFY_DIR=${EMBEDEVAL_VERIFY_DIR:-/scratch/scholar/$USER/embedeval/verify}
ATTEMPT=${VERIFY_ATTEMPT:-1}
export VERIFY_LOGS=$EMBEDEVAL_VERIFY_DIR/logs/job-$SLURM_JOB_ID
mkdir -p "$VERIFY_LOGS"
cp "$0" "$EMBEDEVAL_VERIFY_DIR/logs/verify_statements.sbatch" 2>/dev/null
cp scripts/verify_statements_node.sh "$VERIFY_LOGS/"     # this job's own copy
echo "[$(date +%T)] job $ATTEMPT: $SLURM_JOB_NUM_NODES nodes ($SLURM_JOB_NODELIST)"

timeout -s TERM -k 1m 13800 srun -N "$SLURM_JOB_NUM_NODES" -n "$SLURM_JOB_NUM_NODES" \
    --ntasks-per-node=1 --export=ALL --kill-on-bad-exit=0 --cpu-bind=none --gres=gpu:1 \
    bash "$VERIFY_LOGS/verify_statements_node.sh"
rc=$?
echo "[$(date +%T)] job $ATTEMPT ended (exit $rc)"
LEFT=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts'); import verify_statements as v
print(sum(not v.done(i) for i in (v.WORK / 'tasks.txt').read_text().split()))")
echo "[$(date +%T)] $LEFT tasks not done"
if [ "$LEFT" -gt 0 ] && [ "$ATTEMPT" -lt "${VERIFY_MAX_JOBS:-3}" ]; then
    keep=$(env | grep -E "^(VERIFY_(PARALLEL|GPU_MEMORY|MAX_JOBS)|EMBEDEVAL_VERIFY_DIR)=" | paste -sd, -)
    env $(env | grep -E "^(SLURM|SBATCH)_" | cut -d= -f1 | sed "s/^/-u /") sbatch \
        -N "$SLURM_JOB_NUM_NODES" --export=NONE,VERIFY_ATTEMPT=$((ATTEMPT + 1))${keep:+,$keep} \
        -o "$EMBEDEVAL_VERIFY_DIR/logs/%x-%j.out" "$EMBEDEVAL_VERIFY_DIR/logs/verify_statements.sbatch"
fi
