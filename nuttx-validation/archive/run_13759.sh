#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#13759 — "fs/pseudofile: fix compile break when enable PSEUDOFS_FILE"
#
# Validation logic:
#   FAIL: CONFIG_PSEUDOFS_FILE=y is added to sim:nsh defconfig (exposing the bug),
#         but fs_pseudofile.c is missing `int ret = OK;` — compile error: 'ret'
#         undeclared in pseudofile_munmap().
#   PASS: after applying the one-line fix to fs_pseudofile.c, build succeeds.
#
# Both changes are in the kernel PR — we split them:
#   test_patch.diff  — defconfig change (CONFIG_PSEUDOFS_FILE=y) baked into image
#   fix_patch.diff   — fs_pseudofile.c fix applied at runtime
#
# No companion apps PR. sim:nsh target.
#
# Usage: ./scripts/run_13759.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-13759"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-13759"

KERNEL_BASE_COMMIT="58d233cb429514c0190cafa8cb328a4707c2cc30"
KERNEL_MERGE_COMMIT="8bafb6520a241e59ddf52098a72595401076bad9"
APPS_COMMIT="9cc9a830ebcc5f509bd3f63a5c4a8a856d02da74"

IMAGE="embedeval:nuttx-13759"
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
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx.git \
    "${WORK_DIR}/nuttx-kernel" -q
cd "${WORK_DIR}/nuttx-kernel"
git fetch origin "${KERNEL_MERGE_COMMIT}" -q

# test_patch: defconfig change only — enables PSEUDOFS_FILE, exposing the compile break
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" \
    -- boards/sim/sim/sim/configs/nsh/defconfig \
    > "${WORK_DIR}/test_patch.diff"
echo "test_patch.diff: $(wc -l < "${WORK_DIR}/test_patch.diff") lines"

# fix_patch: fs_pseudofile.c fix only — adds missing `int ret = OK;`
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" \
    -- fs/vfs/fs_pseudofile.c \
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

# Clone kernel at base commit (fs_pseudofile.c missing `int ret = OK;`)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at compatible commit
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_COMMIT}

# Configure sim:nsh (NSH_CONSOLE_LOGIN already n in this defconfig)
RUN ./tools/configure.sh -a ./apps sim:nsh

# Copy and apply test patch — enables CONFIG_PSEUDOFS_FILE=y in defconfig,
# exposing the compile break in fs_pseudofile.c
COPY test_patch.diff /tmp/test_patch.diff
RUN git apply /tmp/test_patch.diff \
    && make olddefconfig

# Pre-build: EXPECTED to fail — `ret` undeclared in pseudofile_munmap()
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
        --build-arg APPS_COMMIT="${APPS_COMMIT}" \
        -t "${IMAGE}" \
        "${INSTANCE_DIR}"
fi

# ── Step 5a: Verify FAIL — compile error on base commit + test patch ──────────
echo ""
echo "=== Step 5a: Verifying compile error on base commit ==="
echo "(expect: 'ret' undeclared in pseudofile_munmap)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_build.log" | tail -20

if echo "${FAIL_OUTPUT}" | grep -qE "error:.*'ret'|'ret'.*undeclared|fs_pseudofile"; then
    echo ""
    echo "CONFIRMED: Compile error on base commit (FAIL step validated) ✓"
    FAIL_STATUS="compile_error"
elif echo "${FAIL_OUTPUT}" | grep -qiE "^make\[.*\]: \*\*\* |^make: \*\*\* "; then
    echo ""
    echo "CONFIRMED: Build error on base commit (FAIL step validated) ✓"
    FAIL_STATUS="build_error"
else
    echo ""
    echo "WARNING: Build may have succeeded — check fail_build.log"
    FAIL_STATUS="unexpected_pass"
fi

# ── Step 5b: Apply fix, verify PASS ──────────────────────────────────────────
echo ""
echo "=== Step 5b: Applying fix and verifying build PASSES ==="

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

FIX_BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${FIX_BUILD_OUTPUT}" | tee "${RESULTS_DIR}/pass_build.log" | tail -10

if echo "${FIX_BUILD_OUTPUT}" | grep -qiE "^make\[.*\]: \*\*\* |^make: \*\*\* "; then
    echo ""
    echo "FAIL: Build still failing after fix — check pass_build.log"
    PASS_STATUS="build_error"
else
    echo ""
    echo "Build succeeded after fix, running boot check..."

    RUN_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'uname -a\npoweroff\n' | timeout 30 ./nuttx 2>&1
    " || true)

    echo "${RUN_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -10

    if echo "${RUN_OUTPUT}" | grep -qE "NuttX|nsh>"; then
        echo ""
        echo "CONFIRMED: NuttX boots after fix ✓"
        PASS_STATUS="boot_success"
    else
        PASS_STATUS="no_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "compile_error" ] || [ "${FAIL_STATUS}" = "build_error" ]; then
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
    "instance_id": "nuttx__nuttx-13759",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
