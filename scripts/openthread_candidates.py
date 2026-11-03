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
import functools
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
from generate_instance import OPENTHREAD_CMAKE_ARGS, OPENTHREAD_LAST_ARG  # noqa: E402

URL = "https://github.com/openthread/openthread/pull/{}"
ENRICH_VERSION = 3
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


def group_records(recs: list[dict]) -> list[dict]:
    """One record per PR. OpenThread squash-merges (one first-parent commit
    ending `(#N)`), but a rebase-merged PR leaves several. Those become one
    record: base = first parent of the earliest commit, fix = the latest
    commit, files = the base..fix diff, `commits` = all of them, oldest first.
    A PR whose commits are not consecutive on the first-parent chain, or that
    continues before the oldest commit listed, gets a "split" reason instead
    of a guessed range."""
    def number(r):
        m = PR_TITLE.search(r["title"])
        return int(m.group(1)) if m and len(r["parents"]) == 1 else None

    groups = {}
    for i, r in enumerate(recs):
        if number(r):
            groups.setdefault(number(r), []).append(i)
    edge = max((g[-1] for g in groups.values()), default=-1)
    out = []
    for i, r in enumerate(recs):
        n = number(r)
        if not n:
            out.append(r)
        elif groups[n][0] == i:
            idx = groups[n]
            chain = [recs[j] for j in reversed(idx)]
            r = dict(r, number=n, commits=[c["fix_commit"] for c in chain],
                     titles=[c["title"] for c in chain])
            base = chain[0]["parents"][0]
            if any(b["parents"][0] != a["fix_commit"] for a, b in zip(chain, chain[1:])):
                r["split"] = f"commits of PR #{n} are interleaved with other PRs on the first-parent chain"
            else:
                if len(chain) > 1:
                    r["parents"] = [base]
                    r["files"] = [f for f in git("diff", "--name-only", "--no-renames",
                                                 base, r["fix_commit"]).split("\n") if f]
                m = PR_TITLE.search(git("log", "-1", "--format=%s", base)) if idx[-1] == edge else None
                if m and int(m.group(1)) == n:
                    r["split"] = f"PR #{n} continues before the discovery window"
            out.append(r)
    return out


def classify(r: dict) -> str | None:
    """Why the hard filters drop this commit, or None for a candidate."""
    m = PR_TITLE.search(r["title"])
    if not m or len(r["parents"]) != 1:
        return "not a squash-merged PR"
    r["number"] = int(m.group(1))
    if r.get("split"):
        return r["split"]
    if any(REVERT.match(t) for t in r.get("titles", [r["title"]])):
        return "revert"
    if not any(f.startswith(TEST_ROOTS) for f in r["files"]):
        return "no change under tests/unit or tests/gtest"
    if not any(not f.startswith(NON_PRODUCTION) for f in r["files"]):
        return "no production change"
    return None


def discover(since: str) -> tuple[list[dict], dict]:
    found, dropped = [], {}
    for r in group_records(log_records(f"--since={since}")):
        why = classify(r)
        if why is None:
            found.append(r)
        else:
            dropped[why] = dropped.get(why, 0) + 1
    return found, dropped


def find_pr(pr: int) -> dict:
    # --grep also matches message bodies; only titles name the PR.
    recs = [r for r in log_records("-E", f"--grep=\\(#{pr}\\)$")
            if (m := PR_TITLE.search(r["title"])) and int(m.group(1)) == pr]
    for r in group_records(recs):
        if r.get("number") == pr:
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


@functools.lru_cache(maxsize=None)
def quoted_includes(rev: str, path: str) -> tuple[str, ...]:
    """Test files `path` includes with #include "...", resolved against its directory."""
    found = (posixpath.normpath(posixpath.join(posixpath.dirname(path), i)) for i in re.findall(
        r'^\s*#\s*include\s*"([^"]+)"', show(rev, path) or "", re.M))
    return tuple(f for f in found if f.startswith(TEST_ROOTS) and show(rev, f) is not None)


def with_includes(rev: str, paths) -> set[str]:
    """`paths` plus every test header they include, transitively."""
    seen, todo = set(), list(paths)
    while todo:
        p = todo.pop()
        if p not in seen:
            seen.add(p)
            todo += quoted_includes(rev, p)
    return seen


