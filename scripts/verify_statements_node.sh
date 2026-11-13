#!/bin/bash
# One Spark node of the statement verification (scripts/verify_statements.py): serve the
# model here, then review this node's share of the tasks. Started by
# cluster/templates/verify_statements.sbatch, one per node.
#
#     bash scripts/verify_statements_node.sh
#
# Settings, from the environment: VERIFY_PARALLEL (tasks at a time, 8; each runs 3 judges
# at once), VERIFY_GPU_MEMORY (0.80: about 100 of the GB10's 124 GiB are free at start), VERIFY_LOGS.
set -u
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"
I=${SLURM_NODEID:-0} N=${SLURM_NNODES:-1}
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
LOGS=${VERIFY_LOGS:?VERIFY_LOGS not set}
D=$LOGS/node$I; mkdir -p "$D"
log() { echo "[$(date +%T)] node$I $*"; }
PAR=${VERIFY_PARALLEL:-8}

export GPU_MEMORY=${VERIFY_GPU_MEMORY:-0.80} MAX_NUM_SEQS=$((PAR * 3 + 4)) MAX_MODEL_LEN=262144
export EMBEDEVAL_VLLM_CACHE=$D/vllm-cache
[ -d "$EMBEDEVAL_VLLM_CACHE" ] || cp -r "$EMBEDEVAL_SHARED/vllm-cache" "$EMBEDEVAL_VLLM_CACHE"
rm -f "$EMBEDEVAL_VLLM_CACHE/.fill.lock"
log "on $(hostname), share $I/$N, $PAR tasks at a time, $MAX_NUM_SEQS sequences"
bash scripts/serve_triage_model.sh qwen38-nvfp4 > "$D/server.log" 2>&1 &
SERVER=$!
trap 'pkill -u "$USER" -f "vllm serve"; kill $SERVER 2>/dev/null' EXIT TERM

for _ in $(seq 1 120); do
    curl -sf 127.0.0.1:8000/v1/models >/dev/null && break
    kill -0 $SERVER 2>/dev/null || break
    sleep 15
done
curl -sf 127.0.0.1:8000/v1/models >/dev/null || { log "model did not start"; tail -5 "$D/server.log"; exit 1; }
log "model ready"
.venv/bin/python scripts/verify_statements.py review --share "$I/$N" --parallel "$PAR" \
    > "$D/review.log" 2>&1
log "review ended (exit $?): $(tail -1 "$D/review.log")"
