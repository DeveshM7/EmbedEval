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
#
# CONFIG_NDEBUG must be forced off. ostest's libc tests report failures only
# through ASSERT(), and assert() in include/assert.h is gated on NDEBUG, not on
# CONFIG_DEBUG_ASSERTIONS. Kconfig declares `config NDEBUG` with
# `default !DEBUG_ASSERTIONS`, but a default only applies to a symbol that is
# absent from .config -- sim:nsh's defconfig already pins CONFIG_NDEBUG=y, so
# olddefconfig preserves it and every ASSERT() in the test compiles to nothing.
# With that in place the test binary carries no reference to __assert at all and
# ostest exits 0 no matter how broken the code under test is.
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN\|CONFIG_DEBUG_FEATURES\|CONFIG_DEBUG_ASSERTIONS\|CONFIG_NDEBUG" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && echo "CONFIG_DEBUG_FEATURES=y" >> .config \
    && echo "CONFIG_DEBUG_ASSERTIONS=y" >> .config \
    && echo "CONFIG_NDEBUG=n" >> .config \
    && make olddefconfig \
    && grep -q "^CONFIG_DEBUG_ASSERTIONS=y" .config \
    && grep -q "^# CONFIG_NDEBUG is not set" .config

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
