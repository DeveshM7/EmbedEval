#!/usr/bin/env bash
# Builds the shared RIOT base image.
# Run this once (or when the RIOT base Dockerfile changes).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "Building embedbench-riot-base:latest ..."
docker build \
    --platform linux/amd64 \
    -f "${REPO_ROOT}/docker/bases/riot.Dockerfile" \
    -t embedbench-riot-base:latest \
    "${REPO_ROOT}/docker/bases/"

echo "Done: embedbench-riot-base:latest"
