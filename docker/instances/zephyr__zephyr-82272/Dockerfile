FROM embedeval-zephyr-base:latest

WORKDIR /testbed

# Clone repo using a blobless filter: fetches all history metadata without
# downloading file blobs until needed. This lets us checkout any commit
# without a depth limit, and is much faster than a full clone.
ARG BASE_COMMIT=2f583a8369ef5b67c4438c40d17721d31db952a0
RUN git clone --filter=blob:none https://github.com/zephyrproject-rtos/zephyr.git . \
    && git checkout ${BASE_COMMIT}

# Initialize west workspace. -l . tells west "this dir is already the Zephyr
# repo, don't clone it". --narrow + --depth=1 minimizes download size for
# the external modules west needs.
RUN west init -l . \
    && west update --narrow -o=--depth=1 \
    && west zephyr-export

# Install Zephyr's Python dependencies (must come before west sdk install
# because `west sdk` requires patoolib which is in the Zephyr requirements).
RUN west packages pip --install 2>/dev/null \
    || pip install -r scripts/requirements.txt

# Install Zephyr SDK using west sdk install.
# For native_sim we need host tools but can skip cross-compiler toolchains.
ENV ZEPHYR_BASE=/testbed
RUN west sdk install -T

# Install run_tests.sh into PATH so the agent can call it as `run_tests`.
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

# Zephyr reference guide for the agent.
COPY ZEPHYR_GUIDE.md /testbed/ZEPHYR_GUIDE.md

# Apply the test-only patch: adds the new failing tests but NOT the fix.
COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build to populate the CMake cache. Incremental rebuilds after a single
# file edit will then take seconds instead of minutes.
# || true is intentional: the build may fail (tests will fail on broken code).
ARG PLATFORM=native_sim/native/64
ARG TEST_PATH=tests/kernel/sched/wraparound
RUN west build -b ${PLATFORM} ${TEST_PATH} || true

# Build a ctags index for code navigation.
RUN ctags -R --languages=C,C++ --exclude=build -f /testbed/tags .
