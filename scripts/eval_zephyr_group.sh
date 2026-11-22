#!/bin/bash
# One node of cluster/templates/eval_zephyr_multi.sbatch: a Zephyr evaluation of a model that
# needs several nodes per server (the profile's "nodes", e.g. 2 for MiniMax M2.5).
#
# The job's nodes form groups of that many nodes, in node order; each group serves the model
# once (pipeline parallel: rank 0 serves the API, the other ranks run headless) and runs
# EVAL_AGENTS agents against it, spread over the group's nodes: agent k runs on rank k mod
# (nodes per server). So each node does the builds and QEMU runs of its own agents only;
# agents on rank > 0 reach rank 0's API over the network, with the job's key. Nodes that do
# not make a whole group are left idle. Every agent takes PRs from one shared queue.
#
# Files in EVAL_SERVERS: key; g<G>.url once group G's server answers; g<G>.failed if it does
# not start; g<G>.r<R>.done once rank R's agents are done. A group's servers stay up until
# all its ranks are done (or the job's end).
#
# Settings, from the environment (see also eval_zephyr_node.sh): EVAL_AGENTS (agents per
# server, = sequences the server takes; 2), EVAL_NODES_PER_SERVER (the profile's "nodes"),
# EVAL_GPU_MEMORY (0.80; ~0.38 on half-memory nodes), EVAL_MAX_MODEL_LEN,
# EVAL_INSTANCES, EVAL_MARGIN (60), EVAL_TIME_LIMIT (120), EVAL_KEEP_IMAGES,
# EVAL_SERVER_WAIT_MIN (minutes to wait for a server, 90), EVAL_LOGS, EVAL_SERVERS.
set -u
MODEL=${1:?usage: eval_zephyr_group.sh <model> <stop-at>}
STOP_AT=${2:?usage: eval_zephyr_group.sh <model> <stop-at>}
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"     # the repository (this may be a job's copy)
I=${SLURM_NODEID:-0}
LOGS=${EVAL_LOGS:?EVAL_LOGS not set}; S=${EVAL_SERVERS:?EVAL_SERVERS not set}; mkdir -p "$LOGS"
AGENTS=${EVAL_AGENTS:-2}

read -r PROFILE NN < <(.venv/bin/python -c "
import json, sys; sys.path.insert(0, 'harness'); import paths, run
p = run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])]
print(p, json.load(open('triage/models.json'))[p].get('nodes', 1))" "$MODEL") \
    || { echo "[$(date +%T)] node $I: no server profile for model $MODEL"; exit 1; }
# EVAL_NODES_PER_SERVER: more nodes than the profile's, e.g. on spark-interactive, where a job
# may use only half a node's memory (60 GB), so the model needs twice the nodes.
NN=${EVAL_NODES_PER_SERVER:-$NN}
# EVAL_NODELIST / EVAL_GROUP_PREFIX: for one part of a heterogeneous job (its own nodes, and
# group names that do not clash with the other part's). A node finds its place by its name.
mapfile -t HOSTS < <(scontrol show hostnames "${EVAL_NODELIST:-${SLURM_JOB_NODELIST:?}}")
for j in "${!HOSTS[@]}"; do [ "${HOSTS[$j]}" = "$(hostname -s)" ] && I=$j; done
G=$(( I / NN )); RANK=$(( I % NN )); GL=${EVAL_GROUP_PREFIX:-}g$G
ME=${SLURM_JOB_ID:-local}-${GL}-r$RANK
log() { echo "[$(date +%T)] $ME $*"; }
if [ $(( (G + 1) * NN )) -gt "${#HOSTS[@]}" ]; then
    log "node $I of ${#HOSTS[@]} does not make a whole group of $NN: idle"; exit 0
