#!/usr/bin/env bash
# Run Zephyr tests on native_sim and exit as soon as results are available.
#
# native_sim runs the test binary as a host process that exits cleanly when
# all tests are done. No PID-file cleanup needed.
#
# Usage (from /testbed):
#   run_tests
#   run_tests [timeout_seconds]   (default: 120)
#
# Exit codes:
#   0  — PROJECT EXECUTION SUCCESSFUL
#   1  — PROJECT EXECUTION FAILED
#   2  — timed out before any result was printed

set -euo pipefail

TIMEOUT="${1:-120}"
cd /testbed

# Rebuild (in case pre-build cache is missing or stale), then run.
west build -b native_sim/native/64 tests/kernel/sched/wraparound 2>&1 || true
OUTPUT=$(timeout "${TIMEOUT}" west build -t run 2>&1) || RC=$?
RC=${RC:-0}

echo "${OUTPUT}"

echo ""
echo "--- Test Results (PASS/FAIL per test) ---"
echo "${OUTPUT}" | grep -E "^ (PASS|FAIL) - " || true
echo "-----------------------------------------"

if echo "${OUTPUT}" | grep -q "PROJECT EXECUTION SUCCESSFUL"; then
    echo "==> run_tests: PROJECT EXECUTION SUCCESSFUL"
    exit 0
elif echo "${OUTPUT}" | grep -q "PROJECT EXECUTION FAILED"; then
    echo "==> run_tests: PROJECT EXECUTION FAILED"
    exit 1
elif [ "${RC}" -eq 124 ]; then
    echo "==> run_tests: TIMEOUT after ${TIMEOUT}s — no completion string seen"
    exit 2
else
    echo "==> run_tests: UNEXPECTED EXIT (rc=${RC})"
    exit 1
fi
