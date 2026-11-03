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
ENRICH_VERSION = 5
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
    """OT_* options a CMake condition needs to be true (negated ones skipped).
    A condition with an OR or a non-OT variable (CMAKE_SYSTEM_NAME, ...) is
    not a plain conjunction of options and names none."""
    if re.search(r"\bOR\b|\bCMAKE_\w+", cond):
        return []
    cond = re.sub(r"\bNOT\s+\w+", "", cond)
    return list(dict.fromkeys(re.findall(r"\bOT_[A-Z0-9_]+\b", cond)))


class Guard(list):
    """The OT_* options of the enclosing if() blocks; `conds` holds the full
    conditions (an elseif/else adds the negation of the earlier branches)."""
    conds: list


def cmake_commands(text: str):
    """(name, [args], guards) for every command outside a macro body, in
    order; `guards` is a Guard for the enclosing if() blocks."""
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
            stack.append({"prev": [], "cur": body})
        elif name in ("elseif", "else"):
            stack[-1]["prev"].append(stack[-1]["cur"])
            stack[-1]["cur"] = body if name == "elseif" else None
        elif name == "endif":
            stack.pop()
        else:
            g = Guard(dict.fromkeys(o for f in stack if f["cur"] for o in option_names(f["cur"])))
            g.conds = [c for f in stack for c in [f"NOT ({p})" for p in f["prev"]]
                       + ([f["cur"]] if f["cur"] else [])]
            yield name, body.split(), g


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
                    "requires_options": list(g), "if_conditions": g.conds})
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
                        "requires_options": list(guards), "if_conditions": guards.conds})
    return out


def config_guards(rev: str, paths: list[str]) -> dict:
    """Macros that the #if/#elif/#ifdef/#ifndef lines (continuations joined) of
    the changed test sources use, with the OT_* CMake option that sets each
    OPENTHREAD_CONFIG_* one (etc/cmake/options.cmake). Such a test body is
    compiled out, so passes vacuously, unless the guard holds; CMakeLists.txt
    shows nothing of it. Include guards are left out."""
    to_option = dict((m, o) for o, m in re.findall(
        r"ot_\w*option\((OT_\w+)\s+(?:\w+\s+)?(OPENTHREAD_\w+)", show(rev, "etc/cmake/options.cmake") or ""))
    out = {}
    for p in paths:
        if p.endswith((".cpp", ".c", ".hpp", ".h")):
            lines = c_lines(show(rev, p) or "")
            found = []
            for i, line in enumerate(lines):
                d = directive(line)
                if d and d[0] in ("if", "ifdef", "ifndef", "elif"):
                    nxt = directive(lines[i + 1]) if i + 1 < len(lines) else None
                    if d[0] == "ifndef" and nxt and nxt[0] == "define" and nxt[1].split()[:1] == [d[1]]:
                        continue
                    found += c_idents(d[1])
            found = [m for m in dict.fromkeys(found) if not m.startswith("__")]
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


def enabled_options(base: str, profile: list[str] | str) -> list[str] | None:
    """The OT_* options that are ON in our execution profile: those
    etc/cmake/options.cmake at `base` declares `option(OT_X "..." ON)` (OT_FTD,
    OT_MTD, OT_RCP), unless the profile sets them otherwise, plus the profile's
    own =ON/1/TRUE ones. None when the profile could not be resolved."""
    if isinstance(profile, str):
        return None
    on = set(re.findall(r'^\s*option\s*\(\s*(OT_\w+)\s+"[^"]*"\s+ON\s*\)',
                        show(base, "etc/cmake/options.cmake") or "", re.M))
    for a in profile:
        name, _, val = a[2:].partition("=")
        (on.add if val.upper() in ("ON", "1", "TRUE") else on.discard)(name)
    return sorted(on)


def missing_options(t: dict, rec: dict) -> list[str]:
    """Options the target's CMake guards need that our profile leaves off."""
    on = rec.get("profile_enabled_options")
    return [] if on is None else [o for o in t["requires_options"] if o not in on]


