#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#5923 — "sim: Fix initialization of static C++ constructors when using glibc >= 2.34"
#
# Validation logic:
#   FAIL: glibc >= 2.34 changed the dynamic linker to parse constructors from the
#         DYNAMIC segment directly, bypassing the old __init_array_start symbol trick.
#         Constructors are called before NuttX initializes its memory manager →
#         crash on startup. Ubuntu 22.04 uses glibc 2.35, so the bug reproduces.
#   PASS: after the Makefile fix, .init_array is emptied (sections moved to .sinit),
#         glibc finds nothing to call, and NuttX calls constructors at the right time.
#         Binary boots and cxxtest passes.
#
# No companion apps PR. sim:libcxxtest target (native sim, no cross-compiler needed).
#
# Usage: ./scripts/run_5923.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-5923"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-5923"

KERNEL_BASE_COMMIT="840ba09b248232914901be405b270a5d4277b749"
KERNEL_MERGE_COMMIT="35009c5d4d600141dc549ed641675e690dcfcb83"
APPS_COMMIT="470174260c363ea636640a85e2a2c412de5ea1dc"

IMAGE="nuttx-embedbench:nuttx-5923"
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

# Clone kernel at base commit (old linker script trick broken by glibc >= 2.34)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at compatible commit
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_COMMIT}

# Configure sim:libcxxtest
RUN ./tools/configure.sh -a ./apps sim:libcxxtest

# Pre-build (succeeds — crash is at runtime due to glibc 2.35 constructor ordering)
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

# ── Step 5a: Verify FAIL — binary crashes on base commit ─────────────────────
echo ""
echo "=== Step 5a: Verifying crash on base commit ==="
echo "(expect: crash before NSH starts — glibc 2.35 calls C++ constructors too early)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    timeout 15 ./nuttx 2>&1
" || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_run.log" | tail -20

if echo "${FAIL_OUTPUT}" | grep -qiE "Segmentation fault|segfault|Aborted|core dumped"; then
    echo ""
    echo "CONFIRMED: Crash on base commit (FAIL step validated) ✓"
    FAIL_STATUS="crash"
elif ! echo "${FAIL_OUTPUT}" | grep -qE "NuttX|nsh>"; then
    echo ""
    echo "CONFIRMED: No NuttX output on base commit (FAIL step validated) ✓"
    FAIL_STATUS="no_output"
else
    echo ""
    echo "WARNING: NuttX booted on base commit — checking cxxtest output..."
    if echo "${FAIL_OUTPUT}" | grep -qiE "FAIL|error|wrong"; then
        FAIL_STATUS="test_failure"
        echo "Test failures detected."
    else
        FAIL_STATUS="unexpected_pass"
        echo "WARNING: No failure detected — check fail_run.log"
    fi
fi

# ── Step 5b: Apply fix, rebuild, verify PASS ──────────────────────────────────
echo ""
echo "=== Step 5b: Applying fix and verifying binary boots and cxxtest passes ==="

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
    echo "Build succeeded, running cxxtest..."

    PASS_OUTPUT=$(docker exec "${CID}" bash -c "
        cd /testbed
        printf 'cxxtest\npoweroff\n' | timeout 60 ./nuttx 2>&1
    " || true)

    echo "${PASS_OUTPUT}" | tee "${RESULTS_DIR}/pass_run.log" | tail -20

    if echo "${PASS_OUTPUT}" | grep -qE "NuttX|nsh>"; then
        if echo "${PASS_OUTPUT}" | grep -qiE "^FAIL|FAILED|assertion failed|panic"; then
            echo ""
            echo "WARNING: NuttX booted but cxxtest reported failures"
            PASS_STATUS="test_failure"
        else
            echo ""
            echo "CONFIRMED: NuttX boots and cxxtest runs after fix ✓"
            PASS_STATUS="boot_success"
        fi
    else
        echo ""
        echo "WARNING: Still no NuttX output after fix — check pass_run.log"
        PASS_STATUS="no_output"
    fi
fi

# ── Write result.json ─────────────────────────────────────────────────────────
STATUS="unknown"
if [ "${FAIL_STATUS}" = "crash" ] || [ "${FAIL_STATUS}" = "no_output" ] || [ "${FAIL_STATUS}" = "test_failure" ]; then
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
    "instance_id": "nuttx__nuttx-5923",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
