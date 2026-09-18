FROM embedeval-zephyr-base:latest

WORKDIR /testbed

# Blobless clone — fetches all commit history without file blobs,
# letting us reach any historical commit without --depth limits.
ARG BASE_COMMIT=af0158d693d581678429d10ffbd9da8adecf95df
RUN git clone --filter=blob:none https://github.com/zephyrproject-rtos/zephyr.git . \
    && git checkout ${BASE_COMMIT}

# Initialize west workspace and fetch module dependencies.
RUN west init -l . && west update --narrow -o=--depth=1 && west zephyr-export

# Install Python dependencies.
# west packages pip --install was added in Zephyr 3.6; fall back for older commits.
RUN west packages pip --install 2>/dev/null || pip install -r scripts/requirements.txt

# Install Zephyr SDK 0.14.2 (required for Zephyr 3.0 era commits).
ARG ZEPHYR_SDK_VERSION=0.14.2
RUN ARCH=$(uname -m) \
    && SDK_ARCHIVE="zephyr-sdk-${ZEPHYR_SDK_VERSION}_linux-${ARCH}.tar.gz" \
    && wget -q "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/${SDK_ARCHIVE}" \
    && tar -xf "${SDK_ARCHIVE}" -C /opt && rm "${SDK_ARCHIVE}" \
    && /opt/zephyr-sdk-${ZEPHYR_SDK_VERSION}/setup.sh -t x86_64-zephyr-elf -h -c

# Install run_tests.sh into PATH.
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

# Zephyr reference guide for the agent.
COPY ZEPHYR_GUIDE.md /testbed/ZEPHYR_GUIDE.md

# Apply the test-only patch: adds the new failing tests but NOT the fix.
COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build for qemu_x86.
# Expected to fail: test file uses is_ready which doesn't exist at base_commit.
ARG PLATFORM=qemu_x86
ARG TEST_PATH=tests/subsys/logging/log_backend_init
RUN west build -b ${PLATFORM} ${TEST_PATH} || true
