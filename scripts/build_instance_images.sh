#!/usr/bin/env bash
# Builds the per-instance Docker image for zephyr__zephyr-65697.
# Requires: embedbench-zephyr-base:latest already built, and
#           test_patch.diff already generated (run generate_test_patch.sh first).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTANCE_DIR="${REPO_ROOT}/docker/instances/zephyr__zephyr-65697"

if [ ! -f "${INSTANCE_DIR}/test_patch.diff" ]; then
    echo "ERROR: test_patch.diff not found."
    echo "Run scripts/generate_test_patch.sh first."
    exit 1
fi

echo "Building embedbench:zephyr-65697 ..."
docker build \
    --build-arg BASE_COMMIT=41b7c17ac4bd0198fc9bb3a0e55aa8d5e2fae96e \
    --build-arg ZEPHYR_SDK_VERSION=0.16.8 \
    --build-arg PLATFORM=qemu_x86 \
    --build-arg TEST_PATH=tests/posix/common \
    -t embedbench:zephyr-65697 \
    "${INSTANCE_DIR}"

echo "Done: embedbench:zephyr-65697"
