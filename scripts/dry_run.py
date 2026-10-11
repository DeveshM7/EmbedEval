#!/usr/bin/env python3
"""
Twister dry run for a Zephyr PR: which of its scenarios can run on our boards.

    python scripts/dry_run.py 85079
    python scripts/dry_run.py 85079 99824 --keep-tree

Runs between enrichment and triage. For every scenario in every test suite the
PR changes, on every candidate board valid at the base commit (one native
64-bit board and qemu_x86), it asks twister's own filters whether the pair is
selected, or filtered and why ("No hardware support for {'memc'}", "In test
case arch exclude", ...). It also lists each scenario's real test names.

Cheap on purpose -- seconds per PR, no SDK, no compilers, no west modules:

    tree    only the parts of the base commit twister reads (its own scripts,
            cmake/, the board, SoC and architecture definitions, snippets,
            VERSION) plus the PR's test suites, from `git archive`, with the
            PR's test diff applied; written to node-local disk ($TMPDIR) and
            deleted afterwards
    python  a venv with the commit's requirements, made inside the shared
            python-<py>.sif image and cached by requirements hash
    twister --dry-run with ZEPHYR_TOOLCHAIN_VARIANT=host --force-toolchain.
            Twister always runs cmake/verify-toolchain.cmake, which fails with
            no SDK installed; the host toolchain passes it, and
            --force-toolchain stops toolchain support from filtering boards.
            Our boards are all supported by the Zephyr SDK anyway.

Not covered: the Kconfig/devicetree `filter:` expressions, which need CMake
and an SDK. A selected pair can still be filtered at build time; validation
finds out.

Writes candidates/dryrun/<pr>.json:

    pairs        [{suite, scenario, board, status: selected|filtered, reason}]
    tests        {suite/scenario: [test identifiers]}
    auto_reject  true when no pair is selected -- the PR cannot run on any of
                 our boards, so it never needs triage
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apptainer_util as au  # noqa: E402
import zephyr_env  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICHED = REPO_ROOT / "candidates" / "enriched"
OUT_DIR = REPO_ROOT / "candidates" / "dryrun"

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# What twister reads besides the test suites themselves: its own code
# (scripts/), the toolchain check (cmake/), board, SoC and architecture
# definitions, snippets, and the version. A fraction of the tree, so exporting
# and deleting it takes about a second instead of a minute.
TWISTER_PATHS = ["scripts", "cmake", "boards", "soc", "arch", "snippets", "VERSION",
                 "tests/test_config.yaml",
                 "west.yml", "zephyr/module.yml"]
CONTAINER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def run(cmd: list[str], log: Path, timeout: int = 900, cwd: Path | None = None) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n")
        fh.flush()
        return subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout,
                              cwd=cwd).returncode


def exists_at(commit: str, path: str) -> bool:
    return subprocess.run(["git", "-C", str(zephyr_env.CLONE), "cat-file", "-e",
                           f"{commit}:{path}"], capture_output=True).returncode == 0


def make_tree(base: str, head: str, tree: Path, log: Path, suites: list[str]) -> None:
    """What twister needs of the base commit, with the PR's test changes: its
    own paths, the PR's suites, and every test file the diff touches that
    already exists (so the diff applies). No .git, no modules."""
    tree.mkdir(parents=True, exist_ok=True)
    # --no-renames: a renamed file must be exported under its old path too.
    touched = zephyr_env.git("diff", "--name-only", "--no-renames", f"{base}..{head}",
                             "--", "tests/").split()
    wanted = TWISTER_PATHS + suites + touched
    paths = sorted({p for p in wanted if exists_at(base, p)})
    archive = subprocess.Popen(["git", "-C", str(zephyr_env.CLONE), "archive", base, "--", *paths],
                               stdout=subprocess.PIPE)
    rc = subprocess.run(["tar", "-x", "-C", str(tree)], stdin=archive.stdout).returncode
    archive.wait()
    if rc or archive.returncode:
        raise RuntimeError(f"git archive {base[:12]} failed")
    patch = zephyr_env.git("diff", f"{base}..{head}", "--", "tests/")
    pf = tree.parent / "test_patch.diff"
    pf.write_text(patch)
    if run(["git", "apply", str(pf)], log, cwd=tree):
        raise RuntimeError("the PR's test diff does not apply at the base commit")


def make_venv(py: str, tree: Path, log: Path, binds: dict) -> Path:
    """A venv with this commit's twister requirements, shared by every PR whose
    requirements files are identical."""
    reqs = ["requirements-base.txt", "requirements-build-test.txt", "requirements-run-test.txt"]
    h = hashlib.sha256()
    for r in reqs:
        h.update((tree / "scripts" / r).read_bytes())
    venv = au.path("venvs", f"py{py}-{h.hexdigest()[:12]}")
    if (venv / ".embedeval-ready").exists():
        return venv
    image = au.python_image(py)
    if not image.exists():
        print(f"  building shared image {image.name} ...")
        if not au.build(image, au.TEMPLATES / "python.def", {"PYTHON": py},
                        au.path("logs", "bases", f"build-{image.stem}.log")):
            raise RuntimeError(f"could not build {image.name}")
    base = au.exec_args(image, binds=binds,
                        env={"UV_CACHE_DIR": str(au.path("cache", "uv", "x").parent)})
    if run(base + ["uv", "venv", "-q", "--seed", "--python", py, str(venv)], log):
        raise RuntimeError("uv venv failed")
    install = ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"),
               "west", "setuptools<82"]
    for r in reqs:
        install += ["-r", str(tree / "scripts" / r)]
    if run(base + install, log, timeout=1800):
        raise RuntimeError("installing the commit's requirements failed")
    (venv / ".embedeval-ready").write_text("ok\n")
    return venv


def twister(py: str, venv: Path, tree: Path, args: list[str], log: Path, binds: dict) -> int:
    cmd = au.exec_args(au.python_image(py), binds=binds,
                       env={"PATH": f"{venv}/bin:{CONTAINER_PATH}", "ZEPHYR_BASE": str(tree),
                            "ZEPHYR_TOOLCHAIN_VARIANT": "host"})
    cmd[2:2] = ["--pwd", str(tree)]
    return run(cmd + [str(venv / "bin" / "python"), "scripts/twister", "--force-toolchain"]
               + args, log)


def board_matches(reported: str, board: str) -> bool:
    """twister may report a board under a qualified name: qemu_x86 runs as
    qemu_x86/atom from 2024 on."""
    return reported == board or reported.startswith(board + "/")


def dry_run(pr: int, keep_tree: bool = False) -> dict:
    rec = json.loads((ENRICHED / f"{pr}.json").read_text())
    facts = rec.get("twister")
    if not facts or "error" in facts:
        raise RuntimeError(f"{pr}: no twister facts; run `filter_candidates.py enrich-twister`")
    py, base, head = facts["python"], facts["base_commit"], facts["head_commit"]
    boards = [b for b in facts["boards"].values() if b]
    suites = {s: v for s, v in facts["suites"].items() if v.get("scenarios")}

    if not suites:
        # The PR changes no test suite (often no test at all): nothing for
        # twister to select, and an empty test diff would not even apply.
        result = {"pr": pr, "base_commit": base, "boards": boards, "pairs": [], "tests": {},
                  "selected": 0, "auto_reject": True, "note": "the PR changes no test suite"}
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"{pr}.json").write_text(json.dumps(result, indent=2) + "\n")
        return result

    work = au.path("dryrun", str(pr), "x").parent
    log = work / "dry_run.log"
    if log.exists():
        log.unlink()
    zephyr_env.ensure_commits(base, head)
    root = au.work_root()
    if keep_tree:
        # For inspection: on the work root, kept after the run.
        local = work / "local"
        if local.exists():
            au.remove(local)
        local.mkdir(parents=True)
        scratch = None
    else:
        # Node-local disk: thousands of small files are created and deleted
        # far faster there than on a network filesystem.
        scratch = tempfile.TemporaryDirectory(
            prefix=f"embedeval-dryrun-{pr}-",
            dir=os.environ.get("EMBEDEVAL_LOCAL_TMP") or os.environ.get("TMPDIR") or "/tmp")
        local = Path(scratch.name)
    try:
        return _dry_run(pr, facts, suites, boards, py, base, head, local, work, log,
                        {root: str(root), local: str(local)})
    finally:
        if scratch:
            scratch.cleanup()


def _dry_run(pr, facts, suites, boards, py, base, head, local, work, log, binds) -> dict:
    tree = local / "tree"
    make_tree(base, head, tree, log, list(suites))
    venv = make_venv(py, tree, log, binds)

    out = local / "twister-out"
    args = ["--dry-run", "-O", str(out)]
    for s in suites:
        args += ["-T", s]
    for b in boards:
        args += ["-p", b]
    plan_csv = local / "plan.csv"
    if facts["twister"]["report_filtered"]:
        args.append("--report-filtered")
    else:
        # Older twister has no --report-filtered: the plan (filtered pairs
        # marked skipped) comes from --save-tests, the reasons from -vv.
        args += ["--save-tests", str(plan_csv), "-vv"]
    rc = twister(py, venv, tree, args, log, binds)
    text = ANSI.sub("", log.read_text(errors="replace"))
    if rc != 0:
        raise RuntimeError(f"twister --dry-run failed (rc={rc}); see {log}")

    # status per (suite path/scenario, reported board)
    reported: dict[tuple[str, str], tuple[str, str]] = {}
    plan_json = out / "testplan.json"
    if plan_json.exists():
        for s in json.loads(plan_json.read_text()).get("testsuites", []):
            st = (s.get("status") or "").lower()
            # Suite names are "<path>/<scenario>" up to 2024 and the bare
            # scenario name from 2025; "path" then holds the directory.
            name = s["name"] if "/" in s["name"] else f"{s.get('path') or ''}/{s['name']}"
            reported[(name, s["platform"])] = (
                "filtered" if st in ("filtered", "skipped") else "selected", s.get("reason") or "")
    if plan_csv.exists():
        # --save-tests writes CSV up to 2022 and JSON (with reasons) from 2023.
        raw = plan_csv.read_text()
        if raw.lstrip().startswith("{"):
            rows = [{"test": s["name"], "platform": s["platform"], "status": s.get("status"),
                     "reason": s.get("reason")} for s in json.loads(raw).get("testsuites", [])]
        else:
            import csv
            rows = list(csv.DictReader(raw.splitlines()))
        for r in rows:
            st = (r.get("status") or "").lower()
            reported.setdefault((r["test"], r["platform"]),
                                ("filtered" if st in ("skipped", "filtered") else "selected",
                                 r.get("reason") or ""))
    # Reasons from the verbose log (older twister), lines such as
    #   "DEBUG   - native_sim_64   tests/posix/common/portability.posix.common.newlib SKIPPED: In test case arch exclude"
    reasons = {}
    for m in re.finditer(r"(\S+)\s+((?:tests|samples)/\S+)\s+(?:SKIPPED|FILTERED):\s*(.+?)\s*$",
                         text, re.M):
        reasons[(m.group(2), m.group(1))] = m.group(3)

    pairs = []
    for suite, info in suites.items():
        for scen in info["scenarios"]:
            name = f"{suite}/{scen}"
            for b in boards:
                hit = next(((st, why, plat) for (n, plat), (st, why) in reported.items()
                            if (n == name or n.endswith("/" + name) or n == f"/{scen}"
                                or n.endswith(f"{suite}/{scen}"))
                            and board_matches(plat, b)), None)
                why = reasons.get((name, b)) or next(
                    (r for (n, plat), r in reasons.items() if n == name and board_matches(plat, b)), "")
                if hit:
                    status, reason = hit[0], hit[1] or why
                    if status == "selected":
                        reason = ""  # newer twister fills it with "Unknown Instance status."
                else:
                    # Absent from the plan: older twister leaves filtered pairs out.
                    status, reason = "filtered", why or "not in twister's plan"
                pairs.append({"suite": suite, "scenario": scen, "board": b,
                              "status": status, "reason": reason})

    # Real test names, per scenario: one --list-tests call for all suites.
    tests: dict[str, list[str]] = {}
    lt_log = work / "list-tests.log"
    if lt_log.exists():
        lt_log.unlink()
    lt_args = ["--list-tests", "-O", str(local / "list-out")]
    for s in suites:
        lt_args += ["-T", s]
    twister(py, venv, tree, lt_args, lt_log, binds)
    names = re.findall(r"^\s*-\s+(\S+)\s*$", ANSI.sub("", lt_log.read_text(errors="replace")),
                       re.M)
    # A scenario name can prefix another, within a suite (driver.sensor,
    # driver.sensor.fpu) or across suites (arch.interrupt,
    # arch.interrupt.gen_isr_table): each test belongs to the longest scenario
    # name it starts with, over all the suites listed together.
    owners = sorted(((suite, scen) for suite, info in suites.items() for scen in info["scenarios"]),
                    key=lambda o: len(o[1]), reverse=True)
    for suite, info in suites.items():        # keep the suites' own order
        for scen in info["scenarios"]:
            tests[f"{suite}/{scen}"] = []
    for n in sorted(set(names)):
        owner = next((o for o in owners if n.startswith(o[1] + ".")), None)
        if owner:
            tests[f"{owner[0]}/{owner[1]}"].append(n)

    result = {"pr": pr, "base_commit": base, "boards": boards, "pairs": pairs, "tests": tests,
              "selected": sum(p["status"] == "selected" for p in pairs),
              "auto_reject": not any(p["status"] == "selected" for p in pairs)}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{pr}.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pr", type=int, nargs="+")
    p.add_argument("--keep-tree", action="store_true", help="keep the exported tree for inspection")
    args = p.parse_args()
    failed = 0
    for pr in args.pr:
        try:
            r = dry_run(pr, args.keep_tree)
        except Exception as e:
            print(f"{pr}: ERROR {e}")
            failed += 1
            continue
        print(f"{pr}: {r['selected']}/{len(r['pairs'])} pairs selected"
              f"{'  -> AUTO-REJECT: nothing can run on our boards' if r['auto_reject'] else ''}")
        for x in r["pairs"]:
            print(f"   {x['status']:<9} {x['suite']}/{x['scenario']} on {x['board']}"
                  f"{'  (' + x['reason'] + ')' if x['reason'] else ''}")
        for scen, names in r["tests"].items():
            print(f"   tests {scen}: {len(names)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
