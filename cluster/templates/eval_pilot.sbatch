#!/bin/bash
#SBATCH -p spark-batch
#SBATCH -N 5
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem-per-cpu=6025M
#SBATCH -t 04:00:00
#SBATCH -J embedeval-pilot
#SBATCH hetjob
#SBATCH -p cpu
#SBATCH -N 1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem-per-cpu=3900M
#SBATCH -t 04:00:00
#
# Model pilots (docs/model_pilots.md): several models side by side in one heterogeneous job
# (one of the account's two jobs). Spark part: one server per model, over the nodes the plan
# gives it, its Zephyr workers on its first node; cpu part: every model's RIOT workers, each
# against its own server. EVAL_PILOT_PLAN: "<model> <nodes> <zephyr workers> <riot workers>"
# per line, in node order (nodes in all = -N of the Spark part). A model's workers in all are
# its server's sequences. EVAL_ZEPHYR_INSTANCES / EVAL_RIOT_INSTANCES: files of instance ids.
# Results: EMBEDEVAL_EVAL_DIR/<model>/<project>/<PR>/. Submit from the repository.
set -u
cd "${SLURM_SUBMIT_DIR:?}"
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared}
export EMBEDEVAL_EVAL_DIR=${EMBEDEVAL_EVAL_DIR:?set EMBEDEVAL_EVAL_DIR (the pilot results)}
export EMBEDEVAL_PILOT_WORK=${EMBEDEVAL_PILOT_WORK:-/scratch/scholar/$USER/embedeval/eval-work/pilot-$SLURM_JOB_ID}
export EVAL_LOGS=$EMBEDEVAL_EVAL_DIR/logs/job-$SLURM_JOB_ID EVAL_SERVERS=$EMBEDEVAL_EVAL_DIR/logs/job-$SLURM_JOB_ID/servers
mkdir -p "$EVAL_SERVERS" && chmod 700 "$EVAL_SERVERS"
( umask 077; openssl rand -hex 32 > "$EVAL_SERVERS/key" )
cp "$0" "$EVAL_LOGS/eval_pilot.sbatch" 2>/dev/null
echo "$EVAL_PILOT_PLAN" > "$EVAL_LOGS/plan.txt"
END=$(( $(date -d "$(squeue -h -j "$SLURM_JOB_ID" -o %e | head -1)" +%s) - 300 ))
[ "$END" -gt "$(date +%s)" ] || END=$(( $(date +%s) + 4 * 3600 - 300 ))
N0=${SLURM_JOB_NUM_NODES_HET_GROUP_0:-1}
echo "[$(date +%T)] pilot job $SLURM_JOB_ID: $N0 Spark nodes (${SLURM_JOB_NODELIST_HET_GROUP_0:-?}), cpu ${SLURM_JOB_NODELIST_HET_GROUP_1:-?}"
echo "$EVAL_PILOT_PLAN" | sed 's/^/  plan: /'
srun --het-group=0 -N "$N0" -n "$N0" --ntasks-per-node=1 --export=ALL --kill-on-bad-exit=0 --cpu-bind=none --gres=gpu:1 \
    bash scripts/eval_pilot_server.sh "$END" > "$EVAL_LOGS/servers.log" 2>&1 &
SERVERS=$!
timeout -s TERM -k 1m $(( END + 120 - $(date +%s) )) \
    srun --het-group=1 -N 1 -n 1 --ntasks-per-node=1 --export=ALL --kill-on-bad-exit=0 --cpu-bind=none \
    bash scripts/eval_pilot_riot.sh "$END" > "$EVAL_LOGS/riot.log" 2>&1
echo "[$(date +%T)] riot part ended ($?)"
wait $SERVERS
echo "[$(date +%T)] pilot job ended"
