FROM embedeval:contiki-ng-3088
WORKDIR /testbed
COPY upstream-fix.diff synthetic-bug.diff /tmp/synthetic/
RUN git apply /tmp/synthetic/upstream-fix.diff && git apply /tmp/synthetic/synthetic-bug.diff && rm -f /tmp/synthetic/synthetic-bug.diff
RUN rm -rf /tmp/synthetic
RUN git add . && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -qm "Prepare synthetic benchmark"
RUN bash tests/08-native-runs/embedeval-tcp-retention/build.sh
CMD ["bash"]
