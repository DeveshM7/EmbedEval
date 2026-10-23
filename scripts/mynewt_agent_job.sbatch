#!/bin/bash
#SBATCH -p cpu
#SBATCH --cpus-per-task=32
#SBATCH --mem=120G
#SBATCH --time=04:00:00
#SBATCH -J mynewt-agent-x86
# Run qwen38-nvfp4 (mini-swe-agent, harness/run.py) on Mynewt Apptainer
# instances on an x86 node, then grade each patch. The model is served by a
# scripts/serve_triage_model.sh job on a Spark node; this job reaches it through
# an SSH tunnel to that node's 127.0.0.1:8000.
#
#   sbatch scripts/mynewt_agent_job.sbatch <server node> <code dir> <out dir> <ids...>
#
# Instance images are built into $EMBEDEVAL_WORK (default <out dir>/work) the
# first time and reused. Per instance, output is <out dir>/logs/<id>.log and
# the grade <out dir>/grades/<id>.json; an instance with a grade is skipped on
# a rerun. The agent's patch and trajectory are in <code dir>/outputs/mynewt/.
# Edit PYENV if your Python environment (mini-swe-agent, litellm) lives elsewhere.
set -u
NODE=$1; CODE=$2; OUT=$3; shift 3
S=${EMBEDEVAL_SCRATCH:-/scratch/scholar/$USER}
PYENV=${PYENV:-$S/agent-venv-x86}
PAR=${PAR:-3}
export PATH=$PYENV/bin:/usr/bin:/bin
export EMBEDEVAL_WORK=${EMBEDEVAL_WORK:-$OUT/work}
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-$S/mynewt-apptainer/r2shared}
export EMBEDEVAL_MYNEWT_CLONE=${EMBEDEVAL_MYNEWT_CLONE:-$S/repos/mynewt-core.git}
mkdir -p "$OUT/logs" "$OUT/grades" "$EMBEDEVAL_WORK"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes \
    -N -L 8000:127.0.0.1:8000 "$NODE" &
TUNNEL=$!
cd "$CODE"

echo "[$(date +%T)] waiting for the model server on $NODE"
until curl -sf 127.0.0.1:8000/v1/models >/dev/null; do sleep 20; done
echo "[$(date +%T)] server ready"

one() {
    id=$1; log=$OUT/logs/$id.log
    [ -f "$OUT/grades/$id.json" ] && { echo "[$(date +%T)] $id: already graded"; return; }
    {
        echo "== build"
        [ -e "$EMBEDEVAL_WORK/images/instances/$id.sif" ] ||
            timeout 40m python scripts/build_instances.py --instance "$id" || exit 1
        echo "== agent"
        timeout 90m python harness/run.py --instance "$id" --model qwen38-nvfp4 \
            --step-limit 50 --time-limit 40
        echo "== grade"
        timeout 30m python harness/evaluate_patches.py --instance "$id" \
            --model qwen38-nvfp4 --output "$OUT/grades/$id.json"
        echo "== rc=$?"
    } >"$log" 2>&1
    echo "[$(date +%T)] $id: $(grep -m1 '^== rc=' "$log")"
}
export -f one; export OUT
printf '%s\n' "$@" | xargs -P "$PAR" -I{} bash -c 'one {}'
echo "[$(date +%T)] all done"
kill "$TUNNEL" 2>/dev/null
