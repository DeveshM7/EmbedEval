FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install only the essentials for compiling/testing native RIOT applications.
# RIOT's "native" board compiles with the host GCC — no cross-compiler or
# SDK required (unlike Zephyr). This keeps the image very small (~200MB).
#
# - build-essential: make, gcc, etc.
# - gcc-multilib / g++-multilib: for 32-bit/64-bit native builds
# - python3 + pyelftools: for RIOT's build-system scripts (mkallsyms.py etc.)
# - libncurses-dev: needed by some RIOT menuconfig or tools
# - xxd: for generating C headers from binary blobs
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc-multilib \
    g++-multilib \
    python3 \
    python3-pip \
    python3-setuptools \
    git \
    make \
    libncurses-dev \
    xxd \
    && rm -rf /var/lib/apt/lists/*

# pyelftools is needed by RIOT's post-link tooling.
# pexpect is needed by RIOT's Python test runner (tests/*/tests/01-run.py).
RUN pip3 install --no-cache-dir --break-system-packages pyelftools pexpect

# RIOT's standard Docker mapping mounts the repo at /data/riotbuild/riotbase.
# We use /testbed to match EmbedEval conventions; the Dockerfile ARGs in
# per-instance images set RIOTBASE accordingly.
WORKDIR /testbed
