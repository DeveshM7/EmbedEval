#!/usr/bin/env bash
# Validates that a RIOT instance image has the expected fail-then-pass behavior.
# Step 1: Verify the failing tests FAIL on the broken code (pre-fix state).
# Step 2: Apply the fix patch and verify tests pass.
#
# Usage:
#   ./scripts/validate_riot_instance.sh <instance_id> [fix_patch.diff]
# Examples:
#   ./scripts/validate_riot_instance.sh riot__riot-20857
#   ./scripts/validate_riot_instance.sh riot__riot-20197
#   ./scripts/validate_riot_instance.sh riot__riot-20857 outputs/my.patch

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

INSTANCE_ID="${1:-}"
PATCH_OVERRIDE="${2:-}"

if [ -z "${INSTANCE_ID}" ]; then
    echo "Usage: $0 <instance_id> [fix_patch.diff]"
    echo "Example: $0 riot__riot-20857"
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
FILES_CHANGED=$(python3 -c "import json; d=json.load(open('${METADATA}')); print(' '.join(d.get('files_changed_by_fix', [])))")
UNIT_TESTS=$(python3 -c "import json; d=json.load(open('${METADATA}')); args=d.get('extra_make_args',[]); ut=[a.replace('UNIT_TESTS=','') for a in args if a.startswith('UNIT_TESTS=')]; print(ut[0] if ut else '')")

DOCKER_PLATFORM=$(python3 -c "import json; d=json.load(open('${METADATA}')); print(d.get('docker_platform', ''))")
PLATFORM_FLAG=""
if [ -n "${DOCKER_PLATFORM}" ]; then
    PLATFORM_FLAG="--platform ${DOCKER_PLATFORM}"
fi

echo "=== Starting container from ${IMAGE} ==="
CID=$(docker run -d ${PLATFORM_FLAG} "${IMAGE}" sleep infinity)

TMPDIR_WORK="$(mktemp -d)"
cleanup() {
    echo "Stopping container..."
    docker stop "${CID}" 2>/dev/null && docker rm "${CID}" 2>/dev/null || true
    rm -rf "${TMPDIR_WORK}"
}
trap cleanup EXIT

echo ""
echo "=== Step 1: Verifying tests FAIL on broken code ==="
set +e
docker exec "${CID}" bash -c "
    cd /testbed
    make -C tests/unittests BOARD=native clean all test UNIT_TESTS=${UNIT_TESTS}
"
STEP1_RC=$?
set -e

if [ "${STEP1_RC}" -eq 0 ]; then
    echo "ERROR: Step 1 should have FAILED but exited 0!"
    exit 1
else
    echo "SUCCESS: Step 1 correctly failed (exit code ${STEP1_RC})."
fi

echo ""
echo "=== Step 2: Applying fix and verifying tests PASS ==="

if [ -n "${PATCH_OVERRIDE}" ]; then
    echo "Using provided patch: ${PATCH_OVERRIDE}"
    docker cp "${PATCH_OVERRIDE}" "${CID}:/tmp/fix_patch.diff"
else
    BASE_COMMIT=$(python3 -c "import json; d=json.load(open('${METADATA}')); print(d['base_commit'])")
    echo "Fetching fix diff from GitHub (base=${BASE_COMMIT}, fix=${FIX_COMMIT})..."
    git clone --filter=blob:none --no-checkout \
        https://github.com/RIOT-OS/RIOT.git \
        "${TMPDIR_WORK}/riot" -q
    cd "${TMPDIR_WORK}/riot"
    git fetch origin "${FIX_COMMIT}" -q
    # Diff base_commit..fix_commit for only the source files (not tests).
    # This handles multi-commit PRs where the fix spans several commits.
    if [ -n "${FILES_CHANGED}" ]; then
        git diff "${BASE_COMMIT}..${FIX_COMMIT}" -- ${FILES_CHANGED} > "${TMPDIR_WORK}/fix_patch.diff"
    else
        git diff "${BASE_COMMIT}..${FIX_COMMIT}" -- ':(exclude)tests/' > "${TMPDIR_WORK}/fix_patch.diff"
    fi
    cd "${REPO_ROOT}"
    docker cp "${TMPDIR_WORK}/fix_patch.diff" "${CID}:/tmp/fix_patch.diff"
fi

echo "Applying fix and rebuilding..."
docker exec "${CID}" bash -c "
    cd /testbed
    git apply --exclude="Makefile.include" --exclude="cpu/native/include/native_internal.h" /tmp/fix_patch.diff
    make -C tests/unittests BOARD=native clean all test UNIT_TESTS=${UNIT_TESTS}
"

echo ""
echo "SUCCESS: Step 2 passed — tests pass after applying the fix."
echo ""
echo "=== Validation complete ==="
