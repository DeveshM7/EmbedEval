FROM embedeval-nuttx-base:latest

WORKDIR /testbed

# ── Clone kernel at base commit (arch_setjmp_x86_64.S returns 0 for longjmp(buf,0))
ARG KERNEL_BASE_COMMIT=5a38c8bfe3cbf906010a906b4aff9f104789a63f
RUN git clone --filter=blob:none https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# ── Clone apps at base commit (before the longjmp(0) test was added)
ARG APPS_BASE_COMMIT=8e7d6cadf4795c3daf27e55637f3864517927cc2
RUN git clone --filter=blob:none https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# ── Configure sim:nsh
#
# CONFIG_NDEBUG must be forced off. ostest reports test failures only through
# ASSERT(), and assert() in include/assert.h is gated on NDEBUG, not on
# CONFIG_DEBUG_ASSERTIONS (which governs only DEBUGASSERT/PANIC). Kconfig
# declares `config NDEBUG` with `default !DEBUG_ASSERTIONS`, but a default
# applies only to a symbol absent from .config, and sim:nsh's defconfig pins
# CONFIG_NDEBUG=y -- so olddefconfig preserves it, every ASSERT() compiles to
# nothing, and ostest exits 0 however broken the code under test is.
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_NDEBUG\|CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && echo "CONFIG_NDEBUG=n" >> .config \
    && make olddefconfig \
    && grep -q "^# CONFIG_NDEBUG is not set" .config

# ── Apply test patch (nuttx-apps PR #2725)
# Adds jump_with_retval() and the longjmp(buf,0) ASSERT to testing/ostest/setjmp.c
COPY test_patch.diff /tmp/
RUN cd apps && git apply /tmp/test_patch.diff

# ── Pre-build (succeeds — the bug is a runtime assertion, not a compile error)
RUN make -j$(nproc) 2>&1 || true

# ── Generate ctags index for agent code navigation
RUN ctags -R --languages=C,C++ -f /testbed/tags /testbed

# ── Install run_tests so agent can call it as a plain command
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
