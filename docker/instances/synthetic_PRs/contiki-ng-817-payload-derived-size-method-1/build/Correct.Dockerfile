FROM embedeval:contiki-ng-817
WORKDIR /testbed
COPY upstream-fix.diff synthetic-test.diff /tmp/preparation/
RUN git apply /tmp/preparation/upstream-fix.diff && rm -rf /tmp/preparation
RUN bash tests/08-native-runs/embedeval-coap-block2/build.sh
CMD ["bash"]
