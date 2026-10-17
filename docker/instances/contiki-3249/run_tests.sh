#!/usr/bin/env bash
# 0 = pass, 1 = build/test failure, 2 = timeout.
set -euo pipefail
cd /testbed
mode="${1:-all}"
case "$mode" in
    all|regression|control) ;;
    *) echo "Usage: run_tests [all|regression|control]" >&2; exit 1 ;;
esac
rc=0
timeout 180 bash -c '
    set -euo pipefail
    bash tests/08-native-runs/embedeval-tsch-cs/build.sh
    python3 tests/08-native-runs/embedeval-tsch-cs/run.py "$1"
' bash "$mode" || rc=$?
case "$rc" in
    0) exit 0 ;;
    2|124|137) exit 2 ;;
    *) exit 1 ;;
esac
