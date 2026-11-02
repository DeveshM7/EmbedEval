#!/usr/bin/env bash
# Run NuttX ostest on the sim target and exit as soon as results are available.
#
# The NuttX sim binary runs as a native Linux process. It never exits on its
# own — this script pipes NSH commands in, watches stdout for the ostest
# completion string, then kills the process cleanly.
#
# Usage (from /testbed):
#   run_tests
#   run_tests [timeout_seconds]   (default: 300)
#
# Exit codes:
#   0  — ostest_main: Exiting with status 0  (all tests passed)
#   1  — ostest_main: Exiting with status N  (one or more failures)
#   2  — timed out before any completion string was seen

set -euo pipefail

TIMEOUT="${1:-300}"
OUTFILE="$(mktemp /tmp/nuttx_test_output.XXXXXX)"
trap 'rm -f "${OUTFILE}"' EXIT

cd /testbed

# Pipe ostest + poweroff into the sim binary.
# setsid gives the binary its own session so the kill below only affects
# the sim process group, not this script itself.
printf 'ostest\npoweroff\n' | setsid ./nuttx > "${OUTFILE}" 2>&1 &
SIM_PID=$!
SIM_PGID="${SIM_PID}"

# Stream output to terminal live while watching for completion.
tail -f "${OUTFILE}" &
TAIL_PID=$!

ELAPSED=0
RESULT=""
while [ "${ELAPSED}" -lt "${TIMEOUT}" ]; do
    if grep -q "Exiting with status 0" "${OUTFILE}" 2>/dev/null; then
        RESULT="PASS"
        break
    elif grep -qE "Exiting with status [^0]" "${OUTFILE}" 2>/dev/null; then
        RESULT="FAIL"
        break
    fi
    sleep 1
    ELAPSED=$((ELAPSED + 1))
done

# Stop tail and kill the sim process group
kill "${TAIL_PID}" 2>/dev/null || true
kill -- "-${SIM_PGID}" 2>/dev/null || true
wait "${SIM_PID}" 2>/dev/null || true

echo ""
echo "--- Test Results ---"
grep -E "Exiting with status|FAILED|PASSED" "${OUTFILE}" || true
echo "--------------------"

case "${RESULT}" in
    PASS)
        echo "==> run_tests: ostest_main: Exiting with status 0 — PASSED"
        exit 0
        ;;
    FAIL)
        echo "==> run_tests: ostest_main exited with non-zero status — FAILED"
        exit 1
        ;;
    *)
        echo "==> run_tests: TIMEOUT after ${TIMEOUT}s — no completion string seen"
        exit 2
        ;;
esac
