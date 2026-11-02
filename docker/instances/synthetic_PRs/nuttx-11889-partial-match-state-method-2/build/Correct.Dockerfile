FROM embedeval:nuttx-11889
WORKDIR /testbed
COPY upstream-fix.diff synthetic-test.diff /tmp/preparation/
RUN git apply /tmp/preparation/upstream-fix.diff && git apply /tmp/preparation/synthetic-test.diff && rm -rf /tmp/preparation
RUN sed -i '/^CONFIG_NDEBUG=/d; /^# CONFIG_NDEBUG /d; /^CONFIG_TESTING_OSTEST_WAITRESULT=/d' .config \
    && printf '\nCONFIG_NDEBUG=n\nCONFIG_TESTING_OSTEST_WAITRESULT=y\n' >> .config \
    && make olddefconfig && grep -q '^# CONFIG_NDEBUG is not set' .config
COPY run_nuttx.py /usr/local/bin/run_nuttx.py
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
RUN make -j4
CMD ["bash"]