def cmake_libraries(cmds: list, directory: str) -> dict:
    """{library: (sources, linked libraries)} for the add_library() commands."""
    libs = {a[0]: [[], []] for n, a, _ in cmds if n == "add_library"}
    for n, a, _ in cmds:
        if n == "add_library":
            libs[a[0]][0] = [s for s in (resolve(x, directory) for x in a[1:] if x.endswith((".c", ".cpp"))) if s]
        elif n == "target_link_libraries" and a[0] in libs:
            libs[a[0]][1] += a[1:]
    return libs


def linked_sources(libs: dict, deps) -> list[str]:
    """Sources of the libraries `deps` name, and of the libraries those link."""
    seen, todo, out = set(), list(deps), []
    while todo:
        d = todo.pop()
        if d in libs and d not in seen:
            seen.add(d)
            out += libs[d][0]
            todo += libs[d][1]
    return out


def unit_inventory(rev: str) -> list[dict]:
    """tests/unit/CMakeLists.txt registers a test through the ot_unit_test(name
    [extra sources]) macro (target and ctest name ot-test-<name>, source
    test_<name>.cpp), ot_unit_ncp_test(name) (ot-test-ncp-<name>, test_ncp_<name>.cpp),
    or a plain add_executable(ot-test-...) with its own add_test().
    `helper_sources` are the files an executable also compiles: the sources
    of the add_library() targets (COMMON_LIBS links them all into every
    test) and the test headers any of those include."""
    text = show(rev, "tests/unit/CMakeLists.txt")
    if text is None:
        return []
    tests, direct, ctest = [], {}, {}
    cmds = list(cmake_commands(text))
    libs = sum((v[0] for v in cmake_libraries(cmds, "tests/unit").values()), [])
    for name, a, guards in cmds:
        if name in ("ot_unit_test", "ot_unit_ncp_test"):
            t = ("ot-test-" if name == "ot_unit_test" else "ot-test-ncp-") + a[0]
            srcs = [("test_" if name == "ot_unit_test" else "test_ncp_") + a[0] + ".cpp"] + a[1:]
            tests.append((t, t, srcs, guards))
        elif name == "add_executable" and a[0].startswith("ot-test-"):
            direct[a[0]] = (a[1:], guards)
        elif name == "add_test" and "NAME" in a and "COMMAND" in a:
            ctest[a[a.index("COMMAND") + 1]] = a[a.index("NAME") + 1]
    tests += [(ctest[t], t, srcs, g) for t, (srcs, g) in direct.items() if t in ctest]
    out = []
    for c, t, srcs, g in tests:
        sources = [s for s in (resolve(x, "tests/unit") for x in srcs) if s]
        out.append({"ctest_name": c, "target": t, "sources": sources,
                    "helper_sources": sorted(with_includes(rev, sources + libs) - set(sources)),
                    "requires_options": g})
    return out


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
    exes, discovered, links = {}, set(), {}
    cmds = list(cmake_commands(text))
    libs = cmake_libraries(cmds, "tests/gtest")
    for name, a, guards in cmds:
        if name == "add_executable":
            exes[a[0]] = (a[1:], guards)
        elif name == "gtest_discover_tests":
            discovered.add(a[0])
        elif name == "target_link_libraries":
            links.setdefault(a[0], []).extend(a[1:])
    out = []
    for t, (srcs, guards) in exes.items():
        if t in discovered:
            sources = [s for s in (resolve(x, "tests/gtest") for x in srcs) if s]
            helpers = linked_sources(libs, links.get(t, []))
            out.append({"target": t, "sources": sources,
                        "helper_sources": sorted(with_includes(rev, sources + helpers) - set(sources)),
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


def _bash_arrays(text: str, platform: str) -> list[str]:
    """The items script/cmake-build passes for `platform`: its `NAME=(...)` /
    `NAME+=(...)` array statements, evaluated in order with the `case` branch
    for the platform (else `*`) kept and the others dropped. Raises ValueError
    when the script has no recognizable structure."""
    text = re.sub(r"(?m)^\s*#.*$", "", text)
    arrays: dict[str, list[str]] = {}
    case_open = False
    keep = True
    stmt = re.compile(r"^\s*(?:local\s+|readonly\s+)?(\w+)(\+?)=\(", re.M)
    # split the text into lines and walk them, tracking the platform case
    lines = text.split("\n")
    i = 0
    branch_hit = False
    while i < len(lines):
        line = lines[i]
        m = re.match(r"\s*case\s+\"?\$\{?platform\}?\"?\s+in", line)
        if m:
            case_open, keep, branch_hit = True, False, False
            i += 1
            continue
        if case_open:
            if re.match(r"\s*esac\b", line):
                case_open, keep = False, True
                i += 1
                continue
            b = re.match(r"\s*([\w*|\"-]+)\)\s*$", line)
            if b:
                pats = [x.strip('"') for x in b.group(1).split("|")]
                keep = platform in pats or ("*" in pats and not branch_hit)
                branch_hit |= platform in pats
                i += 1
                continue
        m = stmt.match(line)
        if m:
            body = line[m.end():]
            while ")" not in body.split("#")[0] and i + 1 < len(lines):
                i += 1
                body += " " + lines[i]
            body = body[:body.rindex(")")]
            items = []
            for tok in re.findall(r'"([^"]*)"|(\S+)', body):
                t = tok[0] or tok[1]
                ref = re.fullmatch(r"\$\{(\w+)\[@\]\}", t)
                if ref:
                    items += arrays.get(ref.group(1), [])
                elif t != "$@":
                    items.append(t.replace("${platform}", platform))
            name = m.group(1)
            # top-level (or kept-branch) statements only
            if keep or not case_open:
                arrays[name] = (arrays.get(name, []) if m.group(2) else []) + items
        i += 1
    if "options" not in arrays:
        raise ValueError("no `options` array in script/cmake-build")
    final = [x for x in arrays["options"] if x.startswith("-DOT_")]
    if not final:
        raise ValueError("no -DOT_ options parsed")
    return final


def profile_options(base: str) -> list[str] | str:
    """The effective -D options of our execution profile at `base`: the -DOT_*
    entries `script/cmake-build simulation` passes, then OPENTHREAD_CMAKE_ARGS
    and OPENTHREAD_LAST_ARG (generate_instance.py); later wins per option.
    A string (an error message) when the script cannot be parsed."""
    text = show(base, "script/cmake-build")
    if text is None:
        return "ERROR: script/cmake-build missing at the base commit"
    try:
        upstream = _bash_arrays(text, "simulation")
    except ValueError as e:
        return f"ERROR: {e}"
    out: list[str] = []
    for a in upstream + OPENTHREAD_CMAKE_ARGS + [OPENTHREAD_LAST_ARG]:
        out = [x for x in out if x.split("=")[0] != a.split("=")[0]] + [a]
    return out


def select_tests(rec: dict) -> dict:
    """The test executables that compile a file the PR's test diff changed
    (their own sources, or the helper libraries and headers they pull in),
    from an enriched record: {"unit": [inventory entries], "gtest": [...]}.
    Both empty means the changed test files (a CMakeLists, a helper no
    registered executable compiles) map to no runnable target."""
    changed = set(rec["test_files"])
    inv = rec["test_inventory"]
    return {kind: [t for t in inv[kind]
                   if changed & set(t["sources"] + t.get("helper_sources", []))]
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
            "commits": r.get("commits", [fix]),
            "body": pr_body(r["number"]),
            "source_files": [f for f in r["files"] if not f.startswith("tests/")],
            "test_files": tests,
            "diffs": {"tests": clip(git("diff", base, fix, "--", "tests/")),
                      "source": clip(git("diff", base, fix, "--", ".", ":(exclude)tests/"))},
            "test_inventory": {"unit": unit_inventory(fix), "gtest": gtest_inventory(fix)},
            "test_config_guards": config_guards(fix, tests),
            "ci_cmake_options": ci_options(base),
            "profile_cmake_options": profile_options(base),
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
    r = find_pr(12913)
    check("12913 (rebase-merged) is one record over all its commits",
          len(r["commits"]) == 6 and r["parents"][0].startswith("c801d291b")
          and r["fix_commit"].startswith("2d602dcd9") and classify(dict(r)) is None)
    check("11239 (commits interleaved with 11240) is not guessed",
          "interleaved" in (classify(dict(find_pr(11239))) or ""))
    fix = find_pr(10982)["fix_commit"]
    inv = {"unit": unit_inventory(fix), "gtest": gtest_inventory(fix)}
    pick = lambda f: {k: len(v) for k, v in select_tests({"test_files": [f], "test_inventory": inv}).items()}
    check("a shared unit helper header selects every unit executable",
          pick("tests/unit/test_util.h") == {"unit": len(inv["unit"]), "gtest": 0})
    check("the fake platform selects only the gtest executables that link it",
          pick("tests/gtest/fake_platform.hpp") == {"unit": 0, "gtest": len(inv["gtest"])}
          and pick("tests/gtest/fake_coprocessor_platform.cpp") == {"unit": 0, "gtest": 1})
    check("a CMakeLists.txt maps to no target", pick("tests/unit/CMakeLists.txt") == {"unit": 0, "gtest": 0})
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
