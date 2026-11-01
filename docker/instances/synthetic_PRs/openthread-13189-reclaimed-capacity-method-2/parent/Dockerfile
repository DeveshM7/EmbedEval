FROM embedeval-openthread-base:latest

WORKDIR /testbed
ARG BASE_COMMIT=4394c0944b9b96f449cd2556ea5b2c1be002ed93
RUN git init . \
    && git remote add origin https://github.com/openthread/openthread.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD

COPY test_patch.diff /tmp/test_patch.diff
RUN git apply /tmp/test_patch.diff
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

# MultiFrameBuffer is header-only: no submodules, radio hardware, or stubs.
# Compilation must succeed even though the regression assertions will fail.
RUN mkdir -p build && c++ -std=c++11 -Wall -Wextra -Werror -O0 -g \
    -Iinclude -Isrc -Isrc/include -Itests/unit \
    tests/unit/test_multi_frame_buffer_recovery.cpp \
    -o build/ot-test-multi-frame-buffer-recovery
