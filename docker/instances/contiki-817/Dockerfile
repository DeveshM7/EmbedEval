FROM embedeval-contiki-ng-base:latest
WORKDIR /testbed
ARG BASE_COMMIT=423d928e974f78291c4ff46c544ea48a22dbaeb3
RUN git init . \
    && git remote add origin https://github.com/contiki-ng/contiki-ng.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD
COPY test_patch.diff /tmp/test_patch.diff
RUN git apply /tmp/test_patch.diff
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
RUN bash tests/08-native-runs/embedeval-coap-block2/build.sh
