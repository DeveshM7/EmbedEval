#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#14152  — "setjmp: fix setjmp returns 0 when calling longjmp with 0"
#   Apps PR   apache/nuttx-apps#2725 — "add test for longjump with 0 as return value"
#
# Validation logic:
#   FAIL: POSIX requires longjmp(buf, 0) to make setjmp() return 1, not 0.
#         The base kernel x86_64 asm returns 0, so ASSERT(value == ret) fires
#         with value=0, ret=1 — NuttX aborts before ostest completes.
#   PASS: After the asm fix in arch_setjmp_x86_64.S (and _x86.S), longjmp(buf,0)
#         correctly causes setjmp to return 1 — ASSERT passes, ostest completes.
#
# Usage: ./scripts/nuttx_PR_validation/run_14152.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-14152"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-14152"

KERNEL_BASE_COMMIT="5a38c8bfe3cbf906010a906b4aff9f104789a63f"
KERNEL_MERGE_COMMIT="89d6abf3dfcf0bec86ed10ec9494995b6f3b41b4"
APPS_BASE_COMMIT="8e7d6cadf4795c3daf27e55637f3864517927cc2"
APPS_MERGE_COMMIT="30215c260bc087aa6675b4804ee600e43ea48839"

IMAGE="nuttx-embedbench:nuttx-14152"
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

# test_patch: apps#2725 diff — adds jump_with_retval() and the longjmp(0) test
# to the existing testing/ostest/setjmp.c
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx-apps.git \
    "${WORK_DIR}/nuttx-apps" -q
cd "${WORK_DIR}/nuttx-apps"
git fetch origin "${APPS_BASE_COMMIT}" "${APPS_MERGE_COMMIT}" -q
git diff "${APPS_BASE_COMMIT}..${APPS_MERGE_COMMIT}" -- testing/ \
    > "${WORK_DIR}/test_patch.diff"
echo "test_patch.diff: $(wc -l < "${WORK_DIR}/test_patch.diff") lines"

# fix_patch: nuttx#14152 diff — fixes arch_setjmp_x86.S and arch_setjmp_x86_64.S
# (the sim-specific assembly files) so longjmp(buf, 0) returns 1 not 0
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx.git \
    "${WORK_DIR}/nuttx-kernel" -q
cd "${WORK_DIR}/nuttx-kernel"
git fetch origin "${KERNEL_MERGE_COMMIT}" -q
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" -- libs/libc/machine/ \
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

# Clone kernel at base commit (arch_setjmp_x86_64.S returns 0 for longjmp(buf,0))
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at base commit (before the longjmp(0) test was added)
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# Configure sim:nsh
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# Apply test patch — adds jump_with_retval() and the ASSERT(value==ret) test
# for longjmp(buf, 0) case to testing/ostest/setjmp.c
COPY test_patch.diff /tmp/test_patch.diff
RUN cd apps && git apply /tmp/test_patch.diff

# Pre-build succeeds — the bug is a runtime assertion, not a compile error
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

# ── Step 5a: Verify FAIL — ASSERT fires on longjmp(buf, 0) ──────────────────
echo ""
echo "=== Step 5a: Verifying runtime failure on base commit ==="
echo "(expect: ASSERT(value == ret) fires — setjmp returns 0, should return 1)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    printf 'ostest\npoweroff\n' | timeout 120 ./nuttx 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -25

if echo "${FAIL_OUTPUT}" | grep -qiE "Assertion|assertion|ASSERT|setjmp.*zero|abort|Aborted"; then
    echo ""
    echo "CONFIRMED: Assertion failure on base commit (FAIL step validated) ✓"
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
echo "(fixing arch_setjmp_x86.S and arch_setjmp_x86_64.S)"

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
    "instance_id": "nuttx__nuttx-14152",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
