#!/usr/bin/env python3
"""
Hard filters and enrichment for OpenThread PR candidates.

    python scripts/openthread_candidates.py selftest
    python scripts/openthread_candidates.py filter [--since "2 years ago"]
    python scripts/openthread_candidates.py enrich --pr 13499 13787

Also reached as `filter_candidates.py --repo openthread ...`. Everything comes
from the clone's `git log --first-parent` (layout.openthread_clone()); OpenThread
squash-merges, so a first-parent commit whose title ends `(#NNNNN)` is one PR,
and its first parent is the base. The GitHub PR body is fetched with `gh` when
it is available, else left null.

Writes <candidates>/openthread/candidates.jsonl and
<candidates>/openthread/enriched/<pr>.json (docs: candidates/openthread/research/contract.md).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)

URL = "https://github.com/openthread/openthread/pull/{}"
ENRICH_VERSION = 1
DIFF_CAP = 120_000      # characters kept of each diff in a record

# Not production code, by what the repository holds at the top level: tests/
# is the oracle; doc/, .github/ and script/ are documentation and CI/dev
# scripts; third_party/ is vendored (mbedtls, tcplp, ...); examples/ and etc/
# are example apps/platforms and build/docker support; tools/ and zephyr/ are
# host tooling and the Zephyr module glue. A PR changing only these cannot be
# detected by a unit or gtest binary built from src/ and include/.
NON_PRODUCTION = ("tests/", "doc/", ".github/", "script/", "third_party/",
                  "examples/", "etc/", "tools/", "zephyr/")
TEST_ROOTS = ("tests/unit/", "tests/gtest/")
PR_TITLE = re.compile(r"\(#(\d+)\)$")
REVERT = re.compile(r"^(\[[^\]]*\]\s*)?revert\b", re.I)


def clone() -> Path:
    return layout.openthread_clone()


def enriched_dir() -> Path:
    return layout.candidates() / "openthread" / "enriched"


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(clone()), *args], capture_output=True,
                       text=True, errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def show(rev: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(clone()), "show", f"{rev}:{path}"],
                       capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else None


# ---------------------------------------------------------------- discovery


def log_records(*args: str) -> list[dict]:
    out = git("log", "--first-parent", "--no-renames", "--name-only",
              "--format=%x1e%H%x1f%P%x1f%cI%x1f%s", *args)
    recs = []
    for chunk in out.split("\x1e")[1:]:
        head, _, files = chunk.partition("\n")
        sha, parents, date, title = head.split("\x1f", 3)
        recs.append({"fix_commit": sha, "parents": parents.split(), "merged_at": date,
                     "title": title, "files": [f for f in files.split("\n") if f]})
    return recs


def classify(r: dict) -> str | None:
    """Why the hard filters drop this commit, or None for a candidate."""
    m = PR_TITLE.search(r["title"])
    if not m or len(r["parents"]) != 1:
        return "not a squash-merged PR"
    r["number"] = int(m.group(1))
    if REVERT.match(r["title"]):
        return "revert"
    if not any(f.startswith(TEST_ROOTS) for f in r["files"]):
        return "no change under tests/unit or tests/gtest"
    if not any(not f.startswith(NON_PRODUCTION) for f in r["files"]):
        return "no production change"
    return None


def discover(since: str) -> tuple[list[dict], dict]:
    found, dropped = [], {}
    for r in log_records(f"--since={since}"):
        why = classify(r)
        if why is None:
            found.append(r)
        else:
            dropped[why] = dropped.get(why, 0) + 1
    return found, dropped


def find_pr(pr: int) -> dict:
    for r in log_records("-E", f"--grep=\\(#{pr}\\)$"):
        m = PR_TITLE.search(r["title"])
        if m and int(m.group(1)) == pr:
            r["number"] = pr
            return r
    raise SystemExit(f"PR {pr}: no first-parent commit ending (#{pr}) in {clone()}")


def cmd_filter(args) -> None:
    found, dropped = discover(getattr(args, "since", None) or "2 years ago")
    out = layout.candidates() / "openthread"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "candidates.jsonl", "w") as f:
        for r in found:
            f.write(json.dumps({k: r[k] for k in ("number", "title", "merged_at", "fix_commit")}
                               | {"base_commit": r["parents"][0]}) + "\n")
    print(f"{len(found)} candidates -> {layout.show(out / 'candidates.jsonl')}")
    print("dropped: " + ", ".join(f"{n} {why}" for why, n in sorted(dropped.items())))


# ---------------------------------------------------------------- test inventory


def option_names(cond: str) -> list[str]:
    """OT_* options a CMake condition needs to be true (negated ones skipped)."""
    cond = re.sub(r"\bNOT\s+\w+", "", cond)
    return list(dict.fromkeys(re.findall(r"\bOT_[A-Z0-9_]+\b", cond)))


def cmake_commands(text: str):
    """(name, [args], guards) for every command outside a macro body, in
    order; `guards` is the OT_* options of the enclosing if() blocks."""
    text = re.sub(r"#[^\n]*", "", text)
    stack, in_macro = [], False
    for m in re.finditer(r"(\w+)\s*\(((?:[^()]|\([^()]*\))*)\)", text):
        name, body = m.group(1).lower(), m.group(2)
        if name == "macro":
            in_macro = True
        elif name == "endmacro":
            in_macro = False
        elif in_macro:
            continue
        elif name == "if":
            stack.append(option_names(body))
        elif name == "elseif":
            stack[-1] = option_names(body)
        elif name == "else":
            stack[-1] = []
        elif name == "endif":
            stack.pop()
        else:
            yield name, body.split(), list(dict.fromkeys(o for g in stack for o in g))


def resolve(src: str, directory: str) -> str | None:
    """A source named in CMake as a repository-relative path, or None for
    anything that depends on a variable we do not know."""
    src = src.replace("${openthread_SOURCE_DIR}/", "")
    return None if "${" in src else (src if "/" in src else f"{directory}/{src}")


def unit_inventory(rev: str) -> list[dict]:
    """tests/unit/CMakeLists.txt registers a test through the ot_unit_test(name
    [extra sources]) macro (target and ctest name ot-test-<name>, source
    test_<name>.cpp), ot_unit_ncp_test(name) (ot-test-ncp-<name>, test_ncp_<name>.cpp),
    or a plain add_executable(ot-test-...) with its own add_test()."""
    text = show(rev, "tests/unit/CMakeLists.txt")
    if text is None:
        return []
    tests, direct, ctest = [], {}, {}
    for name, a, guards in cmake_commands(text):
        if name in ("ot_unit_test", "ot_unit_ncp_test"):
            t = ("ot-test-" if name == "ot_unit_test" else "ot-test-ncp-") + a[0]
            srcs = [("test_" if name == "ot_unit_test" else "test_ncp_") + a[0] + ".cpp"] + a[1:]
            tests.append((t, t, srcs, guards))
        elif name == "add_executable" and a[0].startswith("ot-test-"):
            direct[a[0]] = (a[1:], guards)
        elif name == "add_test" and "NAME" in a and "COMMAND" in a:
            ctest[a[a.index("COMMAND") + 1]] = a[a.index("NAME") + 1]
    tests += [(ctest[t], t, srcs, g) for t, (srcs, g) in direct.items() if t in ctest]
    return [{"ctest_name": c, "target": t,
             "sources": [s for s in (resolve(x, "tests/unit") for x in srcs) if s],
             "requires_options": g} for c, t, srcs, g in tests]


def gtest_cases(rev: str, path: str) -> list[str]:
    text = show(rev, path) or ""
    return [f"{s}.{c}" for s, c in re.findall(
        r"^\s*TEST(?:_F|_P)?\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)", text, re.M)]


def gtest_inventory(rev: str) -> list[dict]:
    """tests/gtest/CMakeLists.txt: every add_executable() later passed to
    gtest_discover_tests(); its cases are the TEST/TEST_F/TEST_P names in the
    sources it compiles (ctest names the discovered cases the same way)."""
    text = show(rev, "tests/gtest/CMakeLists.txt")
    if text is None:
        return []
    exes, discovered = {}, set()
    for name, a, guards in cmake_commands(text):
        if name == "add_executable":
            exes[a[0]] = (a[1:], guards)
        elif name == "gtest_discover_tests":
            discovered.add(a[0])
    out = []
    for t, (srcs, guards) in exes.items():
        if t in discovered:
            sources = [s for s in (resolve(x, "tests/gtest") for x in srcs) if s]
            out.append({"target": t, "sources": sources,
                        "cases": [c for s in sources for c in gtest_cases(rev, s)],
                        "requires_options": guards})
    return out


def config_guards(rev: str, paths: list[str]) -> dict:
    """OPENTHREAD_CONFIG_* macros that `#if` lines of the changed test sources
    test, as the OT_* CMake option that sets each (etc/cmake/options.cmake).
    Such a test body is compiled out, so passes vacuously, unless the option
    is on; CMakeLists.txt shows nothing of it."""
    to_option = dict((m, o) for o, m in re.findall(
        r"ot_option\((OT_\w+)\s+(OPENTHREAD_\w+)", show(rev, "etc/cmake/options.cmake") or ""))
    out = {}
    for p in paths:
        if p.endswith((".cpp", ".c", ".hpp", ".h")):
            macros = re.findall(r"^\s*#\s*(?:el)?if\b(.*)$", show(rev, p) or "", re.M)
            found = list(dict.fromkeys(m for line in macros
                                       for m in re.findall(r"\bOPENTHREAD_\w*CONFIG_\w+", line)))
            if found:
                out[p] = [to_option.get(m, m) for m in found]
    return out


def ci_options(base: str) -> list[str]:
    """-DOT_* configure options of the base commit's unit-test workflow."""
    text = show(base, ".github/workflows/unit.yml") or ""
    return list(dict.fromkeys(re.findall(r"-DOT_\w+=\S+", text)))


