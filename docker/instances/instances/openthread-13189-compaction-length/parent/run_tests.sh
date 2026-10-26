#!/usr/bin/env bash
# 0 = all pass, 1 = build/test failure, 2 = timeout.
set -euo pipefail
cd /testbed
TIMEOUT="${1:-120}"
rc=0
timeout "${TIMEOUT}" bash -c '
    set -euo pipefail
    mkdir -p build
    c++ -std=c++11 -Wall -Wextra -Werror -O0 -g \
        -Iinclude -Isrc -Isrc/include -Itests/unit \
        tests/unit/test_multi_frame_buffer_recovery.cpp \
        -o build/ot-test-multi-frame-buffer-recovery
    failed=0
    for test in normal_iteration append_during_iteration clear_empty \
        clear_shorter clear_same_boundary clear_longer clear_saved_frames; do
        if ./build/ot-test-multi-frame-buffer-recovery "$test"; then
            echo "PASS: $test"
        else
            echo "FAIL: $test"
            failed=1
        fi
    done
    exit "$failed"
' || rc=$?
if [ "$rc" -eq 124 ]; then
    echo "===> run_tests: TIMEOUT after ${TIMEOUT}s"
    exit 2
elif [ "$rc" -ne 0 ]; then
    echo "===> run_tests: TEST OR BUILD FAILURE"
    exit 1
fi
echo "===> run_tests: ALL TESTS PASSED"
