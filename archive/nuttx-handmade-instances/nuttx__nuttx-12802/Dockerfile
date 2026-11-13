FROM embedeval-nuttx-base:latest

ARG KERNEL_BASE_COMMIT
ARG APPS_BASE_COMMIT

WORKDIR /testbed

# Clone kernel at base commit (include/nuttx/event.h does not exist yet)
RUN git clone https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}

# Clone apps at base commit (before nxevent test was added)
RUN git clone https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# Configure sim:nsh, then set the options we need.
# Strip any existing occurrences first to avoid duplicate-definition errors
# in kconfig-frontends, then append the desired values before olddefconfig.
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && grep -v "CONFIG_TESTING_OSTEST\|CONFIG_NSH_CONSOLE_LOGIN" .config > .config.tmp \
    && mv .config.tmp .config \
    && echo "CONFIG_TESTING_OSTEST=y" >> .config \
    && echo "CONFIG_NSH_CONSOLE_LOGIN=n" >> .config \
    && make olddefconfig

# The event API is gated by CONFIG_SCHED_EVENTS, which only exists once the
# fix's sched/Kconfig is applied, so append it after olddefconfig. Without it
# the fixed kernel builds but nxevent.c fails to link.
RUN sed -i '/^CONFIG_SCHED_EVENTS=/d; /^# CONFIG_SCHED_EVENTS is not set/d' .config \
    && printf '%s\n' 'CONFIG_SCHED_EVENTS=y' >> .config \
    && grep -q '^CONFIG_SCHED_EVENTS=y$' .config

# Apply test patch — adds nxevent.c which includes <nuttx/event.h>
COPY test_patch.diff /tmp/test_patch.diff
RUN cd apps && git apply /tmp/test_patch.diff

# Pre-build: EXPECTED to fail — event.h does not exist at base kernel commit
RUN make -j$(nproc) 2>&1 || true

# ── Install run_tests so agent can call it as a plain command
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
