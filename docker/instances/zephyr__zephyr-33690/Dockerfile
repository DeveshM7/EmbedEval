FROM embedbench-zephyr-base:latest

WORKDIR /testbed

# This instance uses Zephyr v2.6 (2021) which predates `west sdk install`.
# The SDK must be installed manually.
ARG BASE_COMMIT=63ca48a7a9b8e9a85d9c18c5dbaf894968bbc35d
RUN git clone --filter=blob:none https://github.com/zephyrproject-rtos/zephyr.git . \
    && git checkout ${BASE_COMMIT}

RUN west init -l . \
    && west update --narrow -o=--depth=1 \
    && west zephyr-export

# Install Python dependencies for old Zephyr.
RUN pip install -r scripts/requirements.txt

# Install Zephyr SDK 0.16.8 manually.
# Old Zephyr doesn't have `west sdk install`. SDK 0.16.8 is the last version
# with a single-bundle download (0.17.0 switched to split archives).
# Newer SDK toolchains are backwards-compatible with old Zephyr.
ENV ZEPHYR_SDK_INSTALL_DIR=/opt/zephyr-sdk-0.16.8
RUN ARCH=$(uname -m) \
    && wget -q https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v0.16.8/zephyr-sdk-0.16.8_linux-${ARCH}.tar.xz \
    && tar xf zephyr-sdk-0.16.8_linux-${ARCH}.tar.xz -C /opt \
    && rm zephyr-sdk-0.16.8_linux-${ARCH}.tar.xz \
    && ${ZEPHYR_SDK_INSTALL_DIR}/setup.sh -h -c

COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

COPY ZEPHYR_GUIDE.md /testbed/ZEPHYR_GUIDE.md

COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build for qemu_x86.
ARG PLATFORM=qemu_x86
ARG TEST_PATH=tests/drivers/sensor/generic
RUN west build -b ${PLATFORM} ${TEST_PATH} || true

RUN ctags -R --languages=C,C++ --exclude=build -f /testbed/tags .
