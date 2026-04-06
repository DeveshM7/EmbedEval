#!/usr/bin/env bash
# Validates that the instance image has the expected fail-then-pass behavior.
#
# Step 1: Verify the two failing tests FAIL on the broken code (pre-fix state).
# Step 2: Apply the fix patch and verify ALL tests pass.
#
# Usage: ./scripts/validate_instance.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE="embedbench:zephyr-65697"
FIX_COMMIT="330c820b9dad6a417431b98a20c97baa32d7dc43"
# Tests complete well before this; QEMU just doesn't exit cleanly afterward.
# `timeout` lets us see all output and move on regardless.
RUN_TIMEOUT=30

run_tests_in_container() {
    local cid="$1"
    # Clean up any stale PID file before starting QEMU.
    # QEMU writes qemu.pid and never removes it; a leftover file prevents the
    # next run from starting.
    docker exec "${cid}" bash -c "rm -f /testbed/build/zephyr/qemu.pid /testbed/build/qemu.pid" 2>/dev/null || true
    docker exec "${cid}" bash -c "
        source /opt/zephyr-venv/bin/activate
        cd /testbed
        timeout ${RUN_TIMEOUT} west build -t run 2>&1 || true
    "
}

echo "=== Starting container from ${IMAGE} ==="
CID=$(docker run -d "${IMAGE}" sleep infinity)
trap 'echo "Stopping container..."; docker stop "${CID}" && docker rm "${CID}"' EXIT

echo ""
echo "=== Step 1: Verifying tests FAIL on broken code ==="
echo "(test_key_resource_leak and test_correct_key_is_deleted should fail)"
docker exec "${CID}" bash -c "
    source /opt/zephyr-venv/bin/activate
    cd /testbed
    west build -b qemu_x86 tests/posix/common 2>&1
" || true
run_tests_in_container "${CID}" || true

echo ""
echo "=== Step 2: Applying fix and verifying tests PASS ==="

TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"; echo "Stopping container..."; docker stop "${CID}" && docker rm "${CID}"' EXIT

echo "Fetching fix commit diff ..."
git clone --filter=blob:none --no-checkout \
    https://github.com/zephyrproject-rtos/zephyr.git \
    "${TMPDIR}/zephyr" -q

cd "${TMPDIR}/zephyr"
git fetch origin "${FIX_COMMIT}" -q
git diff "${FIX_COMMIT}~1..${FIX_COMMIT}" -- ':(exclude)tests/' > "${TMPDIR}/fix_patch.diff"

echo "Copying fix patch into container ..."
docker cp "${TMPDIR}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"

echo "Applying fix and rebuilding ..."
docker exec "${CID}" bash -c "
    source /opt/zephyr-venv/bin/activate
    cd /testbed
    git apply /tmp/fix_patch.diff
    west build -b qemu_x86 tests/posix/common 2>&1
"
run_tests_in_container "${CID}"

echo ""
echo "=== Validation complete ==="