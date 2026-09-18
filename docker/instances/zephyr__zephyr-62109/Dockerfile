FROM embedeval-zephyr-base:latest

WORKDIR /testbed

ARG BASE_COMMIT=e80f71a14dd3eb5738f18bf8b231ae26ba574b90
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

# Pre-build. The test patch changes the log_output_process() signature,
# so this build will fail — that's expected. The CMake cache is still
# populated, speeding up incremental rebuilds after the agent's fix.
ARG PLATFORM=native_sim_64
ARG TEST_PATH=tests/subsys/logging/log_output
RUN west build -b ${PLATFORM} ${TEST_PATH} -- -DCONFIG_LOG_THREAD_ID_PREFIX=y || true

RUN ctags -R --languages=C,C++ --exclude=build -f /testbed/tags .
