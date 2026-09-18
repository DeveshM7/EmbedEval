"""
Build-time project configuration.

Distinct from harness/projects.py, which describes how to talk to an agent at
run time. This describes how to construct an image: which base to build FROM,
and which metadata fields become which docker --build-arg.
"""

from __future__ import annotations


def _riot_unit_tests(meta: dict) -> str:
    """
    RIOT selects its test suite with UNIT_TESTS=tests-<name>.

    Read it out of extra_make_args rather than the `unit_tests` field: that
    field says "tests-saul_reg" in every RIOT instance, which is correct only
    for the (now removed) 5323 instance and stale everywhere else.
    """
    for arg in meta.get("extra_make_args", []):
        if arg.startswith("UNIT_TESTS="):
            return arg.split("=", 1)[1]
    raise KeyError(
        f"{meta['instance_id']}: no UNIT_TESTS=... in extra_make_args"
    )


BUILD: dict[str, dict] = {
    "zephyr": {
        "base_image": "embedeval-zephyr-base:latest",
        "base_dockerfile": "docker/bases/zephyr.Dockerfile",
        # docker ARG name -> how to get its value from metadata
        "build_args": {
            "BASE_COMMIT": lambda m: m["base_commit"],
            "PLATFORM": lambda m: m["platform"],
            "TEST_PATH": lambda m: m["test_path"],
        },
        "platform": None,
        "clean_paths": ["/testbed/build"],
    },
    "nuttx": {
        "base_image": "embedeval-nuttx-base:latest",
        "base_dockerfile": "docker/bases/nuttx.Dockerfile",
        # NuttX is two repos: the kernel and the applications tree.
        "build_args": {
            "KERNEL_BASE_COMMIT": lambda m: m["kernel_base_commit"],
            "APPS_BASE_COMMIT": lambda m: m["apps_base_commit"],
        },
        "platform": None,
        "clean_paths": [],
    },
    "riot": {
        "base_image": "embedeval-riot-base:latest",
        "base_dockerfile": "docker/bases/riot.Dockerfile",
        "build_args": {
            "BASE_COMMIT": lambda m: m["base_commit"],
            "BOARD": lambda m: m.get("board", "native"),
            "UNIT_TESTS": _riot_unit_tests,
        },
        "platform": "linux/amd64",
        # riot builds under tests/unittests and its build_command already does
        # `clean all`, so there is nothing to wipe first.
        "clean_paths": [],
    },
}

# docker/bases/riot_legacy.Dockerfile is not listed above: its only consumer
# was riot__riot-5323, which was removed. Build it by hand if a pre-2017 RIOT
# instance ever needs it.


def config(project: str) -> dict:
    if project not in BUILD:
        raise KeyError(f"unknown project {project!r}; known: {sorted(BUILD)}")
    return BUILD[project]


def build_args(meta: dict) -> dict[str, str]:
    """Resolve every --build-arg for one instance."""
    return {k: str(fn(meta)) for k, fn in config(meta["project"])["build_args"].items()}
