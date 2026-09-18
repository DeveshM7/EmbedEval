FROM embedeval-riot-base:latest

WORKDIR /testbed

# Clone RIOT using a blobless filter (fast, ~300MB vs full clone).
# Checkout the pre-fix base commit so the repo is in the "broken" state.
ARG BASE_COMMIT=a444a7acbc
RUN git clone --filter=blob:none https://github.com/RIOT-OS/RIOT.git . \
    && git checkout ${BASE_COMMIT}

# Install run_tests.sh into PATH so it can be called as `run_tests`.
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

# Apply the test-only patch: adds the new nanocoap unit tests but NOT the fix.
# After this, the repo has the NEW tests but the OLD source code.
COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build to populate build caches. Incremental rebuilds after editing
# a single .c file will then take seconds instead of minutes.
# || true is intentional: the build/test may fail (that's the broken state).
ARG BOARD=native
ARG UNIT_TESTS=tests-nanocoap
RUN make -C tests/unittests BOARD=${BOARD} clean all UNIT_TESTS=${UNIT_TESTS} || true
