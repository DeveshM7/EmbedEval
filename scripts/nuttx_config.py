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
import nuttx_config_probe as probe
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
    key = hashlib.sha256(json.dumps([2, kernel_sha, apps_sha, platform, request])
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


def problems(side: dict, required: dict, one_of: list[dict] = ()) -> list[str]:
    """Why a configuration does not give the required settings (and one
    setting of each one-of group) or does not build and call every changed
    test; empty when it does."""
    found = []
    if not side["ostest"]:
        found.append("CONFIG_TESTING_OSTEST is off")
    for name, value in required.items():
        got = side["settings"].get(name, "n")
        if got != value:
            found.append(f"{name}={got}, needs {value}")
    for group in one_of:
        if not any(side["settings"].get(name, "n") == value for name, value in group.items()):
            found.append("needs one of " + ", ".join(f"{n}={v}" for n, v in group.items()))
    for test in side["tests"]:
        if not test["runs"]:
            found.append(f"{test['path']} is not "
                         + ("called" if test["built"] else "built"))
    return found


def cmake_conditions(text: str, name: str) -> list[str]:
    """CMakeLists.txt's if(...) conditions around each line naming `name`, in
    the form requirements() reads; a form it cannot read is kept as is."""
    stack, found = [], []
    for _, line in probe.logical_lines(text):
        text_ = " ".join(line.split())
        match = re.match(r"if\((NOT )?(CONFIG_\w+)\)$", text_)
        if match:
            stack.append(f"{'#ifndef' if match[1] else '#ifdef'} {match[2]}")
        elif re.match(r"if\(", text_):
            stack.append(text_)
        elif re.match(r"(elseif|else)\(", text_) and stack:
            stack[-1] = f"{stack[-1]} ... {text_}"
        elif text_.startswith("endif(") and stack:
            stack.pop()
        elif re.search(rf"(?<![\w/.-]){re.escape(name)}(?![\w.-])", text_) and "SRCS" in text_:
            found.append(" && ".join(stack))
    return found


def kconfig_requirements(kconfig: str, options: list[str]) -> dict:
    """Options each given option's `depends on` line(s) require: plain
    symbols or negated symbols joined with &&. Selects and other forms go
    to "other"."""
    depends, result = probe.kconfig_depends(kconfig), {"required": {}, "other": []}
    for option in options:
        for line in (depends.get(option) or "").split("; "):
            if not line.startswith("depends on "):
                if line:
                    result["other"].append(f"{option}: {line}")
                continue
            for term in probe.split_top(line[len("depends on "):], "&&"):
                match = re.fullmatch(r"(!?)\s*([A-Z0-9_]+)", probe.strip_parens(term))
                if match:
                    result["required"][f"CONFIG_{match[2]}"] = "n" if match[1] else "y"
                else:
                    result["other"].append(f"{option}: depends on {term}")
    return result


def test_requirements(path: str, entries: list[str], read) -> dict:
    """What a changed ostest source needs to be compiled and called, from
    the sources alone (read(path) returns an apps file's text, or ""). Only
    what is certain goes into "required" and "one_of": the conditions on the
    line that adds the source to the build, on the single call in
    ostest_main.c, on the entry function's own #if branch, and the Kconfig
    `depends on` of the options found. Anything else (several places
    building or calling it, comparisons, #else branches) is in "other"; the
    alternative #if branches inside the test are in "branches"."""
    name, ostest = Path(path).name, "testing/ostest/"
    required, one_of, other, notes = {}, [], [], []

    def merge(found: dict, where: str) -> None:
        required.update(found["required"])
        one_of.extend(group for group in found["one_of"] if group not in one_of)
        other.extend(f"{where}: {text}" for text in found["other"])

    for label, conditions in (("Makefile", probe.makefile_conditions(read(ostest + "Makefile"), name)),
                              ("CMakeLists.txt", cmake_conditions(read(ostest + "CMakeLists.txt"), name))):
        if len(conditions) == 1:
            merge(probe.requirements(conditions[0].split(" && ") if conditions[0] else []), label)
            notes.append(f"built by {label}" + (f" under {conditions[0]}" if conditions[0] else ""))
            break
        if conditions:
            other.append(f"{label} adds {name} under {len(conditions)} different conditions: "
                         + " | ".join(c or "(always)" for c in conditions))
    if entries:
        calls = probe.conditionals(read(ostest + "ostest_main.c"), entries)["calls"]
        if len(calls) == 1:
            merge(probe.requirements(calls[0]["conditions"]), "ostest_main.c")
            notes.append(f"called at ostest_main.c:{calls[0]['line']}"
                         + (f" under {' && '.join(calls[0]['conditions'])}" if calls[0]["conditions"] else ""))
        else:
            other.append(f"ostest_main.c has {len(calls)} calls of {', '.join(entries)}: "
                         + " | ".join(f"line {c['line']}: {' && '.join(c['conditions']) or '(always)'}"
                                      for c in calls[:6]))
    source, branches = read(path), []
    lines = source.splitlines()
    for group in probe.conditionals(source)["groups"]:
        defined = any(re.search(rf"\b{re.escape(e)}\s*\(void\)", line)
                      for e in entries for b in group["branches"][:1]
                      for line in lines[b["start"] - 1:b["end"]])
        if len(group["branches"]) == 1 and defined:
            merge(probe.requirements(group["outer"] + [group["branches"][0]["directive"]]), path)
        elif len(group["branches"]) > 1:
            branches.append([{k: b[k] for k in ("directive", "start", "end")}
                             for b in group["branches"]])
    kconfig = kconfig_requirements(read(ostest + "Kconfig"),
                                   [o for o in required if o not in RESERVED])
    required.update(kconfig["required"])
    other.extend(kconfig["other"])
    for option in RESERVED:
        required.pop(option, None)
    return {"path": path, "required": required, "one_of": one_of, "branches": branches,
            "other": other, "notes": notes}


def describe_requirements(items: list[dict]) -> str:
    lines = []
    for item in items:
        lines.append(f"{item['path']} ({'; '.join(item['notes']) or 'build condition not found'}):")
        lines.append("  required (certain): " + (", ".join(f"{k}={v}" for k, v in item["required"].items())
                                                  or "nothing beyond the board's defaults"))
        for group in item["one_of"]:
            lines.append("  one of: " + " | ".join(f"{k}={v}" for k, v in group.items()))
        for group in item["branches"]:
            lines.append("  alternative branches in the test: " + " | ".join(
                f"{b['directive']} (lines {b['start']}-{b['end']})" for b in group))
        for text in item["other"]:
            lines.append(f"  not decided by the tool, read it: {text}")
    return "\n".join(lines)[:6000]


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
            for group in test.get("branches", []):
                lines.append("    #if branches of the test (compiled under this config): "
                             + "; ".join(f"{b['directive']} (lines {b['start']}-{b['end']}) "
                                         f"{'COMPILED' if b['compiled'] else 'not compiled'}"
                                         for b in group["branches"]))
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
