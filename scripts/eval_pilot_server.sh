#!/bin/bash
# Model pilots (docs/model_pilots.md): one server node of cluster/templates/eval_pilot.sbatch.
# EVAL_PILOT_PLAN lists "<model> <nodes> <zephyr workers> <riot workers>" per model, in node
# order; this node (SLURM_NODEID) serves its model (rank 0 serves the API, reachable with the
# key from the cpu nodes; further ranks run headless) and rank 0 runs the model's Zephyr workers.
set -u
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"
I=${SLURM_NODEID:-0}
LOGS=${EVAL_LOGS:?EVAL_LOGS not set}; S=${EVAL_SERVERS:?EVAL_SERVERS not set}
STOP_AT=${1:?usage: eval_pilot_server.sh <stop-at>}
read -r MODEL RANK NNODES ZW FIRST < <(awk -v i="$I" '
    NF >= 4 { for (r = 0; r < $2; r++) { if (n == i) print $1, r, $2, $3, n - r; n++ } }' <<<"$EVAL_PILOT_PLAN")
[ -n "${MODEL:-}" ] || { echo "node $I: not in the plan"; exit 0; }
ME=${SLURM_JOB_ID:-local}-$MODEL-r$RANK
log() { echo "[$(date +%T)] $ME $*"; }
fail() { log "$*"; echo "$*" > "$S/$MODEL.failed"; exit 1; }
PROFILE=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths, run
print(run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])])" "$MODEL") || fail "no profile for $MODEL"
export HOST=0.0.0.0 PORT=8000 VLLM_API_KEY
VLLM_API_KEY=$(cat "$S/key") || fail "no API key in $S/key"
AGENTS=$(awk -v m="$MODEL" '$1 == m { print $3 + $4 }' <<<"$EVAL_PILOT_PLAN")
export GPU_MEMORY=${EVAL_GPU_MEMORY:-0.80} MAX_NUM_SEQS=$AGENTS
export EMBEDEVAL_VLLM_CACHE=$LOGS/vllm-cache-$MODEL-r$RANK
if [ "$NNODES" -gt 1 ]; then
    MASTER=$(scontrol show hostnames "$SLURM_JOB_NODELIST_HET_GROUP_0" | sed -n "$((FIRST + 1))p")
    export VLLM_NNODES=$NNODES VLLM_NODE_RANK=$RANK VLLM_MASTER_ADDR=$(getent hosts "$MASTER" | awk '{print $1}')
fi
log "on $(hostname -f): $PROFILE, rank $RANK of $NNODES, $AGENTS sequences"
bash scripts/serve_triage_model.sh "$PROFILE" > "$LOGS/server-$MODEL-r$RANK.log" 2>&1 &
SERVER=$!
trap 'pkill -u "$USER" -f "vllm serve" 2>/dev/null; kill $SERVER 2>/dev/null' EXIT TERM
if [ "$RANK" -gt 0 ]; then
    # a headless stage: until rank 0 says the model is finished (or the server stops)
    while kill -0 $SERVER 2>/dev/null && [ ! -f "$S/$MODEL.finished" ]; do sleep 20; done
    log "stopping (rank 0 finished)"; exit 0
fi
ready() { curl -sf -H "Authorization: Bearer $VLLM_API_KEY" 127.0.0.1:$PORT/v1/models >/dev/null; }
for _ in $(seq 1 160); do ready && break; kill -0 $SERVER 2>/dev/null || break; sleep 15; done
ready || { touch "$S/$MODEL.finished"; fail "model server did not start: $(tail -3 "$LOGS/server-$MODEL-r$RANK.log" | tr '\n' ' ' | cut -c1-300)"; }
URL=http://$(hostname -f):$PORT/v1
echo "$URL" > "$S/$MODEL.url"
log "model ready at $URL"
# Zephyr workers on this node (aarch64), against this server.
export EMBEDEVAL_WORK=${EMBEDEVAL_PILOT_WORK:?}/zephyr-$MODEL
PIDS=()
for k in $(seq 1 "$ZW"); do
    EMBEDEVAL_LOCAL_API_BASE=$URL EMBEDEVAL_LOCAL_API_KEY=$VLLM_API_KEY \
        .venv/bin/python harness/eval_worker.py --project zephyr --model "$MODEL" \
        --name "$ME-zw$k" --stop-at "$STOP_AT" --margin "${EVAL_MARGIN:-60}" --time-limit "${EVAL_TIME_LIMIT:-120}" \
        --instances "${EVAL_ZEPHYR_INSTANCES:?}" > "$LOGS/zephyr-$MODEL-w$k.log" 2>&1 &
    PIDS+=($!)
    sleep 20
done
wait "${PIDS[@]}"
log "zephyr workers done; serving until the riot workers finish"
while [ ! -f "$S/riot-done" ] && [ "$(date +%s)" -lt "${STOP_AT%.*}" ]; do sleep 30; done
touch "$S/$MODEL.finished"
log "done"
