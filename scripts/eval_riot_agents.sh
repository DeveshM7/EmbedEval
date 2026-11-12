#!/bin/bash
# One cpu node of a RIOT evaluation: wait for the model servers on the Spark
# nodes, check them, then run EVAL_AGENTS workers (harness/eval_worker.py) per
# server against them until the work, or the job's time, runs out. Started by
# cluster/templates/eval_riot.sbatch (component 1), one per node.
#
#     bash scripts/eval_riot_agents.sh <model> <stop-at epoch seconds> <servers>
#
# This node takes server k for every k with k mod (cpu nodes) = this node.
# Checks, before any agent starts (a failure stops the job, recorded in
# $EVAL_LOGS/checks-failed so that it is not resubmitted):
#   - each server answers a short chat request with the job's key;
#   - a request without the key is refused (the server is on the cluster network);
#   - from inside an agent container (--network none) the server cannot be reached.
# Settings, from the environment: EVAL_AGENTS (4), EVAL_MARGIN (75), EVAL_TIME_LIMIT (60), EVAL_INSTANCES,
# EVAL_KEEP_IMAGES, EVAL_LOGS, EVAL_SERVERS.
set -u
MODEL=${1:?usage: eval_riot_agents.sh <model> <stop-at> <servers>}
STOP_AT=${2:?usage: eval_riot_agents.sh <model> <stop-at> <servers>}
NSERVERS=${3:?usage: eval_riot_agents.sh <model> <stop-at> <servers>}
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"     # the repository (this may be a job's copy)
I=${SLURM_NODEID:-0} N=${SLURM_NNODES:-1}
ME=${SLURM_JOB_ID:-local}-cpu$I
LOGS=${EVAL_LOGS:?EVAL_LOGS not set}; S=${EVAL_SERVERS:?EVAL_SERVERS not set}
AGENTS=${EVAL_AGENTS:-4}
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
# This node's instance layers and overlays; the era images are shared.
export EMBEDEVAL_WORK=${EMBEDEVAL_WORK:-/scratch/scholar/$USER/embedeval/eval-work/riot-cpu$I}
PY=$EMBEDEVAL_SHARED/riot/venv/bin/python      # the x86 environment (cluster/setup_riot.py)
log() { echo "[$(date +%T)] $ME $*"; }
stop() { log "CHECK FAILED: $*"; echo "$ME: $*" >> "$LOGS/checks-failed"; exit 1; }

MINE=$(seq 0 $((NSERVERS - 1)) | awk -v i="$I" -v n="$N" '$1 % n == i')
[ -n "$MINE" ] || { log "no server for this node ($NSERVERS servers, $N cpu nodes)"; exit 0; }
log "on $(hostname), $(nproc) cores, $(uname -m); servers $(echo $MINE), $AGENTS agents each"
[ "$(uname -m)" = x86_64 ] || stop "not an x86 node: $(uname -m)"
[ -x "$PY" ] || stop "no x86 Python at $PY (cluster/setup_riot.py)"
KEY=$(cat "$S/key") || stop "no API key in $S/key"

# Wait for this node's servers (they take about 6 minutes to load the model).
for _ in $(seq 1 160); do
    left=0
    for k in $MINE; do
        [ -f "$S/node$k.failed" ] && stop "server $k: $(cat "$S/node$k.failed")"
        [ -f "$S/node$k.url" ] || left=$((left + 1))
    done
    [ $left = 0 ] && break
    sleep 15
done
for k in $MINE; do [ -f "$S/node$k.url" ] || stop "server $k not ready after 40 minutes"; done

# The checks.
PROFILE=$("$PY" -c "
import sys; sys.path.insert(0, 'harness'); import paths, run
print(run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])])" "$MODEL") || stop "no profile for $MODEL"
ERA=$(ls "$EMBEDEVAL_SHARED"/images/riotbuild-*.sif 2>/dev/null | tail -1)
[ -n "$ERA" ] || stop "no RIOT era image in $EMBEDEVAL_SHARED/images"
for k in $MINE; do
    URL=$(cat "$S/node$k.url"); HP=${URL#http://}; HP=${HP%%/*}
    t0=$(date +%s%N)
    out=$(curl -s --max-time 300 -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
        -d "{\"model\": \"$PROFILE\", \"messages\": [{\"role\": \"user\", \"content\": \"Say OK.\"}], \"max_tokens\": 16}" \
        "$URL/chat/completions")
    echo "$out" | grep -q '"choices"' || stop "server $k ($URL): no answer to a test request: ${out:0:300}"
    log "server $k ($URL): answered in $(( ($(date +%s%N) - t0) / 1000000 )) ms"
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 "$URL/models")
    [ "$code" = 401 ] || stop "server $k: a request without the key got HTTP $code, not 401"
    # The same probe with the network on must reach it, or the check proves nothing.
    probe="timeout 10 bash -c 'echo > /dev/tcp/${HP%:*}/${HP#*:}'"
    apptainer exec --containall "$ERA" bash -c "$probe" 2>/dev/null \
        || stop "server $k: the container probe cannot reach it even with the network on"
    if apptainer exec --containall --net --network none "$ERA" bash -c "$probe" 2>/dev/null; then
        stop "server $k: reachable from inside an agent container (--network none)"
    fi
    log "server $k: refused without the key; not reachable from an agent container"
done
log "checks passed"

EXTRA=()
[ -n "${EVAL_INSTANCES:-}" ] && EXTRA+=(--instances "$EVAL_INSTANCES")
[ "${EVAL_KEEP_IMAGES:-0}" = 1 ] || EXTRA+=(--delete-images)
PIDS=()
for k in $MINE; do
    for a in $(seq 1 "$AGENTS"); do
        EMBEDEVAL_LOCAL_API_BASE=$(cat "$S/node$k.url") EMBEDEVAL_LOCAL_API_KEY=$KEY \
            "$PY" harness/eval_worker.py --project riot --model "$MODEL" \
            --name "$ME-s$k-w$a" --stop-at "$STOP_AT" --margin "${EVAL_MARGIN:-75}" --time-limit "${EVAL_TIME_LIMIT:-60}" "${EXTRA[@]}" \
            > "$LOGS/cpu$I-s$k-w$a.log" 2>&1 &
        PIDS+=($!)
        sleep 10        # stagger the first pulls and container starts
    done
done
# On the job's end, pass SIGTERM on: each worker saves its run and records it
# as cut off, for the next job to retry.
trap 'kill -TERM "${PIDS[@]}" 2>/dev/null; wait' TERM
wait "${PIDS[@]}"
log "workers done: $(cat "$LOGS"/cpu$I-s*-w*.log | grep -c ": pass\|: fail\|: not_run") PRs finished on this node"
