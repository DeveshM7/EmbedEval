#!/bin/bash
# One Spark node of a Zephyr evaluation: serve the model here, then run
# EVAL_AGENTS workers (harness/eval_worker.py) against it until the work, or
# the job's time, runs out. Started by cluster/templates/eval_zephyr.sbatch,
# one per node.
#
#     bash scripts/eval_zephyr_node.sh <model> <stop-at epoch seconds>
#
# The model is a name harness/paths.py knows (e.g. qwen38-nvfp4); its server
# profile comes from harness/run.py LOCAL_MODELS and triage/models.json.
# Settings, all from the environment:
#
#   EVAL_AGENTS       workers on this node, = sequences the server takes (4)
#   EVAL_GPU_MEMORY   share of GPU memory for the server (0.70: the node's
#                     builds and QEMU need the rest of its 120 GB)
#   EVAL_MAX_MODEL_LEN  context (262144)
#   EVAL_INSTANCES    file of instance ids (default: every Zephyr instance)
#   EVAL_MARGIN       minutes before the end after which no new PR is taken (75)
#   EVAL_KEEP_IMAGES  1: keep instance images and git layers after grading
#                     (default: remove)
#   EVAL_LOGS         folder for this node's logs
set -u
MODEL=${1:?usage: eval_zephyr_node.sh <model> <stop-at>}
STOP_AT=${2:?usage: eval_zephyr_node.sh <model> <stop-at>}
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"     # the repository (this may be a job's copy)
I=${SLURM_NODEID:-0}
ME=${SLURM_JOB_ID:-local}-node$I
LOGS=${EVAL_LOGS:?EVAL_LOGS not set}; mkdir -p "$LOGS"
AGENTS=${EVAL_AGENTS:-4}
log() { echo "[$(date +%T)] $ME $*"; }

PROFILE=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths, run
print(run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])])" "$MODEL") \
    || { log "no server profile for model $MODEL (harness/run.py LOCAL_MODELS)"; exit 1; }

export GPU_MEMORY=${EVAL_GPU_MEMORY:-0.70} MAX_NUM_SEQS=$AGENTS
export MAX_MODEL_LEN=${EVAL_MAX_MODEL_LEN:-262144}
# a copy of the warm compile cache for this node, so the server starts without compiling
SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
export EMBEDEVAL_VLLM_CACHE=$LOGS/vllm-cache-node$I
[ -d "$EMBEDEVAL_VLLM_CACHE" ] || cp -r "$SHARED/vllm-cache" "$EMBEDEVAL_VLLM_CACHE" 2>/dev/null
rm -f "$EMBEDEVAL_VLLM_CACHE/.fill.lock"

log "on $(hostname): $PROFILE, $AGENTS agents, GPU memory $GPU_MEMORY, context $MAX_MODEL_LEN"
bash scripts/serve_triage_model.sh "$PROFILE" > "$LOGS/node$I-server.log" 2>&1 &
SERVER=$!
trap 'pkill -u "$USER" -f "vllm serve" 2>/dev/null; kill $SERVER 2>/dev/null' EXIT

for _ in $(seq 1 120); do
    curl -sf 127.0.0.1:8000/v1/models >/dev/null && break
    kill -0 $SERVER 2>/dev/null || break
    sleep 15
done
curl -sf 127.0.0.1:8000/v1/models >/dev/null \
    || { log "model server did not start"; tail -5 "$LOGS/node$I-server.log"; exit 1; }
log "model ready"

EXTRA=()
[ -n "${EVAL_INSTANCES:-}" ] && EXTRA+=(--instances "$EVAL_INSTANCES")
[ "${EVAL_KEEP_IMAGES:-0}" = 1 ] || EXTRA+=(--delete-images)
PIDS=()
for k in $(seq 1 "$AGENTS"); do
    .venv/bin/python harness/eval_worker.py --project zephyr --model "$MODEL" \
        --name "$ME-w$k" --stop-at "$STOP_AT" --margin "${EVAL_MARGIN:-75}" "${EXTRA[@]}" \
        > "$LOGS/node$I-w$k.log" 2>&1 &
    PIDS+=($!)
    sleep 20        # stagger the first image pulls and container starts
done
# On the job's end, pass SIGTERM on: each worker saves its run and records it
# as cut off, for the next job to retry.
trap 'kill -TERM "${PIDS[@]}" 2>/dev/null; wait; pkill -u "$USER" -f "vllm serve"; kill $SERVER 2>/dev/null' TERM
wait "${PIDS[@]}"
log "workers done: $(grep -h ": pass\|: fail\|: not_run" "$LOGS"/node$I-w*.log | wc -l) PRs finished on this node"
