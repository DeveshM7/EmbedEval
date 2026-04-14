FROM nuttx-embedbench-base:latest

WORKDIR /testbed

# ── Clone kernel at base commit (before PR #12802: include/nuttx/event.h does not exist yet)
ARG KERNEL_BASE_COMMIT=93b520f7b05a843512529b617abb2a1f79a64ffd
RUN git clone --filter=blob:none https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# ── Clone apps at base commit (before nuttx-apps PR #2458)
ARG APPS_BASE_COMMIT=ab67cb1911942732e89bd57d3a6015b91acb1b06
RUN git clone --filter=blob:none https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# ── Configure sim:nsh
# Strip existing occurrences first to avoid duplicate-definition errors,
# then append desired values before olddefconfig.
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# ── Apply test patch (nuttx-apps PR #2458)
# nxevent.c includes <nuttx/event.h> which does not exist at KERNEL_BASE_COMMIT
COPY test_patch.diff /tmp/
RUN cd apps && git apply /tmp/test_patch.diff

# ── Pre-build (expected to FAIL with compile error)
# event.h does not exist → fatal error: nuttx/event.h: No such file or directory
RUN make -j$(nproc) 2>&1 || true

# ── Generate ctags index for agent code navigation
RUN ctags -R --languages=C,C++ -f /testbed/tags /testbed

# ── Install run_tests so agent can call it as a plain command
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
