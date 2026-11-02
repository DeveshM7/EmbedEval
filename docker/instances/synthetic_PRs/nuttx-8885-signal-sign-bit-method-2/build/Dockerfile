FROM embedeval:nuttx-8885-signal-sign-bit-correct
WORKDIR /testbed
COPY synthetic-bug.diff /tmp/injection.diff
RUN git apply /tmp/injection.diff && rm /tmp/injection.diff
RUN git add . && git -c user.name=EmbedEval -c user.email=synthetic@embedeval.invalid commit -qm "Prepare synthetic benchmark"
RUN make -j4
CMD ["bash"]
