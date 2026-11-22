#!/bin/bash
#SBATCH -p spark-batch
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem=120500M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-eval-zephyr-multi
# A failed node must not end the whole job (Slurm's default: 491147, 2026-11-16): keep going.
#SBATCH --no-kill
#
# Evaluate a model that needs several nodes per server (the profile's "nodes") on the Zephyr
# instances: the job's nodes form groups of that many, one server per group, its EVAL_AGENTS
# agents spread over the group's nodes (scripts/eval_zephyr_group.sh). -N should be a multiple
# of the profile's nodes; extra nodes stay idle.
#
#     sbatch -N 10 --export=NONE,EVAL_MODEL=minimax-m25-nvfp4,EVAL_AGENTS=2,EVAL_INSTANCES=$PWD/eval-sets/zephyr.txt \
#         -o $SCRATCH/embedeval/eval/logs/%x-%j.out cluster/templates/eval_zephyr_multi.sbatch
#
# Results: <eval dir>/<model>/zephyr/<PR>/. Settings go in --export: EVAL_AGENTS (per server, 2),
# EVAL_GPU_MEMORY (0.80), EVAL_MAX_MODEL_LEN, EVAL_INSTANCES, EVAL_KEEP_IMAGES, EVAL_MAX_JOBS (6),
# EVAL_MARGIN (60), EVAL_TIME_LIMIT (120), EVAL_SERVER_WAIT_MIN (40), EVAL_NODES_PER_SERVER, EMBEDEVAL_EVAL_DIR.
# On spark-interactive (a job gets at most 60 GB of a node): -p spark-interactive --mem=60000M
# --cpus-per-task=10, EVAL_NODES_PER_SERVER twice the profile's nodes, EVAL_GPU_MEMORY=0.38.
# Servers listen on the cluster network, with a key made for each job (only this user can
# read it). Jobs are limited to 4 hours here; the job submits itself again while PRs are left,
# as eval_zephyr.sbatch does. Submit from the repository.
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
cp "$0" "$EVAL/logs/zephyr/eval_zephyr_multi.sbatch" 2>/dev/null
export EVAL_SERVERS=$EVAL_LOGS/servers
mkdir -p "$EVAL_SERVERS" && chmod 700 "$EVAL_SERVERS"
( umask 077; openssl rand -hex 32 > "$EVAL_SERVERS/key" )
# The node script runs from this job's own copy: bash reads a script as it runs
# it, so editing the repository's copy during a job would break the job.
cp scripts/eval_zephyr_group.sh "$EVAL_LOGS/eval_zephyr_group.sh"
# Stop 5 min before the job's own end (its -t, 4 hours at most here).
LIMIT=$(squeue -h -j "$SLURM_JOB_ID" -o %l | head -1)
END=$(( $(date -d "$(squeue -h -j "$SLURM_JOB_ID" -o %e | head -1)" +%s) - 300 ))
[ "$END" -gt "$(date +%s)" ] || END=$(( $(date +%s) + 4 * 3600 - 300 ))
echo "[$(date +%T)] job $ATTEMPT for $MODEL, $SLURM_JOB_NUM_NODES nodes: $SLURM_JOB_NODELIST"
echo "[$(date +%T)] results in $EVAL/zephyr, logs in $EVAL_LOGS"

# The workers stop themselves at END, saving the runs they are on (srun, if
# signalled, kills its tasks outright); this timeout is only a backstop.
# One srun step per server group: a node that fails aborts its own step (its group) only.
NN=${EVAL_NODES_PER_SERVER:-$(.venv/bin/python -c "
import json, sys; sys.path.insert(0, 'harness'); import paths, run
print(json.load(open('triage/models.json'))[run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])]].get('nodes', 1))" "$MODEL")}
mapfile -t HOSTS < <(scontrol show hostnames "$SLURM_JOB_NODELIST")
STEPS=()
for (( g = 0; g + NN <= ${#HOSTS[@]}; g += NN )); do
    W=$(IFS=,; echo "${HOSTS[*]:$g:$NN}")
    EVAL_NODELIST="$SLURM_JOB_NODELIST" \
    timeout -s TERM -k 1m $(( END + 120 - $(date +%s) )) \
        srun -N "$NN" -n "$NN" -w "$W" --ntasks-per-node=1 --export=ALL --kill-on-bad-exit=0 \
        --cpu-bind=none --gres=gpu:1 bash "$EVAL_LOGS/eval_zephyr_group.sh" "$MODEL" "$END" &
    STEPS+=($!)
done
wait "${STEPS[@]}"
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
    keep=$(env | grep -E "^(EVAL_(AGENTS|GPU_MEMORY|MAX_MODEL_LEN|INSTANCES|KEEP_IMAGES|MAX_JOBS|MARGIN|TIME_LIMIT|SERVER_WAIT_MIN|NODES_PER_SERVER)|EMBEDEVAL_EVAL_DIR)=" | paste -sd, -)
    env $(env | grep -E "^(SLURM|SBATCH)_" | cut -d= -f1 | sed "s/^/-u /") sbatch \
        -N "$SLURM_JOB_NUM_NODES" -p "$SLURM_JOB_PARTITION" -t "$LIMIT" \
        --mem="${SLURM_MEM_PER_NODE:-120500}M" --cpus-per-task="${SLURM_CPUS_PER_TASK:-20}" \
        --export=NONE,EVAL_MODEL="$MODEL",EVAL_ATTEMPT=$((ATTEMPT + 1))${keep:+,$keep} \
        -o "$EVAL/logs/zephyr/%x-%j.out" "$EVAL/logs/zephyr/eval_zephyr_multi.sbatch"
fi
