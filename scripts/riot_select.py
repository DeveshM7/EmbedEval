#!/usr/bin/env python3
"""
Test selection for RIOT: which tests does a PR change, and can they run here?

    python scripts/riot_select.py --run-dir DIR                # the run's candidates
    python scripts/riot_select.py --run-dir DIR --pr 20857     # just this PR

The job twister's dry run does for Zephyr. For each PR the hard filter kept
(candidates/candidates.jsonl), it maps the PR's changed test files to:

    unit-test groups   tests/unittests/tests-<name>/...
    test programs      the nearest folder above the file whose Makefile
                       includes RIOT's Makefile.include, directly or through
                       tests/unittests/Makefile

and asks RIOT, in the build image of the PR's era, which of them its CI runs on
the board: TEST_ON_CI_ENABLED says the program has a test script, links, and is
not opted out. CI has no hardware attached, so that is also what can run without
any. It asks about the PR's final code, not the code before it: a new test
program can need a module the same PR adds (PR 21343), and the test must in the
end pass there.

Writes DIR/select/<pr>.json, which the later stages read; a PR that already has
one is skipped (--redo: again). Each PR's temporary copy of RIOT is in
DIR/work/select/<pr>/ and is deleted whatever happens. Runs on an x86 node, in
the x86 environment (<shared>/riot/venv): RIOT's build images are x86 only.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au  # noqa: E402
import riot_env  # noqa: E402

UNIT_TESTS = "tests/unittests"
UNIT_GROUP = re.compile(rf"^{UNIT_TESTS}/(tests-[^/]+)/")
PROBE_TIMEOUT = 900        # seconds, for all of one PR's questions


def find_targets(test_files: list[str], head: str) -> tuple[list[str], list[str]]:
    """The unit-test groups and test programs that files under tests/ belong to."""
    groups, programs, makefiles = set(), set(), {}

    def is_program(folder: str) -> bool:
        if folder not in makefiles:
            text = riot_env.show(head, f"{folder}/Makefile")
            makefiles[folder] = bool(text) and ("Makefile.include" in text
                                                or "unittests/Makefile" in text)
        return makefiles[folder]

    for name in test_files:
        m = UNIT_GROUP.match(name)
        if m:
            if riot_env.has_path(head, f"{UNIT_TESTS}/{m.group(1)}/Makefile"):
                groups.add(m.group(1))
            continue
        parts = name.split("/")
        for k in range(len(parts) - 1, 1, -1):
            folder = "/".join(parts[:k])
            if is_program(folder):
                if folder != UNIT_TESTS:     # its own Makefile, main.c: no group changed
                    programs.add(folder)
                break
    return sorted(groups), sorted(programs)


def unpack(commit: str, tree: Path) -> None:
    tree.mkdir(parents=True)
    a = subprocess.Popen(["git", "-C", str(riot_env.clone()), "archive", commit],
                         stdout=subprocess.PIPE)
    t = subprocess.run(["tar", "-x", "-C", str(tree)], stdin=a.stdout, capture_output=True)
    a.stdout.close()
    if a.wait() or t.returncode:
        raise RuntimeError(f"could not unpack {commit}: {t.stderr.decode()[:200]}")


def ask_ci(tree: Path, tag: str, board: str, programs: list[str]) -> dict[str, str]:
    """TEST_ON_CI_ENABLED of each program; a make that failed is 'error: ...'."""
    script = "cd /testbed\n" + "".join(
        f'v=$(make --no-print-directory -C {p} BOARD={board} '
        f'info-debug-variable-TEST_ON_CI_ENABLED 2>/tmp/err | tail -1); rc=$?\n'
        f'if [ -s /tmp/err ] && [ -z "$v" ] && grep -q "\\*\\*\\*" /tmp/err; then '
        f'v="error: $(grep -m1 "\\*\\*\\*" /tmp/err | cut -c1-150)"; fi\n'
        f'echo "@@{p}|$v"\n' for p in programs)
    r = subprocess.run([*au.exec_args(au.riot_image(tag), binds={tree: "/testbed"},
                                      env={"RIOT_CI_BUILD": "1", "CC_NOCOLOR": "1"}),
                        "bash", "-c", script],
                       capture_output=True, text=True, errors="replace", timeout=PROBE_TIMEOUT)
    found = dict(line[2:].split("|", 1) for line in r.stdout.splitlines()
                 if line.startswith("@@") and "|" in line)
    if len(found) != len(programs):
        raise RuntimeError(f"the container answered for {len(found)} of {len(programs)}: "
                           f"{r.stderr.strip()[-200:]}")
    return found


def select(c: dict) -> dict:
    """One PR's record: what to run, and why anything is not run."""
    pr, merge = c["number"], c["merge_commit_sha"]
    out = {"pr": pr, "merge": merge, "units": [], "programs": [], "not_run": {}}
    riot_env.ensure_commits(merge)
    base, head = riot_env.pr_commits(merge)
    out.update(base=base, head=head, board=riot_env.board(head), image=riot_env.image_tag(base))
    groups, programs = find_targets(c["test_files"], head)
    if not groups and not programs:
        out["reason"] = "changes no test program or unit-test group"
        return out
    asked = programs + ([UNIT_TESTS] if groups else [])
    tree = au.path("select", str(pr), "x").parent
    if tree.exists():
        au.remove(tree)
    try:
        unpack(head, tree)
        answers = ask_ci(tree, out["image"], out["board"], asked)
    finally:
        au.remove(tree)
    for p in programs:
        if answers[p].startswith("error:"):
            out["not_run"][p] = answers[p]
        elif out["board"] in answers[p].split():
            out["programs"].append(p)
        else:
            out["not_run"][p] = "RIOT's CI does not run it on this board"
    if groups:
        if out["board"] in answers[UNIT_TESTS].split():
            out["units"] = groups
        else:
            out["not_run"][UNIT_TESTS] = answers[UNIT_TESTS] or "RIOT's CI does not run it on this board"
    if not out["units"] and not out["programs"]:
        out["reason"] = "no selected test can run on native"
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pr", type=int, nargs="+", help="only these PRs of the candidates")
    p.add_argument("--parallel", type=int, default=8, help="PRs at once (default 8)")
    p.add_argument("--redo", action="store_true", help="again for PRs that have a record")
    p.add_argument("--shard", help="I/N: this node's share of the PRs")
    args = p.parse_args()
    if not layout.run_dir():
        sys.exit("needs --run-dir")
    if platform.machine() != "x86_64":
        sys.exit("RIOT's build images are x86 only: run this on an x86 node "
                 "(the cpu partition), in <shared>/riot/venv")
    run = layout.run_dir()
    folder = run / "select"
    folder.mkdir(exist_ok=True)
    cands = [json.loads(l) for l in open(layout.candidates() / "candidates.jsonl")]
    if args.pr:
        cands = [c for c in cands if c["number"] in args.pr]
    todo = [c for c in cands if (args.redo or not (folder / f"{c['number']}.json").exists())
            and riot_env.in_shard(c["number"], args.shard)]
    print(f"{len(cands)} candidates, {len(todo)} to do, {args.parallel} at a time", flush=True)

    def one(c: dict) -> None:
        t = time.time()
        try:
            rec = select(c)
        except Exception as e:      # one PR's failure must not stop the rest; no record: tried again
            print(f"  {c['number']}: ERROR {type(e).__name__}: {str(e)[:300]}", flush=True)
            return
        rec["seconds"] = round(time.time() - t, 1)
        (folder / f"{c['number']}.json").write_text(json.dumps(rec, indent=2) + "\n")
        what = rec.get("reason") or f"{len(rec['units'])} group(s), {len(rec['programs'])} program(s)"
        print(f"  {c['number']}: {rec['image']} {rec['board']} {rec['seconds']}s: {what}", flush=True)

    with ThreadPoolExecutor(args.parallel) as pool:
        list(pool.map(one, todo))
    recs = [json.loads(f.read_text()) for f in folder.glob("*.json")]
    print(f"\n{len(recs)} records: "
          + ", ".join(f"{v} {k}" for k, v in Counter(r.get("reason", "runnable") for r in recs).items()))


if __name__ == "__main__":
    main()
