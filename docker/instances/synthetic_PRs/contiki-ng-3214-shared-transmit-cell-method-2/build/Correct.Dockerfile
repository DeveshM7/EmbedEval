FROM embedeval:contiki-ng-3214
WORKDIR /testbed
COPY upstream-fix.diff synthetic-test.diff /tmp/preparation/
RUN git apply /tmp/preparation/upstream-fix.diff && git apply /tmp/preparation/synthetic-test.diff && rm -rf /tmp/preparation
RUN bash tests/08-native-runs/embedeval-orchestra/build.sh
CMD ["bash"]