def cmake_if(cond: str, rec: dict) -> tuple[bool | None, list[str]]:
    """A CMake if() condition under the profile: (True/False/None, unknown
    names). CMAKE_SYSTEM_NAME is Linux; an OT_* option is its -D value, ON if
    on by default, else unset (""); NOT, AND, OR, parentheses and STREQUAL
    work. Anything else is unknown (None)."""
    vals = {a[2:].split("=")[0]: a[2:].partition("=")[2] for a in rec.get("profile_cmake_options") or []
            if a.startswith("-D")}
    toks, i, unknown = re.findall(r'"[^"]*"|[()]|[^\s()"]+', cond), 0, []

    def value(tok):
        if tok.startswith('"'):
            return tok[1:-1]
        if tok == "CMAKE_SYSTEM_NAME":
            return "Linux"
        if tok.startswith("OT_"):
            return vals.get(tok, "ON" if tok in (rec.get("profile_enabled_options") or []) else "")
        if tok.upper() in ("ON", "OFF", "TRUE", "FALSE") or tok.isdigit():
            return tok
        unknown.append(tok)
        return None

    def truth(v):
        return None if v is None else v.upper() in ("ON", "TRUE", "Y", "YES") or (v.isdigit() and int(v) != 0)

    def parse(level):
        nonlocal i
        if level == 0:
            left = parse(1)
            while i < len(toks) and toks[i] == "OR":
                i += 1
                right = parse(1)
                left = True if left or right else None if None in (left, right) else False
            return left
        if level == 1:
            left = parse(2)
            while i < len(toks) and toks[i] == "AND":
                i += 1
                right = parse(2)
                left = False if left is False or right is False else None if None in (left, right) else True
            return left
        if level == 2:
            if toks[i] == "NOT":
                i += 1
                v = parse(2)
                return None if v is None else not v
            return parse(3)
        if toks[i] == "(":
            i += 1
            v = parse(0)
            i += 1
            return v
        left = value(toks[i])
        i += 1
        if i < len(toks) and toks[i] == "STREQUAL":
            right = value(toks[i + 1])
            i += 2
            return None if None in (left, right) else left == right
        return truth(left)

    try:
        v = parse(0)
        return (v if i == len(toks) else None), unknown
    except IndexError:
        return None, unknown or ["unparsable condition"]


def unbuilt_reasons(t: dict, rec: dict) -> list[str]:
    """Why the executable is not built in our profile: the options its CMake
    guards need that are off or, failing that, the if() conditions that are
    false or cannot be evaluated (such a target is conservatively not built)."""
    if rec.get("profile_enabled_options") is None:
        return []
    why = missing_options(t, rec)
    for c in [] if why else t.get("if_conditions", []):
        v, unknown = cmake_if(c, rec)
        if v is not True:
            why.append(f"if({c})" + (f" cannot be evaluated ({', '.join(unknown)})" if v is None else " is false"))
    return why


def gated_targets(rec: dict) -> list[tuple[str, list[str]]]:
    """(target, reasons) for each executable that compiles a changed test
    file but is not built in our profile."""
    changed = set(rec["test_files"])
    return [(t["target"], unbuilt_reasons(t, rec)) for kind in ("unit", "gtest")
            for t in rec["test_inventory"][kind]
            if changed & set(t["sources"] + t.get("helper_sources", [])) and unbuilt_reasons(t, rec)]


def select_tests(rec: dict) -> dict:
    """The test executables that compile a file the PR's test diff changed
    (their own sources, or the helper libraries and headers they pull in),
    from an enriched record: {"unit": [inventory entries], "gtest": [...]}.
    Both empty means the changed test files (a CMakeLists, a helper no
    registered executable compiles) map to no runnable target. An executable
    that our profile does not build (a CMake guard needs an option it leaves
    off, or an if() condition is false or unknown) is never selected
    (gated_targets() lists those, for the message)."""
    changed = set(rec["test_files"])
    inv = rec["test_inventory"]
    return {kind: [t for t in inv[kind]
                   if changed & set(t["sources"] + t.get("helper_sources", []))
                   and not unbuilt_reasons(t, rec)]
            for kind in ("unit", "gtest")}


