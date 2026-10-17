FROM embedeval-contiki-ng-base:latest
WORKDIR /testbed
ARG BASE_COMMIT=cf61c0dfa134665edff6315e13b9345c0b713d2f
RUN git init . \
    && git remote add origin https://github.com/contiki-ng/contiki-ng.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD
COPY test_patch.diff /tmp/test_patch.diff
RUN git apply /tmp/test_patch.diff
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
# Compile without running the expected failing base regressions.
RUN bash tests/08-native-runs/embedeval-tcp-retention/build.sh
