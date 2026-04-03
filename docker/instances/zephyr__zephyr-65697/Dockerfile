FROM embedbench-zephyr-base:latest

WORKDIR /testbed

# Clone repo using a blobless filter: fetches all history metadata without
# downloading file blobs until needed. This lets us checkout any commit
# (including ones from 2023) without a depth limit, and is much faster
# than a full clone (~300MB vs ~1.5GB).
ARG BASE_COMMIT=41b7c17ac4bd0198fc9bb3a0e55aa8d5e2fae96e
RUN git clone --filter=blob:none https://github.com/zephyrproject-rtos/zephyr.git . \
    && git checkout ${BASE_COMMIT}

# Initialize west workspace. -l . tells west "this dir is already the Zephyr
# repo, don't clone it". --narrow + --depth=1 minimizes download size for
# the external modules west needs.
RUN west init -l . \
    && west update --narrow -o=--depth=1 \
    && west zephyr-export

# Install Zephyr SDK manually.
# `west sdk install` is a west extension command defined inside the Zephyr
# source tree — it was added in Zephyr 3.6 (late 2023). The base_commit here
# predates that, so we use the pre-3.6 manual method instead.
#
# $(uname -m) resolves to x86_64 or aarch64, making this work on both
# Intel hosts and Apple Silicon (ARM64) Docker environments.
#
# Flags for setup.sh:
#   -t x86_64-zephyr-elf   installs only the toolchain for qemu_x86 (our target)
#   -h                     installs host tools, including Zephyr's patched QEMU
#   -c                     registers the SDK as a CMake package for west build
ARG ZEPHYR_SDK_VERSION=0.16.8
RUN ARCH=$(uname -m) \
    && SDK_ARCHIVE="zephyr-sdk-${ZEPHYR_SDK_VERSION}_linux-${ARCH}.tar.xz" \
    && wget -q "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/${SDK_ARCHIVE}" \
    && tar -xf "${SDK_ARCHIVE}" -C /opt \
    && rm "${SDK_ARCHIVE}" \
    && /opt/zephyr-sdk-${ZEPHYR_SDK_VERSION}/setup.sh -t x86_64-zephyr-elf -h -c

# Install Zephyr's Python dependencies.
# west packages pip --install is the modern method (Zephyr >= 3.6 / west >= 1.2).
# For older commits, fall back to the requirements file.
RUN west packages pip --install 2>/dev/null \
    || pip install -r scripts/requirements.txt

# Apply the test-only patch: adds the new failing tests but NOT the fix.
# After this, the repo is in the "broken state with known-failing tests" state.
COPY test_patch.diff /tmp/
RUN git apply /tmp/test_patch.diff

# Pre-build to populate the CMake cache. Incremental rebuilds after a single
# file edit will then take seconds instead of minutes.
# || true is intentional: the build may fail (tests will fail on broken code).
ARG PLATFORM=qemu_x86
ARG TEST_PATH=tests/posix/common
RUN west build -b ${PLATFORM} ${TEST_PATH} || true

# Build a ctags index for code navigation.
RUN ctags -R --languages=C,C++ --exclude=build -f /testbed/tags .