# ---------------------------------------------------------------- guard resolution

SIM_CONFIG = "examples/platforms/simulation/openthread-core-simulation-config.h"
# Macros the unit/gtest executables get from their build (FTD, no POSIX platform) or the host.
BUILD_FIXED = {"OPENTHREAD_FTD": 1, "OPENTHREAD_MTD": 0, "OPENTHREAD_RADIO": 0,
               "OPENTHREAD_PLATFORM_POSIX": 0, "OPENTHREAD_PLATFORM_NEXUS": 0,
               "__linux__": 1, "__GNUC__": 1, "__cplusplus": 201703}
# Defined by compilers or third-party headers we do not index: value unknown.
OPAQUE = re.compile(r"__|_[A-Z]|MBEDTLS_|PSA_|GTEST_|OPENTHREAD_POSIX_")
TRUE_VALUES = ("ON", "1", "TRUE", "Y", "YES")
C_LEVELS = (("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="),
            ("<<", ">>"), ("+", "-"), ("*", "/", "%"))
C_TOKEN = re.compile(r"\s*(?:(0[xX][0-9a-fA-F]+|\d+)[uUlL]*|([A-Za-z_]\w*)"
                     r"|(<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%<>!~&|^?:()]))")
C_STRIP = re.compile(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', re.S)


def c_lines(text: str) -> list[str]:
    """Logical source lines: comments removed, backslash continuations joined."""
    text = C_STRIP.sub(lambda m: m.group(0) if m.group(0)[0] == '"' else " ", text)
    return text.replace("\\\n", " ").split("\n")


def directive(line: str) -> tuple[str, str] | None:
    m = re.match(r"\s*#\s*(\w+)\s*(.*)$", line)
    return (m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()) if m else None


def c_idents(expr: str) -> list[str]:
    return [i for i in dict.fromkeys(re.findall(r"[A-Za-z_]\w*", expr)) if i not in ("defined", "true", "false")]


def c_eval(expr: str, ident, is_defined) -> int | None:
    """An #if expression as an int; None when it cannot be parsed or a value is
    unknown (three-valued, so `0 && unknown` is 0). `ident(name)` gives a
    macro's value (undefined ones are 0 in C), `is_defined(name)` defined()."""
    toks, pos, expr = [], 0, expr.strip()
    while pos < len(expr):
        m = C_TOKEN.match(expr, pos)
        if not m:
            return None
        pos = m.end()
        toks.append(("n", int(m.group(1), 16 if m.group(1)[1:2] in "xX" else 10)) if m.group(1)
                    else ("i", m.group(2)) if m.group(2) else ("o", m.group(3)))
    i = 0

    def peek():
        return toks[i][1] if i < len(toks) and toks[i][0] == "o" else None

    def take(op):
        nonlocal i
        if peek() != op:
            raise ValueError(op)
        i += 1

    def binary(op, a, b):
        if op == "&&":
            return 0 if a == 0 or b == 0 else None if a is None or b is None else 1
        if op == "||":
            return 1 if a or b else None if a is None or b is None else 0
        if a is None or b is None or (op in "/%" and b == 0) or (op in ("<<", ">>") and not 0 <= b < 64):
            return None
        q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1) if op in "/%" else 0
        return int({"|": lambda: a | b, "^": lambda: a ^ b, "&": lambda: a & b, "==": lambda: a == b,
                    "!=": lambda: a != b, "<": lambda: a < b, ">": lambda: a > b, "<=": lambda: a <= b,
                    ">=": lambda: a >= b, "<<": lambda: a << b, ">>": lambda: a >> b, "+": lambda: a + b,
                    "-": lambda: a - b, "*": lambda: a * b, "/": lambda: q, "%": lambda: a - b * q}[op]())

    def ternary():
        nonlocal i
        c = level(0)
        if peek() != "?":
            return c
        i += 1
        a = ternary()
        take(":")
        b = ternary()
        return None if c is None else a if c else b

    def level(n):
        nonlocal i
        if n == len(C_LEVELS):
            return unary()
        left = level(n + 1)
        while peek() in C_LEVELS[n]:
            op = peek()
            i += 1
            left = binary(op, left, level(n + 1))
        return left

    def unary():
        nonlocal i
        op = peek()
        if op in ("!", "~", "-", "+"):
            i += 1
            v = unary()
            return None if v is None else {"!": int(not v), "~": ~v, "-": -v, "+": v}[op]
        if op == "(":
            i += 1
            v = ternary()
            take(")")
            return v
        if i >= len(toks) or toks[i][0] == "o":
            raise ValueError("operand")
        kind, val = toks[i]
        i += 1
        if kind == "n":
            return val
        if val == "defined":
            paren = peek() == "("
            i += paren
            name = toks[i][1]
            i += 1
            if paren:
                take(")")
            return is_defined(name)
        if val in ("true", "false"):
            return int(val == "true")
        if peek() == "(":               # function-like macro or __has_include(): not modelled
            depth = 0
            while True:
                depth += (peek() == "(") - (peek() == ")")
                i += 1
                if depth == 0:
                    return None
        return ident(val)

    try:
        v = ternary()
        return v if i == len(toks) else None
    except (ValueError, IndexError):
        return None


