#!/bin/bash
#SBATCH -A scholar
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem=120500M
#SBATCH -t 04:00:00
#SBATCH -J ga-pool
#SBATCH -o /scratch/scholar/zhan5136/ga/pool-%j.out
#
# A pool of model servers for the NuttX triage GA: one vLLM server per node of
# this job, on 127.0.0.1:8000 of its node, staying up until the job ends.
# Submit with the partition and node count:
#
#     sbatch -p spark-batch -N 20 scripts/ga_pool.sh
#     sbatch -p spark-interactive -N 70 scripts/ga_pool.sh
#
# Work is sent to the servers by scripts/ga_worker.py, started in this job as
# `srun --jobid=<job> --overlap ...` steps (steps do not count toward the
# account's running-job limit). Each server gets its own copy of the compile
# cache, and starts are staggered about 10 nodes a minute, so that dozens of
# servers neither write one shared cache nor read the weights all at once.
set -u
GA=/scratch/scholar/zhan5136/ga
CODE="${GA_CODE:-$GA/code}"          # a checkout holding scripts/serve_triage_model.sh and triage/models.json
if [ "${1:-}" != node ]; then
    mkdir -p "$GA/pool-$SLURM_JOB_ID"
    echo "pool $SLURM_JOB_ID: $SLURM_JOB_NUM_NODES nodes $SLURM_JOB_NODELIST, ends $(squeue -h -j "$SLURM_JOB_ID" -o %e)"
    echo "$SLURM_JOB_NODELIST" > "$GA/pool-$SLURM_JOB_ID/nodelist"
    exec srun --ntasks-per-node=1 --gres=gpu:1 --kill-on-bad-exit=0 bash "$CODE/scripts/ga_pool.sh" node
fi

NODE=$(hostname -s)
LOG="$GA/pool-$SLURM_JOB_ID/server-$NODE.log"
sleep $((SLURM_PROCID * 6))
export PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '^/apps/cent7' | paste -sd: -)"
export SLURM_SUBMIT_DIR="$CODE"
WARM="$GA/vllm-cache-warm"
OWN="/tmp/ga-vllm-cache-$SLURM_JOB_ID"
rm -rf "$OWN"; mkdir -p "$OWN"
if [ -e "$WARM/.warm" ]; then cp -a "$WARM/." "$OWN/"; echo "compile cache copied from $WARM" >> "$LOG"; fi
export EMBEDEVAL_VLLM_CACHE="$OWN"
# The first server to be ready publishes its cache for later pools.
(
    for _ in $(seq 1 180); do
        if curl -fsS --max-time 5 http://127.0.0.1:8000/v1/models > /dev/null 2>&1; then
            if [ ! -e "$WARM/.warm" ] && mkdir "$WARM.lock" 2>/dev/null; then
                mkdir -p "$WARM" && cp -a "$OWN/." "$WARM/" && touch "$WARM/.warm"
                rmdir "$WARM.lock"
            fi
            echo "ready $(date +%T)" > "$GA/pool-$SLURM_JOB_ID/ready-$NODE"
            break
        fi
        sleep 10
    done
) &
exec bash "$CODE/scripts/serve_triage_model.sh" qwen38-nvfp4 >> "$LOG" 2>&1
