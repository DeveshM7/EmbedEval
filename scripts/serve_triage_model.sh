#!/bin/bash
#SBATCH -p spark-batch
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=20
#SBATCH --mem=120500M
#SBATCH --time=04:00:00
#SBATCH -J triage-server
#SBATCH -o triage-server-%j.log
#
# Serve an open-weights model for PR triage on a Scholar Spark (GB10) node.
#
#     sbatch scripts/serve_triage_model.sh qwen38      # profile from triage/models.json
#     srun --jobid=<id> --overlap -c 4 --pty bash      # shell on the same node
#     curl -s 127.0.0.1:8000/v1/models                 # ready when this answers
#     .venv/bin/python scripts/triage_prs.py run --profile qwen38 --gold
#
# Listens on 127.0.0.1 only: triage runs on the same node, so nothing is
# exposed on the cluster network. Ready takes ~10 min for qwen38 and ~16 for
# nemotron, most of it loading weights and compiling on first use.
#
# Two things differ from the model cards' `docker run`, and both are required
# on this machine, not tuning:
#
#   * No `--nv`. It injects the host's GL/Vulkan driver libraries too, and one
#     of them crashes the container's glibc 2.35 on start with
#     "dl-tls.c: 618: _dl_allocate_tls_init: Assertion `listp != NULL'".
#     Binding only the four CUDA compute libraries avoids it.
#   * --safetensors-load-strategy eager. vLLM's default memory-maps the weight
#     files, and on GB10 a GPU copy out of mmapped memory stalls for minutes
#     per tensor -- from local NVMe as much as from /scratch. Reading each
#     shard into RAM first loads a 1.5 GB model in under a second.
#
# 0.80 of GPU memory, not the cards' 0.90: the job's memory cap is 112 GiB and
# vLLM sees ~120 GiB, so 0.90 leaves nothing for the API server and client.

set -euo pipefail

PROFILE="${1:?usage: sbatch scripts/serve_triage_model.sh <profile>}"
REPO="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SCRATCH_ROOT="${EMBEDEVAL_SCRATCH:-/scratch/scholar/$USER}"
export EMBEDEVAL_MODELS="${EMBEDEVAL_MODELS:-$SCRATCH_ROOT/hf/models}"
IMAGE="${EMBEDEVAL_VLLM_IMAGE:-$SCRATCH_ROOT/images/vllm-0.27.1.sif}"
PORT="${PORT:-8000}"
# Defaults are for a whole node, with the client beside the server. To be reached
# from an x86 node (RIOT's agent), from a smaller allocation: HOST=0.0.0.0, with
# VLLM_API_KEY set, and less GPU memory, sequences and context.
HOST="${HOST:-127.0.0.1}"
# (The cluster's model settings arrive as EMBEDEVAL_GPU_MEMORY_UTILIZATION,
# EMBEDEVAL_MAX_NUM_SEQS and EMBEDEVAL_MAX_MODEL_LEN.)
GPU_MEMORY="${GPU_MEMORY:-${EMBEDEVAL_GPU_MEMORY_UTILIZATION:-0.80}}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-${EMBEDEVAL_MAX_NUM_SEQS:-4}}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-${EMBEDEVAL_MAX_MODEL_LEN:-}}"
# MAX_MODEL_LEN: the environment, else the profile's max_model_len, else 262144.
if [ "$HOST" != 127.0.0.1 ] && [ -z "${VLLM_API_KEY:-}" ]; then
    echo "HOST=$HOST needs VLLM_API_KEY: this would open the server to the whole cluster" >&2
    exit 1
fi
[ -n "${VLLM_API_KEY:-}" ] && export APPTAINERENV_VLLM_API_KEY="$VLLM_API_KEY"

# Read the profile. Printed one argument per line so JSON-bearing arguments
# (speculative configs) survive intact.
mapfile -t ARGS < <(python3 - "$REPO/triage/models.json" "$PROFILE" <<'PY'
import json, os, sys
profiles = json.load(open(sys.argv[1]))
if sys.argv[2] not in profiles or sys.argv[2].startswith("_"):
    sys.exit(f"unknown profile {sys.argv[2]!r}; have: "
             + ", ".join(k for k in profiles if not k.startswith("_")))
p, models = profiles[sys.argv[2]], os.environ["EMBEDEVAL_MODELS"]
print(f"{models}/{p['weights']}")
print(f"MAXLEN {p.get('max_model_len', 262144)}")
for k, v in p.get("env", {}).items():
    print(f"ENV {k}={str(v).replace('{models}', models)}")
for a in p["vllm_args"]:
    print(a.replace("{models}", models).replace("{repo}", os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[1])))))