fi
MASTER=${HOSTS[$(( G * NN ))]}
# this node's agents: k = RANK, RANK + NN, ... below AGENTS
MINE=(); for (( k = RANK; k < AGENTS; k += NN )); do MINE+=($k); done
# EVAL_SERVE_ONLY=1 (cluster/templates/eval_riot_multi.sbatch): serve the model for agents
# elsewhere (RIOT's, on x86 cpu nodes) and run none here; serve until $S/agents.done.
[ "${EVAL_SERVE_ONLY:-0}" = 1 ] && MINE=()

export HOST=0.0.0.0 PORT=8000 VLLM_API_KEY
VLLM_API_KEY=$(cat "$S/key") || { log "no API key in $S/key"; exit 1; }
export GPU_MEMORY=${EVAL_GPU_MEMORY:-0.80} MAX_NUM_SEQS=$AGENTS
[ -n "${EVAL_MAX_MODEL_LEN:-}" ] && export MAX_MODEL_LEN=$EVAL_MAX_MODEL_LEN
if [ "$NN" -gt 1 ]; then
    export VLLM_NNODES=$NN VLLM_NODE_RANK=$RANK
    VLLM_MASTER_ADDR=$(getent hosts "$MASTER" | awk '{print $1; exit}'); export VLLM_MASTER_ADDR
fi
SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
export EMBEDEVAL_VLLM_CACHE=$LOGS/vllm-cache-${GL}-r$RANK
[ -d "$EMBEDEVAL_VLLM_CACHE" ] || cp -r "$SHARED/vllm-cache" "$EMBEDEVAL_VLLM_CACHE" 2>/dev/null
rm -f "$EMBEDEVAL_VLLM_CACHE/.fill.lock"

log "on $(hostname -f): $PROFILE, group $G (master $MASTER), rank $RANK of $NN, agents ${MINE[*]:-none} of $AGENTS, GPU memory $GPU_MEMORY"
[ -n "${EVAL_DRY:-}" ] && exit 0
# stagger the servers' starts (EVAL_STAGGER_SEC per group, 60), so that fewer of them read
# the weights from scratch at once: with 68 nodes at once, loading took ~40 min, not ~15
# EVAL_GROUP_OFFSET: this part's first group number in the job (a heterogeneous job's parts
# number their groups from 0 each), so that no two groups of the job start at the same time.
sleep $(( (G + ${EVAL_GROUP_OFFSET:-0}) * ${EVAL_STAGGER_SEC:-60} ))
bash scripts/serve_triage_model.sh "$PROFILE" > "$LOGS/${GL}-r$RANK-server.log" 2>&1 &
SERVER=$!
trap 'pkill -u "$USER" -f "vllm serve" 2>/dev/null; kill $SERVER 2>/dev/null' EXIT

WAIT=$(( ${EVAL_SERVER_WAIT_MIN:-90} * 4 ))   # 90 min: with many servers loading at once, M2 took ~50 min (2026-11-17)
if [ "$RANK" = 0 ]; then
    ready() { curl -sf -H "Authorization: Bearer $VLLM_API_KEY" 127.0.0.1:$PORT/v1/models >/dev/null; }
    for _ in $(seq 1 $WAIT); do ready && break; kill -0 $SERVER 2>/dev/null || break; sleep 15; done
    if ! ready; then
        log "model server did not start"; tail -5 "$LOGS/${GL}-r$RANK-server.log"
        echo "did not start" > "$S/${GL}.failed"; exit 1
    fi
    echo "http://$(hostname -f):$PORT/v1" > "$S/${GL}.url"
else
    for _ in $(seq 1 $(( WAIT + 8 ))); do
        [ -f "$S/${GL}.url" ] || [ -f "$S/${GL}.failed" ] && break
        kill -0 $SERVER 2>/dev/null || break
        sleep 15
    done
    [ -f "$S/${GL}.url" ] || { log "no server for group $G"; tail -5 "$LOGS/${GL}-r$RANK-server.log"; exit 1; }
fi
URL=$(cat "$S/${GL}.url")
log "model ready at $URL"

