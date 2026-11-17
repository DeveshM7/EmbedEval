#!/bin/bash
# One Spark node of a Mynewt evaluation: serve the model on the cluster network
# for the agents on the cpu nodes, until the job stops it. Started by
# cluster/templates/eval_mynewt.sbatch (component 0), one per node.
#
#     bash scripts/eval_mynewt_server.sh <model>
#
# A server takes the profile's `nodes` (K, 1 if absent) Spark nodes: node I is
# rank I mod K of server I / K. Rank 0 serves the API; the other ranks run
# headless, one pipeline stage each (scripts/serve_triage_model.sh).
# The server takes requests only with this job's key ($EVAL_SERVERS/key).
# When it answers, rank 0 writes its address to $EVAL_SERVERS/node<server>.url;
# if it does not start (on any rank), node<server>.failed, with the reason.
# Logs and compile caches are per node: server<server>-r<rank>. Settings, from the
# environment: EVAL_AGENTS (4, = sequences the server takes), EVAL_GPU_MEMORY
# (0.80), EVAL_MAX_MODEL_LEN (the profile's max_model_len, else 262144),
# EVAL_SERVER_WAIT_MIN (40, minutes for the server to start), EVAL_LOGS, EVAL_SERVERS.
set -u
MODEL=${1:?usage: eval_mynewt_server.sh <model>}
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"     # the repository (this may be a job's copy)
I=${SLURM_NODEID:-0} K=1
LOGS=${EVAL_LOGS:?EVAL_LOGS not set}; S=${EVAL_SERVERS:?EVAL_SERVERS not set}
ME=${SLURM_JOB_ID:-local}-node$I
log() { echo "[$(date +%T)] $ME $*"; }
# The first failure of a server is its reason: a later rank's does not replace it.
fail() { log "$*"; [ -f "$S/node$((I / K)).failed" ] || echo "$*" > "$S/node$((I / K)).failed"; exit 1; }

OUT=$(.venv/bin/python -c "
import json, sys; sys.path.insert(0, 'harness'); import paths, run
p = run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])]
print(p, json.load(open('triage/models.json'))[p].get('nodes', 1))" "$MODEL") \
    || fail "no server profile for model $MODEL (harness/run.py LOCAL_MODELS)"
read -r PROFILE K <<<"$OUT"
SRV=$((I / K)) R=$((I % K))
ME=${SLURM_JOB_ID:-local}-server$SRV-r$R

export HOST=0.0.0.0 PORT=8000 VLLM_API_KEY
VLLM_API_KEY=$(cat "$S/key") || fail "no API key in $S/key"
export GPU_MEMORY=${EVAL_GPU_MEMORY:-0.80} MAX_NUM_SEQS=${EVAL_AGENTS:-4}
# the profile's max_model_len (262144 if none) unless EVAL_MAX_MODEL_LEN is given
[ -n "${EVAL_MAX_MODEL_LEN:-}" ] && export MAX_MODEL_LEN=$EVAL_MAX_MODEL_LEN
SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
export EMBEDEVAL_VLLM_CACHE=$LOGS/vllm-cache-server$SRV-r$R
[ -d "$EMBEDEVAL_VLLM_CACHE" ] || cp -r "$SHARED/vllm-cache" "$EMBEDEVAL_VLLM_CACHE" 2>/dev/null
rm -f "$EMBEDEVAL_VLLM_CACHE/.fill.lock"
if [ "$K" -gt 1 ]; then
    MASTER=$(scontrol show hostnames "${SLURM_JOB_NODELIST_HET_GROUP_0:-${SLURM_JOB_NODELIST:-}}" | sed -n "$((SRV * K + 1))p")
    export VLLM_NNODES=$K VLLM_NODE_RANK=$R VLLM_MASTER_ADDR=$(getent hosts "$MASTER" | awk '{print $1}')
    [ -n "$VLLM_MASTER_ADDR" ] || fail "no address for rank 0's node (${MASTER:-not in the node list})"
fi

ADDR=$(hostname -f)
log "on $ADDR: $PROFILE, rank $R of $K, $MAX_NUM_SEQS sequences, GPU memory $GPU_MEMORY, context ${MAX_MODEL_LEN:-from the profile}"
bash scripts/serve_triage_model.sh "$PROFILE" > "$LOGS/server$SRV-r$R.log" 2>&1 &
SERVER=$!
trap 'pkill -u "$USER" -f "vllm serve" 2>/dev/null; kill $SERVER 2>/dev/null' EXIT TERM

if [ "$R" -gt 0 ]; then
    # A headless stage has no API to poll: it serves while its process lives. If
    # that process ends before rank 0 answered, the whole server is unusable.
    wait $SERVER; rc=$?
    [ -f "$S/node$SRV.url" ] || fail "rank $R stopped (exit $rc) before the server started: $(tail -3 "$LOGS/server$SRV-r$R.log" | tr '\n' ' ' | cut -c1-300)"
    log "server exited ($rc)"; exit 0
fi
ready() { curl -sf -H "Authorization: Bearer $VLLM_API_KEY" 127.0.0.1:$PORT/v1/models >/dev/null; }
for _ in $(seq 1 $(( ${EVAL_SERVER_WAIT_MIN:-40} * 4 ))); do
    ready && break
    kill -0 $SERVER 2>/dev/null || break
    sleep 15
done
ready || fail "model server did not start: $(tail -3 "$LOGS/server$SRV-r$R.log" | tr '\n' ' ' | cut -c1-300)"
echo "http://$ADDR:$PORT/v1" > "$S/node$SRV.url"
log "model ready at http://$ADDR:$PORT/v1"
wait $SERVER
log "server exited ($?)"
