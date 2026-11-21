#!/bin/bash
#SBATCH -p spark-batch
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem-per-cpu=6025M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-eval-riot-multi
# A failed node must not end the whole job (Slurm's default: 491147, 2026-11-16): keep going.
#SBATCH --no-kill
#SBATCH hetjob
#SBATCH -p cpu
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem-per-cpu=3900M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH --no-kill
#
# Evaluate a model on the RIOT instances, with the model served on GB10 nodes and the agents
# on x86 cpu nodes (RIOT's images are x86 only), as one heterogeneous job (one of the
# account's two jobs):
#
#   part 0, GB10 (spark-batch by default; or -p spark-interactive --mem-per-cpu=6000M
#           --cpus-per-task=10, where a job gets at most 60 GB of a node): the job's nodes form
#           groups of EVAL_NODES_PER_SERVER (the profile's "nodes" by default), each serving
#           the model once on the cluster network behind a key made for this job, with no
#           agents on them (scripts/eval_zephyr_group.sh, EVAL_SERVE_ONLY=1); one srun step
#           per group, the starts staggered by EVAL_STAGGER_SEC.
#   part 1, cpu: the agents (scripts/eval_riot_remote.sh): up to EVAL_AGENTS per server
#           (fewer if a server reports less room), 4 cores each; group k goes to cpu node
#           k mod (cpu nodes). Checks and a watchdog per server, as described there.
#
#     sbatch -N 6 --export=NONE,EVAL_MODEL=gpt-oss-120b,EVAL_AGENTS=3,EVAL_INSTANCES=$PWD/eval-sets/riot.txt \
#         : -N 2 -c 48 --export=NONE,EVAL_MODEL=gpt-oss-120b,EVAL_AGENTS=3,EVAL_INSTANCES=$PWD/eval-sets/riot.txt \
#         -o $SCRATCH/embedeval/eval/logs/%x-%j.out cluster/templates/eval_riot_multi.sbatch
#
# Give each cpu node 4 x EVAL_AGENTS x (groups it takes) cores. Settings, in --export (repeat
# them after ":"): EVAL_MODEL, EVAL_AGENTS (3), EVAL_NODES_PER_SERVER, EVAL_GPU_MEMORY (0.80;
# ~0.38-0.42 on spark-interactive), EVAL_MAX_MODEL_LEN, EVAL_INSTANCES, EVAL_KEEP_IMAGES,
# EVAL_MARGIN (60), EVAL_TIME_LIMIT (120), EVAL_STAGGER_SEC (60), EVAL_SERVER_WAIT_MIN (90),
# EVAL_ROOM_PER_AGENT (0.73), EVAL_HANG_MIN (10), EMBEDEVAL_EVAL_DIR.
#
# The job does not submit itself again: it ends when its agents are done, or 5 minutes
# before its time limit, and prints how many PRs are left. Submit from the repository.
set -u
cd "${SLURM_SUBMIT_DIR:?}"
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
export EMBEDEVAL_EVAL_DIR=${EMBEDEVAL_EVAL_DIR:-/scratch/scholar/$USER/embedeval/eval}
MODEL=${EVAL_MODEL:?EVAL_MODEL not set}
EVAL=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths
print(paths.OUTPUTS_DIR / paths.model_dir(sys.argv[1]))" "$MODEL")
# EVAL_PROJECT: the project whose instances are run (riot; also nuttx)
PROJECT=${EVAL_PROJECT:-riot}; export EVAL_PROJECT=$PROJECT
export EVAL_LOGS=$EVAL/logs/$PROJECT/job-$SLURM_JOB_ID
# The servers' addresses and this job's API key, readable by this user only.
export EVAL_SERVERS=$EVAL_LOGS/servers
mkdir -p "$EVAL_SERVERS" && chmod 700 "$EVAL_SERVERS"
( umask 077; openssl rand -hex 32 > "$EVAL_SERVERS/key" )
cp "$0" "$EVAL/logs/$PROJECT/eval_riot_multi.sbatch" 2>/dev/null
# The node scripts run from this job's own copies: bash reads a script as it runs
# it, so editing the repository's copy during a job would break the job.
cp scripts/eval_zephyr_group.sh scripts/eval_riot_remote.sh "$EVAL_LOGS/"
# Stop 5 min before the job's own end (its -t, 4 hours at most here).
END=$(( $(date -d "$(squeue -h -j "$SLURM_JOB_ID" -o %e | head -1)" +%s) - 300 ))
[ "$END" -gt "$(date +%s)" ] || END=$(( $(date +%s) + 4 * 3600 - 300 ))
LS=${SLURM_JOB_NODELIST_HET_GROUP_0:?not a heterogeneous job}; NC=${SLURM_JOB_NUM_NODES_HET_GROUP_1:?}
NN=${EVAL_NODES_PER_SERVER:-$(.venv/bin/python -c "
import json, sys; sys.path.insert(0, 'harness'); import paths, run
print(json.load(open('triage/models.json'))[run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])]].get('nodes', 1))" "$MODEL")}
mapfile -t HOSTS < <(scontrol show hostnames "$LS")
NG=$(( ${#HOSTS[@]} / NN ))
[ "$NG" -gt 0 ] || { echo "[$(date +%T)] ${#HOSTS[@]} GB10 nodes, $NN per server: no server"; exit 1; }
GROUPS_LIST=(); for (( g = 0; g < NG; g++ )); do GROUPS_LIST+=(sg$g); done   # the group script names them <prefix>g<G>
echo "[$(date +%T)] job for $MODEL: $NG servers of $NN node(s) on ${SLURM_JOB_PARTITION_HET_GROUP_0:-?} ($LS),"\
     "agents on $NC cpu nodes (${SLURM_JOB_NODELIST_HET_GROUP_1:-?})"
echo "[$(date +%T)] results in $EVAL/$PROJECT, logs in $EVAL_LOGS"

# The servers: one srun step per group, so a failed node aborts its own group only.
T=$(( END + 120 - $(date +%s) ))
STEPS=()
for (( g = 0; g < NG; g++ )); do
    W=$(IFS=,; echo "${HOSTS[*]:$(( g * NN )):$NN}")
    EVAL_NODELIST="$LS" EVAL_GROUP_PREFIX=s EVAL_NODES_PER_SERVER=$NN EVAL_SERVE_ONLY=1 \
    EVAL_GPU_MEMORY=${EVAL_GPU_MEMORY:-0.80} EVAL_AGENTS=${EVAL_AGENTS:-3} \
    timeout -s TERM -k 1m $T srun --het-group=0 -N "$NN" -n "$NN" -w "$W" --ntasks-per-node=1 \
        --export=ALL --kill-on-bad-exit=0 --cpu-bind=none --gres=gpu:1 \
        bash "$EVAL_LOGS/eval_zephyr_group.sh" "$MODEL" "$END" >> "$EVAL_LOGS/servers.log" 2>&1 &
    STEPS+=($!)
done
# The agents. The workers stop themselves at END, saving the runs they are on (srun, if
# signalled, kills its tasks outright); this timeout is only a backstop.
timeout -s TERM -k 1m $T srun --het-group=1 -N "$NC" -n "$NC" --ntasks-per-node=1 --export=ALL \
    --kill-on-bad-exit=0 --cpu-bind=none \
    bash "$EVAL_LOGS/eval_riot_remote.sh" "$MODEL" "$END" "${GROUPS_LIST[@]}"
echo "[$(date +%T)] agents ended (exit $?)"
touch "$EVAL_SERVERS/agents.done"          # the servers stop within a minute
wait "${STEPS[@]}"
echo "[$(date +%T)] job ended"
[ -f "$EVAL_LOGS/checks-failed" ] && echo "[$(date +%T)] checks failed: $(cat "$EVAL_LOGS/checks-failed")"
LEFT=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths
from pathlib import Path
f = sys.argv[2]
ids = Path(f).read_text().split() if f else paths.discover_instances(sys.argv[3])
print(sum(not paths.grade_path(i, sys.argv[1]).exists() for i in ids))" "$MODEL" "${EVAL_INSTANCES:-}" "$PROJECT")
echo "[$(date +%T)] $LEFT PRs not graded yet (no resubmission: start a follow-up job if needed)"
