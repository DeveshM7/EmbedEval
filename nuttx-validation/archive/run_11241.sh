#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#11241 — "task/pthread_cancelpt: Fix nxtask_delete from another task group"
#
# Validation logic:
#   FAIL: PR #11165 caused a regression — task_delete from another task group fails
#         because nxnotify_cancellation() calls tls_get_info_pid() which checks
#         permissions and returns NULL for cross-group tasks. ostest task_delete
#         test fails or hangs on base commit.
#   PASS: after fix, nxnotify_cancellation() calls nxsched_get_tls() which bypasses
#         the permission check. ostest task_delete test passes.
#
# No companion apps PR. rv-virt:nsh64 target (RISC-V QEMU, ostest included).
#
# Usage: ./scripts/run_11241.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-11241"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-11241"

KERNEL_BASE_COMMIT="e5eabbb411cce22d877593d7a04b21023016dce2"
KERNEL_MERGE_COMMIT="57de6484e9197fe8f50314fa3786f322bb70bac4"
APPS_COMMIT="097411de494f8be77cffc85bf593eab7083f2250"

IMAGE="embedeval:nuttx-11241"
BASE_IMAGE="embedeval-nuttx-base:latest"

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
FROM embedeval-nuttx-base:latest

ARG KERNEL_BASE_COMMIT
ARG APPS_COMMIT

WORKDIR /testbed

# Clone kernel at base commit (broken tls_get_info_pid permission check in cancelpt)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at compatible commit
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_COMMIT}

# Configure rv-virt:nsh64 — already has CONFIG_TESTING_OSTEST=y
RUN ./tools/configure.sh -a ./apps rv-virt:nsh64

# Pre-build (succeeds — bug is runtime only)
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
    echo "Building (clones repos, builds rv-virt:nsh64 at base commit) ..."
    docker build \
        --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
        --build-arg APPS_COMMIT="${APPS_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 5a: Verify FAIL — ostest task_delete fails on base commit ────────────
echo ""
echo "=== Step 5a: Verifying runtime failure on base commit ==="
echo "(expect: task_delete test fails or ostest does not exit cleanly)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    printf 'ostest\npoweroff\n' | timeout 300 \
        qemu-system-riscv64 -M virt -bios ./nuttx -nographic -m 128M 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -30

if echo "${FAIL_OUTPUT}" | grep -qiE "task_delete.*FAILED|FAILED.*task_delete|Assertion.*failed|PANIC|fault|ERROR.*task"; then
    echo ""
    echo "CONFIRMED: task_delete failure on base commit (FAIL step validated) ✓"
    FAIL_STATUS="task_delete_failed"
elif ! echo "${FAIL_OUTPUT}" | grep -qE "ostest_main: Exiting|Exiting with status 0"; then
    echo ""
    echo "CONFIRMED: ostest did not complete on base commit (FAIL step validated) ✓"
    FAIL_STATUS="did_not_complete"
else
    echo ""
    echo "WARNING: ostest completed on base commit — checking exit status..."
    if echo "${FAIL_OUTPUT}" | grep -q "Exiting with status 0"; then
        FAIL_STATUS="unexpected_pass"
    else
        FAIL_STATUS="exited_with_error"
    fi
fi

# ── Step 5b: Apply fix, rebuild, verify PASS ──────────────────────────────────
echo ""
echo "=== Step 5b: Applying fix and verifying ostest passes ==="

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

FIX_BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${FIX_BUILD_OUTPUT}" | tee "${RESULTS_DIR}/pass_build.log" | tail -5

if echo "${FIX_BUILD_OUTPUT}" | grep -qiE "^make\[.*\]: \*\*\* |^make: \*\*\* "; then
    echo ""
    echo "FAIL: Build failed after fix — check pass_build.log"
    PASS_STATUS="build_error"
else
    echo ""
    echo "Build succeeded, running ostest via QEMU..."

    PASS_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'ostest\npoweroff\n' | timeout 300 \
            qemu-system-riscv64 -M virt -bios ./nuttx -nographic -m 128M 2>&1
    " || true)

    echo "${PASS_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -20

    if echo "${PASS_OUTPUT}" | grep -q "Exiting with status 0"; then
        echo ""
        echo "CONFIRMED: ostest completed successfully after fix ✓"
        PASS_STATUS="ostest_passed"
    elif echo "${PASS_OUTPUT}" | grep -qE "ostest_main: Exiting"; then
        echo ""
        echo "ostest exited but with non-zero status — check pass_run.log"
        PASS_STATUS="ostest_failed"
    else
        PASS_STATUS="no_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "unexpected_pass" ]; then
    STATUS="pre_pass"
elif [ "${FAIL_STATUS}" = "task_delete_failed" ] || [ "${FAIL_STATUS}" = "did_not_complete" ] || [ "${FAIL_STATUS}" = "exited_with_error" ]; then
    if [ "${PASS_STATUS}" = "ostest_passed" ]; then
        STATUS="validated"
    else
        STATUS="error"
    fi
else
    STATUS="error"
fi

cat > "${RESULTS_DIR}/result.json" <<EOF
{
    "instance_id": "nuttx__nuttx-11241",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
