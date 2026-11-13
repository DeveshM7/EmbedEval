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
        "a native binary at /testbed/nuttx. Run `make -j$(nproc)` to rebuild "
        "incrementally. Never run distclean or configure.sh -- reconfiguring "
        "discards the container's setup and breaks the test runner."
    ),
    "mynewt": (
        "The Apache Mynewt (mynewt-core) source is at /testbed; its tests are a "
        "selftest package built with `newt` for the native simulator through a "
        "newt project at /project. `run_tests` builds and runs every test case. "
        "Fix library source, never the selftest package. Do not edit /project."
    ),
    "openthread": (
        "OpenThread is built with CMake and Ninja into /testbed/build, which is "
        "already configured and built for this instance's test target. Run "
        "`run_tests` to rebuild incrementally (only what you changed is "
        "recompiled), run the tests and print a status for every test in the "
        "task. Do not run cmake or change its options, and do not build or run "
        "test binaries by hand -- `run_tests` does it with the right target."
    ),
    "riot": (
        "Builds use GNU Make. Tests are unit-test suites under tests/unittests that "
        "compile to a native host binary -- there is no emulator. `run_tests` already "
        "selects and builds the right suite; do not construct make invocations by hand."
    ),
}

# Zephyr, Mynewt and OpenThread: their run_tests have exit codes of their own.
_EXTRA_WARNINGS = {
    "zephyr": (
        "`run_tests` exit codes on this instance: 0 = all tests passed, "
        "1 = a test, the build or the configuration failed, 2 = the test "
        "runner did not start, 3 = nothing ran. It passes only when every test "
        "case in the scenario passes, not just the ones listed in the task: "
        "that is what grading checks. A run takes one to several minutes; "
        "tests under QEMU include the emulator's boot."
    ),
    "mynewt": (
        "`run_tests` exit codes on this instance: 0 = every test case passed, "
        "1 = a test case failed, 2 = timed out, 3 = the build failed or the "
        "results were incomplete."
    ),
    "openthread": (
        "`run_tests` exit codes on this instance: 0 = every listed test passed, "
        "1 = a listed test failed or crashed (a crash marks the running test "
        "`error` and the ones after it `not_run`), 2 = the configure or the "
        "build failed, 3 = a listed test did not run. After a change to a "
        "widely included header a rebuild can take several minutes."
    ),
}

# NuttX instances rendered for Apptainer (scripts/render_nuttx_instance.py), whose
# metadata says "runtime": "apptainer". Their images are configured but not
# prebuilt, and their run_tests (templates/nuttx/apptainer/run_tests.sh.tmpl)
# only runs ostest. The Docker NuttX instances keep the project's settings.
_NUTTX_APPTAINER = (
    "Two repos: the kernel at /testbed and the applications at /testbed/apps. "
    "Builds use GNU Make and are already configured for sim:nsh, which produces "
    "a native binary at /testbed/nuttx. Run `make -j$(nproc)` to build; "
    "later builds are incremental. Never run distclean or configure.sh -- reconfiguring "
    "discards the container's setup and breaks the test runner."
)
_NUTTX_APPTAINER_WARNINGS = (
    "`run_tests` exit codes on this instance: run it after building; it runs the whole "
    "ostest suite. 0 = ostest finished cleanly and every changed test ran, 1 = any "
    "other completed run (a test failed, or a changed test did not run), 2 = timed out. "
    "If `run_tests` times out (exit code 2) with no test results, your change has "
    "introduced a hang or an infinite loop. Do NOT submit -- investigate further."
)

# RIOT instances built as a layer (scripts/riot_instance.py).
_RIOT_LAYER = (
    "Builds use GNU Make and RIOT's native board, which compiles RIOT into an ordinary "
    "Linux process: there is no emulator. The tests are unit-test groups under "
    "tests/unittests or test programs under tests/. `run_tests` builds them "
    "incrementally and runs them; do not construct make invocations by hand."
)
_RIOT_LAYER_WARNINGS = (
    "`run_tests` exit codes on this instance: 0 = all expected tests passed, 1 = a test "
    "or a build failed, 3 = nothing ran."
)

