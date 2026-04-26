#!/usr/bin/env bash
# Run NuttX ostest on the sim target and exit as soon as results are available.
#
# The wqueue_test added by apps PR #1665 queues 100 count_workers and asserts
# all 100 ran (call_count == 100). On the base kernel, only one work item is
# drained per wakeup so fewer than 100 complete — the assert fires in a child
# pthread but ostest itself still exits 0. A plain exit-code check would give a
# false pass, so this script also inspects the wqueue_test output lines directly.
#
# Usage (from /testbed):
#   run_tests
#   run_tests [timeout_seconds]   (default: 300)
#
# Exit codes:
#   0  — all wqueue_test iterations show call = 100 + ostest exits with status 0
#   1  — any wqueue iteration had call < 100, or wqueue test did not run
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
    if grep -q "Exiting with status 0" "${OUTFILE}" 2>/dev/null; then
        # ostest completed — now check wqueue results specifically.
        # The assert fires in a child thread so ostest exits 0 even on failure.
        MIN_WQUEUE=$(grep "wqueue_test: call = " "${OUTFILE}" 2>/dev/null \
            | grep -oE "call = [0-9]+" | grep -oE "[0-9]+" | sort -n | head -1)
        if [ -z "${MIN_WQUEUE}" ]; then
            RESULT="FAIL"   # wqueue test produced no output
        elif [ "${MIN_WQUEUE}" -lt 100 ] 2>/dev/null; then
            RESULT="FAIL"   # at least one iteration had call < 100
        else
            RESULT="PASS"
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
grep -E "wqueue_test|Exiting with status" "${OUTFILE}" || true
echo "--------------------"

case "${RESULT}" in
    PASS)
        echo "==> run_tests: all wqueue_test iterations call = 100 — PASSED"
        exit 0
        ;;
    FAIL)
        echo "==> run_tests: wqueue_test call count < 100 or test missing — FAILED"
        exit 1
        ;;
    *)
        echo "==> run_tests: TIMEOUT after ${TIMEOUT}s — ostest did not complete"
        exit 2
        ;;
esac
