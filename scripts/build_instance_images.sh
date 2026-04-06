#!/usr/bin/env bash
# Builds per-instance Docker images for all instances under docker/instances/.
# Requires: embedbench-zephyr-base:latest already built.
# Each instance directory must contain a Dockerfile, test_patch.diff, and metadata.json.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCES_DIR="${REPO_ROOT}/docker/instances"

# Optional: build only a specific instance if passed as argument
FILTER="${1:-}"

for INSTANCE_DIR in "${INSTANCES_DIR}"/*/; do
    INSTANCE_ID="$(basename "${INSTANCE_DIR}")"

    if [ -n "${FILTER}" ] && [ "${INSTANCE_ID}" != "${FILTER}" ]; then
        continue
    fi

    if [ ! -f "${INSTANCE_DIR}/Dockerfile" ]; then
        echo "SKIP: ${INSTANCE_ID} (no Dockerfile)"
        continue
    fi
    if [ ! -f "${INSTANCE_DIR}/test_patch.diff" ]; then
        echo "ERROR: ${INSTANCE_ID} missing test_patch.diff — skipping"
        continue
    fi
    if [ ! -f "${INSTANCE_DIR}/metadata.json" ]; then
        echo "ERROR: ${INSTANCE_ID} missing metadata.json — skipping"
        continue
    fi

    # Read fields from metadata.json
    BASE_COMMIT=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d['base_commit'])")
    DOCKER_IMAGE=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d['docker_image'])")
    PLATFORM=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d['platform'])")
    TEST_PATH=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d['test_path'])")

    echo "Building ${DOCKER_IMAGE} (${INSTANCE_ID}) ..."
    docker build \
        --build-arg BASE_COMMIT="${BASE_COMMIT}" \
        --build-arg PLATFORM="${PLATFORM}" \
        --build-arg TEST_PATH="${TEST_PATH}" \
        -t "${DOCKER_IMAGE}" \
        "${INSTANCE_DIR}"

    echo "Done: ${DOCKER_IMAGE}"
done