PY
)
WEIGHTS="${ARGS[0]}"
PROFILE_MAXLEN="${ARGS[1]#MAXLEN }"
ARGS=("${ARGS[0]}" "${ARGS[@]:2}")
MAX_MODEL_LEN="${MAX_MODEL_LEN:-$PROFILE_MAXLEN}"
SERVE_ARGS=()
for a in "${ARGS[@]:1}"; do
    if [[ "$a" == ENV\ * ]]; then export "APPTAINERENV_${a#ENV }"; else SERVE_ARGS+=("$a"); fi
done

# The CUDA compute driver libraries, bound individually (see "No --nv" above).
BINDS=()
for lib in libcuda.so.1 libnvidia-ml.so.1 libnvidia-nvvm.so.4 libnvidia-ptxjitcompiler.so.1; do
    real=$(readlink -f "$(ldconfig -p | awk -v l="$lib" '$1 == l {print $NF; exit}')")
    [ -f "$real" ] || { echo "missing host driver library $lib" >&2; exit 1; }
    BINDS+=("$real:/opt/hostnv/$lib")
done

export APPTAINERENV_LD_LIBRARY_PATH=/opt/hostnv:/usr/local/nvidia/lib64:/usr/local/cuda/lib64
export APPTAINERENV_HF_HUB_OFFLINE=1
export APPTAINERENV_HF_HOME="$SCRATCH_ROOT/hf/.cache"
export APPTAINERENV_XDG_CACHE_HOME="$SCRATCH_ROOT/cache"
# The compile cache. vLLM writes the cache's own path into what it compiles,
# so cluster/ servers see theirs at one fixed path, /vllm-cache: then any copy
# of a cache works as well as the original.
if [ -n "${EMBEDEVAL_VLLM_CACHE:-}" ]; then
    mkdir -p "$EMBEDEVAL_VLLM_CACHE"
    BINDS+=("$EMBEDEVAL_VLLM_CACHE:/vllm-cache")
    export APPTAINERENV_VLLM_CACHE_ROOT=/vllm-cache
else
    export APPTAINERENV_VLLM_CACHE_ROOT="$SCRATCH_ROOT/cache/vllm"
    mkdir -p "$APPTAINERENV_VLLM_CACHE_ROOT"
fi
mkdir -p "$SCRATCH_ROOT/cache"

echo "profile=$PROFILE node=$(hostname) port=$PORT start=$(date +%T)"
echo "weights=$WEIGHTS"
# One model over several nodes (VLLM_NNODES > 1): pipeline parallel, one stage
# per node, over the nodes' 10 Gb/s link. Every node runs this script with its
# VLLM_NODE_RANK; rank 0 (VLLM_MASTER_ADDR) serves the API, the others run
# headless. VLLM_NCCL_IFNAME names the interface carrying the node addresses.
NNODES="${VLLM_NNODES:-1}"
if [ "$NNODES" -gt 1 ]; then
    : "${VLLM_NODE_RANK:?multi-node needs VLLM_NODE_RANK}" "${VLLM_MASTER_ADDR:?multi-node needs VLLM_MASTER_ADDR}"
    IFNAME="${VLLM_NCCL_IFNAME:-enp1s0f0np0}"
    export APPTAINERENV_NCCL_SOCKET_IFNAME="$IFNAME" APPTAINERENV_GLOO_SOCKET_IFNAME="$IFNAME"
    # NCCL over TCP sockets by default: its RDMA (RoCE) path fails in the container
    # ("unhandled system error"); VLLM_NCCL_IB=1 tries RDMA. VLLM_NCCL_DEBUG sets NCCL_DEBUG.
    [ "${VLLM_NCCL_IB:-0}" = 1 ] || export APPTAINERENV_NCCL_IB_DISABLE=1
    [ -n "${VLLM_NCCL_DEBUG:-}" ] && export APPTAINERENV_NCCL_DEBUG="$VLLM_NCCL_DEBUG"
    SERVE_ARGS+=(--pipeline-parallel-size "$NNODES" --nnodes "$NNODES" --node-rank "$VLLM_NODE_RANK"
                 --master-addr "$VLLM_MASTER_ADDR" --master-port "${VLLM_MASTER_PORT:-29501}")
    [ "$VLLM_NODE_RANK" -gt 0 ] && SERVE_ARGS+=(--headless)
    echo "multi-node: rank $VLLM_NODE_RANK of $NNODES, master $VLLM_MASTER_ADDR, interface $IFNAME"
fi
exec apptainer exec --cleanenv --bind "$(IFS=,; echo "${BINDS[*]}")" "$IMAGE" \
    vllm serve "$WEIGHTS" --served-model-name "$PROFILE" \
        --host "$HOST" --port "$PORT" \
        --safetensors-load-strategy eager \
        --gpu-memory-utilization "$GPU_MEMORY" --max-num-seqs "$MAX_NUM_SEQS" \
        --max-model-len "$MAX_MODEL_LEN" \
        "${SERVE_ARGS[@]}"
