#!/usr/bin/env bash
# Run Zephyr tests and exit as soon as results are available.
#
# Usage (from /testbed):
#   run_tests
#   run_tests [timeout_seconds]   (default: 120)

set -euo pipefail

TIMEOUT="${1:-120}"
OUTFILE="$(mktemp /tmp/zephyr_test_output.XXXXXX)"
trap 'rm -f "${OUTFILE}"' EXIT

cd /testbed
source /opt/zephyr-venv/bin/activate

# Clean up any stale PID file from a previous run BEFORE starting QEMU.
# QEMU writes this file on startup and never removes it — if it exists from a
# previous run the new QEMU instance refuses to start.
rm -f /testbed/build/zephyr/qemu.pid /testbed/build/qemu.pid

# setsid gives west its own session so kill -- "-${WEST_PGID}" below only
# kills west's process group (west + cmake + ninja + qemu), NOT this script.
# Without setsid, docker exec puts everything in one process group and the
# kill would SIGTERM this script itself (exit 143).
setsid west build -t run > "${OUTFILE}" 2>&1 &
WEST_PID=$!

# With setsid, west is the session leader and its own PGID == its PID.
# No need to retry — it's set immediately.
WEST_PGID="${WEST_PID}"

# Stream output to the terminal live while also watching for completion.
tail -f "${OUTFILE}" &
TAIL_PID=$!

ELAPSED=0
RESULT=""
while [ "${ELAPSED}" -lt "${TIMEOUT}" ]; do
    if grep -q "PROJECT EXECUTION SUCCESSFUL" "${OUTFILE}" 2>/dev/null; then
        RESULT="PASS"
        break
    elif grep -q "PROJECT EXECUTION FAILED" "${OUTFILE}" 2>/dev/null; then
        RESULT="FAIL"
        break
    fi
    sleep 0.5
    ELAPSED=$((ELAPSED + 1))
done

# Kill tail first so it stops writing to the terminal
kill "${TAIL_PID}" 2>/dev/null || true

# Kill the entire process group (west + cmake + ninja + qemu)
if [ -n "${WEST_PGID}" ] && [ "${WEST_PGID}" != "0" ]; then
    kill -- "-${WEST_PGID}" 2>/dev/null || true
fi
# Belt-and-suspenders: also kill qemu directly by name
pkill -f "qemu-system" 2>/dev/null || true
wait "${WEST_PID}" 2>/dev/null || true

# Clean up PID file left by QEMU
rm -f /testbed/build/zephyr/qemu.pid /testbed/build/qemu.pid

echo ""
echo "--- Test Results (PASS/FAIL per test) ---"
grep -E "^ (PASS|FAIL) - " "${OUTFILE}" || true
echo "-----------------------------------------"

case "${RESULT}" in
    PASS)
        echo "==> run_tests: PROJECT EXECUTION SUCCESSFUL"
        exit 0
        ;;
    FAIL)
        echo "==> run_tests: PROJECT EXECUTION FAILED"
        exit 1
        ;;
    *)
        echo "==> run_tests: TIMEOUT after ${TIMEOUT}s — no completion string seen"
        exit 2
        ;;
esac
