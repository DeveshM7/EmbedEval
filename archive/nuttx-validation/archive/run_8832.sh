#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#8832  — "sched/wqueue: Do as much work as possible in work_thread"
#   Apps PR   apache/nuttx-apps#1665 — "ostest: Introduce basic work queue test"
#
# Validation logic:
#   FAIL: kwork_thread.c (base commit) exits its work loop after processing ONE
#         item per wakeup. wqueue_test queues 100 count_workers after a sleep_worker,
#         then asserts all 100 ran (ASSERT(call_count == VERIFY_COUNT)). With only
#         one item processed per pass, fewer than 100 count_workers complete within
#         the timeout window — the assertion fires or ostest does not complete.
#   PASS: After the fix, kwork_thread drains ALL pending work in a single pass.
#         All 100 count_workers run immediately after the sleep_worker finishes,
#         ASSERT(call_count == 100) passes, ostest completes cleanly.
#
# Usage: ./scripts/nuttx_PR_validation/run_8832.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-8832"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-8832"

KERNEL_BASE_COMMIT="673a4aabf5d6856c9b87e258326cc4dae3f8689c"
KERNEL_MERGE_COMMIT="c9a38f42f7572172dd60d6748a7115b60b64835c"
APPS_BASE_COMMIT="3a1893ba7e1e4ea5aef0f5181886defaa5e69e91"
APPS_MERGE_COMMIT="412505d286bcd676513a4333f77a95433095b7ad"

IMAGE="embedeval:nuttx-8832"
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

# test_patch: apps#1665 diff — adds testing/ostest/wqueue.c (190 lines) and
# wires it into Makefile, ostest.h, ostest_main.c under CONFIG_SCHED_LPWORK/HPWORK
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx-apps.git \
    "${WORK_DIR}/nuttx-apps" -q
cd "${WORK_DIR}/nuttx-apps"
git fetch origin "${APPS_BASE_COMMIT}" "${APPS_MERGE_COMMIT}" -q
git diff "${APPS_BASE_COMMIT}..${APPS_MERGE_COMMIT}" -- testing/ \
    > "${WORK_DIR}/test_patch.diff"
echo "test_patch.diff: $(wc -l < "${WORK_DIR}/test_patch.diff") lines"

# fix_patch: nuttx#8832 diff — changes kwork_thread.c to drain all pending
# work items in a single pass instead of returning after processing just one
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx.git \
    "${WORK_DIR}/nuttx-kernel" -q
cd "${WORK_DIR}/nuttx-kernel"
git fetch origin "${KERNEL_MERGE_COMMIT}" -q
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" -- sched/wqueue/ \
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

# Clone kernel at base commit (kwork_thread.c processes only one work item per wakeup)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at base commit (before wqueue.c test was added)
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# Configure sim:nsh and enable work queues explicitly.
# Strip any duplicate entries before olddefconfig to avoid kconfig warnings.
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN\|CONFIG_SCHED_WORKQUEUE\|CONFIG_SCHED_LPWORK\|CONFIG_SCHED_HPWORK" \
       .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && echo "CONFIG_SCHED_WORKQUEUE=y" >> .config \
    && echo "CONFIG_SCHED_LPWORK=y" >> .config \
    && make olddefconfig

# Apply test patch — adds wqueue.c and wires it into ostest
COPY test_patch.diff /tmp/test_patch.diff
RUN cd apps && git apply /tmp/test_patch.diff

# Pre-build succeeds — wqueue.c uses existing kernel APIs, no new headers needed.
# The bug is a runtime assertion failure, not a compile error.
RUN make -j$(nproc) 2>&1 || true
DOCKERFILE

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
    echo "Building ..."
    docker build \
        --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
        --build-arg APPS_BASE_COMMIT="${APPS_BASE_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 5a: Verify FAIL — wqueue_test fails on base commit ──────────────────
echo ""
echo "=== Step 5a: Verifying runtime failure on base commit ==="
echo "(expect: wqueue_test call count < 100, ASSERT fires, ostest aborts)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    printf 'ostest\npoweroff\n' | timeout 120 ./nuttx 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -25

# Check for wqueue call count mismatch, assertion failure, or incomplete ostest
if echo "${FAIL_OUTPUT}" | grep -qE "wqueue_test: call = [0-9]+, expect"; then
    MIN_COUNT=$(echo "${FAIL_OUTPUT}" | grep -oE "call = [0-9]+" | grep -oE "[0-9]+" | sort -n | head -1)
    if [ "${MIN_COUNT:-100}" -lt 100 ] 2>/dev/null || \
       echo "${FAIL_OUTPUT}" | grep -qiE "_assert|__assert|ASSERT|abort|Aborted"; then
        echo ""
        echo "CONFIRMED: wqueue_test call count ${MIN_COUNT} < 100 or assertion fired (FAIL step validated) ✓"
        FAIL_STATUS="assertion_failed"
    else
        echo ""
        echo "WARNING: wqueue_test call count is ${MIN_COUNT} — may have passed unexpectedly"
        FAIL_STATUS="unexpected_pass"
    fi
elif echo "${FAIL_OUTPUT}" | grep -qiE "Assertion|assertion|ASSERT|abort|Aborted"; then
    echo ""
    echo "CONFIRMED: Assertion failure in wqueue_test on base commit (FAIL step validated) ✓"
    FAIL_STATUS="assertion_failed"
elif ! echo "${FAIL_OUTPUT}" | grep -q "Exiting with status 0"; then
    echo ""
    echo "CONFIRMED: ostest did not complete on base commit (FAIL step validated) ✓"
    FAIL_STATUS="did_not_complete"
else
    echo ""
    echo "WARNING: ostest completed on base commit — no failure detected"
    FAIL_STATUS="unexpected_pass"
fi

# ── Step 5b: Apply fix, rebuild, verify PASS ──────────────────────────────────
echo ""
echo "=== Step 5b: Applying kernel fix and verifying ostest passes ==="
echo "(fixing kwork_thread.c to drain all pending work per pass)"

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
    echo "Build succeeded, running ostest..."

    PASS_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'ostest\npoweroff\n' | timeout 300 ./nuttx 2>&1
    " || true)

    echo "${PASS_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -25

    if echo "${PASS_OUTPUT}" | grep -q "Exiting with status 0"; then
        echo ""
        echo "CONFIRMED: ostest passed after fix ✓"
        PASS_STATUS="ostest_passed"
    elif echo "${PASS_OUTPUT}" | grep -qE "ostest_main: Exiting"; then
        echo ""
        echo "ostest exited with non-zero status — check pass_run.log"
        PASS_STATUS="ostest_failed"
    elif echo "${PASS_OUTPUT}" | grep -qE "NuttX|nsh>"; then
        echo ""
        echo "NuttX booted but ostest did not complete — check pass_run.log"
        PASS_STATUS="partial"
    else
        PASS_STATUS="no_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "assertion_failed" ] || [ "${FAIL_STATUS}" = "did_not_complete" ]; then
    if [ "${PASS_STATUS}" = "ostest_passed" ]; then
        STATUS="validated"
    elif [ "${PASS_STATUS}" = "partial" ]; then
        STATUS="partial"
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
    "instance_id": "nuttx__nuttx-8832",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
