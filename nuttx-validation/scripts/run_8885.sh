#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#8885  — "Increase the number of real time signals"
#   Apps   PR apache/nuttx-apps#1682 — "Changes to apps needed by nuttx PR 8885"
#
# Validation logic:
#   FAIL: new ostest code (apps #1682) uses SIGSET_FMT/SIGSET_ELEM macros that
#         don't exist in the kernel at base_commit → compile error
#   PASS: after applying kernel fix (#8885), macros exist → builds cleanly
#
# Usage: ./scripts/run_8885.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-8885"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-8885"

KERNEL_BASE_COMMIT="0eae218b49a46ef88e14f1cb763a6ca74fe342ab"
KERNEL_MERGE_COMMIT="717bb04cb7c6f1efb179d40a464e43e7cd13c7d8"
APPS_BASE_COMMIT="75b4720a6e57dedebb4ce607035350bfc3190fb3"
APPS_MERGE_COMMIT="a1bca5070c03f7208473f005b034ab12ee271838"

IMAGE="nuttx-embedbench:nuttx-8885"
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
WORK_DIR="$(mktemp -d)"

# ── Step 1: Generate test_patch.diff ─────────────────────────────────────────
echo "=== Step 1: Generating test_patch.diff ==="
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx-apps.git \
    "${WORK_DIR}/nuttx-apps" -q
cd "${WORK_DIR}/nuttx-apps"
git fetch origin "${APPS_MERGE_COMMIT}" -q
git diff "${APPS_BASE_COMMIT}..${APPS_MERGE_COMMIT}" \
    > "${INSTANCE_DIR}/test_patch.diff"
echo "test_patch.diff: $(wc -l < "${INSTANCE_DIR}/test_patch.diff") lines"
cd "${REPO_ROOT}"

# ── Step 2: Build base image ──────────────────────────────────────────────────
echo ""
echo "=== Step 2: Base image ==="
if docker image inspect "${BASE_IMAGE}" &>/dev/null; then
    echo "Already exists, skipping."
else
    echo "Building ${BASE_IMAGE} ..."
    docker build \
        -f "${REPO_ROOT}/docker/bases/nuttx.Dockerfile" \
        -t "${BASE_IMAGE}" \
        "${REPO_ROOT}/docker/bases/"
fi

# ── Step 3: Build instance image ─────────────────────────────────────────────
echo ""
echo "=== Step 3: Instance image ==="
if docker image inspect "${IMAGE}" &>/dev/null; then
    echo "Already exists, skipping."
else
    echo "Building (clones both repos, applies test patch, attempts pre-build) ..."
    docker build \
        --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
        --build-arg APPS_BASE_COMMIT="${APPS_BASE_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 4a: Verify FAIL on broken code ──────────────────────────────────────
echo ""
echo "=== Step 4a: Verifying build FAILS on broken code ==="
echo "(expect compile error — SIGSET_FMT/SIGSET_ELEM not in kernel at base_commit)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    make -j\$(nproc) 2>&1
" || true)

echo "${BUILD_OUTPUT}" | tee "${RESULTS_DIR}/fail_build.log" | tail -20

if echo "${BUILD_OUTPUT}" | grep -qE "error:.*SIGSET_FMT|error:.*SIGSET_ELEM|undeclared|implicit declaration"; then
    echo ""
    echo "CONFIRMED: Compile error on broken code (FAIL step validated) ✓"
    FAIL_STATUS="compile_error"
elif echo "${BUILD_OUTPUT}" | grep -q "Error"; then
    echo ""
    echo "CONFIRMED: Build error on broken code (FAIL step validated) ✓"
    FAIL_STATUS="build_error"
else
    echo ""
    echo "WARNING: Build may have unexpectedly succeeded — check fail_build.log"
    FAIL_STATUS="unexpected_pass"
fi

# ── Step 4b: Apply fix, verify PASS ──────────────────────────────────────────
echo ""
echo "=== Step 4b: Applying kernel fix and verifying build PASSES ==="

# Generate fix_patch from kernel PR #8885
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx.git \
    "${WORK_DIR}/nuttx-kernel" -q
cd "${WORK_DIR}/nuttx-kernel"
git fetch origin "${KERNEL_MERGE_COMMIT}" -q
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" \
    > "${WORK_DIR}/fix_patch.diff"
echo "fix_patch.diff: $(wc -l < "${WORK_DIR}/fix_patch.diff") lines"
cd "${REPO_ROOT}"

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

FIX_BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    make -j\$(nproc) 2>&1
" 2>&1 || true)
FIX_BUILD_EXIT=${PIPESTATUS[0]:-$?}

echo "${FIX_BUILD_OUTPUT}" | tee "${RESULTS_DIR}/pass_build.log" | tail -20

if [ ${FIX_BUILD_EXIT} -eq 0 ]; then
    echo ""
    echo "CONFIRMED: Build succeeds after fix (PASS step validated) ✓"
    PASS_STATUS="build_success"
else
    echo ""
    echo "FAIL: Build still failing after fix — check pass_build.log"
    PASS_STATUS="build_error"
fi

# ── Step 4c: Run ostest (optional runtime check) ─────────────────────────────
# NuttX sim never exits on its own — timeout kills it after ostest completes.
# ostest final summary line: "ostest_main: ostest PASSED" or "ostest_main: ostest FAILED"
# Individual subtest pass: "Errors  0  0" (zero errors = passed)
# Full suite takes several minutes — 300s timeout should be sufficient.
if [ "${PASS_STATUS}" = "build_success" ]; then
    echo ""
    echo "=== Step 4c: Running ostest (timeout 300s) ==="
    RUN_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'ostest\npoweroff\n' | timeout 300 ./nuttx 2>&1
    " || true)

    echo "${RUN_OUTPUT}" | tee "${RESULTS_DIR}/run.log" | tail -30

    # Final summary: "ostest_main: Exiting with status 0" = all passed
    #                "ostest_main: Exiting with status 1" (or non-zero) = failures
    if echo "${RUN_OUTPUT}" | grep -q "ostest_main: Exiting with status 0"; then
        OSTEST_RESULT="passed"
    elif echo "${RUN_OUTPUT}" | grep -q "ostest_main: Exiting with status"; then
        OSTEST_RESULT="failed"
    elif echo "${RUN_OUTPUT}" | grep -q "user_main: Exiting"; then
        OSTEST_RESULT="exited_no_status"
    else
        OSTEST_RESULT="incomplete_timeout"
    fi

    echo ""
    echo "ostest final result: ${OSTEST_RESULT}"
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
    "instance_id": "nuttx__nuttx-8885",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
