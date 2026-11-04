#!/bin/bash
#SBATCH -p {partition}
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 16
#SBATCH --mem=64G
#SBATCH -t 02:00:00
#SBATCH --export=NONE
#SBATCH -J embedeval-riot-setup
#SBATCH -o {riot}/setup/job-%j.out
#
# RIOT's x86 setup, written by cluster/setup_riot.py at {stamp}. --export=NONE:
# the login environment points at ARM64 tools, and caps what nproc reports.

set -eu
cd {repo}
RIOT={riot}
export UV_CACHE_DIR=$RIOT/cache/uv UV_PYTHON_INSTALL_DIR=$RIOT/python
log() {{ echo "[$(date +%T)] $*"; }}
log "job $SLURM_JOB_ID on $(hostname), $(uname -m)"

# uv brings the same Python as the Spark side; the node's own is 3.9
if [ ! -x "$RIOT/venv/bin/python" ]; then
    log "x86 Python environment"
    /usr/bin/python3 -m venv "$RIOT/bootstrap"
    "$RIOT/bootstrap/bin/pip" install -q uv==0.5.11
    "$RIOT/bootstrap/bin/uv" python install {python}
    "$RIOT/bootstrap/bin/uv" venv --python {python} "$RIOT/venv"
    "$RIOT/bootstrap/bin/uv" pip install -q --python "$RIOT/venv/bin/python" -r requirements.txt
fi

"$RIOT/venv/bin/python" cluster/setup_riot.py --on-node --config {config}
log "done"
