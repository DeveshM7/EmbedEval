#!/bin/bash
#SBATCH -p spark-interactive
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem=60250M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-riot-llm
#
# RIOT's LLM stage, across the Spark nodes given (sbatch -N, RIOT_RUN):
#
#     .venv/bin/python scripts/riot_triage.py --run-dir <run folder> enrich   # first, needs gh
#     sbatch -N 2 --export=NONE,RIOT_RUN=<run folder> \
#         -o <run folder>/logs/%x-%j.out cluster/templates/riot_llm.sbatch
#
# It judges the valid instances (<run folder>/llm/prs.txt, written on the first run). Each
# node serves the model itself (scripts/riot_llm_node.sh) and takes its share of the PRs.
# Sized for spark-interactive, which caps a job at 60 GB; on spark-batch (120 GB) pass
# -p spark-batch --mem=120500M and LLM_GPU_MEMORY=0.80, LLM_MAX_MODEL_LEN=262144,
# LLM_MEM_LIMIT_GB=112 in --export.
#
# Jobs are limited to 4 hours here: the work stops at 3h50 and the job submits itself again
# while PRs are left, up to 4 jobs in all. A PR with a verdict is skipped, so nothing is
# done twice. PRs too long for the model's context are not retried. Run from the repository.
set -u
cd "${SLURM_SUBMIT_DIR:?}"
mkdir -p "$RIOT_RUN/llm" "$RIOT_RUN/logs"
cp "$0" "$RIOT_RUN/logs/riot_llm.sbatch" 2>/dev/null
LIST=$RIOT_RUN/llm/prs.txt
[ -s "$LIST" ] || python3 - "$RIOT_RUN" > "$LIST" <<'EOF'
import glob, json, sys
for f in sorted(glob.glob(sys.argv[1] + "/instances/*/validation.json")):
    v = json.load(open(f))
    if v.get("valid"):
        print(int(v["instance_id"].rsplit("-", 1)[1]))
EOF
attempt=${RIOT_ATTEMPT:-1}
echo "[$(date +%T)] job $attempt: $(wc -l < "$LIST") PRs in all, on $SLURM_JOB_NUM_NODES nodes"
timeout 13800 srun --export=ALL --kill-on-bad-exit=0 --cpu-bind=none --gres=gpu:1 \
    bash scripts/riot_llm_node.sh "$RIOT_RUN"
echo "[$(date +%T)] job $attempt ended (exit $?)"
left=0
for p in $(cat "$LIST"); do
    [ -f "$RIOT_RUN/triage/$p.json" ] && continue
    grep -qs ContextWindowExceeded "$RIOT_RUN/triage/$p.trace.json" && continue
    left=$((left + 1))
done
echo "[$(date +%T)] $left PRs left"
if [ $left -gt 0 ] && [ "$attempt" -lt 4 ]; then
    keep=$(env | grep -E "^LLM_" | paste -sd, -)        # the model settings this job was given
    env $(env | grep -E "^(SLURM|SBATCH)_" | cut -d= -f1 | sed "s/^/-u /") sbatch -N "$SLURM_JOB_NUM_NODES" \
        -p "$SLURM_JOB_PARTITION" --mem="${SLURM_MEM_PER_NODE}M" \
        --export=NONE,RIOT_RUN="$RIOT_RUN",RIOT_ATTEMPT=$((attempt + 1))${keep:+,$keep} \
        -o "$RIOT_RUN/logs/%x-%j.out" "$RIOT_RUN/logs/riot_llm.sbatch"
fi
