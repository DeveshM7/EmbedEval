#!/usr/bin/env python3
"""
Build a RIOT instance as a small layer on its era's shared image, and validate it.

    python scripts/riot_instance.py --run-dir DIR --pr 20857 18113

A PR the quick check passed (DIR/check/<pr>.json) gets:

    DIR/instances/riot__riot-<pr>/                 the recipe and validation.json
    <work>/images/instances/riot__riot-<pr>.sif    a read-only layer, in a SIF

The layer holds only what the shared image lacks: /testbed, /opt/embedeval (the
instance's config, Rust packages) and run_tests. It is run as

    apptainer exec --overlay <layer>:ro --overlay <writable folder> <era image> run_tests

A SIF holding one squashfs overlay: it is what the registry takes, so
scripts/registry.py pushes and pulls it like any instance image.

/testbed is a new git repository, one commit at the PR's base with the PR's test
changes on top, uncommitted, as in the Zephyr instances; there is no history, so
the fix is not in it. It is built in the order that lets it work offline, with
nothing of the fix left: build with the fix, so Rust packages and external package
code are downloaded; take the fix out and delete what was built, checking that
outside tests/ the tree is the base again; build again, as a first build that may
well fail. External packages stay in build/pkg, which git ignores.

Validation runs on the layer as packaged, on a fresh writable folder: run_tests
must exit 1, the fix must apply, run_tests must exit 0, and the tests must have
gone the way the quick check said. Rust downloads are off while it runs.

If the LLM stage has run (DIR/triage/<pr>.json), a PR it rejected is not built, and
its problem statement and change type go into the metadata.

Temporary files are in <work>/build/<pr>/ and <work>/validate/<pr>/, always deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au  # noqa: E402
import riot_env  # noqa: E402
from riot_check import diff  # noqa: E402
from riot_select import unpack  # noqa: E402

RUN_TESTS = Path(__file__).resolve().parent.parent / "templates" / "riot" / "run_tests.py"
BUILD_TIMEOUT = 3600        # seconds, for each build of all the instance's tests
RUN_TIMEOUT = 3600          # for each run_tests in validation
# gc.auto: git's clean-up in the background deletes files while the layer is packed
# cpu nodes have it only as part of Apptainer
MKSQUASHFS = shutil.which("mksquashfs") or "/usr/libexec/apptainer/bin/mksquashfs"
GIT = ["git", "-c", "user.name=EmbedEval", "-c", "user.email=embedeval@example.com",
       "-c", "core.autocrlf=false", "-c", "gc.auto=0"]


def instance_id(pr: int) -> str:
    return f"riot__riot-{pr}"


def layer_path(pr: int) -> Path:
    return au.instance_image(instance_id(pr))


def plan(check: dict, select: dict) -> tuple[dict, list[str], list[str], list[str]]:
    """What the instance runs, from the quick check: only targets that passed
    with the fix; in a group, only tests that did. (units, programs, F2P, P2P)"""
    units, programs, f2p, p2p = {}, [], [], []
    for name, t in check["targets"].items():
        if t["kind"] == "after_not_passing":
            continue
        if name in select["units"]:
            units[name] = t["fail_to_pass"] + t["pass_to_pass"]
            f2p += t["fail_to_pass"]
            p2p += t["pass_to_pass"]
        else:
            programs.append(name)
            (p2p if t["kind"] == "no_signal" else f2p).append(name)
    return units, sorted(programs), sorted(f2p), sorted(p2p)


def fingerprint(folder: Path) -> str:
    h = hashlib.sha256()
    for name in ("instance.json", "run_tests.py", "test_patch.diff"):
        h.update((folder / name).read_bytes())
    return h.hexdigest()


def write_recipe(check: dict, select: dict, verdict: dict | None) -> Path:
    pr = check["pr"]
    verdict = verdict or {}
    units, programs, f2p, p2p = plan(check, select)
    folder = layout.generated() / instance_id(pr)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "instance.json").write_text(json.dumps(
        {"board": check["board"], "units": units, "programs": programs}, indent=2) + "\n")
    shutil.copy(RUN_TESTS, folder / "run_tests.py")
    (folder / "test_patch.diff").write_bytes(diff(check, "tests/"))
    (folder / "metadata.json").write_text(json.dumps({
        "instance_id": instance_id(pr), "project": "riot", "pipeline": "riot-layer",
        "repo": riot_env.URL, "base_commit": check["base"], "fix_commit": check["head"],
        "merge_commit": check["merge"], "board": check["board"],
        "base_image": f"riotbuild-{check['image']}", "kind": check["outcome"],
        "fail_to_pass": f2p, "pass_to_pass": p2p,
        "problem_statement": verdict.get("problem_statement", ""),
        "change_type": verdict.get("change_type"),
        "files_changed_by_fix": riot_env.git("diff", "--name-only", check["base"], check["head"],
                                             "--", ".", ":!tests/").split(),
        "pr_url": f"{riot_env.URL.removesuffix('.git')}/pull/{pr}"}, indent=2) + "\n")
    return folder


def contained(image: Path, binds: dict, script: str, log: Path, timeout: int) -> int:
    with open(log, "a") as fh:
        fh.write(f"\n$ {script}\n")
        fh.flush()
        cmd = [*au.exec_args(image, binds=binds), "bash", "-c", script]
        try:
            return subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            return 124


def build_layer(check: dict, select: dict, logs: Path, verdict: dict | None = None) -> dict:
    """The layer file; raises if the instance cannot be built as it must be."""
    pr, base = check["pr"], check["base"]
    recipe = write_recipe(check, select, verdict)
    work = au.path("build", str(pr), "x").parent
    if work.exists():
        au.remove(work)
    layer, testbed = work / "layer", work / "layer" / "testbed"
    opt = layer / "opt" / "embedeval"
    (layer / "usr" / "local" / "bin").mkdir(parents=True)
    (opt / "cargo").mkdir(parents=True)
    (opt / "gitcache").mkdir()
    log = logs / "build.log"
    try:
        unpack(base, testbed)
        shutil.copy(recipe / "instance.json", opt / "instance.json")
        shutil.copy(recipe / "test_patch.diff", opt / "test_patch.diff")
        shutil.copy(recipe / "run_tests.py", layer / "usr" / "local" / "bin" / "run_tests")
        (layer / "usr" / "local" / "bin" / "run_tests").chmod(0o755)
        git = lambda *a: subprocess.run([*GIT, "-C", str(testbed), *a], capture_output=True,
                                        text=True, errors="replace")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", f"base {base}"],
                     ["gc", "-q"]):
            r = git(*args)
            if r.returncode:
                raise RuntimeError(f"git {args[0]}: {r.stderr[:200]}")
        fix_file = work / "fix.diff"
        fix_file.write_bytes(diff(check, ".", ":!tests/"))
        for args in (["apply", "--whitespace=nowarn", str(recipe / "test_patch.diff")],
                     ["apply", "--whitespace=nowarn", str(fix_file)]):
            r = git(*args)
            if r.returncode:
                raise RuntimeError(f"patch does not apply: {r.stderr[:200]}")
        image = au.riot_image(check["image"])
        binds = {testbed: "/testbed", opt: "/opt/embedeval", work / "tmp": "/tmp"}
        (work / "tmp").mkdir()
        tool = "python3 /testbed/.run_tests"
        shutil.copy(recipe / "run_tests.py", testbed / ".run_tests")
        git("update-index", "--refresh")
        t = time.time()
        if contained(image, binds, f"{tool} --build-only --online", log, BUILD_TIMEOUT):
            raise RuntimeError("does not build with the fix")
        # the fix out, and everything built with it
        r = git("apply", "-R", "--whitespace=nowarn", str(fix_file))
        if r.returncode:
            raise RuntimeError(f"the fix does not come out: {r.stderr[:200]}")
        for d in testbed.rglob("bin"):
            if d.is_dir() and d.parent.joinpath("Makefile").exists():
                au.remove(d)
        (testbed / ".run_tests").unlink()
        left = git("status", "--porcelain", "--", ".", ":!tests/").stdout.strip()
        if left:
            raise RuntimeError(f"not the base outside tests/: {left[:300]}")
        shutil.copy(recipe / "run_tests.py", testbed / ".git" / "run_tests")
        tool = "python3 /testbed/.git/run_tests"
        first = contained(image, binds, f"{tool} --build-only --online", log, BUILD_TIMEOUT)
        (testbed / ".git" / "run_tests").unlink()
        built = time.time() - t
        if (opt / "tmp").exists():
            au.remove(opt / "tmp")
        # whoever runs the image may use the tree, through the writable folder
        subprocess.run(["chmod", "-R", "a+rwX", str(layer)], check=True)
        out = layer_path(pr)
        if out.exists():
            au.remove(out)
        t = time.time()
        out.parent.mkdir(parents=True, exist_ok=True)
        squash = work / "layer.squashfs"
        r = subprocess.run([MKSQUASHFS, str(layer), str(squash), "-all-root", "-no-progress",
                            "-noappend", "-processors", "8"], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"mksquashfs: {r.stderr[:300]}")
        # datatype 4 partition, fs squash, type overlay, arch amd64
        for cmd in (["new", str(out)],
                    ["add", "--datatype", "4", "--partfs", "1", "--parttype", "4", "--partarch", "2",
                     "--groupid", "1", str(out), str(squash)]):
            r = subprocess.run(["apptainer", "sif", *cmd], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"apptainer sif {cmd[0]}: {r.stderr[:300]}")
        return {"build_seconds": round(built), "squash_seconds": round(time.time() - t),
                "first_build_ok": first == 0, "layer_bytes": out.stat().st_size,
                "tree_bytes": int(subprocess.run(["du", "-sb", str(layer)], capture_output=True,
                                                 text=True).stdout.split()[0])}
    finally:
        au.remove(work)


def results(output: str) -> dict[str, str]:
    """What run_tests said about each test and program."""
    out, program = {}, None
    for line in output.splitlines():
        m = re.fullmatch(r"--- test program (\S+) ---", line)
        if m:
            program = m.group(1)
            out[program] = "not built"
            continue
        if line.startswith("--- unit-test group"):
            program = None
            continue
        m = re.fullmatch(r"  (passed|failed)(?: \(timeout\))?", line)
        if m and program:
            out[program] = m.group(1)
            continue
        m = re.fullmatch(r"  (passed|failed|not run)\s+(\S+)", line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def validate(check: dict, logs: Path) -> dict:
    pr = check["pr"]
    folder = layout.generated() / instance_id(pr)
    meta = json.loads((folder / "metadata.json").read_text())
    report = {"instance_id": instance_id(pr), "valid": False, "image": meta["base_image"],
              "layer": str(layer_path(pr)), "recipe_sha256": fingerprint(folder),
              "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    work = au.path("validate", str(pr), "x").parent
    if work.exists():
        au.remove(work)
    (work / "overlay").mkdir(parents=True)
    fix = work / "fix.diff"
    fix.write_bytes(diff(check, ".", ":!tests/"))
    container = au.exec_args(au.riot_image(check["image"]), binds={work: "/embedeval-run"},
                             overlay=work / "overlay", layer=layer_path(pr))

    def step(script: str, name: str) -> tuple[int, str, int]:
        t = time.time()
        try:
            r = subprocess.run([*container, "bash", "-c", f"cd /testbed && {script}"],
                               capture_output=True, text=True, errors="replace",
                               timeout=RUN_TIMEOUT)
            rc, out = r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            rc, out = 124, "timed out"
        (logs / f"{name}.log").write_text(out)
        return rc, out, round(time.time() - t)

    def finish(reason: str, valid: bool = False) -> dict:
        report.update(reason=reason, valid=valid)
        (folder / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
        return report

    try:
        rc, out, secs = step("run_tests", "before")
        before = results(out)
        report["before"] = {"rc": rc, "seconds": secs}
        if rc != 1:
            return finish(f"run_tests ended with {rc} before the fix, not 1")
        changed = step("git status --porcelain -- . ':!tests/'", "status")[1].strip()
        if changed:
            return finish(f"running the tests changed tracked files: {changed[:200]}")
        rc, out, _ = step("git apply /embedeval-run/fix.diff", "apply")
        if rc:
            return finish(f"the fix does not apply: {out[:200]}")
        rc, out, secs = step("run_tests", "after")
        after = results(out)
        report["after"] = {"rc": rc, "seconds": secs}
        if rc != 0:
            return finish(f"run_tests ended with {rc} with the fix, not 0")
        wrong = [t for t in meta["fail_to_pass"] + meta["pass_to_pass"] if after.get(t) != "passed"]
        wrong += [t for t in meta["fail_to_pass"] if before.get(t) == "passed"]
        wrong += [t for t in meta["pass_to_pass"] if before.get(t) != "passed"]
        if wrong:
            return finish(f"tests differ from the quick check: {sorted(set(wrong))[:8]}")
        return finish("fails before the fix, passes after", True)
    finally:
        au.remove(work)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pr", type=int, nargs="+", help="only these PRs")
    p.add_argument("--parallel", type=int, default=4, help="instances at once, 8 cores each")
    args = p.parse_args()
    if not layout.run_dir():
        sys.exit("needs --run-dir")
    if platform.machine() != "x86_64":
        sys.exit("RIOT's build images are x86 only: run this on an x86 node")
    run = layout.run_dir()
    checks = [json.loads(f.read_text()) for f in sorted((run / "check").glob("*.json"))]
    checks = [c for c in checks if c["outcome"] in ("strong", "weak")
              and (not args.pr or c["pr"] in args.pr)]
    print(f"{len(checks)} instances, {args.parallel} at a time", flush=True)

    def one(c: dict) -> None:
        pr, t = c["pr"], time.time()
        logs = run / "instances-logs" / str(pr)
        if logs.exists():
            shutil.rmtree(logs)
        logs.mkdir(parents=True)
        select = json.loads((run / "select" / f"{pr}.json").read_text())
        triage = run / "triage" / f"{pr}.json"
        verdict = json.loads(triage.read_text()) if triage.exists() else None
        if verdict and verdict["verdict"] != "accept":
            print(f"  {pr}: not built, the LLM stage rejected it", flush=True)
            return
        try:
            built = build_layer(c, select, logs, verdict)
        except Exception as e:
            print(f"  {pr}: BUILD FAILED: {e}", flush=True)
            return
        try:
            v = validate(c, logs)
        except Exception as e:
            print(f"  {pr}: VALIDATION ERROR {type(e).__name__}: {e}", flush=True)
            return
        print(f"  {pr}: {'VALID' if v['valid'] else 'INVALID'} {c['outcome']} {c['image']} "
              f"layer {built['layer_bytes'] / 2**20:.0f} MB (tree {built['tree_bytes'] / 2**20:.0f} MB), "
              f"build {built['build_seconds']}s, squash {built['squash_seconds']}s, "
              f"before {v.get('before', {}).get('seconds')}s, after {v.get('after', {}).get('seconds')}s, "
              f"total {time.time() - t:.0f}s: {v['reason'][:200]}", flush=True)

    with ThreadPoolExecutor(args.parallel) as pool:
        list(pool.map(one, checks))


if __name__ == "__main__":
    main()
