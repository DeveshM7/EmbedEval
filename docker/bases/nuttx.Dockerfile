FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# ── Core build tools (required for all targets) ───────────────────────────────
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    gcc-multilib \
    g++-multilib \
    make \
    git \
    python3 \
    python3-pip \
    python3-dev \
    libncurses-dev \
    bison \
    flex \
    xxd \
    wget \
    curl \
    kconfig-frontends \
    genromfs \
    universal-ctags \
    && rm -rf /var/lib/apt/lists/*

# ── ARM Cortex-M / Cortex-A cross-compiler ────────────────────────────────────
# Covers: stm32*, lpc*, nrf*, samd*, imxrt*, qemu-armv7a, mps2-an385, etc.
RUN apt-get update && apt-get install -y \
    gcc-arm-none-eabi \
    binutils-arm-none-eabi \
    && rm -rf /var/lib/apt/lists/*

# ── ARM64 / AArch64 cross-compiler ───────────────────────────────────────────
# Covers: qemu-armv8a:nsh, etc.
RUN apt-get update && apt-get install -y \
    gcc-aarch64-linux-gnu \
    binutils-aarch64-linux-gnu \
    && rm -rf /var/lib/apt/lists/*

# ── RISC-V cross-compiler (xPack riscv-none-elf-gcc) ─────────────────────────
# Ubuntu apt gcc-riscv64-unknown-elf is GCC 10 and lacks rv64imafdcv (vector).
# NuttX CI uses xPack GCC 14.2 — same toolchain used here.
RUN curl -sL \
    "https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v14.2.0-3/xpack-riscv-none-elf-gcc-14.2.0-3-linux-x64.tar.gz" \
    | tar -C /opt -xz \
    && ln -s /opt/xpack-riscv-none-elf-gcc-14.2.0-3/bin/* /usr/local/bin/

# ── QEMU for runtime testing on emulated targets ─────────────────────────────
# qemu-system-arm  : ARM Cortex-A/M (qemu-armv7a:nsh, mps2-an385:nsh)
# qemu-system-misc : RISC-V (rv-virt:nsh)
RUN apt-get update && apt-get install -y \
    qemu-system-arm \
    qemu-system-misc \
    && rm -rf /var/lib/apt/lists/*

# ── CMake + Ninja (for CMake-based NuttX builds) ─────────────────────────────
RUN apt-get update && apt-get install -y \
    cmake \
    ninja-build \
    && rm -rf /var/lib/apt/lists/*

# ── SDL2 (for sim:lvgl headless builds) ──────────────────────────────────────
RUN apt-get update && apt-get install -y \
    libsdl2-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Python tools ─────────────────────────────────────────────────────────────
# pyelftools + cxxfilt: required by NuttX post-link mkallsyms.py script
# kconfiglib: required by CMake-based NuttX builds for Kconfig processing
RUN pip3 install pyelftools cxxfilt kconfiglib
