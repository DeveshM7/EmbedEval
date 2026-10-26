FROM embedeval-openthread-base:latest

# OTCI imports its ADB connector at module load time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/otci-venv \
    && /opt/otci-venv/bin/pip install --no-cache-dir adb-shell==0.4.4
ENV PATH="/opt/otci-venv/bin:${PATH}"

WORKDIR /testbed
ARG BASE_COMMIT=255021a0b9675061ebd6e6bb3dcec53a68bdae45
RUN git init . \
    && git remote add origin https://github.com/openthread/openthread.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD

COPY test_patch.diff /tmp/test_patch.diff
RUN git apply /tmp/test_patch.diff
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests

# Syntax-check without executing the expected failing regression assertions.
RUN python3 -m compileall -q tools/otci/otci tests/test_otci_pending_lines.py
