"""
Per-project configuration.

Values here are defaults for a whole project; anything a single PR needs to
differ on goes in that instance's metadata.json, which wins:

    value = meta.get(key, PROJECTS[project][key])

Every field below exists because the three runners genuinely differed on it.
Wording that merely differed by accident was unified into the shared templates
in run.py rather than parameterised here.
"""

from __future__ import annotations

# --- orientation -------------------------------------------------------------
# Derived from an analysis of 388 failed agent commands across the existing
# trajectories. Each line addresses an observed failure cluster; nothing is
# included that the agent can discover with `ls`.
#
#   zephyr  37/131 failures were agents re-specifying -b/<test path> (and
#           inventing board names like `native_sim_64`), plus 19 attempts to
#           set Kconfig from the command line with -D.
#   nuttx   51/158 were build-system errors, dominated by distclean +
#           configure.sh, which discards the container's own configuration.
#   riot    46/99 were hand-built make invocations, e.g. `make -C
#           tests/unittests test-nanocoap` -> "No rule to make target".

_ORIENTATION = {
    "zephyr": (
        "Builds use west + CMake + Ninja, and the build directory is already "
        "configured for this test. Run `west build` with no arguments to rebuild "
        "incrementally -- do not pass -b or a test path, and do not set Kconfig "
        "options with -D. Kconfig for a test lives in its prj.conf; the resolved "
        "values are in build/zephyr/.config."
    ),
    "nuttx": (
        "Two repos: the kernel at /testbed and the applications at /testbed/apps. "
        "Builds use GNU Make and are already configured for sim:nsh, which produces "
        "a native binary at /testbed/nuttx. Run `make -j$(nproc)` to rebuild "
        "incrementally. Never run distclean or configure.sh -- reconfiguring "
        "discards the container's setup and breaks the test runner."
    ),
    "riot": (
        "Builds use GNU Make. Tests are unit-test suites under tests/unittests that "
        "compile to a native host binary -- there is no emulator. `run_tests` already "
        "selects and builds the right suite; do not construct make invocations by hand."
    ),
}

# Zephyr only: `west build -t run` starts QEMU, which never exits, so the command
# hangs until the per-command timeout. NuttX and RIOT have no equivalent.
_EXTRA_WARNINGS = {
    "zephyr": (
        "Do NOT use `west build -t run` directly -- QEMU never exits cleanly, so that "
        "command hangs until the timeout and you will never see whether your fix worked."
    ),
}

PROJECTS: dict[str, dict] = {
    "zephyr": {
        "label": "Zephyr RTOS",
        "orientation": _ORIENTATION["zephyr"],
        "extra_warnings": _EXTRA_WARNINGS["zephyr"],
        # Paths the agent must not modify. Feeds the prompt, and lets the runner
        # exclude them from the captured patch while still recording that they
        # were touched.
        "protected_paths": ["tests/"],
        # Zephyr runs tests under QEMU, which holds an exclusive lock on
        # build/zephyr/qemu.pid. A docker exec timeout kills the client but not
        # the container-side QEMU, so every later run_tests fails with "Cannot
        # lock pid file" until the orphan is killed.
        "needs_qemu_cleanup": True,
        # What to remove before a clean rebuild during evaluation. Zephyr builds
        # out-of-tree into /testbed/build; nuttx builds in-tree and riot builds
        # under tests/unittests, so for those the previous hardcoded
        # `rm -rf /testbed/build` was a silent no-op.
        "clean_paths": ["/testbed/build"],
        "run_command": "west build -t run",
        # What the AGENT runs to rebuild after an edit. Distinct from
        # metadata's build_command, which is the cold build the evaluator uses
        # after wiping build/. Passing -b/<test path> here would contradict the
        # orientation and is how agents invented board names like
        # `native_sim_64`.
        "rebuild_command": "west build",
    },
    "nuttx": {
        "label": "NuttX RTOS",
        "orientation": _ORIENTATION["nuttx"],
        "extra_warnings": "",
        "protected_paths": ["apps/testing/", "apps/examples/"],
        "needs_qemu_cleanup": False,
        "clean_paths": [],
        "build_command": "make -j$(nproc)",
        "rebuild_command": "make -j$(nproc)",
        "run_command": "printf 'ostest\\npoweroff\\n' | timeout 300 ./nuttx",
    },
    "riot": {
        "label": "RIOT OS",
        "orientation": _ORIENTATION["riot"],
        "extra_warnings": "",
        "protected_paths": ["tests/"],
        "needs_qemu_cleanup": False,
        "clean_paths": [],
        # RIOT images are built for amd64; without this they fail to start on
        # arm64 hosts.
        "docker_platform": "linux/amd64",
        # run_tests compiles and runs the right suite; a separate rebuild step
        # would mean hand-constructing a make invocation, which is RIOT's single
        # largest observed failure mode.
        "rebuild_command": "",
    },
}


def config(project: str) -> dict:
    if project not in PROJECTS:
        raise KeyError(f"unknown project {project!r}; known: {sorted(PROJECTS)}")
    return PROJECTS[project]


def setting(meta: dict, key: str, default=None):
    """Per-PR metadata wins; fall back to the project default, then `default`."""
    if key in meta:
        return meta[key]
    return config(meta["project"]).get(key, default)
