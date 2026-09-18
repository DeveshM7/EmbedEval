#!/usr/bin/env bash
# Builds the shared RIOT base image.
# Run this once (or when the RIOT base Dockerfile changes).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "Building embedeval-riot-base:latest ..."
docker build \
    --platform linux/amd64 \
    -f "${REPO_ROOT}/docker/bases/riot.Dockerfile" \
    -t embedeval-riot-base:latest \
    "${REPO_ROOT}/docker/bases/"

echo "Done: embedeval-riot-base:latest"

echo ""
echo "Building embedeval-riot-legacy-base:latest ..."
docker build \
    --platform linux/amd64 \
    -f "${REPO_ROOT}/docker/bases/riot_legacy.Dockerfile" \
    -t embedeval-riot-legacy-base:latest \
    "${REPO_ROOT}/docker/bases/"

echo "Done: embedeval-riot-legacy-base:latest"