@functools.lru_cache(maxsize=None)
def cmake_definitions(rev: str) -> str:
    """The text of every CMake file at `rev` that passes a NAME=value definition."""
    paths = [x.split(":", 1)[1] for x in git("grep", "-l", "-I", "-E", r"(OPENTHREAD|OT)_\w+=", rev, "--",
                                              "*CMakeLists.txt", "*.cmake", check=False).split()]
    return "\n".join(show(rev, f) or "" for f in paths)


@functools.lru_cache(maxsize=None)
def define_index(rev: str) -> dict:
    """{macro: [(path, value)]} for the object-like #defines under src/ and
    include/ at `rev` (and the simulation core config), from one git grep."""
    raw, out = {}, {}
    for line in git("grep", "-n", "-I", "-E", r"^\s*#\s*define\s+\w+", rev, "--", "src", "include",
                    SIM_CONFIG, check=False).splitlines():
        _, path, no, text = line.split(":", 3)
        if text.rstrip().endswith("\\"):      # the grep shows only the first line
            lines = raw.setdefault(path, (show(rev, path) or "").split("\n"))
            text, n = "", int(no) - 1
            while n < len(lines):
                text += " " + lines[n].rstrip().rstrip("\\")
                n += 1
                if not lines[n - 1].rstrip().endswith("\\"):
                    break
        m = re.match(r"\s*#\s*define\s+(\w+)(\(?)\s*(.*)$", C_STRIP.sub(" ", text))
        if m and not m.group(2):
            out.setdefault(m.group(1), []).append((path, m.group(3).strip()))
    return out


