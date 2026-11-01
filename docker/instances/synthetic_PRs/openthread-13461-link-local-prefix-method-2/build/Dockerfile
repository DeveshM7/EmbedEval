FROM embedeval:openthread-13461-link-local-prefix-correct
WORKDIR /testbed
COPY synthetic-bug.diff /tmp/injection.diff
RUN git apply /tmp/injection.diff && rm /tmp/injection.diff
RUN git add . && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -qm "Prepare synthetic benchmark"
RUN python3 -m compileall -q tests/scripts/thread-cert/pktverify tests/scripts/thread-cert/test_ipv6_addr_regression.py
CMD ["bash"]
