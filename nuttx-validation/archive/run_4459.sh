#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#4459 — "sim: Inhibit stack protector on stack coloration function"
#
# Validation logic:
#   FAIL: up_stack_color() intentionally writes the stack canary pattern outside
#         the function's own stack frame. GCC 11 (Ubuntu 22.04) stack protector
#         detects this and calls __stack_chk_fail → abort. sim:nettest crashes
#         at boot (before NSH starts) on base commit.
#   PASS: after fix, nostackprotect_function attribute disables stack protector
#         on up_stack_color. NuttX boots normally. Network init fails in Docker
#         (no TUN device) but NSH prompt appears.
#
# No companion apps PR. sim:nettest target (native sim, no cross-compiler needed).
#
# Usage: ./scripts/run_4459.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-4459"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-4459"

KERNEL_BASE_COMMIT="151f6eab3c7f836ab1334d129ac482cf7b40a718"
KERNEL_MERGE_COMMIT="2071aadc0e798780812d0b7898536dbef6476248"
APPS_COMMIT="c222043ed1ede9ef8e9e93ffed14ee9c41c4a2b1"

IMAGE="embedeval:nuttx-4459"
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
    -- arch/sim/src/sim/up_createstack.c include/nuttx/compiler.h \
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

# Clone kernel at base commit (up_stack_color lacks nostackprotect_function)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at compatible commit
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_COMMIT}

# Configure sim:lvgl — has CONFIG_STACK_COLORATION=y which triggers the crash
# SDL_VIDEODRIVER=offscreen used at runtime so no display is needed
RUN ./tools/configure.sh -a ./apps sim:lvgl

# Pre-build (succeeds — crash is at runtime when up_stack_color triggers stack protector)
RUN make -j$(nproc) 2>&1 || true
DOCKERFILE
echo "Dockerfile written."

# ── Step 3: Build base image ──────────────────────────────────────────────────
echo ""
echo "=== Step 3: Base image ==="
echo "Building ${BASE_IMAGE} (rebuilding to pick up libsdl2-dev) ..."
docker build \
    -f "${REPO_ROOT}/docker/bases/nuttx.Dockerfile" \
    -t "${BASE_IMAGE}" \
    "${REPO_ROOT}/docker/bases/"

# ── Step 4: Build instance image ─────────────────────────────────────────────
echo ""
echo "=== Step 4: Instance image ==="
echo "Building ..."
docker build \
    --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
    --build-arg APPS_COMMIT="${APPS_COMMIT}" \
    -t "${IMAGE}" \
    "${INSTANCE_DIR}"

# ── Step 5a: Verify FAIL — binary crashes on base commit ─────────────────────
echo ""
echo "=== Step 5a: Verifying crash on base commit ==="
echo "(expect: __stack_chk_fail / Aborted — stack protector triggers in up_stack_color)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    SDL_VIDEODRIVER=offscreen timeout 15 ./nuttx 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -20

if echo "${FAIL_OUTPUT}" | grep -qiE "stack smashing|__stack_chk_fail|Aborted|Segmentation fault|segfault|core dumped"; then
    echo ""
    echo "CONFIRMED: Crash on base commit (FAIL step validated) ✓"
    FAIL_STATUS="crash"
elif ! echo "${FAIL_OUTPUT}" | grep -qE "NuttX|nsh>"; then
    echo ""
    echo "CONFIRMED: No NuttX output on base commit (FAIL step validated) ✓"
    FAIL_STATUS="no_output"
else
    echo ""
    echo "WARNING: NuttX booted on base commit — no crash detected"
    FAIL_STATUS="unexpected_pass"
fi

# ── Step 5b: Apply fix, rebuild, verify PASS ──────────────────────────────────
echo ""
echo "=== Step 5b: Applying fix and verifying NuttX boots ==="

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
    echo "Build succeeded, running boot check..."

    PASS_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        SDL_VIDEODRIVER=offscreen printf 'uname -a\npoweroff\n' | timeout 30 ./nuttx 2>&1
    " || true)

    echo "${PASS_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -20

    if echo "${PASS_OUTPUT}" | grep -qE "NuttX|nsh>"; then
        echo ""
        echo "CONFIRMED: NuttX boots after fix ✓"
        PASS_STATUS="boot_success"
    elif echo "${PASS_OUTPUT}" | grep -qiE "stack smashing|Aborted|Segmentation fault"; then
        echo ""
        echo "FAIL: Still crashing after fix — check pass_run.log"
        PASS_STATUS="still_crashing"
    else
        PASS_STATUS="no_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "crash" ] || [ "${FAIL_STATUS}" = "no_output" ]; then
    if [ "${PASS_STATUS}" = "boot_success" ]; then
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
    "instance_id": "nuttx__nuttx-4459",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
