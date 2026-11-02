#!/usr/bin/env bash
# Run NuttX ostest on the sim target and exit as soon as results are available.
#
# The setjmp_test added by apps PR #2725 calls longjmp(buf, 0) and asserts
# that setjmp returns 1. On the base kernel the assert fires and ostest aborts
# before reaching "Exiting with status 0", so a timeout here signals failure.
# After the fix, ostest completes cleanly and "setjmp_test: Jump succeed"
# appears in the output.
#
# Usage (from /testbed):
#   run_tests
#   run_tests [timeout_seconds]   (default: 300)
#
# Exit codes:
#   0  — setjmp_test: Jump succeed + ostest_main: Exiting with status 0
#   1  — ostest_main: Exiting with status N  (non-zero failure)
#   2  — timed out before any completion string was seen

set -euo pipefail

TIMEOUT="${1:-300}"
OUTFILE="$(mktemp /tmp/nuttx_test_output.XXXXXX)"
trap 'rm -f "${OUTFILE}"' EXIT

cd /testbed

printf 'ostest\npoweroff\n' | setsid ./nuttx > "${OUTFILE}" 2>&1 &
SIM_PID=$!
SIM_PGID="${SIM_PID}"

tail -f "${OUTFILE}" &
TAIL_PID=$!

ELAPSED=0
RESULT=""
while [ "${ELAPSED}" -lt "${TIMEOUT}" ]; do
    if grep -q "Exiting with status 0" "${OUTFILE}" 2>/dev/null; then
        # Verify the setjmp fix specifically — "Jump succeed" must appear
        if grep -q "setjmp_test: Jump succeed" "${OUTFILE}" 2>/dev/null; then
            RESULT="PASS"
        else
            RESULT="FAIL"
        fi
        break
    elif grep -qE "Exiting with status [^0]" "${OUTFILE}" 2>/dev/null; then
        RESULT="FAIL"
        break
    fi
    sleep 1
    ELAPSED=$((ELAPSED + 1))
done

kill "${TAIL_PID}" 2>/dev/null || true
kill -- "-${SIM_PGID}" 2>/dev/null || true
wait "${SIM_PID}" 2>/dev/null || true

echo ""
echo "--- Test Results ---"
grep -E "setjmp_test|Exiting with status" "${OUTFILE}" || true
echo "--------------------"

case "${RESULT}" in
    PASS)
        echo "==> run_tests: setjmp_test: Jump succeed + Exiting with status 0 — PASSED"
        exit 0
        ;;
    FAIL)
        echo "==> run_tests: ostest failed or setjmp_test did not pass — FAILED"
        exit 1
        ;;
    *)
        echo "==> run_tests: TIMEOUT after ${TIMEOUT}s — ostest did not complete (base kernel bug?)"
        exit 2
        ;;
esac
