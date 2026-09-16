#!/usr/bin/env bash
# Run NuttX ostest on the sim target and exit as soon as results are available.
#
# cancel_test (PR #2329) now asserts that pthread_join on a detached thread
# returns EINVAL. On the base kernel it returns ESRCH, so cancel_test prints
# "ERROR pthread_join failed but with wrong status=3" — but crucially does NOT
# call ASSERT(false) in this branch, so ostest still exits 0. A plain exit-code
# check gives a false pass; this script checks for the specific PASS/FAIL string.
#
# Usage (from /testbed):
#   run_tests
#   run_tests [timeout_seconds]   (default: 300)
#
# Exit codes:
#   0  — cancel_test: PASS pthread_join failed with status=EINVAL + ostest exits 0
#   1  — cancel_test ERROR detected (wrong errno returned by kernel)
#   2  — timed out before ostest completed

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
    # Detect the wrong-errno failure early — no need to wait for ostest to finish
    if grep -q "cancel_test: ERROR pthread_join failed but with wrong status" "${OUTFILE}" 2>/dev/null; then
        RESULT="FAIL"
        break
    fi
    if grep -q "Exiting with status 0" "${OUTFILE}" 2>/dev/null; then
        # ostest completed — verify cancel_test reached its PASS line
        if grep -q "cancel_test: PASS pthread_join failed with status=EINVAL" "${OUTFILE}" 2>/dev/null; then
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
grep -E "cancel_test.*join|Exiting with status" "${OUTFILE}" || true
echo "--------------------"

case "${RESULT}" in
    PASS)
        echo "==> run_tests: cancel_test: PASS pthread_join failed with status=EINVAL — PASSED"
        exit 0
        ;;
    FAIL)
        echo "==> run_tests: cancel_test returned wrong errno or ostest failed — FAILED"
        exit 1
        ;;
    *)
        echo "==> run_tests: TIMEOUT after ${TIMEOUT}s — ostest did not complete"
        exit 2
        ;;
esac
