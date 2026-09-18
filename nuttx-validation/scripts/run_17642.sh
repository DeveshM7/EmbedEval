#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#17642 — "[EXPERIMENTAL]sched/hrtimer: hrtimer state machine improvement for SMP cases"
#   Apps PR  apache/nuttx-apps#3297 — "ostest: sync hrtimer ostest with hrtimer updates"
#
# Validation logic:
#   FAIL: apps PR #3297 updates testing/ostest/hrtimer.c to use the new callback signature:
#         - hrtimer_cb return type changed uint32_t → uint64_t
#         - hrtimer_cb gains a second parameter `uint64_t expired`
#         - hrtimer_init() loses the `arg` parameter (3 args → 2 args)
#         With the test patch applied to the base kernel, the build fails with a
#         compile error: type mismatch and wrong argument count.
#   PASS: after applying the kernel fix (PR #17642), the header matches the test → build succeeds.
#
# Usage: ./scripts/run_17642.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-17642"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-17642"

KERNEL_BASE_COMMIT="2a1f24b817829f4e0a9fd9d1256fc9b72af9e328"
KERNEL_MERGE_COMMIT="24651af90c60006b2ccea01fd960a2caa9c9a921"
APPS_BASE_COMMIT="ca11a7e0930a0170d2c17e7913cd3fc221e2f6a5"
APPS_MERGE_COMMIT="416c315f928110b37f9b7a362f4dd2f2d89b0cac"

IMAGE="embedeval:nuttx-17642"
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

# ── Step 1: Generate patches ──────────────────────────────────────────────────
echo "=== Step 1: Generating patches ==="

# test_patch.diff — apps PR #3297: hrtimer.c updated to new callback signature
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx-apps.git \
    "${WORK_DIR}/nuttx-apps" -q
cd "${WORK_DIR}/nuttx-apps"
git fetch origin "${APPS_MERGE_COMMIT}" -q
git diff "${APPS_BASE_COMMIT}..${APPS_MERGE_COMMIT}" \
    > "${WORK_DIR}/test_patch.diff"
echo "test_patch.diff: $(wc -l < "${WORK_DIR}/test_patch.diff") lines"

# fix_patch.diff — kernel PR #17642: hrtimer.h + impl files
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx.git \
    "${WORK_DIR}/nuttx-kernel" -q
cd "${WORK_DIR}/nuttx-kernel"
git fetch origin "${KERNEL_MERGE_COMMIT}" -q
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" \
    -- include/ sched/ \
    > "${WORK_DIR}/fix_patch.diff"
echo "fix_patch.diff: $(wc -l < "${WORK_DIR}/fix_patch.diff") lines"

cd "${REPO_ROOT}"

# ── Step 2: Write Dockerfile ──────────────────────────────────────────────────
echo ""
echo "=== Step 2: Writing Dockerfile ==="
cat > "${INSTANCE_DIR}/Dockerfile" <<'DOCKERFILE'
FROM embedeval-nuttx-base:latest

ARG KERNEL_BASE_COMMIT
ARG APPS_BASE_COMMIT

WORKDIR /testbed

# Clone kernel at base commit (pre-fix: old hrtimer_cb signature with uint32_t return + arg param)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at base commit (pre-test-update: old hrtimer.c callback signature)
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# Configure rv-virt:smp64 — already includes CONFIG_TESTING_OSTEST=y and CONFIG_SMP=y
# Add HRTIMER support and disable login prompt
RUN ./tools/configure.sh -a ./apps rv-virt:smp64 \
    && echo "CONFIG_HRTIMER=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# Copy the test patch (already available as build context file)
COPY test_patch.diff /tmp/test_patch.diff

# Apply test patch to apps — updates hrtimer.c to use new API signatures.
# This creates the broken state: test expects new API, kernel still has old API.
RUN cd apps && git apply /tmp/test_patch.diff

# Pre-build attempt: EXPECTED to fail with compile error due to API mismatch.
# The failure IS the FAIL signal — we capture it at validation time, not here.
RUN make -j$(nproc) 2>&1 || true
DOCKERFILE