class Resolver:
    """Static macro values for the test executables of one `rev` under cmake
    `options` (-DOT_X=VAL strings). Order: build facts, the cmake option that
    options.cmake maps to the macro, macros CMake sets by logic we do not model
    (unknown), the simulation core config, then the #defines of src/ and include/.
    `local` holds the #defines of the test file being walked (None = #undef)."""

    def __init__(self, rev: str, options):
        self.rev, self.local, self.cache, self.stack = rev, {}, {}, []
        self.opts = dict(o[2:].split("=", 1) for o in options if o.startswith("-D") and "=" in o)
        text = show(rev, "etc/cmake/options.cmake") or ""
        self.mapped = {c: ("option", n, d) for n, c, d in re.findall(
            r'ot_option\((OT_\w+)\s+(OPENTHREAD_\w+)\s+"[^"]*"\s*(\w*)\s*\)', text)}
        self.mapped |= {c: ("int", n, "") for n, c in re.findall(
            r"ot_int_option\((OT_\w+)\s+(OPENTHREAD_\w+)", text)}
        self.mapped |= {c: ("multi", n, p) for n, c, p in re.findall(
            r"ot_multi_option\((OT_\w+)\s+\w+\s+(OPENTHREAD_\w+)\s+(\w+)", text)}
        defs = cmake_definitions(rev)
        self.mapped |= {c: ("flag", n, e) for n, c, e in re.findall(      # if(OT_X) ...MACRO=1 [else() ...MACRO=0]
            r'if\s*\(\s*(OT_\w+)\s*\)\s*target_compile_definitions\(\s*\S+\s+\w+\s+"(\w+)=1"\s*\)'
            r'(?:\s*(else)\s*\(\s*\)\s*target_compile_definitions\(\s*\S+\s+\w+\s+"\2=0"\s*\))?', defs)}
        self.by_cmake = set(re.findall(r"(?<![\w{])((?:OPENTHREAD|OT)_\w+)=", defs)) - set(BUILD_FIXED) - set(self.mapped)

    def value_of(self, text: str) -> int | None:
        return c_eval(text, lambda n: self.lookup(n)[0], self.defined)

    def defined(self, name: str) -> int | None:
        if name in self.local:
            return int(self.local[name] is not None)
        if name in BUILD_FIXED or name in define_index(self.rev):
            return 1
        if name in self.mapped and self.opts.get(self.mapped[name][1]):
            return 1
        return None if name in self.by_cmake or OPAQUE.match(name) else 0

    def lookup(self, name: str) -> tuple[int | None, str, list]:
        """(value, where it came from, the #define texts it expands)."""
        if name in self.local:
            text = self.local[name]
            if text is None:
                return 0, "#undef in the test file", []
            self.stack.append(name)
            try:
                return (None if name in self.stack[:-1] else self.value_of(text)), "defined in the test file", [text]
            finally:
                self.stack.pop()
        if name in self.stack or len(self.stack) > 12:
            return None, "recursive definition", []
        if name not in self.cache:
            self.stack.append(name)
            try:
                self.cache[name] = self._lookup(name)
            finally:
                self.stack.pop()
        return self.cache[name]

    def _lookup(self, name: str) -> tuple[int | None, str, list]:
        if name in BUILD_FIXED:
            return BUILD_FIXED[name], "fixed by the unit-test build (FTD, no POSIX platform)", []
        kind, opt, extra = self.mapped.get(name, (None, None, None))
        val = self.opts.get(opt, "") if opt else ""
        if kind and val:
            src = f"cmake option {opt}={val}"
            if kind == "flag":
                return int(val.upper() in TRUE_VALUES), src, []
            if kind == "option":
                return (1 if val.upper() in TRUE_VALUES else 0 if val.upper() in ("OFF", "0", "FALSE", "N", "NO")
                        else None), src, []
            if kind == "int":
                return (int(val) if val.isdigit() else None), src, []
            return self.value_of(extra + val), src, [extra + val]
        note = f"; cmake option {opt} not set" if opt else ""
        if kind == "option" and extra in ("ON", "OFF"):
            return int(extra == "ON"), f"cmake option {opt} default {extra}", []
        if kind == "flag" and extra:
            return 0, f"cmake option {opt} not set", []
        if name in self.by_cmake:
            return None, "set by CMake logic that is not modelled" + note, []
        defs = define_index(self.rev).get(name, [])
        sim = [d for d in defs if d[0] == SIM_CONFIG]
        defs = sim or [d for d in defs if not d[0].startswith("src/posix/")]
        if not defs:
            if OPAQUE.match(name) or name in define_index(self.rev):
                return None, "defined outside the modelled sources" + note, []
            return 0, "not defined anywhere (0)" + note, []
        values = {self.value_of(v) if v else None for _, v in defs}
        where = "simulation config header" if sim else f"default/derived in {defs[0][0]}"
        return (values.pop() if len(values) == 1 else None), where + note, [v for _, v in defs]

    def trace(self, expr: str) -> dict:
        """{macro: {"value", "source"}} for the identifiers of `expr` and,
        for derived macros, of the expressions they expand to (two levels)."""
        out, todo = {}, [(n, 0) for n in c_idents(expr)]
        while todo and len(out) < 12:
            n, d = todo.pop(0)
            if n not in out:
                v, src, texts = self.lookup(n)
                out[n] = {"value": v, "source": src}
                if d < 2 and not src.startswith("cmake option"):
                    todo += [(i, d + 1) for t in texts[:1] for i in c_idents(t)]
        return out


