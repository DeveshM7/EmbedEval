FROM embedeval-openthread-base:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake ninja-build libgtest-dev libgmock-dev python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /testbed
ARG BASE_COMMIT=5a31f89a9df4ec1cb9ab6d25430d2c07edc55e46
RUN git init . \
    && git remote add origin https://github.com/openthread/openthread.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD \
    && git submodule update --init --recursive

COPY test_patch.diff /tmp/test_patch.diff
RUN git apply /tmp/test_patch.diff
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

# Build upstream GoogleTests without running the expected failing assertions.
RUN cmake -S . -B build -G Ninja -DOT_PLATFORM=external -DOT_BUILD_GTEST=ON -DOT_APP_CLI=OFF -DOT_APP_NCP=OFF -DOT_APP_RCP=OFF -DOT_MTD=OFF -DOT_RCP=OFF -DCMAKE_BUILD_TYPE=Debug \
    && cmake --build build --target ot-ftd-gtest -j$(nproc)
