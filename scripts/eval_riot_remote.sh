#!/bin/bash
# One cpu node of cluster/templates/eval_riot_multi.sbatch: RIOT agents (x86) against model
# servers on GB10 nodes, each served by a group of nodes (scripts/eval_zephyr_group.sh with
# EVAL_SERVE_ONLY=1; one node or several per server).
#
#     bash scripts/eval_riot_remote.sh <model> <stop-at epoch seconds> <group> [<group> ...]
#
# This node takes group number k (in the order given) for every k with k mod (cpu nodes) =
# this node. Each group is handled on its own, as its server comes up (the starts are
# staggered):
#   - wait for $S/<group>.url; a group whose server did not start ($S/<group>.failed, or no
#     URL in time) is skipped, not the whole job;
#   - agents for it: EVAL_AGENTS, fewer if its server reported less room ("Maximum
#     concurrency" in its rank-0 log): EVAL_ROOM_PER_AGENT (0.73) full-length requests each;
#   - checks before its agents start: a test request with the job's key is answered; one
#     without the key is refused; the server cannot be reached from inside an agent
#     container (--network none). A failed check skips that group (recorded in
#     $EVAL_LOGS/checks-failed);
#   - a watchdog: if its server stops answering for 3 minutes, or generates nothing for
#     EVAL_HANG_MIN (10) minutes with requests running, its agents are stopped (their runs
#     are recorded as cut off, for a later job to retry).
# Settings, from the environment: EVAL_AGENTS (3), EVAL_ROOM_PER_AGENT, EVAL_MARGIN (60),
# EVAL_TIME_LIMIT (120), EVAL_INSTANCES, EVAL_KEEP_IMAGES, EVAL_SERVER_WAIT_MIN (90),
# EVAL_STAGGER_SEC (60), EVAL_HANG_MIN, EVAL_LOGS, EVAL_SERVERS.
set -u
MODEL=${1:?usage: eval_riot_remote.sh <model> <stop-at> <group>...}
STOP_AT=${2:?usage: eval_riot_remote.sh <model> <stop-at> <group>...}
shift 2; GROUPS_ALL=("$@")
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")/..}"     # the repository (this may be a job's copy)
I=${SLURM_NODEID:-0} N=${SLURM_NNODES:-1}
ME=${SLURM_JOB_ID:-local}-cpu$I
LOGS=${EVAL_LOGS:?EVAL_LOGS not set}; S=${EVAL_SERVERS:?EVAL_SERVERS not set}
AGENTS=${EVAL_AGENTS:-3}
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
# This node's instance layers and overlays; the era images are shared.
export EMBEDEVAL_WORK=${EMBEDEVAL_WORK:-/scratch/scholar/$USER/embedeval/eval-work/riot-cpu$I}
PY=$EMBEDEVAL_SHARED/riot/venv/bin/python      # the x86 environment (cluster/setup_riot.py)
log() { echo "[$(date +%T)] $ME $*"; }
fail() { log "CHECK FAILED: $*"; echo "$ME: $*" >> "$LOGS/checks-failed"; }

MINE=(); for k in "${!GROUPS_ALL[@]}"; do [ $(( k % N )) = "$I" ] && MINE+=("${GROUPS_ALL[$k]}"); done
[ ${#MINE[@]} -gt 0 ] || { log "no server group for this node (${#GROUPS_ALL[@]} groups, $N cpu nodes)"; exit 0; }
log "on $(hostname), $(nproc) cores, $(uname -m); groups ${MINE[*]}, up to $AGENTS agents each"
[ "$(uname -m)" = x86_64 ] || { fail "not an x86 node: $(uname -m)"; exit 1; }
[ -x "$PY" ] || { fail "no x86 Python at $PY (cluster/setup_riot.py)"; exit 1; }
KEY=$(cat "$S/key") || { fail "no API key in $S/key"; exit 1; }
PROFILE=$("$PY" -c "
import sys; sys.path.insert(0, 'harness'); import paths, run
print(run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])])" "$MODEL") || { fail "no profile for $MODEL"; exit 1; }
ERA=$(ls "$EMBEDEVAL_SHARED"/images/riotbuild-*.sif 2>/dev/null | tail -1)
[ -n "$ERA" ] || { fail "no RIOT era image in $EMBEDEVAL_SHARED/images"; exit 1; }
EXTRA=()
[ -n "${EVAL_INSTANCES:-}" ] && EXTRA+=(--instances "$EVAL_INSTANCES")
[ "${EVAL_KEEP_IMAGES:-0}" = 1 ] || EXTRA+=(--delete-images)

