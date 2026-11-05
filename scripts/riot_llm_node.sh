#!/bin/bash
# One Spark node of RIOT's LLM stage: serve the model here, then judge this node's share of
# <run folder>/llm/prs.txt with scripts/riot_triage.py. Started by
# cluster/templates/riot_llm.sbatch, one per node; also runs as is in a Spark desktop job
# (RIOT_SHARE=I/N picks a share; the default is this node's share of the job's nodes).
#
#     bash scripts/riot_llm_node.sh <run folder>
#
# The defaults fit a 60 GB job (spark-interactive): a smaller slice of GPU memory, two PRs
# at a time, and a 131,072-token context. LLM_GPU_MEMORY, LLM_MAX_NUM_SEQS,
# LLM_MAX_MODEL_LEN, LLM_PARALLEL and LLM_PROFILE change them. The server is stopped before
# the job's memory limit (LLM_MEM_LIMIT_GB), counting what the job really uses and not the
# file cache the kernel can take back. PRs whose record is too long for the context are
# left without a verdict and not tried again.
set -u
RUN=${1:?usage: riot_llm_node.sh <run folder>}
cd "$(dirname "$0")/.."
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared} EMBEDEVAL_PROJECT=riot
export SLURM_SUBMIT_DIR=$PWD
SHARE=${RIOT_SHARE:-${SLURM_NODEID:-0}/${SLURM_NNODES:-1}}
I=${SHARE%/*}; N=${SHARE#*/}
D=$RUN/llm/node$I; mkdir -p "$D"
log() { echo "[$(date +%T)] node$I $*"; }

export GPU_MEMORY=${LLM_GPU_MEMORY:-0.40} MAX_NUM_SEQS=${LLM_MAX_NUM_SEQS:-2}
export MAX_MODEL_LEN=${LLM_MAX_MODEL_LEN:-131072} EMBEDEVAL_VLLM_CACHE=$D/vllm-cache
PROFILE=${LLM_PROFILE:-qwen38-nvfp4}
# a copy of the warm compile cache, so the server starts without compiling
[ -d "$EMBEDEVAL_VLLM_CACHE" ] || cp -r "$EMBEDEVAL_SHARED/vllm-cache" "$EMBEDEVAL_VLLM_CACHE"
rm -f "$EMBEDEVAL_VLLM_CACHE/.fill.lock"

log "on $(hostname), share $SHARE"
bash scripts/serve_triage_model.sh "$PROFILE" > "$D/server.log" 2>&1 &
SERVER=$!
trap 'pkill -u "$USER" -f "vllm serve"; kill $SERVER 2>/dev/null' EXIT

CG=/sys/fs/cgroup$(awk -F: '{print $3}' /proc/self/cgroup | head -1 | sed -E 's#(/job_[0-9]+)/.*#\1#')
[ -r "$CG/memory.current" ] || log "WARNING: no memory reading at $CG, running without the guard"
( LIMIT=$(( ${LLM_MEM_LIMIT_GB:-54} * 1024 * 1024 * 1024 )); PEAK=0
  while [ -r "$CG/memory.current" ] && kill -0 $SERVER 2>/dev/null; do
      CUR=$(( $(cat "$CG/memory.current") - $(awk '$1 == "inactive_file" {print $2}' "$CG/memory.stat") ))
      [ $CUR -gt $PEAK ] && PEAK=$CUR
      echo "$(date +%T) now $((CUR / 1048576)) MB, peak $((PEAK / 1048576)) MB" > "$D/memory.txt"
      if [ $CUR -gt $LIMIT ]; then
          echo "$(date +%T) STOPPED at $((CUR / 1048576)) MB" >> "$D/memory.txt"
          pkill -9 -u "$USER" -f "vllm serve"; break
      fi
      sleep 2
  done ) &

for _ in $(seq 1 120); do
    curl -sf 127.0.0.1:8000/v1/models >/dev/null && break
    kill -0 $SERVER 2>/dev/null || break
    sleep 15
done
curl -sf 127.0.0.1:8000/v1/models >/dev/null || { log "model did not start"; tail -5 "$D/server.log"; exit 1; }
log "model ready"

MINE=$(awk -v i="$I" -v n="$N" '(NR - 1) % n == i' "$RUN/llm/prs.txt")
todo() {
    for p in $MINE; do
        [ -f "$RUN/triage/$p.json" ] && continue
        grep -qs ContextWindowExceeded "$RUN/triage/$p.trace.json" && continue
        [ -f "$RUN/enriched/$p.json" ] || { log "$p: no enriched record (riot_triage.py enrich)" >&2; continue; }
        echo "$p"
    done
}
for attempt in 1 2 3; do
    TODO=$(todo)
    [ -n "$TODO" ] || break
    curl -sf 127.0.0.1:8000/v1/models >/dev/null || { log "server is gone"; exit 1; }
    log "attempt $attempt: $(echo $TODO | wc -w) PRs"
    .venv/bin/python scripts/riot_triage.py --run-dir "$RUN" run --profile "$PROFILE" \
        --parallel "${LLM_PARALLEL:-2}" --pr $TODO
done
log "finished; no verdict: $(for p in $MINE; do [ -f "$RUN/triage/$p.json" ] || echo -n "$p "; done)"
