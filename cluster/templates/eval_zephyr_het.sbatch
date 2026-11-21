#!/bin/bash
#SBATCH -p spark-batch
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem=120500M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-eval-zephyr-het
# A failed node must not end the whole job (Slurm's default: 491147, 2026-11-16): keep going.
#SBATCH --no-kill
#SBATCH hetjob
#SBATCH -p spark-interactive
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=10
#SBATCH --mem=60000M
#SBATCH -t 04:00:00
#
# eval_zephyr_multi.sbatch over both kinds of Spark node at once, as one heterogeneous job (the
# account may run two jobs, and the desktop session is one): part 0 on spark-batch (whole nodes,
# the profile's nodes per server, GPU memory 0.80), part 1 on spark-interactive (a job may use
# half a node's memory, 60 GB: EVAL_I_NODES_PER_SERVER nodes per server, GPU memory
# EVAL_I_GPU_MEMORY, 0.42). Each node runs scripts/eval_zephyr_group.sh; all agents take PRs
# from one shared queue.
#
#     sbatch -N 8 : -N 54 --export=NONE,EVAL_MODEL=minimax-m25-nvfp4,EVAL_AGENTS=2,EVAL_I_NODES_PER_SERVER=6,EVAL_INSTANCES=$PWD/eval-sets/zephyr.txt \
#         -o $SCRATCH/embedeval/eval/logs/%x-%j.out cluster/templates/eval_zephyr_het.sbatch
#
# (MiniMax M2.5, NVFP4: 6 interactive nodes per server, ~22 GB of weights each; tested
# 2026-11-16.) Other settings as eval_zephyr_multi.sbatch. The job submits itself again, with
# the same parts, while PRs are left.
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
cp "$0" "$EVAL/logs/zephyr/eval_zephyr_het.sbatch" 2>/dev/null
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
NB=${SLURM_JOB_NUM_NODES_HET_GROUP_0:?not a heterogeneous job}; NI=${SLURM_JOB_NUM_NODES_HET_GROUP_1:?}
LB=$SLURM_JOB_NODELIST_HET_GROUP_0; LI=$SLURM_JOB_NODELIST_HET_GROUP_1
INN=${EVAL_I_NODES_PER_SERVER:?set EVAL_I_NODES_PER_SERVER (interactive nodes per server)}
left() { .venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths
from pathlib import Path
f = sys.argv[2]
ids = Path(f).read_text().split() if f else paths.discover_instances('zephyr')
print(sum(not paths.grade_path(i, sys.argv[1]).exists() for i in ids))" "$MODEL" "${EVAL_INSTANCES:-}"; }
keep() { env | grep -E "^(EVAL_(AGENTS|GPU_MEMORY|I_GPU_MEMORY|I_NODES_PER_SERVER|MAX_MODEL_LEN|INSTANCES|KEEP_IMAGES|MAX_JOBS|MARGIN|TIME_LIMIT|SERVER_WAIT_MIN|STAGGER_SEC|USE_BATCH|B_NODES_PER_SERVER)|EMBEDEVAL_EVAL_DIR)=" | paste -sd, -; }
UNSET=$(env | grep -E "^(SLURM|SBATCH)_" | cut -d= -f1 | sed "s/^/-u /")

# Right-size a resubmitted job: only as many servers as the PRs left need (batch servers
# first: they load faster), so fewer nodes read the weights at once and no node sits idle.
# If this job has more nodes than that, submit a smaller copy of it and stop.
if [ "$ATTEMPT" -gt 1 ] && [ -z "${EVAL_RESIZED:-}" ]; then
    PB=$(.venv/bin/python -c "
import json, sys; sys.path.insert(0, 'harness'); import paths, run
print(json.load(open('triage/models.json'))[run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])]].get('nodes', 1))" "$MODEL")
    L0=$(left); A=${EVAL_AGENTS:-2}
    S=$(( (L0 + A - 1) / A ))
    PB=${EVAL_B_NODES_PER_SERVER:-$PB}
    SB=$(( NB / PB )); [ "$SB" -gt "$S" ] && SB=$S
    # EVAL_USE_BATCH (0): batch nodes at GPU memory 0.80 with an agent's builds beside the model
    # ran out of memory (2 of 8 nodes hung, 2026-11-16), and below ~0.75 a 2-node MiniMax M2.5
    # server cannot hold one full-length request: resubmissions use interactive nodes only.
    [ "${EVAL_USE_BATCH:-0}" = 1 ] || SB=0
    SI=$(( S - SB )); [ "$SI" -gt $(( NI / INN )) ] && SI=$(( NI / INN ))
    NB2=$(( SB * PB )); NI2=$(( SI * INN ))
    if [ "$NB2" -lt "$NB" ] || [ "$NI2" -lt "$NI" ]; then
        X="NONE,EVAL_MODEL=$MODEL,EVAL_ATTEMPT=$ATTEMPT,EVAL_RESIZED=1,$(keep)"
        echo "[$(date +%T)] $L0 PRs left: resizing to $SB batch + $SI interactive servers ($NB2 + $NI2 nodes)"
        if [ "$NB2" -gt 0 ] && [ "$NI2" -gt 0 ]; then
            env $UNSET sbatch -N "$NB2" -t "$LIMIT" --export="$X" : -N "$NI2" -t "$LIMIT" --export="$X" \
                -o "$EVAL/logs/zephyr/%x-%j.out" "$EVAL/logs/zephyr/eval_zephyr_het.sbatch"
        elif [ "$NB2" -gt 0 ]; then
            env $UNSET sbatch -p spark-batch -N "$NB2" -t "$LIMIT" --export="$X" \
                -o "$EVAL/logs/zephyr/%x-%j.out" cluster/templates/eval_zephyr_multi.sbatch
        elif [ "$NI2" -gt 0 ]; then
            env $UNSET sbatch -p spark-interactive -N "$NI2" -t "$LIMIT" --mem=60000M --cpus-per-task=10 \
                --export="$X,EVAL_NODES_PER_SERVER=$INN,EVAL_GPU_MEMORY=${EVAL_I_GPU_MEMORY:-0.42}" \
                -o "$EVAL/logs/zephyr/%x-%j.out" cluster/templates/eval_zephyr_multi.sbatch
        fi
        exit 0
    fi
