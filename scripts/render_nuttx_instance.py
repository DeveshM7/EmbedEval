#!/usr/bin/env python3
"""Render a native NuttX instance from reviewed metadata and an apps test patch.

This step deliberately consumes a selected test patch. PR discovery, commit
selection, and separation of test changes from production code happen before it.
Output defaults to generated/, leaving hand-written instances untouched.

An instance is a Docker image by default (Dockerfile). With --runtime apptainer
it is an Apptainer recipe instead (instance.def, FROM the shared image of
templates/nuttx/apptainer/base.def), for hosts without Docker such as
Scholar's ARM64 nodes; its metadata says "runtime": "apptainer". An existing
instance is re-rendered that way from its own files:

    python scripts/render_nuttx_instance.py --runtime apptainer \
        --metadata docker/instances/nuttx__nuttx-11889/metadata.json \
        --test-patch docker/instances/nuttx__nuttx-11889/test_patch.diff
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path

import nuttx_platforms as platforms

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates" / "nuttx"
DEFAULT_OUT = REPO_ROOT / "generated"


# printf("\nuser_main: cancel test\n"); in ostest_main.c prints this banner
# just before calling the test's entry function.
BANNER_PRINTF = re.compile(r'printf\s*\(\s*"\\nuser_main: ([^"\\%]+)\\n"\s*\);')
BANNER_TEXT = re.compile(r"user_main: [^\n\\%]+")
# Non-static `void name(void)` definitions at the start of a line.
ENTRY_DEF = re.compile(r"^(?!static\b)[a-z][\w ]*?\b(\w+)\s*\(void\)\s*\{?\s*$",
                       re.MULTILINE)


def ostest_banners(ostest_dir: Path, changed: list[str], required: bool = True) -> list[str]:
    """Banners ostest prints before the tests defined in the changed sources.

    ostest_dir is testing/ostest with the test patch applied. Each changed
    source's entry functions are looked up where ostest_main.c calls them,
    and the banner printed within a few lines before that call is kept. A
    preprocessor line between them (e.g. a skip under #ifdef) means the
    banner does not prove the call ran. A test called without a banner is
    fine when another call of it has one (since apps #3759, `ostest wqueue`
    calls wqueue_test() a second time, bannerless); a test whose every call
    lacks a banner is an error. With required=False (compile_error cases,
    where the test is that the changed source compiles) missing banners are
    not: a test without one is skipped, and the list may be empty.
    """
    main_lines = (ostest_dir / "ostest_main.c").read_text().splitlines()
    banners: list[str] = []
    for path in changed:
        name = Path(path).name
        if not path.startswith("testing/ostest/") or not name.endswith(".c") or \
                name == "ostest_main.c":
            continue
        for func in ENTRY_DEF.findall((ostest_dir / name).read_text()):
            call = re.compile(rf"\b{re.escape(func)}\s*\(\s*\)\s*(?:;|[!=<>]=?|&&|\|\||\))")
            called, found = False, []
            for i, line in enumerate(main_lines):
                if not call.search(line):
                    continue
                called = True
                for prev in reversed(main_lines[max(0, i - 8):i]):
                    if prev.lstrip().startswith("#"):
                        break
                    match = BANNER_PRINTF.search(prev)
                    if match:
                        found.append(f"user_main: {match[1]}")
                        break
            if called and not found and required:
                raise ValueError(f"no ostest banner directly precedes the call to {func}")
            for banner in found:
                if banner not in banners:
                    banners.append(banner)
    if not banners and required:
        raise ValueError("no ostest banner precedes a test defined in the changed sources")
    return banners


def render_runner(banners: list[str], sim_config: str = "sim:nsh",
                  runtime: str = "docker") -> str:
    qemu = platforms.is_qemu(sim_config)
    # Apptainer's sim runner types its commands one at a time (2020 kernels
    # drop console input that arrives early); Docker's is frozen with its images.
    runner = (TEMPLATES / ("qemu-armv8a/run_tests.sh.tmpl" if qemu
                           else "apptainer/run_tests.sh.tmpl" if runtime == "apptainer"
                           else "run_tests.sh.tmpl")).read_text()
    if qemu:
        runner = runner.replace("@@QEMU_COMMAND@@", platforms.qemu_command(sim_config))
    return runner.replace("@@BANNERS@@", " ".join(shlex.quote(b) for b in banners))


def render(meta: dict, patch_file: Path, out_root: Path,
           tag_suffix: str = "-gen", runtime: str = "docker") -> Path:
    instance_id = meta["instance_id"]
    if meta["project"] != "nuttx" or not re.fullmatch(r"nuttx__nuttx-\d+", instance_id):
        raise ValueError("expected a NuttX instance ID")
    if runtime not in ("docker", "apptainer"):
        raise ValueError("runtime must be docker or apptainer")
    # Empty gives the standard embedeval:nuttx-<PR> tag of a checked-in instance.
    if not re.fullmatch(r"(-[a-z0-9-]+)?", tag_suffix):
        raise ValueError("tag_suffix must be empty, or start with - and contain "
                         "lowercase letters, digits, or -")
    if meta["kernel_repo"] != "https://github.com/apache/nuttx" or \
            meta["apps_repo"] != "https://github.com/apache/nuttx-apps":
        raise ValueError("expected the Apache NuttX kernel and apps repositories")
    sim_config = meta.get("sim_config", "sim:nsh")
    if meta.get("test_app", "ostest") != "ostest" or not platforms.supported(sim_config) or (
            runtime == "docker" and sim_config != "sim:nsh"):
        raise ValueError("this runtime supports sim:nsh or a QEMU board "
                         f"({', '.join(platforms.QEMU_BOARDS)}) with ostest")
    if meta["fail_type"] not in ("runtime_assert", "runtime_other", "compile_error"):
        raise ValueError("expected runtime_assert, runtime_other or compile_error")
    for name in ("kernel_base_commit", "kernel_merge_commit", "apps_base_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", meta[name]):
            raise ValueError(f"{name} must be a full commit SHA")
    if not meta.get("files_changed_by_fix"):
        raise ValueError("files_changed_by_fix is required for validation")
    if meta["fail_type"] == "compile_error" and \
            (not meta.get("compile_test_source") or not meta.get("missing_api")):
        raise ValueError("compile cases need compile_test_source and missing_api")
    banners = meta.get("test_banners")
    if (not banners and meta["fail_type"] != "compile_error") or \
            any(not BANNER_TEXT.fullmatch(b) for b in banners or []):
        raise ValueError("test_banners must list ostest 'user_main: ...' lines")
    if not isinstance(meta.get("clean_after_patch", False), bool):
        raise ValueError("clean_after_patch must be a boolean")
    overrides = dict(meta.get("config_overrides") or {})
    overrides.update(platforms.board_overrides(sim_config))
    reserved = {"CONFIG_TESTING_OSTEST", "CONFIG_NSH_CONSOLE_LOGIN", "CONFIG_NDEBUG"}
    # y/n for bools; decimal or hex for int-valued symbols (e.g. CONFIG_SMP_NCPUS=4,
    # CONFIG_SMP_DEFAULT_CPUSET=0xffffffff); a quoted literal for string-valued ones.
    value_pattern = re.compile(r'y|n|-?\d+|0x[0-9a-fA-F]+|"[^"\n]*"')
    for name, value in overrides.items():
        if name in reserved:
            raise ValueError(f"reserved config override: {name}")
        if not re.fullmatch(r"CONFIG_[A-Z0-9_]+", name) or not value_pattern.fullmatch(value):
            raise ValueError(f"invalid config override: {name}={value}")

    patch = patch_file.read_text()
    changed = re.findall(r"^diff --git a/(\S+) b/\1$", patch, re.MULTILINE)
    listed = meta["files_changed_by_test_patch"]
    if not changed or len(changed) != patch.count("diff --git ") or set(changed) != set(listed):
        raise ValueError("patch paths must match files_changed_by_test_patch exactly")
    if any(not path.startswith("testing/ostest/") for path in changed):
        raise ValueError("this ostest template accepts only testing/ostest/ patch paths")

    inst = out_root / instance_id
    if inst.exists():
        raise FileExistsError(f"output already exists: {inst}")
    inst.mkdir(parents=True)

    steps = []
    for name, value in sorted(overrides.items()):
        steps.append(
            f"sed -i '/^{name}=/d; /^# {name} is not set/d' .config"
        )
        steps.append(f"printf '%s\\n' '{name}={value}' >> .config")
        expected = f"{name}={value}"
        # New symbols may not exist until the kernel fix is applied, so
        # append after olddefconfig and verify this exact input is present.
        steps.append(f"grep -q '^{re.escape(expected)}$' .config")
    runner = render_runner(banners, sim_config, runtime)

    generated_meta = dict(meta)
    generated_meta.update({
        "sim_config": sim_config,
        "config_overrides": overrides,
        "test_app": "ostest",
        "build_command": platforms.build_command(sim_config, overrides),
        "docker_image": f"embedeval:nuttx-{instance_id.rsplit('-', 1)[1]}{tag_suffix}",
        "generated": True,
    })
    if runtime == "docker":
        dockerfile = (TEMPLATES / "Dockerfile.tmpl").read_text()
        dockerfile = dockerfile.replace("@@KERNEL_BASE_COMMIT@@", meta["kernel_base_commit"])
        dockerfile = dockerfile.replace("@@APPS_BASE_COMMIT@@", meta["apps_base_commit"])
        extra_config_run = "RUN " + " \\\n    && ".join(steps) if steps else ""
        dockerfile = dockerfile.replace("@@EXTRA_CONFIG_RUN@@", extra_config_run)
        (inst / "Dockerfile").write_text(dockerfile)
    else:
        recipe = (TEMPLATES / "apptainer" / "instance.def.tmpl").read_text()
        # Derive defaults for enabled base-kernel symbols, then restore any
        # override whose symbol only appears after the kernel fix.
        apptainer_steps = steps + (["make olddefconfig", *steps] if steps else [])
        for key, value in (("INSTANCE_ID", instance_id),
                           ("KERNEL_BASE_COMMIT", meta["kernel_base_commit"]),
                           ("APPS_BASE_COMMIT", meta["apps_base_commit"]),
                           ("SIM_CONFIG", sim_config),
                           ("EXTRA_CONFIG", "\n".join(f"    {s}" for s in apptainer_steps))):
            recipe = recipe.replace(f"@@{key}@@", value)
        # No Docker image: scripts/build_instances.py builds a .sif from the
        # recipe, and the harness takes "runtime" over the project default.
        del generated_meta["docker_image"]
        generated_meta.update(runtime="apptainer", apptainer_image=f"{instance_id}.sif")
        (inst / "instance.def").write_text(recipe)
    (inst / "run_tests.sh").write_text(runner)
    (inst / "test_patch.diff").write_text(patch)
    (inst / "metadata.json").write_text(json.dumps(generated_meta, indent=4) + "\n")
    return inst


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--test-patch", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tag-suffix", default="-gen")
    parser.add_argument("--runtime", choices=("docker", "apptainer"), default="docker")
    args = parser.parse_args()
    inst = render(json.loads(args.metadata.read_text()), args.test_patch,
                  args.out, args.tag_suffix, args.runtime)
    print(inst)


if __name__ == "__main__":
    main()
