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
#           set Kconfig from the command line with -D. Zephyr instances now
#           come from the twister pipeline, where `run_tests` fixes the board
#           and scenario, so the guidance is to use it and nothing else.
#   nuttx   51/158 were build-system errors, dominated by distclean +
#           configure.sh, which discards the container's own configuration.
#   riot    46/99 were hand-built make invocations, e.g. `make -C
#           tests/unittests test-nanocoap` -> "No rule to make target".

_ORIENTATION = {
    "zephyr": (
        "This instance builds and tests with Zephyr's twister for one board "
        "and one test scenario, both fixed. `run_tests` rebuilds incrementally "
        "(only what you changed is recompiled), runs the tests, and prints a "
        "status for every test case. Do not run `west build`, `west build -t "
        "run` or `scripts/twister` yourself, and do not pass a board, test "
        "path or Kconfig options anywhere -- the scenario's configuration is "
        "already applied. Build output is under /testbed/twister-out."
    ),
    "nuttx": (
        "Two repos: the kernel at /testbed and the applications at /testbed/apps. "
        "Builds use GNU Make and are already configured for sim:nsh, which produces "
        "a native binary at /testbed/nuttx. Run `make -j$(nproc)` to build; "
        "later builds are incremental. Never run distclean or configure.sh -- reconfiguring "
        "discards the container's setup and breaks the test runner."
    ),
    "riot": (
        "Builds use GNU Make. Tests are unit-test suites under tests/unittests that "
        "compile to a native host binary -- there is no emulator. `run_tests` already "
        "selects and builds the right suite; do not construct make invocations by hand."
    ),
}

# Zephyr only: twister's run_tests has its own exit codes.
_EXTRA_WARNINGS = {
    "zephyr": (
        "`run_tests` exit codes on this instance: 0 = all tests passed, "
        "1 = a test, the build or the configuration failed, 2 = the test "
        "runner did not start, 3 = nothing ran. A run takes one to a few "
        "minutes; tests under QEMU include the emulator's boot."
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
        # Zephyr instances are Apptainer images from the twister pipeline
        # (harness/apptainer_env.py). NuttX and RIOT still run in Docker,
        # except NuttX instances whose metadata sets "runtime": "apptainer".
        "runtime": "apptainer",
        # run_tests rebuilds as well as running, so there is no separate
        # rebuild step to show the agent.
        "rebuild_command": "",
        # Long enough for a full rebuild plus a QEMU test run. A command that
        # times out ends its container and every process in it, so QEMU cannot
        # be left running (it was under docker exec).
        "command_timeout": 900,
    },
    "nuttx": {
        "label": "NuttX RTOS",
        "orientation": _ORIENTATION["nuttx"],
        "extra_warnings": "",
        "protected_paths": ["apps/testing/", "apps/examples/"],
        "clean_paths": [],
        "docker_platform": "linux/amd64",
        # Generated images are configured but not prebuilt; a cold native
        # compile plus ostest took up to five minutes in local validation.
        "command_timeout": 600,
        "build_command": "make -j$(nproc)",
        "rebuild_command": "make -j$(nproc)",
        "run_command": "printf 'ostest\\npoweroff\\n' | timeout 300 ./nuttx",
    },
    "riot": {
        "label": "RIOT OS",
        "orientation": _ORIENTATION["riot"],
        "extra_warnings": "",
        "protected_paths": ["tests/"],
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


def config_for(meta: dict) -> dict:
    """The project configuration for one instance. A Zephyr instance must come
    from the twister pipeline; the Docker ones in docker/instances/ are kept as
    reference metadata only and cannot be run."""
    if meta["project"] == "zephyr" and meta.get("pipeline") != "twister":
        raise ValueError(f"{meta.get('instance_id')}: not a twister-pipeline instance. "
                         "Zephyr instances are run from generated/ "
                         "(EMBEDEVAL_INSTANCES_DIR=generated).")
    return config(meta["project"])


def config(project: str) -> dict:
    if project not in PROJECTS:
        raise KeyError(f"unknown project {project!r}; known: {sorted(PROJECTS)}")
    return PROJECTS[project]


def setting(meta: dict, key: str, default=None):
    """Per-PR metadata wins; fall back to the project default, then `default`."""
    if key in meta:
        return meta[key]
    return config(meta["project"]).get(key, default)
