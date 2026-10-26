#!/usr/bin/env bash
# 0 = all pass, 1 = test/runtime failure, 2 = timeout.
set -euo pipefail
cd /testbed
TIMEOUT="${1:-120}"
rc=0
timeout "${TIMEOUT}" python3 tests/test_otci_pending_lines.py -v || rc=$?
if [ "$rc" -eq 124 ]; then
    echo "===> run_tests: TIMEOUT after ${TIMEOUT}s"
    exit 2
elif [ "$rc" -ne 0 ]; then
    echo "===> run_tests: TEST OR RUNTIME FAILURE"
    exit 1
fi
echo "===> run_tests: ALL TESTS PASSED"
