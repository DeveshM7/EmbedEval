#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#14802 — "segger/stream_rtt: fix warning after stream update"
#
# Validation logic:
#   FAIL: PR #14778 updated the stream interface to use ssize_t/size_t for puts/gets
#         function pointers. stream_rtt.c still uses int, causing a type mismatch.
#         Whether this is a compile error or warning-only depends on the build flags.
#   PASS: after applying the fix, types match the updated stream interface.
#
# No companion apps PR. sim:segger target (native sim, no cross-compiler needed).
#
# Usage: ./scripts/run_14802.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-14802"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-14802"

KERNEL_BASE_COMMIT="71b169e5fd8ab3dd1eed440645e0dd518fe032a1"
KERNEL_MERGE_COMMIT="f1e1aab3b735fba7173a48102adfd5bf4ad617eb"
APPS_COMMIT="3c4ddd2802a189fccc802230ab946d50a97cb93c"

IMAGE="nuttx-embedbench:nuttx-14802"
BASE_IMAGE="nuttx-embedbench-base:latest"

WORK_DIR=""
CID=""

cleanup() {
    if [ -n "${CID}" ]; then
        echo "Stopping container..."
        docker stop "${CID}" >/dev/null 2>&1 && docker rm "${CID}" >/dev/null 2>&1 || true
    fi
    if [ -n "${WORK_DIR}" ] && [ -d "${WORK_DIR}" ]; then
        rm -rf "${WORK_DIR}"
    fi
}
trap cleanup EXIT

mkdir -p "${RESULTS_DIR}"
mkdir -p "${INSTANCE_DIR}"
WORK_DIR="$(mktemp -d)"

# ── Step 1: Generate fix_patch.diff ──────────────────────────────────────────
echo "=== Step 1: Generating fix_patch.diff ==="
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx.git \
    "${WORK_DIR}/nuttx-kernel" -q
cd "${WORK_DIR}/nuttx-kernel"
git fetch origin "${KERNEL_MERGE_COMMIT}" -q
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" \
    > "${WORK_DIR}/fix_patch.diff"
echo "fix_patch.diff: $(wc -l < "${WORK_DIR}/fix_patch.diff") lines"
cd "${REPO_ROOT}"

# ── Step 2: Write Dockerfile ──────────────────────────────────────────────────
echo ""
echo "=== Step 2: Writing Dockerfile ==="
cat > "${INSTANCE_DIR}/Dockerfile" <<'DOCKERFILE'
FROM nuttx-embedbench-base:latest

ARG KERNEL_BASE_COMMIT
ARG APPS_COMMIT

WORKDIR /testbed

# Clone kernel at base commit (stream_rtt.c uses int instead of ssize_t/size_t)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at compatible commit
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_COMMIT}

# Configure sim:segger
RUN ./tools/configure.sh -a ./apps sim:segger \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# Pre-build — may fail with compile error or succeed with warnings
RUN make -j$(nproc) 2>&1 || true
DOCKERFILE
echo "Dockerfile written."

# ── Step 3: Build base image ──────────────────────────────────────────────────
echo ""
echo "=== Step 3: Base image ==="
if docker image inspect "${BASE_IMAGE}" &>/dev/null; then
    echo "Already exists, skipping."
else
    echo "Building ${BASE_IMAGE} ..."
    docker build \
        -f "${REPO_ROOT}/docker/bases/nuttx.Dockerfile" \
        -t "${BASE_IMAGE}" \
        "${REPO_ROOT}/docker/bases/"
fi

# ── Step 4: Build instance image ─────────────────────────────────────────────
echo ""
echo "=== Step 4: Instance image ==="
if docker image inspect "${IMAGE}" &>/dev/null; then
    echo "Already exists, skipping."
else
    echo "Building ..."
    docker build \
        --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
        --build-arg APPS_COMMIT="${APPS_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 5a: Verify FAIL on base commit ──────────────────────────────────────
echo ""
echo "=== Step 5a: Verifying FAIL on base commit ==="

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_build.log" | tail -20

if echo "${FAIL_OUTPUT}" | grep -qE "error:.*stream_rtt|stream_rtt.*error:|incompatible.*pointer|error:.*rttstream"; then
    echo ""
    echo "CONFIRMED: Compile error on base commit (FAIL step validated) ✓"
    FAIL_STATUS="compile_error"
elif echo "${FAIL_OUTPUT}" | grep -qiE "^make\[.*\]: \*\*\* |^make: \*\*\* "; then
    echo ""
    echo "CONFIRMED: Build error on base commit (FAIL step validated) ✓"
    FAIL_STATUS="build_error"
elif echo "${FAIL_OUTPUT}" | grep -qE "warning:.*stream_rtt|stream_rtt.*warning|incompatible pointer type"; then
    echo ""
    echo "WARNING: Build succeeded with warnings only — weak FAIL signal"
    FAIL_STATUS="warnings_only"
else
    echo ""
    echo "WARNING: Build appears to have succeeded cleanly — check fail_build.log"
    FAIL_STATUS="unexpected_pass"
fi

# ── Step 5b: Apply fix, verify PASS ──────────────────────────────────────────
echo ""
echo "=== Step 5b: Applying fix and verifying build PASSES ==="

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

FIX_BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${FIX_BUILD_OUTPUT}" | tee "${RESULTS_DIR}/pass_build.log" | tail -10

if echo "${FIX_BUILD_OUTPUT}" | grep -qiE "^make\[.*\]: \*\*\* |^make: \*\*\* "; then
    echo ""
    echo "FAIL: Build failed after fix — check pass_build.log"
    PASS_STATUS="build_error"
else
    echo ""
    echo "Build succeeded after fix, running boot check..."

    RUN_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'uname -a\npoweroff\n' | timeout 30 ./nuttx 2>&1
    " || true)

    echo "${RUN_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -10

    if echo "${RUN_OUTPUT}" | grep -qE "NuttX|nsh>"; then
        echo ""
        echo "CONFIRMED: NuttX boots after fix ✓"
        PASS_STATUS="boot_success"
    else
        PASS_STATUS="no_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "compile_error" ] || [ "${FAIL_STATUS}" = "build_error" ]; then
    if [ "${PASS_STATUS}" = "boot_success" ]; then
        STATUS="validated"
    else
        STATUS="error"
    fi
elif [ "${FAIL_STATUS}" = "warnings_only" ]; then
    STATUS="weak_signal"
elif [ "${FAIL_STATUS}" = "unexpected_pass" ]; then
    STATUS="pre_pass"
else
    STATUS="error"
fi

cat > "${RESULTS_DIR}/result.json" <<EOF
{
    "instance_id": "nuttx__nuttx-14802",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
