#!/bin/bash
#SBATCH -p {partition}
#SBATCH -N {nodes}
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task={cpus}
{gres_line}
#SBATCH --mem={mem}
#SBATCH -t {time_limit}
#SBATCH --export=NONE
#SBATCH -J {job_name}
#SBATCH -o {run}/jobs/job-%j.out

set -u
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin
export SLURM_EXPORT_ENV=ALL
cd {repo}
RUN={run}
CONFIG={config}
END=$(( $(/usr/bin/date +%s) + {limit_min} * 60 ))
M=$(( (END - $(/usr/bin/date +%s)) / 60 - {report_min} ))
echo "[$(/usr/bin/date +%T)] {phase} job $SLURM_JOB_ID on $SLURM_JOB_NODELIST ($M min)"
/usr/bin/timeout -k 2m ${{M}}m srun -N "$SLURM_JOB_NUM_NODES" -n "$SLURM_JOB_NUM_NODES" \
    --ntasks-per-node=1 -c {cpus} --kill-on-bad-exit=0 \
    /bin/bash -c 'exec /usr/bin/env -i PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin" HOME="$HOME" USER="$USER" \
        SLURM_JOB_ID="$SLURM_JOB_ID" SLURM_PROCID="$SLURM_PROCID" SLURM_NTASKS="$SLURM_NTASKS" \
        {python} cluster/node_worker.py --run "{run}" --config "{config}" --phase {phase} --minutes "'"$M"'"'
worker_rc=$?
echo "[$(/usr/bin/date +%T)] worker rc=$worker_rc"
{end_server}{python} cluster/report.py --run "$RUN"
report_rc=$?
if [ "$worker_rc" -ne 0 ]; then
    exit "$worker_rc"
fi
exit "$report_rc"
