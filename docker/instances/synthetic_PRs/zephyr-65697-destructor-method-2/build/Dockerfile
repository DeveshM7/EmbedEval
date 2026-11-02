FROM embedeval:zephyr-65697

WORKDIR /testbed
COPY upstream-fix.diff synthetic-bug.diff test_patch.diff /tmp/synthetic/
RUN git apply /tmp/synthetic/upstream-fix.diff \
    && git apply /tmp/synthetic/test_patch.diff \
    && git apply /tmp/synthetic/synthetic-bug.diff \
    && rm -rf /tmp/synthetic \
    && git add lib/posix/key.c tests/posix/common/src/key.c \
    && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -m "Prepare synthetic pthread-key benchmark" \
    && west build -b qemu_x86 tests/posix/common

CMD ["bash"]
