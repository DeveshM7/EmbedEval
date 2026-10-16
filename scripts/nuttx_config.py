#!/usr/bin/env python3
"""The effective configuration of a proposed NuttX pair.

Configures the kernel and apps trees exactly as templates/nuttx/Dockerfile.tmpl
does (sim:nsh, ostest on, console login and NDEBUG off; another board:config
such as qemu-armv8a:nsh_smp the same way), adds the proposed
config_overrides, runs `make olddefconfig`, and reports which overrides
stuck, why any were dropped, and whether the changed ostest sources are built
and their entry functions called. Runs scripts/nuttx_config_probe.py inside
the NuttX image with Apptainer, so it needs a Scholar Spark node (or another
host with apptainer and EMBEDEVAL_NUTTX_SIF). About 30 s per configuration;
results are cached under candidates/nuttx/config_checks/.

    python scripts/nuttx_config.py --apps-pr 3790 --kernel-pr 20112 \
        CONFIG_SCHED_WORKQUEUE=y
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import nuttx_candidates as candidates
from render_nuttx_instance import ENTRY_DEF

SCRIPTS = Path(__file__).resolve().parent
CACHE = candidates.DATA / "config_checks"
CLONES = {"kernel": candidates.DATA / "kernel.git", "apps": candidates.DATA / "apps.git"}
IMAGE = Path(os.environ.get(
    "EMBEDEVAL_NUTTX_SIF",
    f"/scratch/scholar/{os.environ.get('USER', '')}/nuttx-arm64-test/nuttx-sim.sif"))
# Node-local scratch: a NuttX tree is tens of thousands of small files.
WORK = Path(os.environ.get("EMBEDEVAL_CONFIG_WORK", tempfile.gettempdir()))
RESERVED = {"CONFIG_TESTING_OSTEST", "CONFIG_NSH_CONSOLE_LOGIN", "CONFIG_NDEBUG"}
VALUE = re.compile(r'y|n|-?\d+|0x[0-9a-fA-F]+|"[^"\n]*"')
# templates/nuttx/Dockerfile.tmpl, step for step, with the board:config.
CONFIGURE = """./tools/configure.sh -a ./apps {platform} \\
    && sed -i '/^CONFIG_TESTING_OSTEST=/d; /^# CONFIG_TESTING_OSTEST is not set/d; /^CONFIG_NSH_CONSOLE_LOGIN=/d; /^# CONFIG_NSH_CONSOLE_LOGIN is not set/d; /^CONFIG_NDEBUG=/d; /^# CONFIG_NDEBUG is not set/d' .config \\
    && printf '%s\\n' 'CONFIG_TESTING_OSTEST=y' 'CONFIG_NSH_CONSOLE_LOGIN=n' 'CONFIG_NDEBUG=n' >> .config \\
    && make olddefconfig \\
    && grep -q '^CONFIG_TESTING_OSTEST=y$' .config \\
    && grep -q '^# CONFIG_NDEBUG is not set$' .config"""


def available() -> bool:
    return IMAGE.exists() and shutil.which("apptainer") is not None


def check_overrides(overrides: dict) -> list[str]:
    """What the renderer would refuse, in its words."""
    problems = []
    if not isinstance(overrides, dict):
        return ["config_overrides must be a mapping of config names to values"]
    for name, value in overrides.items():
        if name in RESERVED:
            problems.append(f"reserved config override: {name}")
        elif not re.fullmatch(r"CONFIG_[A-Z0-9_]+", str(name)) or \
                not isinstance(value, str) or not VALUE.fullmatch(value):
            problems.append(f"invalid config override: {name}={value}")
    return problems


def ensure_commit(repo: str, sha: str) -> None:
    clone = str(CLONES[repo])
    if subprocess.run(["git", "-C", clone, "cat-file", "-e", f"{sha}^{{commit}}"],
                      capture_output=True).returncode:
        # PR branch commits live on forks; GitHub serves them by ID.
        subprocess.run(["git", "-C", clone, "fetch", "-q", "origin", sha],
                       capture_output=True, timeout=600)


def script(kernel_sha: str, apps_sha: str, overrides: dict, platform: str) -> str:
    steps = []
    for name, value in sorted(overrides.items()):
        steps.append(f"sed -i '/^{name}=/d; /^# {name} is not set/d' .config")
        steps.append(f"printf '%s\\n' '{name}={value}' >> .config")
    return "\n".join([
        "set -e", "mkdir -p /w/t/apps",
        f"git --git-dir=/repos/nuttx.git archive {kernel_sha} | tar -x -C /w/t",
        f"git --git-dir=/repos/nuttx-apps.git archive {apps_sha} | tar -x -C /w/t/apps",
        "cd /w/t", "{ " + CONFIGURE.format(platform=platform) + "; } > /w/configure.log 2>&1",
        *steps,
        "make olddefconfig > /w/olddefconfig.log 2>&1",
        "python3 /s/nuttx_config_probe.py /w/t /w/request.json > /w/result.json", ""])


def resolve(kernel_sha: str, apps_sha: str, overrides: dict, tests: list[dict],
            report: list[str] = (), platform: str = "sim:nsh") -> dict:
    """The probe's report for one kernel/apps commit pair and overrides."""
    problems = check_overrides(overrides)
    if not re.fullmatch(r"[\w-]+:[\w-]+", platform):
        problems.append(f"platform must be <board>:<config>, not {platform}")
    if problems:
        raise ValueError("; ".join(problems))
    request = {"overrides": dict(sorted(overrides.items())), "tests": tests,
               "report": sorted(set(report) | set(overrides))}
    key = hashlib.sha256(json.dumps([kernel_sha, apps_sha, platform, request])
                         .encode()).hexdigest()[:24]

    def run():
        if not available():
            raise RuntimeError("config checks need apptainer and the NuttX image "
                               f"({IMAGE}); run them on a Scholar Spark node")
        ensure_commit("kernel", kernel_sha)
        ensure_commit("apps", apps_sha)
        work = Path(tempfile.mkdtemp(prefix="embedeval-config-", dir=WORK))
        try:
            (work / "request.json").write_text(json.dumps(request))
            (work / "run.sh").write_text(script(kernel_sha, apps_sha, overrides, platform))
            binds = ",".join([f"{CLONES['kernel'].resolve()}:/repos/nuttx.git:ro",
                              f"{CLONES['apps'].resolve()}:/repos/nuttx-apps.git:ro",
                              f"{SCRIPTS}:/s:ro", f"{work}:/w"])
            done = subprocess.run(["apptainer", "exec", "--containall", "--bind", binds,
                                   "--pwd", "/w", str(IMAGE), "bash", "/w/run.sh"],
                                  capture_output=True, text=True, timeout=900)
            if done.returncode:
                logs = "".join((work / name).read_text()[-1500:] for name in
                               ("configure.log", "olddefconfig.log") if (work / name).exists())
                raise RuntimeError(f"configuration failed: {(logs or done.stderr)[-1500:]}")
            return {"kernel": kernel_sha, "apps": apps_sha, "platform": platform, **json.loads(
                (work / "result.json").read_text())}
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return candidates.cached(CACHE / f"{key}.json", run)


