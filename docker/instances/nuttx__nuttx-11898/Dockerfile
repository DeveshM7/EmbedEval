FROM embedeval-nuttx-base:latest

WORKDIR /testbed

# ── Clone kernel at base commit (pthread_join of detached thread returns ESRCH)
ARG KERNEL_BASE_COMMIT=2b18917a6b7a35b44c3448ef1f6a87caa13e4f90
RUN git clone --filter=blob:none https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# ── Clone apps at base commit (before cancel.c asserts EINVAL)
ARG APPS_BASE_COMMIT=fec49af501f15b547a062e38f13c020a90e6512b
RUN git clone --filter=blob:none https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# ── Configure sim:nsh with ostest enabled
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# ── Apply test patch (nuttx-apps PR #2329)
# Changes cancel_test to assert EINVAL (not ESRCH) for pthread_join on detached thread
COPY test_patch.diff /tmp/
RUN cd apps && git apply /tmp/test_patch.diff

# ── Pre-build (succeeds — cancel.c uses only existing kernel APIs, bug is behavioral)
RUN make -j$(nproc) 2>&1 || true

# ── Generate ctags index for agent code navigation
RUN ctags -R --languages=C,C++ -f /testbed/tags /testbed

# ── Install run_tests so agent can call it as a plain command
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
