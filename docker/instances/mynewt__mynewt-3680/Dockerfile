FROM embedeval-mynewt-base:latest
ARG BASE_COMMIT
RUN git init /testbed \
    && cd /testbed \
    && git remote add origin https://github.com/apache/mynewt-core.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD
COPY metadata.json test_patch.diff /opt/benchmark/
RUN cd /testbed \
    && git apply /opt/benchmark/test_patch.diff \
    && git add -A \
    && git -c user.name=EmbedEval -c user.email=benchmark@localhost commit -m "Benchmark starting snapshot with regression tests" \
    && python3 /opt/benchmark/setup.py
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
WORKDIR /testbed
