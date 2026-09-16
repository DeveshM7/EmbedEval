#!/bin/bash
set -e

# Change to the appropriate test directory
cd /testbed/tests/unittests

# Run the test via make
make clean all test BOARD=native UNIT_TESTS=tests-saul_reg