@functools.lru_cache(maxsize=None)
def resolver(rev: str, options: tuple) -> Resolver:
    return Resolver(rev, options)


def resolve_macro(rev: str, name: str, options) -> int | None:
    """Value of macro `name` in the unit-test build at `rev` under cmake
    `options` (the profile's plus any requested), or None when unknown."""
    return resolver(rev, tuple(options)).lookup(name)[0]


def k_and(a, b):
    return 0 if a == 0 or b == 0 else None if a is None or b is None else 1


def k_not(a):
    return None if a is None else int(not a)


def k_or(a, b):
    return 1 if a == 1 or b == 1 else None if a is None or b is None else 0


def scan_file(rev: str, path: str, options, config_rev: str | None = None) -> dict:
    """Walk the preprocessor conditionals of a test source, keeping its own
    #defines. Result: "guards" [{expr, value, macros}] (each distinct #if
    condition), "body_enabled" True/False/None and "body_chain" (the
    conditions around the test body). The body is the call statements of
    main() other than printf/return, or the TEST* cases of a file without
    main(); it is enabled when any of them is compiled in. The source is read
    at `rev` (the test patch); macro values come from `config_rev` (the base,
    which the test must fail against), defaulting to `rev`."""
    res = resolver(config_rev or rev, tuple(options))
    res.local, res.cache = {}, {}
    stack, guards, stmts, in_main = [], [], [], False   # frame: [taken, cond, branch, guard, kind]
    for line in c_lines(show(rev, path) or ""):
        d = directive(line)
        state = 1
        for f in stack:
            state = k_and(state, f[2])
        if d is None:
            if re.search(r"\bint\s+main\s*\(", line):
                in_main = True
            elif (in_main and re.search(r"\w\s*\(", line) and not re.search(
                    r"\b(printf|fprintf|puts|return|OT_UNUSED_VARIABLE)\b|\(void\)", line)) or (
                    not in_main and re.match(r"\s*TEST(_F|_P)?\s*\(", line)):
                stmts.append((state, [(f[3], f[4]) for f in stack]))
            continue
        word, arg = d
        if word in ("if", "ifdef", "ifndef", "elif"):
            expr = {"ifdef": f"defined({arg})", "ifndef": f"!defined({arg})"}.get(word, arg)
            v = c_eval(expr, lambda n: res.lookup(n)[0], res.defined)
            g = next((x for x in guards if x["expr"] == expr), None)
            if g is None:
                g = {"expr": expr, "value": v, "macros": res.trace(expr)}
                guards.append(g)
            if word == "elif" and stack:
                f = stack[-1]
                taken = k_or(f[0], f[1])
                stack[-1] = [taken, v, k_and(k_not(taken), v), g, "if"]
            else:
                stack.append([0, v, v, g, "if"])
        elif word == "else" and stack:
            f = stack[-1]
            taken = k_or(f[0], f[1])
            stack[-1] = [taken, 0, k_not(taken), f[3], "else"]
        elif word == "endif" and stack:
            stack.pop()
        elif word == "define" and state != 0:
            m = re.match(r"(\w+)(\(?)\s*(.*)$", arg)
            if m and not m.group(2):
                res.local[m.group(1)] = m.group(3) if state == 1 else "(?)"
        elif word == "undef" and state == 1:
            res.local[arg] = None
    res.local, res.cache = {}, {}
    states = [s for s, _ in stmts]
    enabled = None if not states else True if 1 in states else False if set(states) == {0} else None
    pick = next((c for s, c in stmts if s == 1), stmts[0][1] if stmts else [])
    return {"guards": guards, "body_enabled": enabled,
            "body_chain": [{"expr": g["expr"], "value": g["value"], "branch": k} for g, k in pick]}


