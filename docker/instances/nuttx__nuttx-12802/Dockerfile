FROM embedeval-nuttx-base:latest

WORKDIR /testbed

ARG KERNEL_BASE_COMMIT=93b520f7b05a843512529b617abb2a1f79a64ffd
ARG APPS_BASE_COMMIT=ab67cb1911942732e89bd57d3a6015b91acb1b06
RUN git clone --filter=blob:none https://github.com/apache/nuttx.git . \
    && git checkout ${KERNEL_BASE_COMMIT}
RUN git clone --filter=blob:none https://github.com/apache/nuttx-apps.git apps \
    && cd apps && git checkout ${APPS_BASE_COMMIT}

# Keep assertions active: ostest uses ASSERT(), which CONFIG_NDEBUG disables.
RUN ./tools/configure.sh -a ./apps sim:nsh \
    && sed -i '/^CONFIG_TESTING_OSTEST=/d; /^# CONFIG_TESTING_OSTEST is not set/d; /^CONFIG_NSH_CONSOLE_LOGIN=/d; /^# CONFIG_NSH_CONSOLE_LOGIN is not set/d; /^CONFIG_NDEBUG=/d; /^# CONFIG_NDEBUG is not set/d' .config \
    && printf '%s\n' 'CONFIG_TESTING_OSTEST=y' 'CONFIG_NSH_CONSOLE_LOGIN=n' 'CONFIG_NDEBUG=n' >> .config \
    && make olddefconfig \
    && grep -q '^CONFIG_TESTING_OSTEST=y$' .config \
    && grep -q '^# CONFIG_NDEBUG is not set$' .config
RUN sed -i '/^CONFIG_SCHED_EVENTS=/d; /^# CONFIG_SCHED_EVENTS is not set/d' .config \
    && printf '%s\n' 'CONFIG_SCHED_EVENTS=y' >> .config \
    && grep -q '^CONFIG_SCHED_EVENTS=y$' .config

COPY test_patch.diff /tmp/test_patch.diff
RUN cd apps && git apply /tmp/test_patch.diff

COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
