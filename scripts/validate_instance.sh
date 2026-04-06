#!/usr/bin/env bash
# Validates that an instance image has the expected fail-then-pass behavior.
# Step 1: Verify the failing tests FAIL on the broken code (pre-fix state).
# Step 2: Apply the fix patch and verify tests pass.
# Usage:
#   ./scripts/validate_instance.sh <instance_id> [fix_patch.diff]
# Examples:
#   ./scripts/validate_instance.sh zephyr__zephyr-65697
#   ./scripts/validate_instance.sh zephyr__zephyr-43405
#   ./scripts/validate_instance.sh zephyr__zephyr-65697 outputs/my.patch   # test a specific patch

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

INSTANCE_ID="${1:-}"
PATCH_OVERRIDE="${2:-}"

if [ -z "${INSTANCE_ID}" ]; then
    echo "Usage: $0 <instance_id> [fix_patch.diff]"
    echo "Example: $0 zephyr__zephyr-65697"
    exit 1
fi

METADATA="${REPO_ROOT}/docker/instances/${INSTANCE_ID}/metadata.json"
if [ ! -f "${METADATA}" ]; then
    echo "ERROR: metadata not found at ${METADATA}"
    exit 1
fi

# Read fields from metadata
IMAGE=$(python3 -c "import json; d=json.load(open('${METADATA}')); print(d['docker_image'])")
FIX_COMMIT=$(python3 -c "import json; d=json.load(open('${METADATA}')); print(d['fix_commit'])")
PLATFORM=$(python3 -c "import json; d=json.load(open('${METADATA}')); print(d['platform'])")
TEST_PATH=$(python3 -c "import json; d=json.load(open('${METADATA}')); print(d['test_path'])")
FILES_CHANGED=$(python3 -c "import json; d=json.load(open('${METADATA}')); print(' '.join(d.get('files_changed_by_fix', [])))")

echo "=== Starting container from ${IMAGE} ==="
CID=$(docker run -d "${IMAGE}" sleep infinity)

TMPDIR_WORK="$(mktemp -d)"
cleanup() {
    echo "Stopping container..."
    docker stop "${CID}" 2>/dev/null && docker rm "${CID}" 2>/dev/null || true
    rm -rf "${TMPDIR_WORK}"
}
trap cleanup EXIT

build_in_container() {
    docker exec "${CID}" bash -c "
        source /opt/zephyr-venv/bin/activate
        cd /testbed
        west build -b ${PLATFORM} ${TEST_PATH} 2>&1
    "
}

run_tests_in_container() {
    docker exec "${CID}" bash -c "
        source /opt/zephyr-venv/bin/activate
        cd /testbed
        run_tests
    "
}

echo ""
echo "=== Step 1: Verifying tests FAIL on broken code ==="
build_in_container || true
run_tests_in_container || true

echo ""
echo "=== Step 2: Applying fix and verifying tests PASS ==="

if [ -n "${PATCH_OVERRIDE}" ]; then
    echo "Using provided patch: ${PATCH_OVERRIDE}"
    docker cp "${PATCH_OVERRIDE}" "${CID}:/tmp/fix_patch.diff"
else
    echo "Fetching fix commit diff from GitHub..."
    git clone --filter=blob:none --no-checkout \
        https://github.com/zephyrproject-rtos/zephyr.git \
        "${TMPDIR_WORK}/zephyr" -q
    cd "${TMPDIR_WORK}/zephyr"
    git fetch origin "${FIX_COMMIT}" -q
    # Only diff the files the fix should touch (excludes test files)
    if [ -n "${FILES_CHANGED}" ]; then
        git diff "${FIX_COMMIT}~1..${FIX_COMMIT}" -- ${FILES_CHANGED} > "${TMPDIR_WORK}/fix_patch.diff"
    else
        git diff "${FIX_COMMIT}~1..${FIX_COMMIT}" -- ':(exclude)tests/' > "${TMPDIR_WORK}/fix_patch.diff"
    fi
    cd "${REPO_ROOT}"
    docker cp "${TMPDIR_WORK}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"
fi

echo "Applying fix and rebuilding..."
docker exec "${CID}" bash -c "
    source /opt/zephyr-venv/bin/activate
    cd /testbed
    git apply /tmp/fix_patch.diff
    rm -rf build
    west build -b ${PLATFORM} ${TEST_PATH} 2>&1
"
run_tests_in_container

echo ""
echo "=== Validation complete ==="
