#!/bin/bash
#SBATCH -p spark-batch
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem=120500M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-eval-zephyr
#
# Evaluate one model on the Zephyr instances, across the Spark nodes given:
#
#     sbatch -N 10 --export=NONE,EVAL_MODEL=qwen38-nvfp4 \
#         -o $SCRATCH/embedeval/eval/logs/%x-%j.out cluster/templates/eval_zephyr.sbatch
#
# Each node serves the model and runs EVAL_AGENTS agents against it
# (scripts/eval_zephyr_node.sh); every node takes PRs from one shared queue.
# Results: <eval dir>/<model>/zephyr/<PR>/ (harness/paths.py). Settings go in
# --export: EVAL_AGENTS, EVAL_GPU_MEMORY, EVAL_MAX_MODEL_LEN, EVAL_INSTANCES
# (a file of ids, for a trial), EVAL_KEEP_IMAGES, EVAL_MAX_JOBS (6), EVAL_MARGIN
# (minutes before the end after which no new PR is taken, 75), EMBEDEVAL_EVAL_DIR
# (results elsewhere, for a test). A shorter job: -t, also kept on resubmission.
#
# Jobs are limited to 4 hours here. Agents take no new PR in a job's last
# EVAL_MARGIN (75) minutes (a run is capped at an hour, then graded); a run cut off at the
# end is retried by the next job. The job submits itself again while PRs are
# left, up to EVAL_MAX_JOBS jobs in all. Submit from the repository.
set -u
cd "${SLURM_SUBMIT_DIR:?}"
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
export EMBEDEVAL_EVAL_DIR=${EMBEDEVAL_EVAL_DIR:-/scratch/scholar/$USER/embedeval/eval}
MODEL=${EVAL_MODEL:?EVAL_MODEL not set}
ATTEMPT=${EVAL_ATTEMPT:-1}
EVAL=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths
print(paths.OUTPUTS_DIR / paths.model_dir(sys.argv[1]))" "$MODEL")
export EVAL_LOGS=$EVAL/logs/zephyr/job-$SLURM_JOB_ID
mkdir -p "$EVAL_LOGS"
cp "$0" "$EVAL/logs/zephyr/eval_zephyr.sbatch" 2>/dev/null
# The node script runs from this job's own copy: bash reads a script as it runs
# it, so editing the repository's copy during a job would break the job.
cp scripts/eval_zephyr_node.sh "$EVAL_LOGS/eval_zephyr_node.sh"
# Stop 5 min before the job's own end (its -t, 4 hours at most here).
LIMIT=$(squeue -h -j "$SLURM_JOB_ID" -o %l | head -1)
END=$(( $(date -d "$(squeue -h -j "$SLURM_JOB_ID" -o %e | head -1)" +%s) - 300 ))
[ "$END" -gt "$(date +%s)" ] || END=$(( $(date +%s) + 4 * 3600 - 300 ))
echo "[$(date +%T)] job $ATTEMPT for $MODEL, $SLURM_JOB_NUM_NODES nodes: $SLURM_JOB_NODELIST"
echo "[$(date +%T)] results in $EVAL/zephyr, logs in $EVAL_LOGS"

# The workers stop themselves at END, saving the runs they are on (srun, if
# signalled, kills its tasks outright); this timeout is only a backstop.
timeout -s TERM -k 1m $(( END + 120 - $(date +%s) )) \
    srun --export=ALL --kill-on-bad-exit=0 --cpu-bind=none --gres=gpu:1 \
    bash "$EVAL_LOGS/eval_zephyr_node.sh" "$MODEL" "$END"
rc=$?
echo "[$(date +%T)] job $ATTEMPT ended (exit $rc)"

LEFT=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths
from pathlib import Path
f = sys.argv[2]
ids = Path(f).read_text().split() if f else paths.discover_instances('zephyr')
print(sum(not paths.grade_path(i, sys.argv[1]).exists() for i in ids))" "$MODEL" "${EVAL_INSTANCES:-}")
echo "[$(date +%T)] $LEFT PRs not graded yet"
if [ "$LEFT" -gt 0 ] && [ "$ATTEMPT" -lt "${EVAL_MAX_JOBS:-6}" ]; then
    keep=$(env | grep -E "^(EVAL_(AGENTS|GPU_MEMORY|MAX_MODEL_LEN|INSTANCES|KEEP_IMAGES|MAX_JOBS|MARGIN|TIME_LIMIT)|EMBEDEVAL_EVAL_DIR)=" | paste -sd, -)
    env $(env | grep -E "^(SLURM|SBATCH)_" | cut -d= -f1 | sed "s/^/-u /") sbatch \
        -N "$SLURM_JOB_NUM_NODES" -p "$SLURM_JOB_PARTITION" -t "$LIMIT" \
        --export=NONE,EVAL_MODEL="$MODEL",EVAL_ATTEMPT=$((ATTEMPT + 1))${keep:+,$keep} \
        -o "$EVAL/logs/zephyr/%x-%j.out" "$EVAL/logs/zephyr/eval_zephyr.sbatch"
fi
