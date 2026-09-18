FROM embedeval-zephyr-base:latest

WORKDIR /testbed

# Clone repo using a blobless filter: fetches all history metadata without
# downloading file blobs until needed. This lets us checkout any commit
# without a depth limit, and is much faster than a full clone.
ARG BASE_COMMIT=4a31b8036d9c20349a0ee9ab83fa86147fdf983e
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
# SDK 0.17.0 uses a split distribution (separate hosttools + toolchain archives)
# so the older manual download method no longer works. west sdk install handles
# the new format automatically.
#   -t x86_64-zephyr-elf   installs only the toolchain for qemu_x86 (our target)
#   Host tools (including QEMU) are installed by default.
ENV ZEPHYR_BASE=/testbed
RUN west sdk install -t x86_64-zephyr-elf

# Install run_tests.sh into PATH so the agent can call it as `run_tests`.
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

# Zephyr reference guide for the agent.
COPY ZEPHYR_GUIDE.md /testbed/ZEPHYR_GUIDE.md

# Apply the test-only patch: adds the new failing tests but NOT the fix.
# After this, the repo is in the "broken state with known-failing tests" state.
COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build to populate the CMake cache. Incremental rebuilds after a single
# file edit will then take seconds instead of minutes.
# || true is intentional: the build may fail (tests will fail on broken code).
ARG PLATFORM=qemu_x86
ARG TEST_PATH=tests/net/dhcpv4/client
RUN west build -b ${PLATFORM} ${TEST_PATH} || true

# Build a ctags index for code navigation.
RUN ctags -R --languages=C,C++ --exclude=build -f /testbed/tags .
