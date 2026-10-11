#!/usr/bin/env python3
"""Render a native NuttX instance from reviewed metadata and an apps test patch.

This step deliberately consumes a selected test patch. PR discovery, commit
selection, and separation of test changes from production code happen before it.
Output defaults to generated/, leaving hand-written instances untouched.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates" / "nuttx"
DEFAULT_OUT = REPO_ROOT / "generated"


def render(meta: dict, patch_file: Path, out_root: Path,
           tag_suffix: str = "-gen") -> Path:
    instance_id = meta["instance_id"]
    if meta["project"] != "nuttx" or not re.fullmatch(r"nuttx__nuttx-\d+", instance_id):
        raise ValueError("expected a NuttX instance ID")
    if not re.fullmatch(r"-[a-z0-9-]+", tag_suffix):
        raise ValueError("tag_suffix must start with - and contain lowercase letters, digits, or -")
    if meta["kernel_repo"] != "https://github.com/apache/nuttx" or \
            meta["apps_repo"] != "https://github.com/apache/nuttx-apps":
        raise ValueError("expected the Apache NuttX kernel and apps repositories")
    if meta.get("sim_config", "sim:nsh") != "sim:nsh" or meta.get("test_app", "ostest") != "ostest":
        raise ValueError("this template supports only sim:nsh with ostest")
    if meta["fail_type"] not in ("runtime_assert", "compile_error"):
        raise ValueError("expected runtime_assert or compile_error")
    for name in ("kernel_base_commit", "kernel_merge_commit", "apps_base_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", meta[name]):
            raise ValueError(f"{name} must be a full commit SHA")
    if not meta.get("files_changed_by_fix"):
        raise ValueError("files_changed_by_fix is required for validation")
    if meta["fail_type"] == "compile_error" and \
            (not meta.get("compile_test_source") or not meta.get("missing_api")):
        raise ValueError("compile cases need compile_test_source and missing_api")
    for name in ("test_success_marker", "test_failure_marker"):
        if not meta.get(name) or "\n" in meta[name]:
            raise ValueError(f"{name} must be one nonempty line")
    if "\n" in meta.get("test_failure_context_marker", ""):
        raise ValueError("test_failure_context_marker must be one line")
    if meta.get("test_failure_match", "literal") not in ("literal", "regex"):
        raise ValueError("test_failure_match must be literal or regex")
    if not isinstance(meta.get("clean_after_patch", False), bool):
        raise ValueError("clean_after_patch must be a boolean")
    overrides = meta.get("config_overrides") or {}
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

    dockerfile = (TEMPLATES / "Dockerfile.tmpl").read_text()
    dockerfile = dockerfile.replace("@@KERNEL_BASE_COMMIT@@", meta["kernel_base_commit"])
    dockerfile = dockerfile.replace("@@APPS_BASE_COMMIT@@", meta["apps_base_commit"])
    extra_config_run = ""
    if overrides:
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
        extra_config_run = "RUN " + " \\\n    && ".join(steps)
    dockerfile = dockerfile.replace("@@EXTRA_CONFIG_RUN@@", extra_config_run)
    runner = (TEMPLATES / "run_tests.sh.tmpl").read_text()
    runner = runner.replace("@@SUCCESS_MARKER@@", shlex.quote(meta["test_success_marker"]))
    runner = runner.replace("@@FAILURE_MARKER@@", shlex.quote(meta["test_failure_marker"]))
    runner = runner.replace("@@FAILURE_MATCH@@",
                            shlex.quote(meta.get("test_failure_match", "literal")))
    runner = runner.replace("@@FAILURE_CONTEXT_MARKER@@",
                            shlex.quote(meta.get("test_failure_context_marker", "")))

    generated_meta = dict(meta)
    generated_meta.update({
        "sim_config": "sim:nsh",
        "test_app": "ostest",
        "build_command": "make -j$(nproc)",
        "docker_image": f"embedeval:nuttx-{instance_id.rsplit('-', 1)[1]}{tag_suffix}",
        "generated": True,
    })
    (inst / "Dockerfile").write_text(dockerfile)
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
    args = parser.parse_args()
    inst = render(json.loads(args.metadata.read_text()), args.test_patch,
                  args.out, args.tag_suffix)
    print(inst)


if __name__ == "__main__":
    main()
