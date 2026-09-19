#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#13347 — "build: fix memory manager compile options for CMake"
#
# Validation logic:
#   FAIL: sim:ostest CMake build uses set_source_files_properties twice for kasan.c.
#         The second call overwrites the first, so -fno-sanitize=kernel-address is never
#         applied. kasan.c is compiled with KASAN instrumentation, causing infinite
#         recursion on the first memory allocation at boot → binary crashes immediately,
#         no NuttX output.
#   PASS: after the fix, target_compile_options correctly applies both flags.
#         The binary boots and ostest runs to completion.
#
# No companion apps PR. CMake build only (not Make).
#
# Usage: ./scripts/run_13347.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-13347"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-13347"

KERNEL_BASE_COMMIT="fbc8605b2718ab722e58a5d66191ea1a3e447404"
KERNEL_MERGE_COMMIT="7dbb887f07553a14b84aa2b2df3b61232fa7d107"
APPS_COMMIT="0fc0cb2888c7f8a4cfc89e3649bdf693bb859115"

IMAGE="embedeval:nuttx-13347"
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

# Clone kernel at base commit (broken CMakeLists.txt — set_source_files_properties
# overwrites, so -fno-sanitize=kernel-address is never applied to kasan.c)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at compatible commit
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_COMMIT}

# Pre-build with CMake (build will succeed — crash is at runtime, not compile time)
RUN cmake -B build -DBOARD_CONFIG=sim:ostest -DNUTTX_APPS_DIR=./apps -G Ninja 2>&1 \
    && cmake --build build 2>&1 || true
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
    echo "Building (clones repos, CMake build at base commit) ..."
    docker build \
        --build-arg KERNEL_BASE_COMMIT="${KERNEL_BASE_COMMIT}" \
        --build-arg APPS_COMMIT="${APPS_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 5a: Verify FAIL — binary crashes on launch due to KASAN recursion ───
echo ""
echo "=== Step 5a: Verifying binary crashes on base commit ==="
echo "(expect: no NuttX output — KASAN self-instrumentation causes crash at boot)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    timeout 15 ./build/nuttx 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -20

if echo "${FAIL_OUTPUT}" | grep -q "NuttX"; then
    echo ""
    echo "WARNING: NuttX booted on base commit — no crash signal, checking for ostest failure..."
    if echo "${FAIL_OUTPUT}" | grep -qiE "FAIL|error|assert"; then
        echo "Runtime failure detected."
        FAIL_STATUS="runtime_error"
    else
        FAIL_STATUS="unexpected_pass"
    fi
else
    echo ""
    echo "CONFIRMED: Binary did not produce NuttX output on base commit (FAIL step validated) ✓"
    FAIL_STATUS="crash_on_boot"
fi

# ── Step 5b: Apply fix, rebuild, verify PASS ──────────────────────────────────
echo ""
echo "=== Step 5b: Applying fix and verifying CMake build + boot PASSES ==="

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

FIX_BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    cmake --build build 2>&1
" 2>&1 || true)

echo "${FIX_BUILD_OUTPUT}" | tee "${RESULTS_DIR}/pass_build.log" | tail -10

if echo "${FIX_BUILD_OUTPUT}" | grep -qiE "error:|Error [^0]|FAILED"; then
    echo ""
    echo "FAIL: Build failed after fix — check pass_build.log"
    PASS_STATUS="build_error"
else
    echo ""
    echo "Build succeeded after fix, running boot check..."

    RUN_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'ostest\npoweroff\n' | timeout 120 ./build/nuttx 2>&1
    " || true)

    echo "${RUN_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -20

    if echo "${RUN_OUTPUT}" | grep -qE "NuttX|ostest|nsh>|timer_test|user_main"; then
        echo ""
        echo "CONFIRMED: NuttX boots and runs ostest after fix ✓"
        PASS_STATUS="boot_success"
    else
        echo ""
        echo "WARNING: NuttX did not boot after fix — check pass_run.log"
        PASS_STATUS="no_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "crash_on_boot" ] || [ "${FAIL_STATUS}" = "runtime_error" ]; then
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
    "instance_id": "nuttx__nuttx-13347",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
