FROM embedbench-zephyr-base:latest

WORKDIR /testbed

ARG BASE_COMMIT=4d0a8c1e8b8316ec5071a3fcc98cd292d3bc4e6d
RUN git clone --filter=blob:none https://github.com/zephyrproject-rtos/zephyr.git . \
    && git checkout ${BASE_COMMIT}

RUN west init -l . \
    && west update --narrow -o=--depth=1 \
    && west zephyr-export

RUN west packages pip --install 2>/dev/null \
    || pip install -r scripts/requirements.txt

# Install Zephyr SDK manually — this Zephyr version predates `west sdk install`.
# Minimal SDK is sufficient for native_sim (uses host compiler, not cross-toolchain).
ENV ZEPHYR_BASE=/testbed
ENV ZEPHYR_SDK_INSTALL_DIR=/opt/zephyr-sdk-0.16.8
RUN ARCH=$(uname -m) \
    && wget -q https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v0.16.8/zephyr-sdk-0.16.8_linux-${ARCH}_minimal.tar.xz \
    && tar xf zephyr-sdk-0.16.8_linux-${ARCH}_minimal.tar.xz -C /opt \
    && rm zephyr-sdk-0.16.8_linux-${ARCH}_minimal.tar.xz \
    && ${ZEPHYR_SDK_INSTALL_DIR}/setup.sh -h -c

COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

COPY ZEPHYR_GUIDE.md /testbed/ZEPHYR_GUIDE.md

COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build. The test references new RTIO workq infrastructure that
# doesn't exist yet, so this will fail — expected. CMake cache is
# still populated for faster incremental rebuilds.
ARG PLATFORM=native_sim_64
ARG TEST_PATH=tests/subsys/rtio/workq
RUN west build -b ${PLATFORM} ${TEST_PATH} || true

RUN ctags -R --languages=C,C++ --exclude=build -f /testbed/tags .
