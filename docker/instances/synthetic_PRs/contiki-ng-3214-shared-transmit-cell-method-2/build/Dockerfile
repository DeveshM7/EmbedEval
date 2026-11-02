FROM embedeval:contiki-ng-3214-shared-transmit-cell-correct
WORKDIR /testbed
COPY synthetic-bug.diff /tmp/injection.diff
RUN git apply /tmp/injection.diff && rm /tmp/injection.diff
RUN git add . && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -qm "Prepare synthetic benchmark"
RUN bash tests/08-native-runs/embedeval-orchestra/build.sh
CMD ["bash"]
