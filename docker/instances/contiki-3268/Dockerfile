FROM embedeval-contiki-ng-base:latest
WORKDIR /testbed
ARG BASE_COMMIT=93b31c8d1d46797207b06156ea38083a39be42e5
RUN git init . \
    && git remote add origin https://github.com/contiki-ng/contiki-ng.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD
COPY test_patch.diff /tmp/test_patch.diff
RUN git apply /tmp/test_patch.diff
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
# Compile without running the intentionally failing base regressions.
RUN bash tests/08-native-runs/28-coap-parse/embedeval/build.sh