def guard_resolution(rev: str, paths: list[str], profile, ci: list[str], base: str) -> dict:
    """scan_file() of each changed test source under the profile. For a
    disabled body, "ci_flips" lists the CI options (ci_cmake_options) that
    would enable it on their own, and "ci_flips_combined" whether all of them
    together would."""
    out = {}
    if isinstance(profile, str):
        return out
    for p in paths:
        if p.endswith((".cpp", ".c")):
            out[p] = r = scan_file(rev, p, profile, base)
            if r["body_enabled"] is False:
                r["ci_flips"] = [o for o in ci if scan_file(rev, p, profile + [o], base)["body_enabled"]]
                r["ci_flips_combined"] = bool(scan_file(rev, p, profile + ci, base)["body_enabled"])
    return out


def body_enabled(rec: dict, path: str, extra_options=()) -> bool | None:
    """Whether the test body of `path` is compiled in under the record's
    profile plus `extra_options` (a verdict's cmake_options); None if unknown."""
    profile = rec.get("profile_cmake_options")
    if isinstance(profile, str) or profile is None:
        return None
    return scan_file(rec["fix_commit"], path, list(profile) + list(extra_options),
                     rec["base_commit"])["body_enabled"]




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
    profile = profile_options(base)
    ci = ci_options(base)
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
            "test_guard_resolution": guard_resolution(fix, tests, profile, ci, base),
            "ci_cmake_options": ci,
            "profile_cmake_options": profile,
            "profile_enabled_options": enabled_options(base, profile),
            "enrich_version": ENRICH_VERSION}


def reenrich_local(rec: dict) -> dict:
    """The clone-derived fields of an enriched record recomputed, keeping
    what came from GitHub or the base commit (no network)."""
    fix, tests = rec["fix_commit"], rec["test_files"]
    return rec | {"test_inventory": {"unit": unit_inventory(fix), "gtest": gtest_inventory(fix)},
                  "test_config_guards": config_guards(fix, tests),
                  "test_guard_resolution": guard_resolution(
                      fix, tests, rec["profile_cmake_options"], rec["ci_cmake_options"],
                      rec["base_commit"]),
                  "enrich_version": ENRICH_VERSION}


def cmd_enrich(args) -> None:
    if args.local:
        for path in sorted(enriched_dir().glob("*.json")):
            if not args.pr or int(path.stem) in args.pr:
                path.write_text(json.dumps(reenrich_local(json.loads(path.read_text())), indent=2) + "\n")
                print(f"  {path.stem}  re-enriched locally")
        return
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
        for t, opts in gated_targets(rec):
            print(f"    not built in our profile (needs {', '.join(opts)}): {t}")


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
    r = find_pr(13536)
    scan = scan_file(r["fix_commit"], "tests/unit/test_dhcp6_pd_client.cpp", ["-DOT_BORDER_ROUTING=ON"])
    check("13536 DHCP6 PD client body is disabled by a derived macro", scan["body_enabled"] is False)
    check("...and enabled by the option that sets its default macro", scan_file(
        r["fix_commit"], "tests/unit/test_dhcp6_pd_client.cpp",
        ["-DOT_BORDER_ROUTING=ON", "-DOT_BORDER_ROUTING_DHCP6_PD=ON", "-DOT_BORDER_ROUTING_DHCP6_PD_CLIENT=ON"]
    )["body_enabled"] is True)
    check("#if expressions evaluate three-valued", c_eval("0 && X", lambda n: None, lambda n: None) == 0
          and c_eval("X || 1", lambda n: None, lambda n: None) == 1 and c_eval("X", lambda n: None, lambda n: None) is None)
    gate = 'NOT CMAKE_SYSTEM_NAME STREQUAL "Linux" AND (OT_BACKBONE_ROUTER OR OT_BACKBONE_ROUTER STREQUAL "")'
    check("a non-Linux-only CMake gate is false on Linux", cmake_if(gate, {}) == (False, []))
    check("a CMake condition with an unknown variable is not true", cmake_if("FOO AND OT_FTD", {})[0] is not True)
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
    e.add_argument("--local", action="store_true", help="recompute clone-derived fields of existing records")
    dispatch(p.parse_args())


if __name__ == "__main__":
    main()
