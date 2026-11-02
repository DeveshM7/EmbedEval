FROM embedeval:contiki-ng-817-payload-derived-size-correct
WORKDIR /testbed
COPY synthetic-bug.diff /tmp/injection.diff
RUN git apply /tmp/injection.diff && rm /tmp/injection.diff
RUN git add . && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -qm "Prepare synthetic benchmark"
RUN bash tests/08-native-runs/embedeval-coap-block2/build.sh
CMD ["bash"]
