FROM embedeval:openthread-13189
WORKDIR /testbed
COPY upstream-fix.diff synthetic-bug.diff /tmp/synthetic/
RUN git apply /tmp/synthetic/upstream-fix.diff \
    && git apply /tmp/synthetic/synthetic-bug.diff \
    && rm -rf /tmp/synthetic \
    && git add src/lib/spinel/multi_frame_buffer.hpp tests/unit/test_multi_frame_buffer_recovery.cpp \
    && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -m "Prepare synthetic benchmark" \
    && mkdir -p build && c++ -std=c++11 -Wall -Wextra -Werror -O0 -g -Iinclude -Isrc -Isrc/include -Itests/unit tests/unit/test_multi_frame_buffer_recovery.cpp -o build/ot-test-multi-frame-buffer-recovery
CMD ["bash"]
