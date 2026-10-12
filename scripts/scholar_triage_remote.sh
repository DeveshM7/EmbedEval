#!/bin/bash
# Drive NuttX triage on Scholar's Spark nodes from your own machine over SSH.
#
#     scripts/scholar_triage_remote.sh serve            # submit a model-server job
#     scripts/scholar_triage_remote.sh status           # your jobs, and which servers answer
#     scripts/scholar_triage_remote.sh sync             # copy prompt + triage script up
#     scripts/scholar_triage_remote.sh run <job> [args] # start a run in server <job> (default --gold)
#     scripts/scholar_triage_remote.sh wait             # block until no run is active
#     scripts/scholar_triage_remote.sh pull             # copy triage/nuttx_runs back
#
# Needs key-based SSH to Scholar (see docs/spark_triage_setup.md, "Driving it
# from your laptop") and the one-time setup there done on a Spark node.
#
# Two things here are not obvious:
#   * sbatch and srun copy the submitting shell's environment. A login-node
#     PATH can hold x86_64 tools (e.g. a `conda init` block from another
#     course), which fail on the aarch64 Spark nodes with "Exec format error".
#     Every remote command first drops /apps/cent7 entries from PATH.
#   * Scholar has several login nodes and each ssh may land on another, so
#     "is a run active" is asked of Slurm (job steps), not pgrep.
set -u

HOST="${SCHOLAR_HOST:-scholar.rcac.purdue.edu}"
REMOTE_REPO="${SCHOLAR_REPO:-EmbedEval}"          # relative to the remote home
PROFILE="${TRIAGE_PROFILE:-qwen38-nvfp4}"
LOCAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=30 "$HOST")
CLEAN='export PATH=$(printf "%s" "$PATH" | tr ":" "\n" | grep -v "^/apps/cent7" | paste -sd: -); unset CONDA_EXE CONDA_PREFIX CONDA_PYTHON_EXE CONDA_SHLVL CONDA_DEFAULT_ENV;'

# Scholar prints a login banner and the shell profile may print warnings.
quiet() { grep -v '^\*\*\*\|^$\|BoilerKey\|module: command not found'; }
remote() { "${SSH[@]}" "$CLEAN $1" 2>&1 | quiet; }
copy() { rsync -a -e "ssh -o BatchMode=yes" "$@" 2>&1 | quiet; return "${PIPESTATUS[0]}"; }

case "${1:-}" in
  serve)
    remote "cd ~/$REMOTE_REPO && sbatch -A scholar scripts/serve_triage_model.sh $PROFILE" ;;
  status)
    remote "squeue -u \$USER -o '%.10i %.15j %.3t %.10M %.10l %R'
            for j in \$(squeue -u \$USER -h -n triage-server -t R -o %i); do
              printf 'server %s: ' \$j
              srun --jobid=\$j --overlap -n1 curl -s -m 5 127.0.0.1:8000/v1/models >/dev/null && echo ready || echo loading
            done" ;;
  sync)
    for f in docs/nuttx_pr_triage.md scripts/triage_nuttx_prs.py; do
      copy "$LOCAL_REPO/$f" "$HOST:$REMOTE_REPO/$f" || exit 1
    done
    remote "sha256sum ~/$REMOTE_REPO/docs/nuttx_pr_triage.md | cut -c1-16"
    shasum -a 256 "$LOCAL_REPO/docs/nuttx_pr_triage.md" | cut -c1-16 ;;
  run)
    job="${2:?usage: run <server job id> [triage args]}"; shift 2
    args="${*:---gold}"
    # bash -c plus disown detaches the srun step, so ssh returns at once and
    # the run survives the connection closing.
    # `cd || exit;` not `cd &&`: with &&, the trailing & would background the
    # whole list in a subshell that keeps the ssh session open.
    remote "cd ~/$REMOTE_REPO || exit 1; nohup setsid bash -c 'srun --jobid=$job --overlap -n1 -c 4 .venv/bin/python scripts/triage_nuttx_prs.py run --profile $PROFILE $args' > triage_run_$job.log 2>&1 < /dev/null & disown; echo 'started; log: ~/$REMOTE_REPO/triage_run_$job.log'" ;;
  wait)
    # Ask Slurm, not pgrep: each ssh may land on a different login node.
    # Triage runs are srun steps named after their executable, python.
    while "${SSH[@]}" 'squeue -s -h -u $USER -o %j | grep -qx python' 2>/dev/null; do
      sleep 60
    done
    remote "for f in ~/$REMOTE_REPO/triage_run_*.log; do echo \"== \$f\"; tail -22 \"\$f\"; done" ;;
  pull)
    copy "$HOST:$REMOTE_REPO/triage/nuttx_runs/" "$LOCAL_REPO/triage/nuttx_runs/" || exit 1
    ls -t "$LOCAL_REPO/triage/nuttx_runs" | head -3 ;;
  *)
    sed -n '2,10p' "$0"; exit 2 ;;
esac
