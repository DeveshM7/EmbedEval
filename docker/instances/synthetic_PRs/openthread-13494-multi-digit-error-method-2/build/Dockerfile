FROM embedeval:openthread-13494-multi-digit-error-correct
WORKDIR /testbed
COPY synthetic-bug.diff /tmp/injection.diff
RUN git apply /tmp/injection.diff && rm /tmp/injection.diff
RUN git add . && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -qm "Prepare synthetic benchmark"
RUN python3 -m compileall -q tools/otci/otci tests/test_otci_pending_lines.py
CMD ["bash"]
