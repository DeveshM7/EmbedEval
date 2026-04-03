#!/usr/bin/env bash
# Generates test_patch.diff for instance zephyr__zephyr-65697.
#
# This script clones the Zephyr repo (blobless, so fast) into a temp dir,
# computes the base commit (parent of the fix), extracts only the test-file
# changes introduced by the test commit, and writes the diff into place.
#
# Run this ONCE before building the Docker instance image.
# Output: docker/instances/zephyr__zephyr-65697/test_patch.diff

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT="${REPO_ROOT}/docker/instances/zephyr__zephyr-65697/test_patch.diff"

FIX_COMMIT="330c820b9dad6a417431b98a20c97baa32d7dc43"
TEST_COMMIT="ba723889f47a5685b564052ed7ab7754f4e80fc6"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

echo "Cloning Zephyr (blobless) into ${TMPDIR}/zephyr ..."
git clone --filter=blob:none --no-checkout \
    https://github.com/zephyrproject-rtos/zephyr.git \
    "${TMPDIR}/zephyr"

cd "${TMPDIR}/zephyr"

# Fetch both commits explicitly in case they aren't reachable yet
# (blobless clones sometimes need an explicit fetch for specific SHAs)
echo "Fetching fix and test commits ..."
git fetch origin "${FIX_COMMIT}" "${TEST_COMMIT}"

BASE_COMMIT="$(git rev-parse "${FIX_COMMIT}~1")"
echo "Base commit (pre-fix): ${BASE_COMMIT}"

# Sanity check: confirm BASE_COMMIT matches what's in metadata.json
EXPECTED_BASE="46ecf540f37e60d601a52f5df06d0c4e4ee58b7a"
if [ "${BASE_COMMIT}" != "${EXPECTED_BASE}" ]; then
    echo "WARNING: computed base commit ${BASE_COMMIT} does not match"
    echo "         expected ${EXPECTED_BASE} from metadata.json."
    echo "         Update metadata.json base_commit and the Dockerfile ARG if needed."
fi

echo "Generating test-only diff (tests/ changes between base and test commit) ..."
git diff "${BASE_COMMIT}..${TEST_COMMIT}" -- tests/ > "${OUTPUT}"

if [ ! -s "${OUTPUT}" ]; then
    echo "ERROR: generated diff is empty. Check that TEST_COMMIT touches files under tests/."
    exit 1
fi

echo "Written: ${OUTPUT}"
echo "Lines in diff: $(wc -l < "${OUTPUT}")"