# Copy the test patch into the instance dir so Dockerfile can COPY it
cp "${WORK_DIR}/test_patch.diff" "${INSTANCE_DIR}/test_patch.diff"

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
    echo "Building (clones repos, applies test patch, expects compile error on pre-build) ..."
    docker build \
        --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
        --build-arg APPS_BASE_COMMIT="${APPS_BASE_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 5a: Verify FAIL — build should fail with compile error ───────────────
echo ""
echo "=== Step 5a: Verifying compile error on base commit + test patch ==="
echo "(expect: type mismatch / wrong argument count for hrtimer_cb / hrtimer_init)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_build.log" | tail -30

if echo "${FAIL_OUTPUT}" | grep -qE "error:.*hrtimer|hrtimer.*error:|incompatible.*type|too (few|many) argument|implicit declaration"; then
    echo ""
    echo "CONFIRMED: Compile error on base commit (FAIL step validated) ✓"
    FAIL_STATUS="compile_error"
elif echo "${FAIL_OUTPUT}" | grep -qiE "^make\[.*\]: \*\*\* |^make: \*\*\* "; then
    echo ""
    echo "CONFIRMED: Build error on base commit (FAIL step validated) ✓"
    FAIL_STATUS="build_error"
else
    echo ""
    echo "WARNING: Build may have unexpectedly succeeded — check fail_build.log"
    FAIL_STATUS="unexpected_pass"
fi

# ── Step 5b: Apply kernel fix, verify PASS ────────────────────────────────────
echo ""
echo "=== Step 5b: Applying kernel fix and verifying build PASSES ==="

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

FIX_BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${FIX_BUILD_OUTPUT}" | tee "${RESULTS_DIR}/pass_build.log" | tail -10

if echo "${FIX_BUILD_OUTPUT}" | grep -qiE "^make\[.*\]: \*\*\* |^make: \*\*\* "; then
    echo ""
    echo "FAIL: Build still failing after kernel fix — check pass_build.log"
    PASS_STATUS="build_error"
else
    echo ""
    echo "Build succeeded after kernel fix, running ostest hrtimer check..."
    PASS_STATUS="build_success"

    # ── Step 5c: Boot check — run ostest via QEMU, check hrtimer test passes ──
    echo ""
    echo "=== Step 5c: Running ostest via QEMU rv-virt (timeout 120s) ==="
    RUN_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'ostest\npoweroff\n' | timeout 120 \
            qemu-system-riscv64 -M virt -bios ./nuttx -nographic \
            -smp 8 -m 128M 2>&1
    " || true)

    echo "${RUN_OUTPUT}" | tee "${RESULTS_DIR}/run.log" | tail -20

    if echo "${RUN_OUTPUT}" | grep -q "hrtimer test"; then
        if echo "${RUN_OUTPUT}" | grep -qiE "hrtimer.*FAILED|FAILED.*hrtimer"; then
            echo ""
            echo "WARNING: hrtimer test reported FAILED in ostest output"
            BOOT_RESULT="hrtimer_failed"
        else
            echo ""
            echo "CONFIRMED: hrtimer test ran without FAILED marker ✓"
            BOOT_RESULT="passed"
        fi
    elif echo "${RUN_OUTPUT}" | grep -q "NuttX"; then
        echo ""
        echo "NuttX booted but hrtimer test output not found (may not have run yet)"
        BOOT_RESULT="no_hrtimer_output"
    else
        BOOT_RESULT="no_nuttx_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "compile_error" ] || [ "${FAIL_STATUS}" = "build_error" ]; then
    if [ "${PASS_STATUS}" = "build_success" ]; then
        STATUS="validated"
    else
        STATUS="error"
    fi
elif [ "${FAIL_STATUS}" = "unexpected_pass" ]; then
    STATUS="pre_pass"
else
    STATUS="error"
fi

cat > "${RESULTS_DIR}/result.json" <<EOF
{
    "instance_id": "nuttx__nuttx-17642",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "boot_result": "${BOOT_RESULT:-n/a}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
