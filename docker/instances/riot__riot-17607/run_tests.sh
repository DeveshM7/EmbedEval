#!/usr/bin/env bash
# Run RIOT unittests and report pass/fail.
#
# RIOT unittests for the "native" board compile to a host-native ELF binary.
# The test harness sends 's' to stdin to start, then parses the output for
# "OK (N tests)" (pass) or "run N failures M" (fail).
#
# Usage (from /testbed):
#   run_tests
#   run_tests [timeout_seconds]   (default: 120)
#
# Exit codes:
#   0  — all tests passed
#   1  — one or more tests failed
#   2  — timed out

set -euo pipefail

TIMEOUT="${1:-120}"
UNIT_TESTS="${UNIT_TESTS:-tests-ztimer}"

cd /testbed

# Build and run via make. RIOT's test target sends 'r' then 's' automatically.
OUTPUT=$(timeout "${TIMEOUT}" make -C tests/unittests \
    BOARD=native clean all test \
    UNIT_TESTS="${UNIT_TESTS}" 2>&1) || RC=$?
RC=${RC:-0}

echo "${OUTPUT}"

echo ""
echo "--- Test Results ---"

if echo "${OUTPUT}" | grep -q "OK ("; then
    TESTS_LINE=$(echo "${OUTPUT}" | grep "OK (")
    echo "${TESTS_LINE}"
    echo "===> run_tests: ALL TESTS PASSED"
    exit 0
elif echo "${OUTPUT}" | grep -q "failures"; then
    FAIL_LINE=$(echo "${OUTPUT}" | grep "failures")
    echo "${FAIL_LINE}"
    echo "===> run_tests: TEST FAILURES DETECTED"
    exit 1
elif [ "${RC}" -eq 124 ]; then
    echo "===> run_tests: TIMEOUT after ${TIMEOUT}s"
    exit 2
else
    echo "===> run_tests: UNEXPECTED EXIT (rc=${RC})"
    exit 1
fi
