#!/usr/bin/env bash
set -euo pipefail
cd /testbed
mode="${1:-all}"
case "$mode" in
    all|regression|control) ;;
    *) echo "Usage: run_tests [all|regression|control]" >&2; exit 1 ;;
esac
bash tests/08-native-runs/28-coap-parse/embedeval/build.sh
set +e
if [[ "$mode" == all ]]; then
    timeout 60 ./build/coap-regression
else
    timeout 60 ./build/coap-regression "$mode"
fi
status=$?
set -e
case "$status" in
    0) exit 0 ;;
    124|137) exit 2 ;;
    *) exit 1 ;;
esac
