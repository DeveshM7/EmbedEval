FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

# System packages — matches official Zephyr getting started guide (Ubuntu 24.04)
# NOTE: No QEMU here. The Zephyr SDK bundles its own custom QEMU build
# with Zephyr-specific board/machine definitions. Installing system QEMU
# would conflict with the SDK's version.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake ninja-build gperf ccache dfu-util device-tree-compiler wget \
    python3-dev python3-pip python3-venv python3-tk \
    xz-utils file make gcc libsdl2-dev libmagic1t64 \
    universal-ctags cscope \
    && rm -rf /var/lib/apt/lists/*

# Create a Python virtual environment matching the official Zephyr docs
RUN python3 -m venv /opt/zephyr-venv
ENV PATH="/opt/zephyr-venv/bin:$PATH"

# Install west inside the venv
RUN pip install west
