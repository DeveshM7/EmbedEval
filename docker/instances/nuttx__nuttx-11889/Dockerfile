FROM embedeval-nuttx-base:latest

WORKDIR /testbed

# ── Clone kernel at base commit (memmem off-by-one: i < haystacklen-needlelen
#    instead of i <= haystacklen-needlelen; also missing zero-length needle handling)
ARG KERNEL_BASE_COMMIT=5ac401d941da22109be4de2ec28a77b86b790144
RUN git clone --filter=blob:none https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# ── Clone apps at base commit (before nuttx-apps PR #2327)
ARG APPS_BASE_COMMIT=fec49af501f15b547a062e38f13c020a90e6512b
RUN git clone --filter=blob:none https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# ── Configure sim:nsh
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# ── Apply test patch (nuttx-apps PR #2327)
# Adds libc_memmem.c unconditionally to ostest — build succeeds, bug is runtime
COPY test_patch.diff /tmp/
RUN cd apps && git apply /tmp/test_patch.diff

# ── Pre-build (succeeds — memmem exists but has off-by-one, crash is at runtime)
RUN make -j$(nproc) 2>&1 || true

# ── Generate ctags index for agent code navigation
RUN ctags -R --languages=C,C++ -f /testbed/tags /testbed

# ── Install run_tests so agent can call it as a plain command
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
