#!/usr/bin/env bash
# Run the whole ostest suite to completion, then classify its log once.
# Exit 0 when ostest finished cleanly and ran every changed test, 1 for any
# other completed run, and 2 for timeout.
#
# The simulator's own exit code is ignored: nsh's poweroff ends it with 0
# even after ostest fails (status 256), so only the log decides.
set -u

TIMEOUT="${1:-300}"
# The "user_main: ... test" lines ostest prints before each changed test.
BANNERS=('user_main: smp call test')
OUTFILE="$(mktemp /tmp/nuttx_test_output.XXXXXX)"
FIFO="$(mktemp -u /tmp/nuttx_console.XXXXXX)"
mkfifo "$FIFO"
trap 'exec 3>&-; rm -f "$OUTFILE" "$FIFO"' EXIT

cd /testbed || exit 1
# The console is a FIFO fed in two steps: kernels of 2020 drop input that
# arrives before nsh runs, or together with the previous line, so "ostest" is
# typed after the boot and "poweroff" once ostest has returned.
timeout -k 10 "$TIMEOUT" ./nuttx <"$FIFO" >"$OUTFILE" 2>&1 &
SIM_PID=$!
exec 3<>"$FIFO"   # read-write, so that opening it cannot block, nor a dead simulator break the pipe
sleep 2
printf 'ostest\n' >&3
while kill -0 "$SIM_PID" 2>/dev/null && ! grep -Fq 'ostest_main: Exiting' "$OUTFILE"; do
    sleep 1
done
printf 'poweroff\n' >&3 2>/dev/null
wait "$SIM_PID"
SIM_RC=$?
cat "$OUTFILE"

# 124: timeout's TERM worked; 137: it needed KILL (or something else killed
# the simulator, so check the elapsed time too).
if (( SIM_RC == 124 || (SIM_RC == 137 && SECONDS >= TIMEOUT) )); then
    echo "run_tests: result=2 (timed out after ${TIMEOUT}s)"
    exit 2
fi

REASONS=()
grep -Fq 'user_main: Exiting' "$OUTFILE" || REASONS+=("no 'user_main: Exiting'")
grep -Fq 'ostest_main: Exiting with status 0' "$OUTFILE" ||
    REASONS+=("no 'ostest_main: Exiting with status 0'")
grep -Fq 'ERROR' "$OUTFILE" && REASONS+=("ERROR in output")
for banner in "${BANNERS[@]}"; do
    grep -Fq -- "$banner" "$OUTFILE" || REASONS+=("changed test did not run: '$banner'")
done

if (( ${#REASONS[@]} == 0 )); then
    echo "run_tests: result=0"
    exit 0
fi
printf 'run_tests: %s\n' "${REASONS[@]}"
echo "run_tests: result=1"
exit 1
