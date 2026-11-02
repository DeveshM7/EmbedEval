FROM embedeval:openthread-13189-reclaimed-capacity-correct
WORKDIR /testbed
COPY synthetic-bug.diff /tmp/injection.diff
RUN git apply /tmp/injection.diff && rm /tmp/injection.diff
RUN git add . && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -qm "Prepare synthetic benchmark"
RUN mkdir -p build && c++ -std=c++11 -Wall -Wextra -Werror -O0 -g -Iinclude -Isrc -Isrc/include -Itests/unit tests/unit/test_multi_frame_buffer_recovery.cpp -o build/ot-test-multi-frame-buffer-recovery
CMD ["bash"]
