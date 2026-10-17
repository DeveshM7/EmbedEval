#!/usr/bin/env bash
# 0 = all pass, 1 = build/test failure, 2 = timeout.
set -euo pipefail
cd /testbed
TIMEOUT="${1:-600}"
rc=0
timeout "${TIMEOUT}" bash -c '
    set -euo pipefail
    cmake -S . -B build -G Ninja -DOT_PLATFORM=external -DOT_BUILD_GTEST=ON -DOT_APP_CLI=OFF -DOT_APP_NCP=OFF -DOT_APP_RCP=OFF -DOT_MTD=OFF -DOT_RCP=OFF -DCMAKE_BUILD_TYPE=Debug
    cmake --build build --target ot-ftd-gtest -j$(nproc)
    ./build/tests/gtest/ot-ftd-gtest --gtest_filter="DatasetManagerSave.*:otDatasetSetActiveTlvs.*:otDatasetIsValid.*"
' || rc=$?
if [ "$rc" -eq 124 ]; then
    echo "===> run_tests: TIMEOUT after ${TIMEOUT}s"
    exit 2
elif [ "$rc" -ne 0 ]; then
    echo "===> run_tests: TEST OR BUILD FAILURE"
    exit 1
fi
echo "===> run_tests: ALL TESTS PASSED"
