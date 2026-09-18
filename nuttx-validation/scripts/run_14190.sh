#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#14190 — "Revert 'sched/spinlock: remove nesting spinlock support'"
#
# Validation logic:
#   FAIL: the reverted commit changed spinlock.h in a way that causes infinite recursion
#         in arch_atomic.c (spin_lock_wo_note inlined into __atomic_load_2).
#         Running sim:smp ostest hits the recursive atomic path → stack overflow / crash.
#         The build succeeds but ostest does not complete normally.
#   PASS: after applying this revert, the recursion is broken. ostest runs to completion.
#
# No companion apps PR. sim:smp target (native sim, SMP + ostest enabled).
#
# Usage: ./scripts/run_14190.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-14190"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-14190"

KERNEL_BASE_COMMIT="a2b129fb1d0a2aa0946157fd92451ecbe6d7f9c0"
KERNEL_MERGE_COMMIT="9e81f5efacd6a169e8923ad0fe7a842ce11359e5"
APPS_COMMIT="131ff36842785fad58c09eadd71c8bbc4d69a8b2"

IMAGE="embedeval:nuttx-14190"
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

# Clone kernel at base commit (bad spinlock change causing atomic infinite recursion)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at compatible commit
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_COMMIT}

# Configure sim:smp — already has CONFIG_TESTING_OSTEST=y and CONFIG_TESTING_SMP=y
RUN ./tools/configure.sh -a ./apps sim:smp \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# Pre-build (succeeds — crash is runtime only)
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

# ── Step 5a: Verify FAIL — ostest crashes/hangs on base commit ────────────────
echo ""
echo "=== Step 5a: Verifying runtime failure on base commit ==="
echo "(expect: crash, hang, or ostest does not complete — infinite recursion in atomics)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    printf 'ostest\npoweroff\n' | timeout 60 ./nuttx 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -20

if echo "${FAIL_OUTPUT}" | grep -qE "Segmentation fault|stack overflow|Assertion|PANIC|Aborted|fault"; then
    echo ""
    echo "CONFIRMED: Crash detected on base commit (FAIL step validated) ✓"
    FAIL_STATUS="crash"
elif ! echo "${FAIL_OUTPUT}" | grep -qE "ostest_main: Exiting|Exiting with status"; then
    echo ""
    echo "CONFIRMED: ostest did not complete on base commit (FAIL step validated) ✓"
    FAIL_STATUS="did_not_complete"
else
    echo ""
    echo "WARNING: ostest completed on base commit — no FAIL signal"
    FAIL_STATUS="unexpected_pass"
fi

# ── Step 5b: Apply fix, rebuild, verify PASS ──────────────────────────────────
echo ""
echo "=== Step 5b: Applying revert and verifying ostest completes ==="

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

    echo "${PASS_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -20

    if echo "${PASS_OUTPUT}" | grep -qE "ostest_main: Exiting|Exiting with status 0"; then
        echo ""
        echo "CONFIRMED: ostest completed after fix ✓"
        PASS_STATUS="ostest_passed"
    elif echo "${PASS_OUTPUT}" | grep -qE "NuttX|nsh>"; then
        echo ""
        echo "NuttX booted but ostest did not reach exit — check pass_run.log"
        PASS_STATUS="partial"
    else
        PASS_STATUS="no_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "crash" ] || [ "${FAIL_STATUS}" = "did_not_complete" ]; then
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
    "instance_id": "nuttx__nuttx-14190",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
