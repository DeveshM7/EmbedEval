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
BANNERS=('user_main: nxevent test' 'user_main: robust test')
OUTFILE="$(mktemp /tmp/nuttx_test_output.XXXXXX)"
trap 'rm -f "$OUTFILE"' EXIT

cd /testbed || exit 1
printf 'ostest\npoweroff\n' | timeout -k 10 "$TIMEOUT" ./nuttx >"$OUTFILE" 2>&1
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
