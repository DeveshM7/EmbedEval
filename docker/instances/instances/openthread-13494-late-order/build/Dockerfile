FROM embedeval:openthread-13494
WORKDIR /testbed
COPY upstream-fix.diff synthetic-bug.diff /tmp/synthetic/
RUN git apply /tmp/synthetic/upstream-fix.diff \
    && git apply /tmp/synthetic/synthetic-bug.diff \
    && rm -rf /tmp/synthetic \
    && git add tools/otci/otci/command_handlers.py tests/test_otci_pending_lines.py \
    && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -m "Prepare synthetic benchmark" \
    && python3 -m compileall -q tools/otci/otci tests/test_otci_pending_lines.py
CMD ["bash"]
