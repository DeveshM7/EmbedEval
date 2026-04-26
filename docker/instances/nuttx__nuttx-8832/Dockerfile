FROM nuttx-embedbench-base:latest

WORKDIR /testbed

# ── Clone kernel at base commit (kwork_thread.c processes only one work item per wakeup)
ARG KERNEL_BASE_COMMIT=673a4aabf5d6856c9b87e258326cc4dae3f8689c
RUN git clone --filter=blob:none https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# ── Clone apps at base commit (before wqueue.c test was added)
ARG APPS_BASE_COMMIT=3a1893ba7e1e4ea5aef0f5181886defaa5e69e91
RUN git clone --filter=blob:none https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# ── Configure sim:nsh and enable work queues explicitly
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN\|CONFIG_SCHED_WORKQUEUE\|CONFIG_SCHED_LPWORK\|CONFIG_SCHED_HPWORK" \
       .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && echo "CONFIG_SCHED_WORKQUEUE=y" >> .config \
    && echo "CONFIG_SCHED_LPWORK=y" >> .config \
    && make olddefconfig

# ── Apply test patch (nuttx-apps PR #1665)
# Adds wqueue.c and wires it into ostest — build succeeds, bug is runtime assertion
COPY test_patch.diff /tmp/
RUN cd apps && git apply /tmp/test_patch.diff

# ── Pre-build (succeeds — wqueue.c uses existing kernel APIs, bug is runtime)
RUN make -j$(nproc) 2>&1 || true

# ── Generate ctags index for agent code navigation
RUN ctags -R --languages=C,C++ -f /testbed/tags /testbed

# ── Install run_tests so agent can call it as a plain command
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
