#!/usr/bin/env bash
# Builds and validates the NuttX EmbedEval instance for:
#   Kernel PR apache/nuttx#12802  — "sched/nxevent: add support of kernel event group"
#   Apps PR   apache/nuttx-apps#2458 — "testing/ostest: add nxevent test into ostest"
#
# Validation logic:
#   FAIL: include/nuttx/event.h is new in PR #12802. The apps test nxevent.c
#         (added by #2458) includes it. On the base kernel commit the header
#         does not exist → guaranteed compile error.
#   PASS: after applying the kernel fix patch, event.h and sched/event/* are
#         present. With CONFIG_SCHED_EVENTS=y enabled, the build succeeds and
#         ostest runs the nxevent test cleanly.
#
# Usage: ./scripts/run_12802.sh   (run from nuttx-sim-runs/)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/nuttx__nuttx-12802"
RESULTS_DIR="${REPO_ROOT}/results/nuttx__nuttx-12802"

KERNEL_BASE_COMMIT="93b520f7b05a843512529b617abb2a1f79a64ffd"
KERNEL_MERGE_COMMIT="aedef710706d8986f3b19ca061c241cb6729c4f8"
APPS_BASE_COMMIT="ab67cb1911942732e89bd57d3a6015b91acb1b06"
APPS_MERGE_COMMIT="054edfc653cd41e1d036225c37737b7505e2c448"

IMAGE="nuttx-embedbench:nuttx-12802"
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

# test_patch: apps PR diff with CONFIG_SCHED_EVENTS guards stripped.
# We clone at the merge commit, remove the guards directly from the source
# files, then re-diff against the base commit so hunk headers are always valid.
git clone --filter=blob:none \
    https://github.com/apache/nuttx-apps.git \
    "${WORK_DIR}/nuttx-apps" -q
cd "${WORK_DIR}/nuttx-apps"
git fetch origin "${APPS_BASE_COMMIT}" "${APPS_MERGE_COMMIT}" -q
git checkout "${APPS_MERGE_COMMIT}" -q

# Remove outer CONFIG_SCHED_EVENTS ifeq/endif from Makefile
python3 - testing/ostest/Makefile <<'PYEOF'
import sys, re
text = open(sys.argv[1]).read()
# Remove "ifeq ($(CONFIG_SCHED_EVENTS),y)\n" and its matching closing "endif\n"
# The block is: ifeq SCHED_EVENTS \n ifeq BUILD_FLAT \n CSRCS... \n endif \n endif
text = re.sub(
    r'ifeq \(\$\(CONFIG_SCHED_EVENTS\),y\)\n(ifeq \(\$\(CONFIG_BUILD_FLAT\),y\)\nCSRCS \+= nxevent\.c\nendif\n)endif\n',
    r'\1',
    text
)
open(sys.argv[1], 'w').write(text)
PYEOF

# Remove CONFIG_SCHED_EVENTS from the #if in ostest_main.c
sed -i '' \
    's/#if defined(CONFIG_SCHED_EVENTS) && defined(CONFIG_BUILD_FLAT)/#if defined(CONFIG_BUILD_FLAT)/' \
    testing/ostest/ostest_main.c

# Generate clean diff — git recalculates all hunk headers correctly
git diff "${APPS_BASE_COMMIT}" -- testing/ > "${WORK_DIR}/test_patch.diff"
echo "test_patch.diff: $(wc -l < "${WORK_DIR}/test_patch.diff") lines"

# fix_patch: kernel PR diff — adds event.h, sched/event/*, Kconfig, Makefile
# Exclude docs (not needed for build)
git clone --filter=blob:none --no-checkout \
    https://github.com/apache/nuttx.git \
    "${WORK_DIR}/nuttx-kernel" -q
cd "${WORK_DIR}/nuttx-kernel"
git fetch origin "${KERNEL_MERGE_COMMIT}" -q
git diff "${KERNEL_BASE_COMMIT}..${KERNEL_MERGE_COMMIT}" \
    -- include/ sched/ boards/ \
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

# Clone kernel at base commit (include/nuttx/event.h does not exist yet)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at base commit (before nxevent test was added)
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# Configure sim:nsh, then set the options we need.
# Strip any existing occurrences first to avoid duplicate-definition errors
# in kconfig-frontends, then append the desired values before olddefconfig.
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# Apply test patch — adds nxevent.c which includes <nuttx/event.h>
COPY test_patch.diff /tmp/test_patch.diff
RUN cd apps && git apply /tmp/test_patch.diff

# Pre-build: EXPECTED to fail — event.h does not exist at base kernel commit
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

# ── Step 5a: Verify FAIL — compile error on base commit ──────────────────────
echo ""
echo "=== Step 5a: Verifying compile error on base commit ==="
echo "(expect: fatal error: nuttx/event.h: No such file or directory)"

CID=$(docker run -d "${IMAGE}" sleep infinity)

FAIL_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    make -j\$(nproc) 2>&1
" 2>&1 || true)

echo "${FAIL_OUTPUT}" | tee "${RESULTS_DIR}/fail_build.log" | tail -20

if echo "${FAIL_OUTPUT}" | grep -qE "event\.h.*No such file|No such file.*event\.h|nxevent.*error|error.*nxevent"; then
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

# ── Step 5b: Apply fix, rebuild, verify PASS ──────────────────────────────────
echo ""
echo "=== Step 5b: Applying kernel fix and verifying ostest passes ==="

docker cp "${WORK_DIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

FIX_BUILD_OUTPUT=$(docker exec "${CID}" bash -c "
    cd /testbed
    git apply /tmp/fix_patch.diff
    echo 'CONFIG_SCHED_EVENTS=y' >> .config
    make olddefconfig
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
if [ "${FAIL_STATUS}" = "compile_error" ] || [ "${FAIL_STATUS}" = "build_error" ]; then
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
    "instance_id": "nuttx__nuttx-12802",
    "status": "${STATUS}",
    "fail_status": "${FAIL_STATUS}",
    "pass_status": "${PASS_STATUS}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo ""
echo "=== Result: ${STATUS} ==="
echo "Logs written to ${RESULTS_DIR}/"
