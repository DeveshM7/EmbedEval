#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#11898  — "sched/pthread/join: refactor pthread join to support join task"
#   Apps PR   apache/nuttx-apps#2329 — "testing/ostest/cancel: joining a detached/canceled thread should return EINVAL, not ESRCH"
#
# Validation logic:
#   FAIL: POSIX requires pthread_join() on a detached/canceled thread to return
#         EINVAL. The base kernel returns ESRCH instead. The test patch changes
#         cancel_test to assert EINVAL — so on the base kernel the check
#         (status != EINVAL) fires, prints "ERROR pthread_join failed but with
#         wrong status=3", then ASSERT(false) aborts ostest.
#   PASS: After the 17-file pthread join refactor (introducing task_join.c),
#         pthread_join on a detached thread correctly returns EINVAL —
#         cancel_test prints "PASS pthread_join failed with status=EINVAL"
#         and ostest completes cleanly.
#
# Usage: ./scripts/nuttx_PR_validation/run_11898.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-11898"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-11898"

KERNEL_BASE_COMMIT="2b18917a6b7a35b44c3448ef1f6a87caa13e4f90"
KERNEL_MERGE_COMMIT="df30a1f8d35e21ce245328b4240285540adbe9ae"
APPS_BASE_COMMIT="fec49af501f15b547a062e38f13c020a90e6512b"
APPS_MERGE_COMMIT="69e497f68127cad345fd13786c5342822c58173f"

IMAGE="nuttx-embedbench:nuttx-11898"
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

# ── Step 1: Generate patches ──────────────────────────────────────────────────
echo "=== Step 1: Generating patches ==="

# test_patch: apps#2329 diff — changes cancel.c to assert EINVAL instead of
# ESRCH when pthread_join is called on a detached/canceled thread (2 lines)
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx-apps.git \
    "${WORK_DIR}/nuttx-apps" -q
cd "${WORK_DIR}/nuttx-apps"
git fetch origin "${APPS_BASE_COMMIT}" "${APPS_MERGE_COMMIT}" -q
git diff "${APPS_BASE_COMMIT}..${APPS_MERGE_COMMIT}" -- testing/ \
    > "${WORK_DIR}/test_patch.diff"
echo "test_patch.diff: $(wc -l < "${WORK_DIR}/test_patch.diff") lines"

# fix_patch: nuttx#11898 diff — major pthread join refactor: introduces
# task_join.c, overhauls pthread_completejoin, pthread_detach, pthread_join,
# pthread_findjoininfo, pthread_release, sched_releasetcb, and supporting
# headers/build files so that joining a detached thread returns EINVAL not ESRCH
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx.git \
    "${WORK_DIR}/nuttx-kernel" -q
cd "${WORK_DIR}/nuttx-kernel"
git fetch origin "${KERNEL_MERGE_COMMIT}" -q
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" -- sched/ include/nuttx/sched.h \
    > "${WORK_DIR}/fix_patch.diff"
echo "fix_patch.diff: $(wc -l < "${WORK_DIR}/fix_patch.diff") lines"

cd "${REPO_ROOT}"

# ── Step 2: Write Dockerfile ──────────────────────────────────────────────────
echo ""
echo "=== Step 2: Writing Dockerfile ==="
cat > "${INSTANCE_DIR}/Dockerfile" <<'DOCKERFILE'
FROM nuttx-embedbench-base:latest

ARG KERNEL_BASE_COMMIT
ARG APPS_BASE_COMMIT

WORKDIR /testbed

# Clone kernel at base commit (pthread_join of detached thread returns ESRCH)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at base commit (before cancel.c asserts EINVAL)
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# Configure sim:nsh with ostest enabled
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# Apply test patch — changes cancel_test to expect EINVAL instead of ESRCH
# The cancel test is an existing ostest component, no new files needed
COPY test_patch.diff /tmp/test_patch.diff
RUN cd apps && git apply /tmp/test_patch.diff

# Pre-build succeeds — cancel.c uses only existing kernel APIs, the bug is
# purely behavioral (wrong errno returned at runtime), not a compile error
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

# ── Step 5a: Verify FAIL — ASSERT fires on wrong errno from pthread_join ─────
echo ""
echo "=== Step 5a: Verifying runtime failure on base commit ==="
echo "(expect: cancel_test ERROR wrong status=3 (ESRCH), ASSERT fires, ostest aborts)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    printf 'ostest\npoweroff\n' | timeout 120 ./nuttx 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -25

if echo "${FAIL_OUTPUT}" | grep -qiE "cancel_test.*wrong status|ERROR pthread_join"; then
    echo ""
    echo "CONFIRMED: cancel_test detected wrong errno from pthread_join (FAIL step validated) ✓"
    FAIL_STATUS="assertion_failed"
elif echo "${FAIL_OUTPUT}" | grep -qiE "_assert|__assert|ASSERT|abort|Aborted"; then
    echo ""
    echo "CONFIRMED: Assertion failure in cancel_test on base commit (FAIL step validated) ✓"
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
echo "(applying 17-file pthread join refactor introducing task_join.c)"

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
    elif echo "${PASS_OUTPUT}" | grep -qE "cancel_test: PASS pthread_join failed with status=EINVAL"; then
        echo ""
        echo "cancel_test passed but ostest did not complete cleanly — check pass_run.log"
        PASS_STATUS="partial"
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
    "instance_id": "nuttx__nuttx-11898",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
