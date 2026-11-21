#!/bin/bash
#SBATCH -p spark-batch
#SBATCH -N 1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem-per-cpu=6025M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-eval-mynewt
#SBATCH hetjob
#SBATCH -p cpu
#SBATCH -N 1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem-per-cpu=3900M
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#
# Evaluate one model on the Mynewt instances. Mynewt's instance images run on
# x86 only (every instance names host_arch x86_64: old mynewt-core trees build
# -m32 binaries) and the GPUs are on the (arm64) Spark nodes, so this is one
# heterogeneous job, which counts as one job against the account's limit:
#
#   component 0, spark-batch: the model servers, each on the profile's `nodes`
#                Spark nodes (triage/models.json; 1 if absent), on the cluster
#                network behind an API key made for this job (scripts/eval_mynewt_server.sh)
#   component 1, cpu:         the agents and their containers, EVAL_AGENTS per
#                server, 4 cores each (scripts/eval_mynewt_agents.sh)
#
#     sbatch --export=NONE,EVAL_MODEL=qwen38-nvfp4 -o $SCRATCH/embedeval/eval/logs/%x-%j.out \
#         cluster/templates/eval_mynewt.sbatch
#
# Sizes on the command line, one part per component, separated by ":" (the
# defaults: 1 Spark node, 1 cpu node with 16 cores = 4 agents):
#
#     sbatch --export=NONE,EVAL_MODEL=... -N 3 : -N 1 -c 48 --export=NONE,EVAL_MODEL=... \
#         -o ... cluster/templates/eval_mynewt.sbatch
#
# -N of the Spark part is the profile's nodes x servers (else the job stops at
# once and is not resubmitted). The four evaluation models, with the agents per
# server (EVAL_AGENTS, also the server's sequences) that suit them: MiniMax M2.5
# (minimax-m25-nvfp4, 2 nodes) 2, MiniMax M2 (minimax-m2, 3 nodes) 2, GLM 4.6
# (glm-46-nvfp4, 3 nodes) 3, gpt-oss-120b (1 node) 3. Two gpt-oss servers, one
# GLM server (4 cores per agent on the cpu side):
#
#     sbatch --export=NONE,EVAL_MODEL=gpt-oss-120b,EVAL_AGENTS=3 -N 2 : -N 1 -c 24 \
#         --export=NONE,EVAL_MODEL=gpt-oss-120b,EVAL_AGENTS=3 -o ... cluster/templates/eval_mynewt.sbatch
#     sbatch --export=NONE,EVAL_MODEL=glm-46-nvfp4,EVAL_AGENTS=3 -N 3 : -N 1 -c 12 \
#         --export=NONE,EVAL_MODEL=glm-46-nvfp4,EVAL_AGENTS=3 -o ... cluster/templates/eval_mynewt.sbatch
#
# Give each cpu node 4 x EVAL_AGENTS x (servers it takes) cores; server k goes
# to cpu node k mod (cpu nodes). Settings, in --export (repeat them after ":"):
# EVAL_MODEL, EVAL_AGENTS (4), EVAL_GPU_MEMORY (0.80: the Spark node runs only
# the model), EVAL_MAX_MODEL_LEN (the profile's), EVAL_SERVER_WAIT_MIN (minutes
# a server may take to start, 40), EVAL_INSTANCES (a file of ids, for a trial),
# EVAL_KEEP_IMAGES, EVAL_MAX_JOBS (6), EVAL_MARGIN (minutes before the end after
# which no new PR is taken, 60), EVAL_TIME_LIMIT (minutes per run, 120),
# EMBEDEVAL_EVAL_DIR (results elsewhere, for a test), EMBEDEVAL_MYNEWT_CLONE
# (a local mynewt-core clone for the image builds; else GitHub).
# A shorter job: -t on both components, also kept on resubmission; a -p or -x for the Spark nodes too.
#
# Before any agent starts, the cpu side sends each server one request, checks
# that a request without the key is refused and that an agent container cannot
# reach the server; if any check fails, the job stops with the reason. The
# rest works as cluster/templates/eval_zephyr.sbatch: one shared PR queue, no
# new PR in the last EVAL_MARGIN minutes, and the job submits itself again while PRs
# are left. Submit from the repository.
set -u
cd "${SLURM_SUBMIT_DIR:?}"
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
export EMBEDEVAL_EVAL_DIR=${EMBEDEVAL_EVAL_DIR:-/scratch/scholar/$USER/embedeval/eval}
MODEL=${EVAL_MODEL:?EVAL_MODEL not set}
ATTEMPT=${EVAL_ATTEMPT:-1}
EVAL=$(.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness'); import paths
print(paths.OUTPUTS_DIR / paths.model_dir(sys.argv[1]))" "$MODEL")
export EVAL_LOGS=$EVAL/logs/mynewt/job-$SLURM_JOB_ID
# The servers' addresses and this job's API key, readable by this user only.
export EVAL_SERVERS=$EVAL_LOGS/servers
mkdir -p "$EVAL_SERVERS" && chmod 700 "$EVAL_SERVERS"
# A requeued job keeps its ID and folder: clear what the earlier attempt left (dead servers' addresses, its stop mark).
rm -f "$EVAL_SERVERS"/node*.url "$EVAL_SERVERS"/node*.failed "$EVAL_LOGS/checks-failed"
( umask 077; openssl rand -hex 32 > "$EVAL_SERVERS/key" )
cp "$0" "$EVAL/logs/mynewt/eval_mynewt.sbatch" 2>/dev/null
# The node scripts run from this job's own copies: bash reads a script as it runs
# it, so editing the repository's copy during a job would break the job.
cp scripts/eval_mynewt_server.sh scripts/eval_mynewt_agents.sh "$EVAL_LOGS/"
# Stop 5 min before the job's own end (its -t, 4 hours at most here).
LIMIT=$(squeue -h -j "$SLURM_JOB_ID" -o %l | head -1)
END=$(( $(date -d "$(squeue -h -j "$SLURM_JOB_ID" -o %e | head -1)" +%s) - 300 ))
[ "$END" -gt "$(date +%s)" ] || END=$(( $(date +%s) + 4 * 3600 - 300 ))
N0=${SLURM_JOB_NUM_NODES_HET_GROUP_0:-1} N1=${SLURM_JOB_NUM_NODES_HET_GROUP_1:-1}
C1=${SLURM_CPUS_PER_TASK_HET_GROUP_1:-16}
# Spark nodes per server: the profile's nodes (triage/models.json; 1 if absent).
K=$(.venv/bin/python -c "
import json, sys; sys.path.insert(0, 'harness'); import paths, run
print(json.load(open('triage/models.json'))[run.LOCAL_MODELS[paths.resolve_model(sys.argv[1])]].get('nodes', 1))" "$MODEL")
if ! [[ "$K" =~ ^[1-9][0-9]*$ ]] || [ $((N0 % K)) != 0 ]; then
    echo "$N0 Spark nodes do not make whole servers of $MODEL (${K:-no profile} nodes each)" > "$EVAL_LOGS/checks-failed"
    echo "[$(date +%T)] not started, not resubmitted: $(cat "$EVAL_LOGS/checks-failed")"
    exit 1
fi
NSERVERS=$((N0 / K))
echo "[$(date +%T)] job $ATTEMPT for $MODEL: $NSERVERS servers of $K nodes (${SLURM_JOB_NODELIST_HET_GROUP_0:-?}),"\
     "$N1 cpu nodes x $C1 cores (${SLURM_JOB_NODELIST_HET_GROUP_1:-?})"
echo "[$(date +%T)] results in $EVAL/mynewt, logs in $EVAL_LOGS"

srun --het-group=0 -N "$N0" -n "$N0" --ntasks-per-node=1 --export=ALL --kill-on-bad-exit=0 --cpu-bind=none --gres=gpu:1 \
    bash "$EVAL_LOGS/eval_mynewt_server.sh" "$MODEL" > "$EVAL_LOGS/servers.log" 2>&1 &
SERVERS=$!
# The workers stop themselves at END, saving the runs they are on (srun, if
# signalled, kills its tasks outright); this timeout is only a backstop.
timeout -s TERM -k 1m $(( END + 120 - $(date +%s) )) \
    srun --het-group=1 -N "$N1" -n "$N1" --ntasks-per-node=1 --export=ALL --kill-on-bad-exit=0 --cpu-bind=none \
    bash "$EVAL_LOGS/eval_mynewt_agents.sh" "$MODEL" "$END" "$NSERVERS"
rc=$?
echo "[$(date +%T)] agents ended (exit $rc)"
# If the agents' step did not even start (no worker logs), a new job would fail the same way.
if [ "$rc" != 0 ] && ! ls "$EVAL_LOGS"/cpu*-s*-w*.log >/dev/null 2>&1 && [ ! -f "$EVAL_LOGS/checks-failed" ]; then
    echo "the agents' step failed (exit $rc) before any worker started" > "$EVAL_LOGS/checks-failed"
fi
kill -TERM $SERVERS 2>/dev/null; wait $SERVERS 2>/dev/null
echo "[$(date +%T)] job $ATTEMPT ended"

# Instances of the other architecture are not taken by this job: the summary names them.
LEFT=$(.venv/bin/python harness/eval_set.py left --project mynewt --model "$MODEL" --arch x86_64 \
    ${EVAL_INSTANCES:+--instances "$EVAL_INSTANCES"})
echo "[$(date +%T)] $LEFT PRs not graded yet ($(.venv/bin/python harness/eval_set.py summary --project mynewt --model "$MODEL" --arch x86_64 \
    ${EVAL_INSTANCES:+--instances "$EVAL_INSTANCES"}))"
# Stop for good if the checks before the agents failed: a new job would fail the same way.
if [ -f "$EVAL_LOGS/checks-failed" ]; then
    echo "[$(date +%T)] not resubmitted: $(cat "$EVAL_LOGS/checks-failed")"
elif [ "$LEFT" -gt 0 ] && [ "$ATTEMPT" -lt "${EVAL_MAX_JOBS:-6}" ]; then
    keep=$(env | grep -E "^(EVAL_(AGENTS|GPU_MEMORY|MAX_MODEL_LEN|SERVER_WAIT_MIN|INSTANCES|KEEP_IMAGES|MAX_JOBS|MARGIN|TIME_LIMIT)|EMBEDEVAL_EVAL_DIR|EMBEDEVAL_MYNEWT_CLONE)=" | paste -sd, -)
    exp=NONE,EVAL_MODEL="$MODEL",EVAL_ATTEMPT=$((ATTEMPT + 1))${keep:+,$keep}
    XN=$(squeue -h -j "$SLURM_JOB_ID" -o "%i %x" | awk '$1 ~ /\+0$/ {print $2}')
    env $(env | grep -E "^(SLURM|SBATCH)_" | cut -d= -f1 | sed "s/^/-u /") sbatch \
        -N "$N0" -p "${SLURM_JOB_PARTITION_HET_GROUP_0:-$SLURM_JOB_PARTITION}" ${XN:+-x "$XN"} -t "$LIMIT" --export="$exp" -o "$EVAL/logs/mynewt/%x-%j.out" \
        : -N "$N1" -c "$C1" -t "$LIMIT" --export="$exp" "$EVAL/logs/mynewt/eval_mynewt.sbatch"
fi
