FROM embedbench-riot-legacy-base:latest

ARG BASE_COMMIT
ARG BOARD
ARG UNIT_TESTS

WORKDIR /testbed

# Clone RIOT at the base commit
RUN git clone --filter=blob:none https://github.com/RIOT-OS/RIOT.git . && \
    git checkout ${BASE_COMMIT}

# Inject legacy fixes
RUN sed -i 's/SIGSTKSZ/8192/g' cpu/native/include/native_internal.h
RUN echo "CFLAGS += -Wno-implicit-fallthrough -Wno-format-overflow -Wno-format-truncation -Wno-unused-function -Wno-cast-function-type" >> Makefile.include

# The test runner will overwrite tests/unittests with the failing version
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build to cache objects
RUN make -C tests/unittests BOARD=${BOARD} clean all UNIT_TESTS=${UNIT_TESTS} || true
