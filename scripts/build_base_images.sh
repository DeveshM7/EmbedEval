#!/usr/bin/env bash
# Builds the shared Zephyr base image.
# Run this once (or when the base Dockerfile changes).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "Building embedbench-zephyr-base:latest ..."
docker build \
    -f "${REPO_ROOT}/docker/bases/zephyr.Dockerfile" \
    -t embedbench-zephyr-base:latest \
    "${REPO_ROOT}/docker/bases/"

echo "Done: embedbench-zephyr-base:latest"
