FROM embedeval:openthread-13494
WORKDIR /testbed
COPY upstream-fix.diff synthetic-test.diff /tmp/preparation/
RUN git apply /tmp/preparation/upstream-fix.diff && git apply /tmp/preparation/synthetic-test.diff && rm -rf /tmp/preparation
RUN python3 -m compileall -q tools/otci/otci tests/test_otci_pending_lines.py
CMD ["bash"]