def changed_tests(test_paths: list[str], test_contents: dict) -> list[dict]:
    """The changed ostest sources and the entry functions ostest_main.c calls."""
    return [{"path": path, "entries": ENTRY_DEF.findall(test_contents.get(path) or "")}
            for path in test_paths
            if path.startswith("testing/ostest/") and path.endswith(".c")
            and Path(path).name != "ostest_main.c"]


def check_pair(apps: dict, kernel: dict, overrides: dict, test_paths: list[str],
               test_contents: dict, report: list[str] = (),
               sides: tuple[str, ...] = ("fixed", "old"), platform: str = "sim:nsh") -> dict:
    """The configuration with the kernel PR's head (fixed) and base (old),
    both with the apps PR's head, which holds the changed tests."""
    tests = changed_tests(test_paths, test_contents)
    commits = {"fixed": kernel["head_commit"], "old": kernel["base_commit"]}
    return {side: resolve(commits[side], apps["head_commit"], overrides, tests, report, platform)
            for side in sides}


def problems(side: dict, required: dict) -> list[str]:
    """Why a configuration does not give the required settings or does not
    build and call every changed test; empty when it does."""
    found = []
    if not side["ostest"]:
        found.append("CONFIG_TESTING_OSTEST is off")
    for name, value in required.items():
        got = side["settings"].get(name, "n")
        if got != value:
            found.append(f"{name}={got}, needs {value}")
    for test in side["tests"]:
        if not test["runs"]:
            found.append(f"{test['path']} is not "
                         + ("called" if test["built"] else "built"))
    return found


def describe(check: dict) -> str:
    """check_pair's result for the model, bounded."""
    lines = []
    for side, label in (("fixed", "Fixed kernel (the kernel PR's head)"),
                        ("old", "Old kernel (the kernel PR's base)")):
        if side not in check:
            continue
        result = check[side]
        lines.append(f"{label} {result['kernel'][:12]} + apps {result['apps'][:12]}, "
                     f"{result.get('platform', 'sim:nsh')} + ostest + overrides, "
                     "after make olddefconfig:")
        for item in result["overrides"]:
            text = f"  {item['name']}={item['requested']}: {item['status']}"
            if item["status"] == "dropped":
                text += (f" (now {item['final']}); depends on {item.get('depends_on')}"
                         + (f"; unmet: {', '.join(item['unmet'])}" if item.get("unmet") else ""))
            elif item["status"] == "unknown":
                text += f" (now {item['final']}): {item.get('why')}"
            lines.append(text)
        if not result["overrides"]:
            lines.append("  no overrides")
        for test in result["tests"]:
            calls = "; ".join(
                f"{c['function']}() at ostest_main.c:{c['line']} "
                f"{'compiled' if c['compiled'] else 'compiled out'}"
                + (f" under {' && '.join(c['conditions'])}" if c["conditions"] else "")
                for c in test["calls"][:6]) or "no call in ostest_main.c"
            lines.append(f"  {test['path']}: built {'yes' if test['built'] else 'no'}; {calls}")
        missing = problems(result, {})
        lines.append("  => the changed tests run" if not missing
                     else "  => NOT RUNNABLE: " + "; ".join(missing))
    return "\n".join(lines)[:8000]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apps-pr", type=int, required=True)
    parser.add_argument("--kernel-pr", type=int, required=True)
    parser.add_argument("--platform", default="sim:nsh", help="board:config")
    parser.add_argument("--test-path", nargs="+",
                        help="default: the apps PR's changed testing/ostest/ files")
    parser.add_argument("overrides", nargs="*", metavar="CONFIG_NAME=value")
    args = parser.parse_args()
    record = candidates.apps_record(args.apps_pr)
    paths = args.test_path or [f["filename"] for f in record["apps"]["files"]
                               if f["filename"].startswith("testing/ostest/")]
    overrides = dict(item.split("=", 1) for item in args.overrides)
    print(describe(check_pair(record["apps"], candidates.kernel_record(args.kernel_pr),
                              overrides, paths, record["test_contents"],
                              platform=args.platform)))


if __name__ == "__main__":
    main()
