#!/bin/bash
# Model pilots: the cpu node of cluster/templates/eval_pilot.sbatch. Runs each model's RIOT
# workers (x86) against its server, after main's checks (key required; the server is not
# reachable from an agent container).
set -u
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"
STOP_AT=${1:?usage: eval_pilot_riot.sh <stop-at>}
LOGS=${EVAL_LOGS:?}; S=${EVAL_SERVERS:?}
ME=${SLURM_JOB_ID:-local}-cpu
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
PY=$EMBEDEVAL_SHARED/riot/venv/bin/python
log() { echo "[$(date +%T)] $ME $*"; }
[ "$(uname -m)" = x86_64 ] || { log "not an x86 node"; exit 1; }
KEY=$(cat "$S/key")
ERA=$(ls "$EMBEDEVAL_SHARED"/images/riotbuild-*.sif | tail -1)
PIDS=()
while read -r MODEL NODES ZW RW; do
    [ -n "${MODEL:-}" ] && [ "${RW:-0}" -gt 0 ] || continue
    for _ in $(seq 1 200); do [ -f "$S/$MODEL.url" ] || [ -f "$S/$MODEL.failed" ] && break; sleep 15; done
    if [ ! -f "$S/$MODEL.url" ]; then log "$MODEL: no server ($(cat "$S/$MODEL.failed" 2>/dev/null))"; continue; fi
    URL=$(cat "$S/$MODEL.url"); HP=${URL#http://}; HP=${HP%%/*}
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 "$URL/models")
    [ "$code" = 401 ] || { log "$MODEL: request without the key got HTTP $code, not 401: skipped"; continue; }
    probe="timeout 10 bash -c 'echo > /dev/tcp/${HP%:*}/${HP#*:}'"
    apptainer exec --containall "$ERA" bash -c "$probe" 2>/dev/null || { log "$MODEL: probe cannot reach the server: skipped"; continue; }
    if apptainer exec --containall --net --network none "$ERA" bash -c "$probe" 2>/dev/null; then
        log "$MODEL: server reachable from an agent container: skipped"; continue
    fi
    log "$MODEL: checks passed ($URL); $RW riot workers"
    for k in $(seq 1 "$RW"); do
        EMBEDEVAL_WORK=${EMBEDEVAL_PILOT_WORK:?}/riot-$MODEL EMBEDEVAL_LOCAL_API_BASE=$URL EMBEDEVAL_LOCAL_API_KEY=$KEY \
            "$PY" harness/eval_worker.py --project riot --model "$MODEL" \
            --name "$ME-$MODEL-rw$k" --stop-at "$STOP_AT" --margin "${EVAL_MARGIN:-60}" --time-limit "${EVAL_TIME_LIMIT:-120}" \
            --instances "${EVAL_RIOT_INSTANCES:?}" > "$LOGS/riot-$MODEL-w$k.log" 2>&1 &
        PIDS+=($!)
        sleep 10
    done
done <<<"$EVAL_PILOT_PLAN"
[ ${#PIDS[@]} -gt 0 ] && wait "${PIDS[@]}"
touch "$S/riot-done"
log "riot workers done"
