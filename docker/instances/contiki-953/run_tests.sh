#!/usr/bin/env bash
set -euo pipefail
cd /testbed
mode="${1:-all}"
case "$mode" in
    all|regression|control) ;;
    *) echo "Usage: run_tests [all|regression|control]" >&2; exit 1 ;;
esac
bash tests/08-native-runs/embedeval-coap-separate/build.sh
status=0
run_one() {
    local rc=0
    timeout 60 "$@" || rc=$?
    case "$rc" in
        0) ;;
        124|137) status=2 ;;
        *) if [[ "$status" != 2 ]]; then status=1; fi ;;
    esac
}
if [[ "$mode" == all ]]; then
    run_one ./build/coap-separate
else
    run_one ./build/coap-separate "$mode"
fi
if [[ "$mode" != control ]]; then run_one ./build/coap-separate-dtls dtls; fi
if [[ "$mode" != regression ]]; then run_one ./build/coap-upstream-controls; fi
exit "$status"