EXTRA=()
[ -n "${EVAL_INSTANCES:-}" ] && EXTRA+=(--instances "$EVAL_INSTANCES")
[ "${EVAL_KEEP_IMAGES:-0}" = 1 ] || EXTRA+=(--delete-images)
PIDS=()
for k in "${MINE[@]}"; do
    EMBEDEVAL_LOCAL_API_BASE=$URL EMBEDEVAL_LOCAL_API_KEY=$VLLM_API_KEY \
        .venv/bin/python harness/eval_worker.py --project zephyr --model "$MODEL" \
        --name "$ME-w$k" --stop-at "$STOP_AT" --margin "${EVAL_MARGIN:-60}" --time-limit "${EVAL_TIME_LIMIT:-120}" "${EXTRA[@]}" \
        > "$LOGS/${GL}-r$RANK-w$k.log" 2>&1 &
    PIDS+=($!)
    sleep 20        # stagger the first image pulls and container starts
done
# On the job's end, pass SIGTERM on: each worker saves its run and records it
# as cut off, for the next job to retry.
trap 'kill -TERM "${PIDS[@]}" 2>/dev/null; wait; pkill -u "$USER" -f "vllm serve"; kill $SERVER 2>/dev/null' TERM
# Watchdog: if the group's server stops answering (a node of the group failed: 491147), stop
# this node's agents, so they do not run PR after PR against a dead server and mark them not run.
if [ ${#PIDS[@]} -gt 0 ]; then
    # Also a hung engine (491802's ig4: its API kept answering /v1/models while generation
    # had stopped for 3 h): requests running but no new tokens for EVAL_HANG_MIN (10) minutes.
    (   miss=0; still=0; prev=""
        while sleep 60; do
            if curl -sf -m 30 -H "Authorization: Bearer $VLLM_API_KEY" "$URL/models" >/dev/null; then miss=0
            else miss=$(( miss + 1 )); fi
            if [ $miss -ge 3 ]; then
                log "server not answering for 3 min: stopping this node's agents"
                kill -TERM "${PIDS[@]}" 2>/dev/null; exit 0
            fi
            m=$(curl -sf -m 30 -H "Authorization: Bearer $VLLM_API_KEY" "${URL%/v1}/metrics" 2>/dev/null)
            tok=$(awk '/^vllm:generation_tokens_total/ {s+=$NF} END {printf "%.0f", s}' <<<"$m")
            run=$(awk '/^vllm:num_requests_running/ {s+=$NF} END {printf "%.0f", s}' <<<"$m")
            if [ -n "$m" ] && [ "${run:-0}" -gt 0 ] && [ "$tok" = "$prev" ]; then still=$(( still + 1 )); else still=0; fi
            prev=$tok
            if [ $still -ge "${EVAL_HANG_MIN:-10}" ]; then
                log "server generated nothing for ${EVAL_HANG_MIN:-10} min with requests running: stopping this node's agents"
                kill -TERM "${PIDS[@]}" 2>/dev/null; exit 0
            fi
        done ) &
    WATCH=$!
    wait "${PIDS[@]}"
    kill $WATCH 2>/dev/null
fi
log "workers done: $(cat "$LOGS"/${GL}-r$RANK-w*.log 2>/dev/null | grep -c ": pass\|: fail\|: not_run") PRs finished on this node"
touch "$S/${GL}.r$RANK.done"
if [ "${EVAL_SERVE_ONLY:-0}" = 1 ]; then
    # serve until the agents elsewhere are done (or the job's end, or the server stops)
    while [ ! -f "$S/agents.done" ] && [ "$(date +%s)" -lt "${STOP_AT%.*}" ] && kill -0 $SERVER 2>/dev/null; do
        sleep 30 & wait $!
    done
else
    # keep serving until every rank of the group is done (rank 0's API serves them all)
    while [ "$(ls "$S"/${GL}.r*.done 2>/dev/null | wc -l)" -lt "$NN" ] && [ "$(date +%s)" -lt "${STOP_AT%.*}" ] \
          && kill -0 $SERVER 2>/dev/null; do
        sleep 30 & wait $!
    done
fi
log "group $G done"
