FROM embedeval:openthread-13461
WORKDIR /testbed
COPY upstream-fix.diff synthetic-test.diff /tmp/preparation/
RUN git apply /tmp/preparation/upstream-fix.diff && git apply /tmp/preparation/synthetic-test.diff && rm -rf /tmp/preparation
RUN python3 -m compileall -q tests/scripts/thread-cert/pktverify tests/scripts/thread-cert/test_ipv6_addr_regression.py
CMD ["bash"]
