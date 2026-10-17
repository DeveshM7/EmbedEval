"""Where a NuttX instance runs: the native simulator, or a QEMU board.

An instance's metadata `sim_config` is the ./tools/configure.sh target
(sim:nsh, qemu-armv8a:nsh_smp, rv-virt:nsh64, ...). This module says, for each
target, which shared image builds and runs it, how QEMU is started and which
config the runner needs besides the PR's own. The triage prompt names boards
loosely (qemu-rv:nsh, qemu-rv-virt:knsh64); `canonical` maps those names to
real targets.

The runner is templates/nuttx/qemu-armv8a/run_tests.sh.tmpl for every QEMU
board; only the QEMU command line differs.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "nuttx"

# Names the triage prompt uses -> configure targets.
ALIASES = {
    "qemu-rv:nsh": "rv-virt:nsh",
    "qemu-rv:nsh64": "rv-virt:nsh64",
    "qemu-rv-virt:knsh64": "rv-virt:knsh64",
    "qemu-rv64ilp32:nsh": "rv-virt:nsh64",
}
# Targets reached through an alias that need more than the target's own
# defconfig (the triage name says so).
ALIAS_OVERRIDES = {
    "qemu-rv64ilp32:nsh": {"CONFIG_RISCV_TOOLCHAIN_GNU_RV64": "n",
                           "CONFIG_RISCV_TOOLCHAIN_GNU_RV64ILP32": "y"},
}

_ARMV8A = ("qemu-armv8a/nuttx-qemu.def",
           'qemu-system-aarch64 -cpu cortex-a53 -smp "$NCPUS" -nographic '
           "-machine virt,virtualization=on,gic-version=3 -net none "
           "-chardev stdio,id=con,mux=on -serial chardev:con "
           "-mon chardev=con,mode=readline -kernel ./nuttx")
_ARMV7A = ("qemu-misc/nuttx-qemu-misc.def",
           'qemu-system-arm -cpu cortex-a7 -smp "$NCPUS" -nographic '
           "-machine virt,virtualization=off,gic-version=2 -net none "
           "-chardev stdio,id=con,mux=on -serial chardev:con "
           "-mon chardev=con,mode=readline -kernel ./nuttx")
_RV32 = ("qemu-misc/nuttx-qemu-misc.def",
         'qemu-system-riscv32 -semihosting -M virt -cpu rv32 -smp "$NCPUS" '
         "-bios none -kernel ./nuttx -nographic")
_RV64 = ("qemu-misc/nuttx-qemu-misc.def",
         'qemu-system-riscv64 -semihosting -M virt -cpu rv64 -smp "$NCPUS" '
         "-bios none -kernel ./nuttx -nographic")

# target -> (image recipe under templates/nuttx, QEMU command line)
QEMU_BOARDS = {
    "qemu-armv8a:nsh": _ARMV8A,
    "qemu-armv8a:nsh_smp": _ARMV8A,
    "qemu-armv7a:nsh": _ARMV7A,
    "qemu-armv7a:smp": _ARMV7A,
    "rv-virt:nsh": _RV32,
    "rv-virt:smp": _RV32,
    "rv-virt:nsh64": _RV64,
    "rv-virt:smp64": _RV64,
    "rv-virt:knsh64": _RV64,
}
# Config every run on the board needs, set after the PR's own overrides: the
# scheduler's info logging interleaves between CPUs on SMP boards and floods
# the log.
BOARD_OVERRIDES = {"qemu-armv8a:": {"CONFIG_DEBUG_SCHED_INFO": "n"}}


# Triage names whose toolchain exists for x86 hosts only.
X86_ONLY = {"qemu-rv64ilp32:nsh"}


def canonical(platform: str) -> str:
    return ALIASES.get(platform, platform)


def supported(platform: str) -> bool:
    return platform == "sim:nsh" or canonical(platform) in QEMU_BOARDS


def is_qemu(platform: str) -> bool:
    return canonical(platform) in QEMU_BOARDS


def alias_overrides(platform: str) -> dict:
    return dict(ALIAS_OVERRIDES.get(platform, {}))


def board_overrides(target: str) -> dict:
    return next((dict(v) for k, v in BOARD_OVERRIDES.items() if target.startswith(k)), {})


def image_recipe(target: str) -> Path:
    return TEMPLATES / (QEMU_BOARDS[target][0] if target in QEMU_BOARDS else "apptainer/base.def")


def qemu_command(target: str) -> str:
    return QEMU_BOARDS[target][1]
