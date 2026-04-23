#!/usr/bin/env bash
# Builds per-instance Docker images for RIOT instances under docker/instances/.
# Requires: embedbench-riot-base:latest already built.
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

    PROJECT=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d['project'])")
    BASE_COMMIT=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d['base_commit'])")
    DOCKER_IMAGE=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d['docker_image'])")

    if [ "${PROJECT}" != "riot" ]; then
        echo "SKIP: ${INSTANCE_ID} (project=${PROJECT}; handled by scripts/build_instance_images.sh)"
        continue
    fi

    BOARD=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d.get('board', 'native'))")
    UNIT_TESTS=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(' '.join(d.get('extra_make_args', [])))" \
        | sed 's/UNIT_TESTS=//')

    DOCKER_PLATFORM=$(python3 -c "import json; d=json.load(open('${INSTANCE_DIR}/metadata.json')); print(d.get('docker_platform', ''))")
    PLATFORM_FLAG=""
    if [ -n "${DOCKER_PLATFORM}" ]; then
        PLATFORM_FLAG="--platform ${DOCKER_PLATFORM}"
    fi

    echo "Building ${DOCKER_IMAGE} (${INSTANCE_ID}, project=${PROJECT}) ..."

    docker build \
        ${PLATFORM_FLAG} \
        --build-arg BASE_COMMIT="${BASE_COMMIT}" \
        --build-arg BOARD="${BOARD}" \
        --build-arg UNIT_TESTS="${UNIT_TESTS}" \
        -t "${DOCKER_IMAGE}" \
        "${INSTANCE_DIR}"

    echo "Done: ${DOCKER_IMAGE}"
done