# One group: wait, size, check, run its agents, watch its server.
group() {
    local g=$1 url hp out code room n pids=() a watch
    # its server: up to the start-up wait after its staggered start (all groups of the job count)
    local wait_s=$(( ${EVAL_SERVER_WAIT_MIN:-90} * 60 + ${#GROUPS_ALL[@]} * ${EVAL_STAGGER_SEC:-60} + 120 ))
    local t_end=$(( $(date +%s) + wait_s ))
    until [ -f "$S/$g.url" ] || [ -f "$S/$g.failed" ] || [ "$(date +%s)" -gt "$t_end" ]; do sleep 15; done
    [ -f "$S/$g.url" ] || { log "$g: no server ($(cat "$S/$g.failed" 2>/dev/null || echo 'not ready in time')): skipped"; return; }
    url=$(cat "$S/$g.url"); hp=${url#http://}; hp=${hp%%/*}
    # agents for its room (its server takes EVAL_AGENTS sequences; fewer agents if it has less room)
    room=$(grep -hoE "Maximum concurrency for [0-9,]+ tokens per request: [0-9.]+x" "$LOGS/$g-r0-server.log" 2>/dev/null \
           | grep -oE "[0-9.]+x$" | tr -d x | tail -1)
    n=$(awk -v r="${room:-0}" -v per="${EVAL_ROOM_PER_AGENT:-0.73}" -v max="$AGENTS" \
        'BEGIN { n = (r > 0) ? int(r / per) : max; if (n > max) n = max; if (n < 1) n = 1; print n }')
    # the checks
    out=$(curl -s --max-time 300 -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
        -d "{\"model\": \"$PROFILE\", \"messages\": [{\"role\": \"user\", \"content\": \"Say OK.\"}], \"max_tokens\": 16}" \
        "$url/chat/completions")
    echo "$out" | grep -q '"choices"' || { fail "$g ($url): no answer to a test request: ${out:0:200}"; return; }
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 "$url/models")
    [ "$code" = 401 ] || { fail "$g: a request without the key got HTTP $code, not 401"; return; }
    local probe="timeout 10 bash -c 'echo > /dev/tcp/${hp%:*}/${hp#*:}'"
    apptainer exec --containall "$ERA" bash -c "$probe" 2>/dev/null \
        || { fail "$g: the container probe cannot reach it even with the network on"; return; }
    if apptainer exec --containall --net --network none "$ERA" bash -c "$probe" 2>/dev/null; then
        fail "$g: reachable from inside an agent container (--network none)"; return
    fi
    log "$g ($url): checks passed; room ${room:-?}x: $n agents"
    for a in $(seq 1 "$n"); do
        EMBEDEVAL_LOCAL_API_BASE=$url EMBEDEVAL_LOCAL_API_KEY=$KEY \
            "$PY" harness/eval_worker.py --project "${EVAL_PROJECT:-riot}" --model "$MODEL" \
            --name "$ME-$g-w$a" --stop-at "$STOP_AT" --margin "${EVAL_MARGIN:-60}" --time-limit "${EVAL_TIME_LIMIT:-120}" "${EXTRA[@]}" \
            > "$LOGS/cpu$I-$g-w$a.log" 2>&1 &
        pids+=($!)
        sleep 10        # stagger the first pulls and container starts
    done
    trap 'kill -TERM "${pids[@]}" 2>/dev/null; wait' TERM
    # the watchdog (as in eval_zephyr_group.sh): dead for 3 min, or hung for EVAL_HANG_MIN
    (   miss=0; still=0; prev=""
        while sleep 60; do
            if curl -sf -m 30 -H "Authorization: Bearer $KEY" "$url/models" >/dev/null; then miss=0; else miss=$(( miss + 1 )); fi
            if [ $miss -ge 3 ]; then log "$g: server not answering for 3 min: stopping its agents"
                kill -TERM "${pids[@]}" 2>/dev/null; exit 0; fi
            m=$(curl -sf -m 30 -H "Authorization: Bearer $KEY" "${url%/v1}/metrics" 2>/dev/null)
            tok=$(awk '/^vllm:generation_tokens_total/ {s+=$NF} END {printf "%.0f", s}' <<<"$m")
            run=$(awk '/^vllm:num_requests_running/ {s+=$NF} END {printf "%.0f", s}' <<<"$m")
            if [ -n "$m" ] && [ "${run:-0}" -gt 0 ] && [ "$tok" = "$prev" ]; then still=$(( still + 1 )); else still=0; fi
            prev=$tok
            if [ $still -ge "${EVAL_HANG_MIN:-10}" ]; then
                log "$g: server generated nothing for ${EVAL_HANG_MIN:-10} min with requests running: stopping its agents"
                kill -TERM "${pids[@]}" 2>/dev/null; exit 0; fi
        done ) &
    watch=$!
    wait "${pids[@]}"
    kill $watch 2>/dev/null
    log "$g: agents done: $(cat "$LOGS"/cpu$I-$g-w*.log 2>/dev/null | grep -c ": pass\|: fail\|: not_run") PRs finished"
}

HANDLERS=()
for g in "${MINE[@]}"; do group "$g" & HANDLERS+=($!); done
# On the job's end, pass SIGTERM on: each worker saves its run and records it as cut off.
trap 'kill -TERM "${HANDLERS[@]}" 2>/dev/null; wait' TERM
wait "${HANDLERS[@]}"
log "all groups done"
