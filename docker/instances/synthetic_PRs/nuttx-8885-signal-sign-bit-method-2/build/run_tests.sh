#!/bin/bash
set -euo pipefail
cd /testbed
exec python3 /usr/local/bin/run_nuttx.py
