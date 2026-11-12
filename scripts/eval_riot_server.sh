#!/bin/bash
# One Spark node of a RIOT evaluation: serve the model on the cluster network
# for the agents on the cpu nodes, until the job stops it. Started by
# cluster/templates/eval_riot.sbatch (component 0), one per node.
#
#     bash scripts/eval_riot_server.sh <model>
#
# The server takes requests only with this job's key ($EVAL_SERVERS/key).
# When it answers, this node writes its address to $EVAL_SERVERS/node<I>.url;
# if it does not start, node<I>.failed, with the reason. Settings, from the
# environment: EVAL_AGENTS (4, = sequences the server takes), EVAL_GPU_MEMORY
# (0.80), EVAL_MAX_MODEL_LEN (262144), EVAL_LOGS, EVAL_SERVERS.
set -u
MODEL=${1:?usage: eval_riot_server.sh <model>}
cd "$(dirname "$0")/.."
I=${SLURM_NODEID:-0}
ME=${SLURM_JOB_ID:-local}-server$I
LOGS=${EVAL_LOGS:?EVAL_LOGS not set}; S=${EVAL_SERVERS:?EVAL_SERVERS not set}
log() { echo "[$(date +%T)] $ME $*"; }
fail() { log "$*"; echo "$*" > "$S/node$I.failed"; exit 1; }

PROFILE=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths, run
print(run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])])" "$MODEL") \
    || fail "no server profile for model $MODEL (harness/run.py LOCAL_MODELS)"

export HOST=0.0.0.0 PORT=8000 VLLM_API_KEY
VLLM_API_KEY=$(cat "$S/key") || fail "no API key in $S/key"
export GPU_MEMORY=${EVAL_GPU_MEMORY:-0.80} MAX_NUM_SEQS=${EVAL_AGENTS:-4}
export MAX_MODEL_LEN=${EVAL_MAX_MODEL_LEN:-262144}
SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
export EMBEDEVAL_VLLM_CACHE=$LOGS/vllm-cache-server$I
[ -d "$EMBEDEVAL_VLLM_CACHE" ] || cp -r "$SHARED/vllm-cache" "$EMBEDEVAL_VLLM_CACHE" 2>/dev/null
rm -f "$EMBEDEVAL_VLLM_CACHE/.fill.lock"

ADDR=$(hostname -f)
log "on $ADDR: $PROFILE, $MAX_NUM_SEQS sequences, GPU memory $GPU_MEMORY, context $MAX_MODEL_LEN"
bash scripts/serve_triage_model.sh "$PROFILE" > "$LOGS/server$I.log" 2>&1 &
SERVER=$!
trap 'pkill -u "$USER" -f "vllm serve" 2>/dev/null; kill $SERVER 2>/dev/null' EXIT TERM

ready() { curl -sf -H "Authorization: Bearer $VLLM_API_KEY" 127.0.0.1:$PORT/v1/models >/dev/null; }
for _ in $(seq 1 120); do
    ready && break
    kill -0 $SERVER 2>/dev/null || break
    sleep 15
done
ready || fail "model server did not start: $(tail -3 "$LOGS/server$I.log" | tr '\n' ' ' | cut -c1-300)"
echo "http://$ADDR:$PORT/v1" > "$S/node$I.url"
log "model ready at http://$ADDR:$PORT/v1"
wait $SERVER
log "server exited ($?)"
