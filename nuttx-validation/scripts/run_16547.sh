#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#16547 — "cmake(bugfix): fix VERSION generator strip error in CMake build"
#
# Validation logic:
#   FAIL: cmake/nuttx_mkversion.cmake generates a malformed version.h with a
#         missing closing quote on CONFIG_VERSION_STRING → CMake build compile error
#   PASS: after applying the one-line fix, version.h is generated correctly → build succeeds
#
# No companion apps PR — the FAIL is in the kernel CMake build itself.
#
# Usage: ./scripts/run_16547.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-16547"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-16547"

KERNEL_BASE_COMMIT="a0aa654c701d94a1313cf54bde7e6e578c4788f8"
KERNEL_MERGE_COMMIT="8dd49d2fac6f86adce1230e84dcd073e7fb0c485"
APPS_COMMIT="7790894bb67ee2591ea6dfc88710e3b2e69580af"

IMAGE="nuttx-embedbench:nuttx-16547"
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
cat > "${INSTANCE_DIR}/Dockerfile" <<DOCKERFILE
FROM nuttx-embedbench-base:latest

ARG KERNEL_BASE_COMMIT
ARG APPS_COMMIT

WORKDIR /testbed

# Clone kernel at base commit (broken cmake version generator)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout \${KERNEL_BASE_COMMIT}

# Clone apps at compatible commit
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout \${APPS_COMMIT}
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
    echo "Building (clones kernel at base commit + apps) ..."
    docker build \
        --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
        --build-arg APPS_COMMIT="${APPS_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 5a: Verify FAIL on broken code ──────────────────────────────────────
echo ""
echo "=== Step 5a: Verifying CMake build FAILS on broken code ==="
echo "(expect compile error — malformed version.h with missing closing quote)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    cmake -B build -DBOARD_CONFIG=sim:nsh -DNUTTX_APPS_DIR=./apps -G Ninja 2>&1
    cmake --build build 2>&1
" || true)

echo "${BUILD_OUTPUT}" | tee "${RESULTS_DIR}/fail_build.log" | tail -20

if echo "${BUILD_OUTPUT}" | grep -qE "missing closing quote|CONFIG_VERSION_STRING|error:.*version"; then
    echo ""
    echo "CONFIRMED: Compile error on broken code (FAIL step validated) ✓"
    FAIL_STATUS="compile_error"
elif echo "${BUILD_OUTPUT}" | grep -qiE "error|failed"; then
    echo ""
    echo "CONFIRMED: Build error on broken code (FAIL step validated) ✓"
    FAIL_STATUS="build_error"
else
    echo ""
    echo "WARNING: Build may have unexpectedly succeeded — check fail_build.log"
    FAIL_STATUS="unexpected_pass"
fi

# ── Step 5b: Apply fix, verify PASS ──────────────────────────────────────────
echo ""
echo "=== Step 5b: Applying kernel fix and verifying CMake build PASSES ==="

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

FIX_BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    cmake -B build -DBOARD_CONFIG=sim:nsh -DNUTTX_APPS_DIR=./apps -G Ninja 2>&1
    cmake --build build 2>&1
" 2>&1 || true)

echo "${FIX_BUILD_OUTPUT}" | tee "${RESULTS_DIR}/pass_build.log" | tail -20

if echo "${FIX_BUILD_OUTPUT}" | grep -qiE "^(cmake --build build|Build succeeded|\[100%\])"; then
    PASS_STATUS="build_success"
elif ! echo "${FIX_BUILD_OUTPUT}" | grep -qiE "error|failed"; then
    PASS_STATUS="build_success"
else
    PASS_STATUS="build_error"
fi

if [ "${PASS_STATUS}" = "build_success" ]; then
    echo ""
    echo "CONFIRMED: Build succeeds after fix (PASS step validated) ✓"
else
    echo ""
    echo "FAIL: Build still failing after fix — check pass_build.log"
fi

# ── Step 5c: Boot check ───────────────────────────────────────────────────────
if [ "${PASS_STATUS}" = "build_success" ]; then
    echo ""
    echo "=== Step 5c: Boot check (sim:nsh, timeout 30s) ==="
    RUN_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'uname -a\nexit\n' | timeout 30 ./build/nuttx 2>&1
    " || true)

    echo "${RUN_OUTPUT}" | tee "${RESULTS_DIR}/run.log" | tail -15

    if echo "${RUN_OUTPUT}" | grep -q "NuttX"; then
        echo ""
        echo "CONFIRMED: NuttX boots successfully ✓"
        BOOT_RESULT="passed"
    else
        BOOT_RESULT="no_nuttx_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" != "unexpected_pass" ] && [ "${PASS_STATUS}" = "build_success" ]; then
    STATUS="validated"
elif [ "${FAIL_STATUS}" = "unexpected_pass" ]; then
    STATUS="pre_pass"
else
    STATUS="error"
fi

cat > "${RESULTS_DIR}/result.json" <<EOF
{
    "instance_id": "nuttx__nuttx-16547",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
