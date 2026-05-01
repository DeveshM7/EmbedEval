#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#17776 — "drivers/pty: fix memory leak when pty_destroy"
#
# Validation logic:
#   FAIL: incorrect i_crefs handling in pty_close() leaks pipe handles.
#         Detected at runtime by LeakSanitizer when running `lsan` in NSH.
#         Build succeeds on base commit — failure is runtime only.
#   PASS: after applying the fix, `lsan` reports no memory leaks.
#
# No companion apps PR. Requires ASAN build (CONFIG_SIM_ASAN=y).
#
# Usage: ./scripts/run_17776.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-17776"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-17776"

KERNEL_BASE_COMMIT="2a1f24b817829f4e0a9fd9d1256fc9b72af9e328"
KERNEL_MERGE_COMMIT="cf4a9d7241163866a233c5f5af28b213ee4fef93"
APPS_COMMIT="ca11a7e0930a0170d2c17e7913cd3fc221e2f6a5"

IMAGE="nuttx-embedbench:nuttx-17776"
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

# Clone kernel at base commit (broken pty_close i_crefs logic)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps inside nuttx dir (required by configure.sh -a flag)
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_COMMIT}

# Configure sim:nsh and enable ASAN + lsan + hello
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && echo "CONFIG_SIM_ASAN=y" >> .config \
    && echo "CONFIG_MM_CUSTOMIZE_MANAGER=y" >> .config \
    && echo "CONFIG_FRAME_POINTER=y" >> .config \
    && echo "CONFIG_SYSTEM_LSAN=y" >> .config \
    && echo "CONFIG_EXAMPLES_HELLO=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# Pre-build (will succeed — bug is runtime only)
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
    echo "Building (clones repos, enables ASAN, pre-builds) ..."
    docker build \
        --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
        --build-arg APPS_COMMIT="${APPS_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 5a: Verify FAIL — lsan reports leak on base commit ──────────────────
echo ""
echo "=== Step 5a: Verifying LSAN reports memory leak on base commit ==="
echo "(expect: LeakSanitizer reports leaked pipe handles from pty_close)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    printf 'hello\nlsan\npoweroff\n' | timeout 60 ./nuttx 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -30

if echo "${FAIL_OUTPUT}" | grep -qE "LeakSanitizer|Direct leak|indirect leak|SUMMARY: AddressSanitizer.*leak"; then
    echo ""
    echo "CONFIRMED: Memory leak detected on base commit (FAIL step validated) ✓"
    FAIL_STATUS="runtime_leak"
elif echo "${FAIL_OUTPUT}" | grep -q "lsan: No memory leaks"; then
    echo ""
    echo "WARNING: lsan reports no leaks on base commit — no FAIL signal"
    FAIL_STATUS="unexpected_pass"
else
    echo ""
    echo "WARNING: Could not determine leak status — check fail_run.log"
    FAIL_STATUS="unknown"
fi

# ── Step 5b: Apply fix, verify PASS — no leaks ───────────────────────────────
echo ""
echo "=== Step 5b: Applying fix and verifying LSAN reports no leaks ==="

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

PASS_BUILD=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${PASS_BUILD}" | tee "${RESULTS_DIR}/pass_build.log" | tail -5

if echo "${PASS_BUILD}" | grep -qE "^make\[1\].*Error|^make.*Error [^0]"; then
    echo ""
    echo "FAIL: Build failed after fix — check pass_build.log"
    PASS_STATUS="build_error"
else
    echo ""
    echo "Build succeeded after fix, running lsan check..."

    PASS_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'hello\nlsan\npoweroff\n' | timeout 60 ./nuttx 2>&1
    " || true)

    echo "${PASS_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -30

    if echo "${PASS_OUTPUT}" | grep -q "lsan: No memory leaks"; then
        echo ""
        echo "CONFIRMED: No memory leaks after fix (PASS step validated) ✓"
        PASS_STATUS="no_leaks"
    elif echo "${PASS_OUTPUT}" | grep -qE "LeakSanitizer|Direct leak"; then
        echo ""
        echo "FAIL: Memory leak still present after fix"
        PASS_STATUS="leak_persists"
    else
        echo ""
        echo "WARNING: lsan output unclear — check pass_run.log"
        PASS_STATUS="unknown"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "runtime_leak" ] && [ "${PASS_STATUS}" = "no_leaks" ]; then
    STATUS="validated"
elif [ "${FAIL_STATUS}" = "unexpected_pass" ]; then
    STATUS="pre_pass"
else
    STATUS="error"
fi

cat > "${RESULTS_DIR}/result.json" <<EOF
{
    "instance_id": "nuttx__nuttx-17776",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