PROJECTS: dict[str, dict] = {
    "contiki-ng": {
        "label": "Contiki-NG",
        "orientation": "Run `run_tests` to rebuild and execute the fixed upstream native units or Cooja scenarios. The selected target and configuration are fixed by this instance.",
        "extra_warnings": "Exit 2 means a build failed; exit 3 means required named tests did not execute.",
        "protected_paths": ["tests/", "regression-tests/", "tools/cooja/"],
        "runtime": "apptainer", "rebuild_command": "", "command_timeout": 1800,
    },
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
        # except NuttX instances whose metadata sets "runtime": "apptainer"
        # and RIOT layer instances (config_for).
        "runtime": "apptainer",
        # run_tests rebuilds as well as running, so there is no separate
        # rebuild step to show the agent.
        "rebuild_command": "",
        # Long enough for a full rebuild plus a QEMU test run. A command that
        # times out ends its container and every process in it, so QEMU cannot
        # be left running (it was under docker exec).
        "command_timeout": 900,
        # The image has a ctags index at /testbed/tags; the prompt points to it.
        "ctags": True,
    },
    "openthread": {
        "label": "OpenThread",
        "orientation": _ORIENTATION["openthread"],
        "extra_warnings": _EXTRA_WARNINGS["openthread"],
        "protected_paths": ["tests/"],
        "runtime": "apptainer",
        # run_tests configures (once), builds and runs.
        "rebuild_command": "",
        # A rebuild of the core library after a header change, then the tests.
        "command_timeout": 1800,
    },
    "nuttx": {
        "label": "NuttX RTOS",
        "orientation": _ORIENTATION["nuttx"],
        "extra_warnings": "",
        "protected_paths": ["apps/testing/", "apps/examples/"],
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
        "clean_paths": [],
        # RIOT images are built for amd64; without this they fail to start on
        # arm64 hosts.
        "docker_platform": "linux/amd64",
        # run_tests compiles and runs the right suite; a separate rebuild step
        # would mean hand-constructing a make invocation, which is RIOT's single
        # largest observed failure mode.
        "rebuild_command": "",
    },
    "mynewt": {
        "label": "Apache Mynewt",
        "orientation": _ORIENTATION["mynewt"],
        # An instance's own warnings (metadata) follow these (harness/run.py).
        "extra_warnings": _EXTRA_WARNINGS["mynewt"],
        "protected_paths": ["fs/fcb2/selftest/", "kernel/os/selftest/", "encoding/json/selftest/"],
        "needs_qemu_cleanup": False,
        "clean_paths": [],
        "docker_platform": "linux/amd64",
        "runtime": "apptainer",
        "build_command": "true",
        "rebuild_command": "",
        "run_command": "run_tests",
    },
}


# Instance pipelines graded as Apptainer images (evaluate_patches.py): twister
# (Zephyr), riot-layer (RIOT), cmake (OpenThread), contiki-ng. Apptainer NuttX
# and Mynewt instances are told apart by their metadata "runtime" instead.
APPTAINER_PIPELINES = ("twister", "riot-layer", "cmake", "contiki-ng")


def config_for(meta: dict) -> dict:
    """The project configuration for one instance. A Zephyr instance must come
    from the twister pipeline; the Docker ones in docker/instances/ are kept as
    reference metadata only and cannot be run."""
    if meta["project"] == "contiki-ng" and meta.get("pipeline") != "contiki-ng":
        raise ValueError("Contiki-NG requires a generated Contiki-NG pipeline instance")
    if meta.get("pipeline") == "riot-layer":
        return {**config("riot"), "runtime": "apptainer", "orientation": _RIOT_LAYER,
                "extra_warnings": _RIOT_LAYER_WARNINGS}
    if meta["project"] == "nuttx" and meta.get("runtime") == "apptainer":
        # A cold native compile plus ostest took up to five minutes in local
        # validation, hence the longer command limit.
        return {**config("nuttx"), "runtime": "apptainer", "orientation": _NUTTX_APPTAINER,
                "extra_warnings": _NUTTX_APPTAINER_WARNINGS, "command_timeout": 600}
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
