#!/bin/bash
#SBATCH -p cpu
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH -t 04:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-riot
#
# RIOT's x86 stages, across the nodes given (sbatch -N, RIOT_RUN, RIOT_STAGES):
#
#     sbatch -N 6 --export=NONE,RIOT_RUN=<run folder>,RIOT_STAGES="select check" \
#         -o <run folder>/logs/%x-%j.out cluster/templates/riot_x86.sbatch
#
# Stages: select, check, instance. Each node works on its share of the PRs, in its own
# work folder. A stage can be run again: what has a record is skipped, so a job that ran
# out of time just continues. A stage that fails starts the job again, up to 3 times in all,
# from the copy of this file in <run folder>/logs. Run from the repository, one at a time per
# run folder.
set -u
cd "${SLURM_SUBMIT_DIR:?}"
cp "$0" "$RIOT_RUN/logs/riot_x86.sbatch" 2>/dev/null
# Submitted from a desktop job, its settings arrive here and srun refuses them.
for v in ${!SLURM_TRES_@} ${!SLURM_GPU@} ${!SLURM_GRES@} ${!SBATCH_GRES@} ${!SBATCH_GPU@} SLURM_CPUS_PER_TASK; do unset "$v"; done
export EMBEDEVAL_SHARED=${EMBEDEVAL_SHARED:-/scratch/scholar/$USER/embedeval/shared} EMBEDEVAL_PROJECT=riot
PY=$EMBEDEVAL_SHARED/riot/venv/bin/python
for stage in $RIOT_STAGES; do
    echo "[$(date +%T)] $stage on $SLURM_JOB_NUM_NODES nodes"
    srun --export=ALL --ntasks-per-node=1 --kill-on-bad-exit=0 --cpu-bind=none bash -c '
        echo "$(hostname): $(nproc) cores"
        export EMBEDEVAL_WORK=$RIOT_RUN/nodes/node$SLURM_NODEID/work
        share=$SLURM_NODEID/$SLURM_NNODES; slots=$((SLURM_CPUS_ON_NODE / 8))
        case '"$stage"' in
            select)   '"$PY"' scripts/riot_select.py --run-dir "$RIOT_RUN" --shard $share --parallel $((slots * 2)) ;;
            check)    '"$PY"' scripts/riot_check.py --run-dir "$RIOT_RUN" --shard $share --parallel $slots ;;
            instance) '"$PY"' scripts/riot_instance.py --run-dir "$RIOT_RUN" --shard $share --parallel $slots ;;
        esac'
    rc=$?
    echo "[$(date +%T)] $stage finished (exit $rc)"
    if [ $rc -ne 0 ]; then         # the next stage needs this one finished: the job starts again
        attempt=${RIOT_ATTEMPT:-1}
        if [ "$attempt" -lt 3 ]; then
            env $(env | grep -E "^(SLURM|SBATCH)_" | cut -d= -f1 | sed "s/^/-u /") sbatch -N "$SLURM_JOB_NUM_NODES" \
                --export=NONE,RIOT_RUN="$RIOT_RUN",RIOT_STAGES="$RIOT_STAGES",RIOT_ATTEMPT=$((attempt + 1)) \
                -o "$RIOT_RUN/logs/%x-%j.out" "$RIOT_RUN/logs/riot_x86.sbatch"
        fi
        exit $rc
    fi
done
