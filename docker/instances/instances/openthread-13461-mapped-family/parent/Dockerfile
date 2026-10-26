# Python 3.13 reproduces the IPv4-mapped address formatting change.
FROM python:3.13.7-slim-bookworm AS python_runtime
FROM embedeval-openthread-base:latest
COPY --from=python_runtime /usr/local /usr/local
RUN apt-get update && apt-get install -y --no-install-recommends \
    libbz2-1.0 libffi8 liblzma5 libsqlite3-0 libssl3 zlib1g \
    && rm -rf /var/lib/apt/lists/* && ldconfig

WORKDIR /testbed
ARG BASE_COMMIT=182c0c3f2068746bbed9323719a21905091f75dc
RUN git init . \
    && git remote add origin https://github.com/openthread/openthread.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD

COPY test_patch.diff /tmp/test_patch.diff
RUN git apply /tmp/test_patch.diff
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

# Compile without executing the expected failing regression tests.
RUN python3 -m compileall -q tests/scripts/thread-cert/pktverify \
    tests/scripts/thread-cert/test_ipv6_addr_regression.py
