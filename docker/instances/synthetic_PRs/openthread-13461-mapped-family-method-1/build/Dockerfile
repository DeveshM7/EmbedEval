FROM embedeval:openthread-13461
WORKDIR /testbed
COPY upstream-fix.diff synthetic-bug.diff /tmp/synthetic/
RUN git apply /tmp/synthetic/upstream-fix.diff \
    && git apply /tmp/synthetic/synthetic-bug.diff \
    && rm -rf /tmp/synthetic \
    && git add tests/scripts/thread-cert/pktverify/addrs.py tests/scripts/thread-cert/test_ipv6_addr_regression.py \
    && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -m "Prepare synthetic benchmark" \
    && python3 -m compileall -q tests/scripts/thread-cert/pktverify tests/scripts/thread-cert/test_ipv6_addr_regression.py
CMD ["bash"]
