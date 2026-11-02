FROM embedeval:openthread-13189
WORKDIR /testbed
COPY upstream-fix.diff synthetic-test.diff /tmp/preparation/
RUN git apply /tmp/preparation/upstream-fix.diff && git apply /tmp/preparation/synthetic-test.diff && rm -rf /tmp/preparation
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
RUN mkdir -p build && c++ -std=c++11 -Wall -Wextra -Werror -O0 -g -Iinclude -Isrc -Isrc/include -Itests/unit tests/unit/test_multi_frame_buffer_recovery.cpp -o build/ot-test-multi-frame-buffer-recovery
CMD ["bash"]