fi
echo "[$(date +%T)] job $ATTEMPT for $MODEL: batch $NB nodes ($LB), interactive $NI nodes ($LI), $INN per server"
echo "[$(date +%T)] results in $EVAL/zephyr, logs in $EVAL_LOGS"

# The workers stop themselves at END, saving the runs they are on (srun, if
# signalled, kills its tasks outright); this timeout is only a backstop.
T=$(( END + 120 - $(date +%s) ))
PROF_NN=$(.venv/bin/python -c "
import json, sys; sys.path.insert(0, 'harness'); import paths, run
print(json.load(open('triage/models.json'))[run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])]].get('nodes', 1))" "$MODEL")
BNN=${EVAL_B_NODES_PER_SERVER:-$PROF_NN}
# One srun step per server group: a node that fails aborts its own step (its group) only.
# Each part's settings through the environment (a node list has commas, which --export splits).
STEPS=()
launch() {   # <het group> <node list> <nodes per server> <group prefix> <GPU memory> <first group number>
    local hosts g w; mapfile -t hosts < <(scontrol show hostnames "$2")
    for (( g = 0; g + $3 <= ${#hosts[@]}; g += $3 )); do
        w=$(IFS=,; echo "${hosts[*]:$g:$3}")
        EVAL_NODELIST="$2" EVAL_GROUP_PREFIX=$4 EVAL_NODES_PER_SERVER=$3 EVAL_GPU_MEMORY=$5 EVAL_GROUP_OFFSET=$6 \
        timeout -s TERM -k 1m $T srun --het-group=$1 -N "$3" -n "$3" -w "$w" --ntasks-per-node=1 \
            --export=ALL --kill-on-bad-exit=0 --cpu-bind=none --gres=gpu:1 \
            bash "$EVAL_LOGS/eval_zephyr_group.sh" "$MODEL" "$END" &
        STEPS+=($!)
    done
}
launch 0 "$LB" "$BNN" b "${EVAL_GPU_MEMORY:-0.80}" 0
launch 1 "$LI" "$INN" i "${EVAL_I_GPU_MEMORY:-0.42}" $(( NB / BNN ))   # interactive groups after the batch ones
wait "${STEPS[@]}"; rb=0; ri=0
echo "[$(date +%T)] job $ATTEMPT ended"

LEFT=$(left)
echo "[$(date +%T)] $LEFT PRs not graded yet"
if [ "$LEFT" -gt 0 ] && [ "$ATTEMPT" -lt "${EVAL_MAX_JOBS:-6}" ]; then
    X="NONE,EVAL_MODEL=$MODEL,EVAL_ATTEMPT=$((ATTEMPT + 1)),$(keep)"
    env $UNSET sbatch -N "$NB" -t "$LIMIT" --export="$X" : -N "$NI" -t "$LIMIT" --export="$X" \
        -o "$EVAL/logs/zephyr/%x-%j.out" "$EVAL/logs/zephyr/eval_zephyr_het.sbatch"
fi