def select_tests(rec: dict) -> dict:
    """The test executables whose sources the PR's test diff changed, from an
    enriched record: {"unit": [inventory entries], "gtest": [inventory entries]}.
    Both empty means the changed test files (a CMakeLists, a helper no
    registered executable compiles) map to no runnable target."""
    changed = set(rec["test_files"])
    inv = rec["test_inventory"]
    return {kind: [t for t in inv[kind] if changed & set(t["sources"])]
            for kind in ("unit", "gtest")}


# ---------------------------------------------------------------- enrichment


def clip(text: str) -> str:
    return text if len(text) <= DIFF_CAP else text[:DIFF_CAP] + "\n[... diff truncated ...]\n"


def pr_body(pr: int) -> str | None:
    try:
        r = subprocess.run(["gh", "pr", "view", str(pr), "-R", "openthread/openthread",
                            "--json", "body", "-q", ".body"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def enrich(r: dict) -> dict:
    base = r["parents"][0]
    fix = r["fix_commit"]
    tests = [f for f in r["files"] if f.startswith("tests/")]
    return {"number": r["number"], "title": r["title"], "url": URL.format(r["number"]),
            "merged_at": r["merged_at"], "fix_commit": fix, "base_commit": base,
            "body": pr_body(r["number"]),
            "source_files": [f for f in r["files"] if not f.startswith("tests/")],
            "test_files": tests,
            "diffs": {"tests": clip(git("diff", base, fix, "--", "tests/")),
                      "source": clip(git("diff", base, fix, "--", ".", ":(exclude)tests/"))},
            "test_inventory": {"unit": unit_inventory(fix), "gtest": gtest_inventory(fix)},
            "test_config_guards": config_guards(fix, tests),
            "ci_cmake_options": ci_options(base),
            "enrich_version": ENRICH_VERSION}


def cmd_enrich(args) -> None:
    if args.pr:
        todo = [find_pr(p) for p in args.pr]
    else:
        todo = discover("2 years ago")[0][:args.limit]
    out = enriched_dir()
    out.mkdir(parents=True, exist_ok=True)
    for r in todo:
        path = out / f"{r['number']}.json"
        if path.exists() and not args.refresh:
            print(f"  {r['number']}  already enriched")
            continue
        why = classify(dict(r))
        if why:
            print(f"  {r['number']}  not a candidate: {why}")
            continue
        rec = enrich(r)
        path.write_text(json.dumps(rec, indent=2) + "\n")
        inv = rec["test_inventory"]
        print(f"  {r['number']}  {len(inv['unit'])} unit, "
              f"{sum(len(g['cases']) for g in inv['gtest'])} gtest cases -> {layout.show(path)}")


# ---------------------------------------------------------------- selftest


def cmd_selftest(args=None) -> None:
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        ok &= cond
        print(f"  {'ok  ' if cond else 'FAIL'} {label}")

    r = find_pr(13499)
    check("13499 is a candidate", classify(dict(r)) is None)
    gt = {c for g in gtest_inventory(r["fix_commit"]) for c in g["cases"]}
    check("13499 inventory lists DatasetManagerSave gtest cases",
          any(c.startswith("DatasetManagerSave.") for c in gt))
    check("13787 heap_array is a unit test", any(
        t["ctest_name"] == "ot-test-heap_array" for t in unit_inventory(find_pr(13787)["fix_commit"])))
    check("13787 test is guarded by OT_EXTERNAL_HEAP", "OT_EXTERNAL_HEAP" in config_guards(
        find_pr(13787)["fix_commit"], ["tests/unit/test_heap_array.cpp"]).get(
        "tests/unit/test_heap_array.cpp", []))
    check("13742 (production-only) is filtered", classify(dict(find_pr(13742))) is not None)
    check("revert titles are filtered", classify(
        {"title": "Revert \"[x] y\" (#1)", "parents": ["a"], "files": ["tests/unit/a.cpp", "src/a.cpp"]}
    ) == "revert")
    sys.exit(0 if ok else 1)


def dispatch(args) -> None:
    command = getattr(args, "command", None) or args.cmd
    if command == "selftest":
        cmd_selftest()
    elif command == "fetch":
        print("nothing to fetch: OpenThread candidates are read from the clone; use `filter`")
    elif command == "filter":
        cmd_filter(args)
    else:
        cmd_enrich(args)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest")
    f = sub.add_parser("filter")
    f.add_argument("--since", default="2 years ago")
    e = sub.add_parser("enrich")
    e.add_argument("--pr", type=int, nargs="+")
    e.add_argument("--limit", type=int)
    e.add_argument("--refresh", action="store_true")
    dispatch(p.parse_args())


if __name__ == "__main__":
    main()
