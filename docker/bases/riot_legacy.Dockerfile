FROM docker.io/riot/riotbuild@sha256:08fa7da2c702ac4db7cf57c23fc46c1971f3bffc4a6eff129793f853ec808736

# Override the entrypoint if there is any inherited from riotbuild
ENTRYPOINT []

# Change to the testbed working directory expected by EmbedEval
WORKDIR /testbed
