FROM nuttx-embedbench-base:latest

WORKDIR /testbed

# ── Clone kernel at base_commit (before PR #8885: sigset_t is still uint32_t,
#    SIGSET_FMT / SIGSET_ELEM macros do not exist yet) ─────────────────────────
ARG KERNEL_BASE_COMMIT=0eae218b49a46ef88e14f1cb763a6ca74fe342ab
RUN git clone --filter=blob:none https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# ── Clone apps at base_commit (before nuttx-apps PR #1682) ───────────────────
# Must be named 'apps' and live inside the nuttx directory
ARG APPS_BASE_COMMIT=75b4720a6e57dedebb4ce607035350bfc3190fb3
RUN git clone --filter=blob:none https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# ── Configure sim:nsh ─────────────────────────────────────────────────────────
# sim:nsh defconfig already has CONFIG_TESTING_OSTEST=y — no extra config needed
# -a ./apps: apps is inside nuttx dir (not the default ../apps sibling location)
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && make olddefconfig

# ── Apply test patch (nuttx-apps PR #1682) ───────────────────────────────────
# New ostest code uses SIGSET_FMT / SIGSET_ELEM macros from nuttx/signal.h.
# Those macros don't exist in the kernel at KERNEL_BASE_COMMIT.
COPY test_patch.diff /tmp/
RUN cd apps && git apply /tmp/test_patch.diff

# ── Pre-build (expected to FAIL with compile error) ───────────────────────────
# SIGSET_FMT / SIGSET_ELEM are undefined → compiler error in ostest files.
# The || true prevents Docker from aborting — the failure is intentional.
RUN make -j$(nproc) 2>&1 || true

# ── Generate ctags index for agent code navigation ────────────────────────────
RUN ctags -R --languages=C,C++ -f /testbed/tags /testbed

# ── Install run_tests so agent can call it as a plain command ─────────────────
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
