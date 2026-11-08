#!/usr/bin/env python3
"""
Stage-2 PR triage with an open-weights model, and scoring against a gold set.

    python scripts/triage_prs.py run --profile qwen38 --gold
    python scripts/triage_prs.py run --profile qwen38 --pr 74435 99824
    python scripts/triage_prs.py eval                      # latest run
    python scripts/triage_prs.py eval triage/runs/<id>

The model is served separately (scripts/serve_triage_model.sh) and reached
over vLLM's OpenAI-compatible API, so nothing here is specific to one model:
--profile names an entry in triage/models.json, which carries the sampling
settings its model card recommends.

For each PR the model gets docs/pr_triage.md as its instructions and the whole
enriched record (scripts/filter_candidates.py enrich) as the first message,
including the pre-change source the PR modifies and our boards' files at its
base commit. Every record so far is under 85K tokens, far inside the context
window, so it is given whole rather than fed through tools.

Tools stay, for whatever else the model decides it needs -- any file in the
Zephyr tree at the base or merge commit. They are budgeted: each round costs a
full pass of the model's thinking, which is where the time goes on this
hardware. After MAX_TOOL_ROUNDS the model is told to answer and the request is
sent without tools, so a PR always ends with a verdict rather than an error.

The verdict is checked by code before it is accepted: the fields pr_triage.md
requires, a board/scenario pair the twister dry run selected
(scripts/dry_run.py), and fail_to_pass names that actually appear in the
suite's sources. A failed check goes back to the model with the reason,
up to MAX_REPAIRS times; whatever is left is recorded, not hidden.

Each run writes to its own directory and never overwrites another:

    triage/runs/<YYYY-MM-DD_HHMM>_<profile>/
        manifest.json       model, sampling, git commit, prompt hash, PR list
        pr_triage.md        the exact instructions used
        <pr>.json           the verdict, in pr_triage.md's format
        <pr>.trace.json     every message, the model's reasoning, tool calls
                            and their results, token counts, timings
        scorecard.md/.json  written by `eval`
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
from generate_instance import ensure_clone  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICHED = layout.enriched()
INSTRUCTIONS = REPO_ROOT / "docs" / "pr_triage.md"


def instruction_text():
    return INSTRUCTIONS.read_text()

TRIAGE = REPO_ROOT / "triage"
RUNS = layout.triage_runs()

# Board names pr_triage.md allows. native_sim_64 is the pre-2024 spelling of
# native_sim/native/64; the hand-built instances use both.
DRYRUN = layout.dryrun()
# Spellings of one board across Zephyr's history, for comparing with gold.
SAME_BOARD = {"native_sim_64": "native_sim/native/64", "native_posix_64": "native_sim/native/64"}


def load_dry_run(pr: int) -> dict | None:
    p = DRYRUN / f"{pr}.json"
    return json.loads(p.read_text()) if p.exists() else None

MAX_TOOL_ROUNDS = 8     # model replies that call tools; then it must answer.
                        # 43405 took 24 rounds and 41 minutes reading log_core.c
                        # in chunks before that file was put in the record.
MAX_REPAIRS = 2         # times a failed verdict is sent back with the reasons
MAX_TURNS = MAX_TOOL_ROUNDS + MAX_REPAIRS + 2   # hard stop on requests per PR
REQUEST_TIMEOUT = 3600  # seconds. LiteLLM's default of 600 silently retried a
                        # 10-minute Qwen3.8 request three times over.
TOOL_OUTPUT_CAP = 60_000
# The rendered record, in characters (~3.6 per token). The model's context is
# 262,144 tokens; 32,768 are kept for its answer and ~4K go to the
# instructions, which leaves ~110K tokens of record and ~115K for tool rounds.
# Most records are far below; a few PRs that touch a big suite or carry long
# review hunks ran to 200-350K tokens and failed outright. Records under the
# budget are rendered unchanged (see fit_record).
RECORD_BUDGET = 400_000
HUNK_TAIL = 15          # lines of a review comment's diff hunk kept when trimming
OMITTED_LISTED = 30     # left-out files of one suite listed by name; more are counted


# ---------------------------------------------------------------- the record


def render_record(rec: dict, budget: int | None = RECORD_BUDGET) -> str:
    """The enriched record as readable markdown: the same substance as the
    GitHub page, with diffs and test sources in fenced blocks rather than
    escaped inside JSON strings. Over `budget` characters, parts are left out
    (fit_record); budget=None renders everything."""
    if budget is not None:
        return fit_record(rec, budget)
    trimmed = rec.get("_trimmed") or []
    out = [f"# PR #{rec['number']}: {rec['title']}", "",
           f"- URL: {rec['url']}",
           f"- Merged: {rec.get('merged_at')}",
           f"- Labels: {', '.join(rec.get('labels') or []) or '(none)'}",
           f"- Base commit (repo before this PR): {rec.get('base_commit')}",
           f"- Merge commit (repo after this PR): {rec.get('merge_commit_sha')}",
           f"- Size: +{rec.get('additions')} -{rec.get('deletions')} "
           f"in {len(rec['files'])} files", ""]
    if trimmed:
        out += ["**This record is too large to show in full. Left out to fit:** "
                + "; ".join(trimmed) + ". Everything left out is still listed by name; "
                "read what you need with `read_file`.", ""]
    out += ["## Description", "", rec.get("body") or "(empty)", ""]

    for issue in rec.get("linked_issues") or []:
        out += [f"## Linked issue #{issue['number']}: {issue['title']}",
                f"State: {issue['state']}. Labels: {', '.join(issue['labels']) or '(none)'}",
                "", issue["body"] or "(empty)", ""]
        for c in issue["comments"]:
            out += [f"**{c['author']}** commented:", "", c["body"], ""]

    out += ["## Commits", ""]
    for c in rec.get("commits") or []:
        out += [f"### {c['sha'][:12]}", "", c["message"], ""]

    out += [f"## Changed files ({len(rec['files'])})", ""]
    for f in rec["files"]:
        out.append(f"### {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']})")
        if f.get("_omitted"):
            out += [f"(diff not included to fit the prompt; {len(f['patch'])} characters)", ""]
        elif f.get("patch"):
            out += ["```diff", f["patch"], "```", ""]
        else:
            out += ["(no diff shown by GitHub: binary, or too large to render)", ""]

    if rec.get("comments") or rec.get("reviews") or rec.get("review_comments"):
        out += ["## Discussion", ""]
        for c in rec.get("comments") or []:
            out += [f"**{c['author']}** commented:", "", c["body"], ""]
        for r in rec.get("reviews") or []:
            out += [f"**{r['author']}** reviewed ({r['state']}):", "", r["body"], ""]
        for c in rec.get("review_comments") or []:
            hunk = c.get("diff_hunk") or ""
            if rec.get("_short_hunks") and hunk.count("\n") >= HUNK_TAIL:
                hunk = "...\n" + "\n".join(hunk.split("\n")[-HUNK_TAIL:])
            out += [f"**{c['author']}** on {c['path']}:{c['line']}:",
                    "```diff", hunk, "```", "", c["body"], ""]

    suites = rec.get("test_suites") or {}
    out += ["## Test suites touched by this PR, in full, at the merge commit", ""]
    if not suites:
        out += ["(this PR touches no test suite)", ""]
    for root, suite in suites.items():
        out += [f"### Suite {root} (config: {suite['config']})", ""]
        left_out = [n for n, f in suite["files"].items() if f.get("_omitted")]
        if len(left_out) > OMITTED_LISTED:
            # too many to list one by one: the listing alone would not fit
            out += [f"({len(left_out)} files of this suite, "
                    f"{sum(suite['files'][n]['size'] or 0 for n in left_out):,} bytes, are not "
                    f"included to fit the prompt; list_dir and read_file at the merge commit)", ""]
        for name, f in sorted(suite["files"].items()):
            if f.get("_omitted") and len(left_out) > OMITTED_LISTED:
                continue
            if f.get("_omitted"):
                out += [f"#### {root}/{name}", f"(not included to fit the prompt; {f['size']} "
                        "bytes; read_file at the merge commit)", ""]
            elif f["content"] is None:
                out += [f"#### {root}/{name}", f"(binary, {f['size']} bytes)", ""]
            else:
                out += [f"#### {root}/{name}", "```", f["content"], "```", ""]

    out += ["## Twister dry run at the base commit", ""]
    dr = load_dry_run(rec["number"])
    if not dr:
        out += ["(no dry run for this PR; run scripts/dry_run.py first)", ""]
    else:
        out += ["Twister's own filters, run on the base tree with this PR's tests "
                "applied, for every scenario of every suite the PR changes, on each "
                "board we can run. `selected`: twister would build and run that pair. "
                "`filtered`: twister's reason. Kconfig/devicetree `filter:` "
                "expressions are not evaluated here, so a selected pair can still "
                "be filtered when it is built.", "",
                "| suite | scenario | board | status | reason |", "|---|---|---|---|---|"]
        if rec.get("_compact_dry_run"):
            out += compact_pairs(dr["pairs"])
        else:
            out += [f"| {p['suite']} | {p['scenario']} | {p['board']} | {p['status']} | "
                    f"{p['reason']} |" for p in dr["pairs"]]
        out += ["", "### Test cases per scenario (twister's names)", ""]
        for scen, names in dr["tests"].items():
            out += [f"- {scen}: {', '.join(names) or '(none found)'}"]
        out += [""]

    sources = rec.get("base_sources") or {}
    out += ["## Source files before this PR, in full, at the base commit", ""]
    if not sources:
        out += ["(this PR modifies no existing non-test file)", ""]
    for path, f in sources.items():
        if f.get("_omitted"):
            out += [f"### {path}", f"(not included to fit the prompt; {f.get('size')} bytes; "
                    "read_file at the base commit)", ""]
        elif f.get("content") is None:
            out += [f"### {path}", f"(not included: {f.get('omitted')}; {f.get('size')} bytes)", ""]
        else:
            out += [f"### {path}", "```", f["content"], "```", ""]
    return "\n".join(out)


def compact_pairs(pairs: list[dict]) -> list[str]:
    """The dry-run table with every selected pair, and the filtered ones
    folded into one row per suite, scenario and reason."""
    rows = [f"| {p['suite']} | {p['scenario']} | {p['board']} | selected | |"
            for p in pairs if p["status"] == "selected"]
    groups: dict[tuple, list[str]] = {}
    for p in pairs:
        if p["status"] != "selected":
            groups.setdefault((p["suite"], p["scenario"], p["reason"]), []).append(p["board"])
    rows += [f"| {suite} | {scen} | {', '.join(boards)} | filtered | {why} |"
             for (suite, scen, why), boards in groups.items()]
    return rows


def fit_record(rec: dict, budget: int) -> str:
    """render_record within `budget` characters. A record that fits is
    rendered exactly as before. One that does not loses, in this order and
    only until it fits: the start of long review-comment hunks (the commented
    line is the hunk's last), unchanged test-suite files (largest first; the
    PR's own test files and the suite yaml stay), the filtered rows of the dry
    run (folded per reason), pre-change source files (largest first), and
    finally the largest diffs. All of it stays listed by name."""
    text = render_record(rec, budget=None)
    if len(text) <= budget:
        return text
    import copy
    rec = copy.deepcopy(rec)
    trimmed = rec["_trimmed"] = []
    changed = {f["filename"] for f in rec["files"]}

    def size() -> int:
        return len(render_record(rec, budget=None))

    if any((c.get("diff_hunk") or "").count("\n") >= HUNK_TAIL for c in rec.get("review_comments") or []):
        rec["_short_hunks"] = True
        trimmed.append(f"review comments show the last {HUNK_TAIL} lines of their diff hunk")
    over = size() - budget
    suite_files = sorted(((f["size"] or 0, root, name, f)
                          for root, s in (rec.get("test_suites") or {}).items()
                          for name, f in s["files"].items()
                          if f["content"] is not None and f"{root}/{name}" not in changed
                          and name != s["config"]), key=lambda x: -x[0])
    dropped = 0
    for _, _, _, f in suite_files:
        if over <= 0:
            break
        f["_omitted"] = True
        over -= len(f["content"])
        dropped += 1
    if dropped:
        trimmed.append(f"{dropped} test-suite files the PR does not change")
    if size() > budget:
        rec["_compact_dry_run"] = True
        trimmed.append("the dry run's filtered pairs, folded per reason")
    over = size() - budget
    dropped = 0
    for _, f in sorted((rec.get("base_sources") or {}).items(),
                       key=lambda kv: -len(kv[1].get("content") or "")):
        if over <= 0:
            break
        if f.get("content"):
            f["_omitted"] = True
            over -= len(f["content"])
            dropped += 1
    if dropped:
        trimmed.append(f"{dropped} pre-change source files")
    over = size() - budget
    dropped = 0
    for f in sorted(rec["files"], key=lambda f: -len(f.get("patch") or "")):
        if over <= 0:
            break
        if f.get("patch"):
            f["_omitted"] = True
            over -= len(f["patch"])
            dropped += 1
    if dropped:
        trimmed.append(f"the {dropped} largest diffs")
    return render_record(rec, budget=None)


def test_sources(rec: dict) -> str:
    """Everything a fail_to_pass name could legitimately come from."""
    parts = [f["content"] or "" for s in (rec.get("test_suites") or {}).values()
             for f in s["files"].values()]
    parts += [f.get("patch") or "" for f in rec["files"] if f["filename"].startswith("tests/")]
    return "\n".join(parts)


# ---------------------------------------------------------------- tools


GIT_LOCK = threading.Lock()   # lazy blob fetches into one clone, one at a time

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file from the selected upstream repository, before (base) or after "
                       "(merge) this PR. Use for files the record does not include: "
                       "board definitions, Kconfig, headers, other source.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Repository-relative path, e.g. boards/qemu/x86/qemu_x86.yaml"},
            "ref": {"type": "string", "enum": ["base", "merge"], "description": "Default: merge"},
            "start_line": {"type": "integer", "description": "1-based; default 1"},
            "max_lines": {"type": "integer", "description": "Default 800"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List the entries of a directory in the selected upstream repository.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Repository-relative directory, e.g. boards/qemu/x86"},
            "ref": {"type": "string", "enum": ["base", "merge"], "description": "Default: merge"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents with an extended regular expression, under "
                       "a directory or across the whole repository. Returns up to 300 "
                       "matching lines.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Repository-relative directory; empty for the whole tree"},
            "ref": {"type": "string", "enum": ["base", "merge"], "description": "Default: merge"}},
            "required": ["pattern", "path"]}}},
]


def git(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    with GIT_LOCK:
        return subprocess.run(["git", "-C", str(PROJECTS[PROJECT]["clone"]()), *args],
                              capture_output=True, text=True, timeout=timeout)


def run_tool(name: str, args: dict, rec: dict) -> str:
    refs = {"base": rec.get("base_commit"),
            "merge": rec.get("merge_commit_sha") or rec.get("fix_commit")}
    sha = refs.get(args.get("ref") or "merge")
    if not sha:
        return f"error: this record has no {args.get('ref') or 'merge'} commit"
    path = (args.get("path") or "").strip().strip("/")

    if name == "read_file":
        r = git("show", f"{sha}:{path}")
        if r.returncode:
            return f"error: {r.stderr.strip()[:300]}"
        lines = r.stdout.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        count = max(1, int(args.get("max_lines") or 800))
        chunk = lines[start - 1:start - 1 + count]
        body = "\n".join(f"{i:>5}  {l}" for i, l in enumerate(chunk, start))
        more = start - 1 + count < len(lines)
        return body + (f"\n... ({len(lines)} lines in total; pass start_line to read on)" if more else "")

    if name == "list_dir":
        r = git("ls-tree", f"{sha}:{path}" if path else sha)
        if r.returncode:
            return f"error: {r.stderr.strip()[:300]}"
        return "\n".join(f"{l.split()[1]:<5} {l.split(chr(9))[1]}" for l in r.stdout.splitlines()) or "(empty)"

    if name == "grep":
        r = git("grep", "-n", "-E", args.get("pattern") or "", sha, "--", path or ".", timeout=300)
        if r.returncode not in (0, 1):
            return f"error: {r.stderr.strip()[:300]}"
        hits = [l.split(":", 1)[1] for l in r.stdout.splitlines()]   # drop "<sha>:"
        return "\n".join(hits[:300]) + (f"\n... {len(hits) - 300} more" if len(hits) > 300 else "") or "(no matches)"

    return f"error: unknown tool {name}"


# ---------------------------------------------------------------- the verdict


def parse_verdict(content: str | None) -> dict | None:
    if not content:
        return None
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return None


def check_pair(v: dict, rec: dict) -> list[str]:
    """platform, test_path and scenario must name one pair the dry run selected
    (or, without a dry run, a board and scenario that exist at the base)."""
    plat, tp, scen = v.get("platform"), v.get("test_path"), v.get("scenario")
    if not all(isinstance(x, str) and x for x in (plat, tp, scen)):
        return ["`platform`, `test_path` and `scenario` are all required"]
    dr = load_dry_run(rec["number"])
    if dr:
        selected = [p for p in dr["pairs"] if p["status"] == "selected"]
        if any(p["suite"] == tp and p["scenario"] == scen and p["board"] == plat
               for p in selected):
            return []
        options = "; ".join(f"{p['board']} + {p['suite']} / {p['scenario']}" for p in selected)
        return [f"{plat} + {tp} / {scen} is not a pair the dry run selected. "
                f"Selected pairs: {options or 'none'}"]
    facts = rec.get("twister") or {}
    issues = []
    if plat not in [b for b in (facts.get("boards") or {}).values() if b]:
        issues.append(f"`platform` must be one of {facts.get('boards')}")
    if scen not in ((facts.get("suites") or {}).get(tp) or {}).get("scenarios", {}):
        issues.append(f"`scenario` {scen!r} is not a scenario of suite {tp!r}")
    return issues


def check_verdict(v: dict | None, rec: dict) -> list[str]:
    """What pr_triage.md requires, checked mechanically. Judgement is not
    second-guessed here -- only form, and names that must exist."""
    if v is None:
        return ["the reply is not a JSON object"]
    issues = []
    if v.get("pr") != rec["number"]:
        issues.append(f"`pr` must be {rec['number']}")
    if v.get("verdict") not in ("accept", "reject"):
        issues.append("`verdict` must be \"accept\" or \"reject\"")
    if not str(v.get("reason") or "").strip():
        issues.append("`reason` is required")
    if v.get("verdict") != "accept":
        return issues

    if v.get("confidence") not in ("high", "medium", "low"):
        issues.append("`confidence` must be high, medium or low")
    oracle = v.get("oracle", "runtime")
    if oracle not in ("runtime", "compile"):
        issues.append("`oracle` must be \"runtime\" or \"compile\" (or left out for runtime)")
    elif oracle == "compile":
        known = {e["symbol"] for e in rec.get("fix_symbols") or []}
        syms = v.get("compile_symbols")
        if not isinstance(syms, list) or not syms:
            issues.append("a compile oracle needs a non-empty `compile_symbols` list")
        elif [x for x in syms if x not in known]:
            issues.append(f"`compile_symbols` {[x for x in syms if x not in known]} are not in the record's "
                          f"symbols the test patch needs from the fix: {sorted(known)}")
        if v.get("pass_to_pass"):
            issues.append("a compile oracle has `pass_to_pass` [] (nothing runs before the fix)")
    if v.get("change_type") not in ("fix", "feature"):
        issues.append("`change_type` must be fix or feature")
    issues += check_pair(v, rec)
    if not str(v.get("problem_statement") or "").strip():
        issues.append("`problem_statement` is required")
    sources = test_sources(rec)
    for field, required in (("fail_to_pass", True), ("pass_to_pass", False)):
        names = v.get(field, [])
        if not isinstance(names, list) or (required and not names):
            issues.append(f"`{field}` must be a {'non-empty ' if required else ''}list of test names")
            continue
        missing = [n for n in names if not isinstance(n, str)
                   or not re.search(rf"\b{re.escape(n)}\b", sources)]
        if missing:
            issues.append(f"`{field}` names not found in the suite's sources: {missing}. "
                          "Use identifiers exactly as written in ZTEST(...).")
    return issues


# ---------------------------------------------------------------- OpenThread


def render_guard_resolution(res: dict, ci: list[str]) -> list[str]:
    """Lines for the statically resolved #if guards of the changed test files
    (test_guard_resolution of the enriched record)."""
    fmt = lambda ms, all_: ", ".join(f"{m} = {'unknown' if v['value'] is None else v['value']} ({v['source']})"
                                     for m, v in list(ms.items())[:8] if all_ or v["value"] != 1)
    state = {True: "YES", False: "NO", None: "unknown"}
    out = []
    for path, r in res.items():
        out.append(f"- {path}: test body enabled under the profile: {state[r['body_enabled']]}")
        by_expr = {g["expr"]: g for g in r["guards"]}
        for c in r["body_chain"]:
            g = by_expr[c["expr"]]
            out.append(f"  - around the test body: `{c['expr']}` ({c['branch']} branch) = "
                       f"{'unknown' if c['value'] is None else c['value']}: {fmt(g['macros'], True)}")
        if r["body_enabled"] is False and "ci_flips" in r:
            if r["ci_flips"]:
                out.append(f"  - a CI option could enable it: {', '.join(r['ci_flips'])}")
            elif r["ci_flips_combined"]:
                out.append("  - no single CI option enables it, but the CI options together (ci_cmake_options) do")
            else:
                out.append("  - no option in ci_cmake_options enables it")
        shown = {c["expr"] for c in r["body_chain"]}
        rest = sorted((g for g in r["guards"] if g["expr"] not in shown), key=lambda g: g["value"] == 1)[:8]
        out += [f"  - other guard `{g['expr']}` = {'unknown' if g['value'] is None else g['value']}: "
                f"{fmt(g['macros'], True)}" for g in rest]
    return out


def render_profiles(rec: dict) -> list[str]:
    """Which execution profile the record's selection uses and why, and for each
    profile of the base commit (upstream's own CI test jobs) whether it builds
    the selected executables and compiles in the body of each changed test
    source."""
    name = rec.get("profile", "simulation")
    out = ["", "## Execution profile", "",
           f"- Selected profile: {name} ({rec.get('profile_why', 'the default')}). The options and the guard "
           "resolution below are this profile's. A verdict may set `profile` to any profile below that builds "
           "the target and does not compile out its test body.", "",
           "| profile | targets built | test body enabled |", "|---|---|---|"]
    state = {True: "YES", False: "NO", None: "unknown"}
    for pname, p in (rec.get("profiles") or {}).items():
        if isinstance(p["cmake_options"], str):
            out.append(f"| {pname} | unavailable: {p['cmake_options'].removeprefix('ERROR: ')} | |")
        else:
            out.append(f"| {pname} | {', '.join(p['targets']) or 'none'} | "
                       + ("; ".join(f"{s.rsplit('/', 1)[-1]} {state[b]}" for s, b in p["bodies"].items()) or "-") + " |")
    return out


def render_fix_symbols(rec: dict) -> list[str]:
    """The record's fix_symbols: what the changed tests use that the fix's
    production diff adds (mechanical; judging whether it is the PR's purpose is
    the verdict's job)."""
    syms = rec.get("fix_symbols") or []
    out = ["## Symbols the test patch needs from the fix", ""]
    if not syms:
        return out + ["none found: no identifier, header or source the new test lines use is added by the "
                      "production diff, so the tests likely build at the base.", ""]
    out += ["Identifiers on added test lines that also occur on added production lines, new headers the "
            "tests #include and new sources their CMake references. `at_base` yes: the name occurs "
            "somewhere in the base tree (often on another class), so it does not prove the test builds.", "",
            "| symbol | kind | used at | added at | at_base |", "|---|---|---|---|---|"]
    out += [f"| {e['symbol']} | {e['kind']} | {e['test']} | {e['fix']} | {'yes' if e['at_base'] else 'no'} |"
            for e in syms]
    return out + [""]


def render_record_openthread(rec: dict) -> str:
    """The OpenThread enriched record (scripts/openthread_candidates.py) as
    markdown. There are no twister or dry-run facts; its diffs are capped when
    enriched, so the result is far below RECORD_BUDGET."""
    out = [f"# PR #{rec['number']}: {rec['title']}", "",
           f"- URL: {rec['url']}", f"- Merged: {rec.get('merged_at')}",
           f"- Base commit (repo before this PR): {rec['base_commit']}",
           f"- Fix commit (repo after this PR): {rec['fix_commit']}", "",
           "## Description", "", rec.get("body") or "(not available)", "",
           "## Changed files", "",
           "Tests: " + ", ".join(rec["test_files"]),
           "Production: " + ", ".join(rec["source_files"]), "",
           "## Test inventory at the fix commit", "",
           "`requires_options` are the OT_* options in the CMake `if()` guards around the target. "
           "A unit test is one executable; its ctest name is the name in the first column.", "",
           "### Unit executables (tests/unit/CMakeLists.txt)", ""]
    out += [f"- {u['ctest_name']}: {', '.join(u['sources'])}"
            + (f" [requires {', '.join(u['requires_options'])}]" if u["requires_options"] else "")
            for u in rec["test_inventory"]["unit"]] or ["(none)"]
    out += ["", "### GoogleTest executables (tests/gtest/CMakeLists.txt)", ""]
    for g in rec["test_inventory"]["gtest"]:
        out += [f"- {g['target']}: {', '.join(g['sources'])}"
                + (f" [requires {', '.join(g['requires_options'])}]" if g["requires_options"] else ""),
                f"  cases: {', '.join(g['cases']) or '(none found)'}"]
    out += [] if rec["test_inventory"]["gtest"] else ["(none)"]
    out += ["", "## Preprocessor guards in the changed test sources", ""]
    out += [f"- {p}: {', '.join(o)}" for p, o in rec.get("test_config_guards", {}).items()] \
        or ["(none)"]
    out += render_profiles(rec)
    out += ["", "## Guards resolved under the execution profile (static; they take precedence over your own "
            "reading of headers)", "",
            "A test whose body is not enabled only prints a notice and passes without running anything, "
            "so it cannot fail before the fix.", ""]
    out += render_guard_resolution(rec.get("test_guard_resolution", {}), rec["ci_cmake_options"]) or ["(none)"]
    prof = rec.get("profile_cmake_options")
    name = rec.get("profile", "simulation")
    sim = name == "simulation"
    out += ["", "## Options already set in the execution profile (" + (
            "upstream script/cmake-build simulation at the base commit, then the profile's own args; later wins" if sim else
            f"upstream's `{name}` CI job at the base commit"
            + (", then -DOT_COMPILE_WARNING_AS_ERROR=OFF" if name == "toranj" else
               f": what `{rec['profiles'][name]['configure']}` passes, then the job's arguments and "
               "-DOT_COMPILE_WARNING_AS_ERROR=OFF; later wins")) + ")", "",
            prof if isinstance(prof, str) else " ".join(prof or []) or "(not available)", ""]
    out += ["## Additional options a verdict may request via cmake_options (the base commit's own CI, "
            ".github/workflows/unit.yml)", "",
            f"(none: the {name} profile takes no `cmake_options`)" if not sim else
            " ".join(rec["ci_cmake_options"]) or "(none)", "",
            *render_fix_symbols(rec),
            "## Test changes (git diff base fix -- tests/)", "", "```diff", rec["diffs"]["tests"], "```", "",
            "## Production changes (everything else)", "", "```diff", rec["diffs"]["source"], "```", ""]
    return "\n".join(out)


def check_verdict_openthread(v: dict | None, rec: dict) -> list[str]:
    """What docs/openthread_triage.md requires, checked mechanically: form,
    names that must exist in the inventory, options the base CI uses, and that
    the test patch applies to the base. Judgement is not second-guessed."""
    if v is None:
        return ["the reply is not a JSON object"]
    issues = []
    if v.get("pr") != rec["number"]:
        issues.append(f"`pr` must be {rec['number']}")
    if v.get("verdict") not in ("accept", "reject", "hold"):
        issues.append("`verdict` must be \"accept\", \"reject\" or \"hold\"")
    if not str(v.get("reason") or "").strip():
        issues.append("`reason` is required")
    if v.get("verdict") != "accept":
        return issues

    if v.get("confidence") not in ("high", "medium", "low"):
        issues.append("`confidence` must be high, medium or low")
    oracle = v.get("oracle", "runtime")
    if oracle not in ("runtime", "compile"):
        issues.append("`oracle` must be \"runtime\" or \"compile\" (or left out for runtime)")
    elif oracle == "compile":
        known = {e["symbol"] for e in rec.get("fix_symbols") or []}
        syms = v.get("compile_symbols")
        if not isinstance(syms, list) or not syms:
            issues.append("a compile oracle needs a non-empty `compile_symbols` list")
        elif [x for x in syms if x not in known]:
            issues.append(f"`compile_symbols` {[x for x in syms if x not in known]} are not in the record's "
                          f"symbols the test patch needs from the fix: {sorted(known)}")
        if v.get("pass_to_pass"):
            issues.append("a compile oracle has `pass_to_pass` [] (nothing runs before the fix)")
    if v.get("change_type") not in ("fix", "feature"):
        issues.append("`change_type` must be fix or feature")
    for field in ("problem_statement", "causal_chain"):
        if not str(v.get(field) or "").strip():
            issues.append(f"`{field}` is required")
    suite, target = v.get("suite"), v.get("target")
    if suite not in ("unit", "gtest"):
        return issues + ["`suite` must be unit or gtest"]
    inventory = rec["test_inventory"][suite]
    entry = next((e for e in inventory if e["target"] == target), None)
    if entry is None:
        return issues + [f"`target` {target!r} is not a {suite} target in the inventory: "
                         f"{[e['target'] for e in inventory]}"]

    if suite == "gtest":
        f2p_allowed = p2p_allowed = set(entry["cases"])
    else:
        f2p_allowed = {entry["ctest_name"]}     # the whole executable is the unit
        p2p_allowed = {e["ctest_name"] for e in inventory} - f2p_allowed
    for field, required, allowed in (("fail_to_pass", True, f2p_allowed),
                                     ("pass_to_pass", False, p2p_allowed)):
        names = v.get(field, [])
        if not isinstance(names, list) or (required and not names):
            issues.append(f"`{field}` must be a {'non-empty ' if required else ''}list of test names")
        elif [n for n in names if n not in allowed]:
            issues.append(f"`{field}` names not in the inventory of {target}: "
                          f"{[n for n in names if n not in allowed]}. Allowed: {sorted(allowed)}")
    if (isinstance(v.get("fail_to_pass"), list) and isinstance(v.get("pass_to_pass"), list)
            and set(v["fail_to_pass"]) & set(v["pass_to_pass"])):
        issues.append("a test cannot be in both `fail_to_pass` and `pass_to_pass`")

    profile = v.get("profile") or rec.get("profile", "simulation")
    import openthread_candidates
    profiles = rec["profiles"]
    if profile not in profiles:
        return issues + [f"`profile` must be one of {list(profiles)} (or left out)"]
    if isinstance(profiles[profile]["cmake_options"], str):
        return issues + [f"`profile` {profile} is not available at the base commit "
                         f"({profiles[profile]['cmake_options']}); use the default or hold"]
    if profile != "simulation" and v.get("cmake_options"):
        return issues + [f"the {profile} profile is upstream's own CI job and takes no `cmake_options` "
                         "(only simulation does)"]
    rec = openthread_candidates.profile_view(rec, profile)
    if profile != "simulation" and (why := openthread_candidates.unbuilt_reasons(entry, rec)):
        return issues + [f"{target} is not built under {profile}: {'; '.join(why)}"]
    options = v.get("cmake_options", [])
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        issues.append("`cmake_options` must be a list of strings (usually [])")
    elif [o for o in options if o not in rec["ci_cmake_options"]]:
        issues.append(f"`cmake_options` {[o for o in options if o not in rec['ci_cmake_options']]} "
                      f"are not options of the base commit's CI ({rec['ci_cmake_options']}); "
                      "use [] or hold")
    if issues:
        return issues

    states = {s: openthread_candidates.body_enabled(rec, s, options)
              for s in entry["sources"] if s in rec["test_files"]}
    if False in states.values() and True not in states.values():
        off = [s for s, v in states.items() if v is False]
        return [f"the test body of {off} is compiled out under the profile plus `cmake_options` "
                f"({options}), so {target} passes without running it and cannot fail before the fix; "
                "reject, or hold, unless a CI option enables it"]

    patch = git("diff", rec["base_commit"], rec["fix_commit"], "--", "tests/").stdout
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        clone = str(PROJECTS[PROJECT]["clone"]())
        subprocess.run(["git", "-C", clone, "read-tree", rec["base_commit"]], env=env, check=True)
        r = subprocess.run(["git", "-C", clone, "apply", "--check", "--cached", "-"],
                           input=patch, env=env, capture_output=True, text=True)
    return [f"the test patch does not apply to the base commit: {r.stderr.strip()[:200]}"] if r.returncode else []


def use_project(name: str) -> None:
    """Select the repository being triaged (module-level, as --run-dir is)."""
    global PROJECT, INSTRUCTIONS, ENRICHED
    PROJECT = name
    INSTRUCTIONS = REPO_ROOT / PROJECTS[name]["instructions"]
    ENRICHED = PROJECTS[name]["enriched"]()


def openthread_enriched() -> Path:
    import openthread_candidates
    return openthread_candidates.enriched_dir()


def contiki_ng_enriched():
    import contiki_ng_candidates
    return contiki_ng_candidates.enriched_dir()

def render_record_contiki_ng(rec):
    from contiki_ng_pipeline import render_record
    return render_record(rec)

def check_verdict_contiki_ng(v, rec):
    from contiki_ng_candidates import check_verdict
    return check_verdict(v, rec)

PROJECTS = {
    "contiki-ng": {"instructions": "docs/contiki_ng_triage.md", "enriched": contiki_ng_enriched,
                   "clone": layout.contiki_ng_clone, "render": render_record_contiki_ng, "check": check_verdict_contiki_ng},
    "zephyr": {"instructions": "docs/pr_triage.md", "enriched": layout.enriched,
               "clone": ensure_clone, "render": render_record, "check": check_verdict},
    "openthread": {"instructions": "docs/openthread_triage.md", "enriched": openthread_enriched,
                   "clone": layout.openthread_clone, "render": render_record_openthread,
                   "check": check_verdict_openthread},
}
PROJECT = "zephyr"


# ---------------------------------------------------------------- one PR


def converse(messages: list, rec: dict, check, kw: dict, trace: dict) -> tuple:
    """Talk to the served model until it returns an answer that passes check
    (a list of issues), or the tool/repair budgets run out. Tool rounds read
    upstream source at the record's commits. Fills trace; returns (answer, issues)."""
    import litellm
    verdict, issues, repairs, rounds = None, ["no reply"], 0, 0
    try:
        for turn in range(MAX_TURNS):
            t = time.time()
            # Once the budget is spent, the request carries no tools at all,
            # so the only thing left to do is answer.
            tools = {"tools": TOOLS} if rounds < MAX_TOOL_ROUNDS else {}
            r = litellm.completion(messages=messages, **tools, **kw)
            choice = r.choices[0]
            msg = choice.message
            trace["turns"].append({
                "seconds": round(time.time() - t, 1),
                "finish_reason": choice.finish_reason,
                "usage": {"prompt": r.usage.prompt_tokens, "completion": r.usage.completion_tokens},
                "reasoning": getattr(msg, "reasoning_content", None),
                "content": msg.content,
                "tool_calls": [{"name": c.function.name, "arguments": c.function.arguments}
                               for c in (msg.tool_calls or [])],
            })
            assistant = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant["tool_calls"] = [c.model_dump() for c in msg.tool_calls]
            messages.append(assistant)

            if msg.tool_calls:
                for c in msg.tool_calls:
                    try:
                        result = run_tool(c.function.name, json.loads(c.function.arguments or "{}"), rec)
                    except (json.JSONDecodeError, ValueError, subprocess.TimeoutExpired) as e:
                        result = f"error: {e}"
                    result = result[:TOOL_OUTPUT_CAP]
                    trace["turns"][-1].setdefault("tool_results", []).append(result)
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                trace["tool_calls"] += len(msg.tool_calls)
                rounds += 1
                if rounds == MAX_TOOL_ROUNDS:
                    trace["budget_hit"] = True
                    messages.append({"role": "user", "content":
                                     f"You have used all {MAX_TOOL_ROUNDS} tool rounds. Give your "
                                     "verdict now from what you have, as the JSON object only."})
                continue

            verdict = parse_verdict(msg.content)
            issues = check(verdict)
            if not issues or repairs >= MAX_REPAIRS:
                break
            repairs += 1
            trace["repairs"].append(issues)
            messages.append({"role": "user", "content":
                             "Your verdict does not meet the output requirements:\n- "
                             + "\n- ".join(issues)
                             + "\nReturn the corrected JSON object only."})
    except Exception as e:                      # one PR failing must not stop the run
        trace["error"] = f"{type(e).__name__}: {e}"

    return verdict, issues


def model_kw(profile: str, prof: dict, api_base: str) -> dict:
    """litellm arguments for the served model with its card's sampling."""
    sampling = dict(prof.get("sampling", {}))
    extra = {k: sampling.pop(k) for k in ("top_k", "min_p") if k in sampling}
    kw = dict(model=f"openai/{profile}", api_base=api_base, api_key="local",
              timeout=REQUEST_TIMEOUT, num_retries=0,
              max_tokens=prof.get("max_tokens", 32768), **sampling)
    if extra:
        kw["extra_body"] = extra
    return kw


def triage_one(pr: int, profile: str, prof: dict, api_base: str, run_dir: Path) -> dict:
    rec = json.loads((ENRICHED / f"{pr}.json").read_text())
    render, check = PROJECTS[PROJECT]["render"], PROJECTS[PROJECT]["check"]
    kw = model_kw(profile, prof, api_base)

    messages = [{"role": "system", "content": instruction_text()},
                {"role": "user", "content": "Enriched PR record:\n\n" + render(rec)
                 + f"\n\n---\nYou may use up to {MAX_TOOL_ROUNDS} tool rounds for this PR."}]
    trace = {"pr": pr, "profile": profile, "turns": [], "repairs": [], "error": None,
             "tool_calls": 0, "budget_hit": False}
    t0 = time.time()
    verdict, issues = converse(messages, rec, lambda v: check(v, rec), kw, trace)
    trace["seconds"] = round(time.time() - t0, 1)
    trace["issues"] = issues
    trace["messages"] = messages
    (run_dir / f"{pr}.trace.json").write_text(json.dumps(trace, indent=2, default=str))

    out = verdict if isinstance(verdict, dict) else {"pr": pr, "verdict": "error",
                                                     "reason": trace["error"] or "no parseable verdict"}
    (run_dir / f"{pr}.json").write_text(json.dumps(out, indent=2) + "\n")
    return {"pr": pr, "verdict": out.get("verdict"), "seconds": trace["seconds"],
            "turns": len(trace["turns"]), "tool_calls": trace["tool_calls"],
            "budget_hit": trace["budget_hit"], "issues": issues, "error": trace["error"]}


# Version of the statement rules and check below; an instance's metadata
# records the version its statement passed (cluster/status.py keeps a copy).
# 1 rejected only quoted lines of the fix; 2 adds the identifier check and
# the model review of whether the statement gives the fix away.
STATEMENT_CHECK = 2

STATEMENT_REQUEST = """This PR is already a validated benchmark instance: model-free
observation found that the upstream tests below fail before the production fix
and pass after it, in the selected units. You are not triaging it. Write only
what an agent will see in place of the PR text, following the instructions'
problem-statement rules: describe the observable misbehavior an issue report
would describe, never the fix, its diff or code it adds.

Describe only what a caller or a test can observe from outside the code: the
inputs or conditions, the wrong outcome (a crash, a read past the input, a
wrong return value or result, a timeout) and the required behavior. Do not say
how the code goes wrong inside -- which check is missing, what it compares
against, which variable or type is too small -- or what the code must
validate, track or add. Name no function, macro, type or file of the
production change unless the tests use it; finding those is the task.

Observed fail-to-pass: {f2p}
Observed pass-to-pass: {p2p}

Reply with the JSON object only:
{{"pr": {pr}, "problem_statement": "...", "causal_chain": "...", "change_type": "..."}}"""

REVIEW_REQUEST = """You review one problem statement of a coding benchmark. An agent is
given only this statement and the repository before the fix; it must find the
defect and write the fix itself. A statement may describe the symptom, the
inputs or conditions that trigger it, and the required observable behavior,
and may name an interface the tests call. It gives the fix away if it:
- says what the code checks, fails to check, compares or computes, or where in
  the code the defect is;
- says what the code must validate, track, pass along, add or change
  internally, rather than the observable result;
- names a function, macro, variable, type or file of the production change
  that the tests do not use;
- describes the fix or the change itself.

The production change (the agent does not see it):
{diff}

Names the tests use: {test_names}

The statement:
{statement}

Reply with the JSON object only:
{{"gives_away_fix": true or false, "phrases": ["each phrase of the statement that gives the fix away, verbatim"]}}"""

REVIEW_DIFF_CAP = 60_000
IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def _code_like(token: str) -> bool:
    return ("_" in token or (len(token) >= 5 and token.isalpha() and token.isupper())
            or re.search(r"[a-z][A-Z]", token) is not None)


def fix_identifiers(statement: str, rec: dict) -> list[str]:
    """Identifiers from the production change's edited lines that the
    statement names, other than ones the tests use (an interface may be
    named as the tests use it)."""
    changed = set()
    for line in (rec.get("production_diff") or "").splitlines():
        if line[:1] in "+-" and line[:3] not in ("+++", "---"):
            changed |= {t for t in IDENT.findall(line) if _code_like(t)}
    tests = (rec.get("test_patch") or "").lower()
    return sorted({t for t in IDENT.findall(statement) if t in changed and t.lower() not in tests})


def review_statement(statement: str, rec: dict, kw: dict, trace: dict) -> list[str]:
    """Ask the served model, in a fresh conversation without tools, whether
    the statement gives the fix away. Returns the offending phrases (a
    generic one when it says so without quoting); raises when the reply
    twice carries no verdict."""
    import litellm
    test_names = sorted({t for t in IDENT.findall(rec.get("test_patch") or "") if _code_like(t)})
    request = REVIEW_REQUEST.format(diff=(rec.get("production_diff") or "")[:REVIEW_DIFF_CAP],
                                    test_names=", ".join(test_names) or "(none)", statement=statement)
    for _ in range(2):
        r = litellm.completion(messages=[{"role": "user", "content": request}], **kw)
        v = parse_verdict(r.choices[0].message.content)
        trace.setdefault("reviews", []).append({"statement": statement, "reply": r.choices[0].message.content})
        if isinstance(v, dict) and isinstance(v.get("gives_away_fix"), bool):
            if not v["gives_away_fix"]:
                return []
            phrases = [x for x in v.get("phrases") or [] if isinstance(x, str) and x.strip()]
            return phrases or ["(the reviewer quoted no phrase)"]
    raise ValueError("the statement review returned no verdict")


def check_statement(v, rec: dict, review=None) -> list[str]:
    """The statement fields; no line of the production fix quoted, no
    identifier of the fix the tests do not use, and (given review) no
    phrase the model review says gives the fix away."""
    if not isinstance(v, dict):
        return ["the reply must be one JSON object"]
    issues = []
    if v.get("pr") != rec["number"]:
        issues.append(f"`pr` must be {rec['number']}")
    for field in ("problem_statement", "causal_chain", "change_type"):
        if not isinstance(v.get(field), str) or not v[field].strip():
            issues.append(f"`{field}` must be a nonempty string")
    statement = v.get("problem_statement") if isinstance(v.get("problem_statement"), str) else ""
    fix = rec.get("production_diff") or ""
    added = {line[1:].strip() for line in fix.splitlines()
             if line.startswith("+") and not line.startswith("+++")}
    if any(len(line) >= 25 and line in statement for line in added):
        issues.append("`problem_statement` quotes the production fix; describe the behavior instead")
    named = fix_identifiers(statement, rec)
    if named:
        issues.append("`problem_statement` names code of the fix that the tests do not use ("
                      + ", ".join(named) + "); describe what a caller observes instead")
    if not issues and review is not None:
        phrases = review(statement)
        if phrases:
            issues.append("`problem_statement` gives the fix away: " + "; ".join(f"\"{x}\"" for x in phrases)
                          + ". Describe only the observable symptom, its trigger and the required "
                          "behavior, not what the code checks or must do internally")
    return issues


def statement_one(folder: Path, profile: str, prof: dict, api_base: str, out_dir: Path,
                  recheck: bool = False) -> dict:
    """Ask the served model for the problem statement of a validated
    instance and write it into the instance's metadata.json, which the
    recipe fingerprint does not cover. With recheck, an existing statement
    is first checked under the current rules and kept if it passes; one
    that fails is replaced, and kept under superseded_statements."""
    meta = json.loads((folder / "metadata.json").read_text())
    pr = int(meta["instance_id"].rsplit("-", 1)[1])
    rec = json.loads((ENRICHED / f"{pr}.json").read_text())
    kw = model_kw(profile, prof, api_base)
    trace = {"pr": pr, "profile": profile, "mode": "statement", "turns": [], "repairs": [],
             "error": None, "tool_calls": 0, "budget_hit": False}
    review = lambda s: review_statement(s, rec, kw, trace)
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / f"{pr}.statement.trace.json"
    old = (meta.get("problem_statement") or "").strip()
    t0 = time.time()

    def write(meta: dict) -> None:
        # The leak check's fix-identifier test on the statement as written now
        # (promote_instance.py accepts it in place of the validation's).
        statement = (meta.get("problem_statement") or "").strip()
        if statement and PROJECT == "contiki-ng":
            import contiki_ng_candidates
            import contiki_ng_leakcheck
            meta["statement_leak_check"] = {
                "statement_sha256": hashlib.sha256(meta["problem_statement"].encode()).hexdigest(),
                "fix_identifiers": contiki_ng_leakcheck.statement_fix_identifiers(
                    contiki_ng_candidates.clone(), rec["base_commit"], meta["problem_statement"],
                    rec.get("production_diff") or "", rec.get("test_patch") or "")}
        (folder / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")

    if recheck and old:
        try:
            trace["recheck_issues"] = check_statement(
                {"pr": pr, "problem_statement": old, "causal_chain": meta.get("causal_chain") or "-",
                 "change_type": meta.get("change_type") or "-"}, rec, review)
        except Exception as e:                 # no verdict: decide nothing
            trace.update(error=f"{type(e).__name__}: {e}", seconds=round(time.time() - t0, 1))
            trace_path.write_text(json.dumps(trace, indent=2, default=str))
            return {"pr": pr, "ok": False, "issues": [], "error": trace["error"]}
        if not trace["recheck_issues"]:
            trace["seconds"] = round(time.time() - t0, 1)
            trace_path.write_text(json.dumps(trace, indent=2, default=str))
            meta["statement_check"] = {"version": STATEMENT_CHECK, "result": "kept",
                                       "trace": str(trace_path)}
            write(meta)
            return {"pr": pr, "ok": True, "kept": True, "issues": [], "error": None}

    request = STATEMENT_REQUEST.format(pr=pr, f2p=json.dumps(meta["fail_to_pass"]),
                                       p2p=json.dumps(meta["pass_to_pass"]))
    messages = [{"role": "system", "content": instruction_text()},
                {"role": "user", "content": "Enriched PR record:\n\n" + PROJECTS[PROJECT]["render"](rec)
                 + "\n\n---\n" + request
                 + f"\n\nYou may use up to {MAX_TOOL_ROUNDS} tool rounds."}]
    answer, issues = converse(messages, rec, lambda v: check_statement(v, rec, review), kw, trace)
    if trace["error"] and not issues:
        issues = [trace["error"]]
    trace.update(seconds=round(time.time() - t0, 1), issues=issues, messages=messages)
    trace_path.write_text(json.dumps(trace, indent=2, default=str))
    if old:
        meta.setdefault("superseded_statements", []).append(
            {"problem_statement": old, "provenance": meta.get("statement_provenance") or "triage verdict",
             "check_issues": trace.get("recheck_issues")})
    if issues:
        if old:       # a statement that failed the check is not promotable
            meta.update(problem_statement="", statement_check={
                "version": STATEMENT_CHECK, "result": "failed", "issues": issues, "trace": str(trace_path)})
            write(meta)
        return {"pr": pr, "ok": False, "issues": issues, "error": trace["error"]}
    meta.update(problem_statement=answer["problem_statement"].strip(),
                causal_chain=answer["causal_chain"].strip(), change_type=answer["change_type"].strip(),
                statement_provenance={"model": profile, "mode": "statement",
                                      "trace": str(trace_path),
                                      "prompt_sha256": hashlib.sha256(messages[0]["content"].encode()).hexdigest()[:16]},
                statement_check={"version": STATEMENT_CHECK, "result": "written", "trace": str(trace_path)})
    write(meta)
    return {"pr": pr, "ok": True, "kept": False, "issues": [], "error": None}


def cmd_statement(args) -> None:
    use_project(args.project)
    profiles = json.loads((TRIAGE / "models.json").read_text())
    if args.profile not in profiles or args.profile.startswith("_"):
        sys.exit(f"unknown profile {args.profile!r}")
    folder = Path(args.instance)
    receipt = json.loads((folder / "validation.json").read_text())
    if not receipt.get("valid"):
        sys.exit(f"{folder.name}: not a valid instance")
    s = statement_one(folder, args.profile, profiles[args.profile], args.api_base, RUNS, args.recheck)
    print(f"  {s['pr']}: " + ("statement kept (passes the check)" if s.get("kept") else
                              "statement written" if s["ok"] else
                              "no statement: " + "; ".join(s["issues"] or [s["error"] or "?"])))
    sys.exit(0 if s["ok"] else 1)


# ---------------------------------------------------------------- scoring


def gold_file() -> dict:
    name = {"zephyr": "gold.json", "contiki-ng": "gold-contiki-ng.json"}.get(PROJECT)
    if name is None:
        raise ValueError(f"no gold set for {PROJECT}")
    return json.loads((TRIAGE / name).read_text())


def load_gold() -> dict[int, str]:
    g = gold_file()
    return {**{int(p): "accept" for p in g["accept"]},
            **{int(p): "reject" for p in g["reject"]},
            **{int(p): "info" for p in g.get("info_only", {})}}


def gold_stage(pr: int) -> str:
    """Where the pipeline itself rejects a gold reject: hard_filter, dry_run
    or triage. Every gold PR is sent to the model regardless."""
    return (gold_file()["reject"].get(str(pr)) or {}).get("stage", "")


def secondary(pr: int, v: dict) -> str:
    """Accepted gold PRs only: the verdict's pair and tests against what the
    validated instance proved (gold.json `expected`). Reported, never scored:
    another selected pair may validate too, so a different pair is shown as
    such, not as wrong."""
    exp = ((gold_file()["accept"].get(str(pr)) or {}).get("expected"))
    if not exp or v.get("verdict") != "accept":
        return ""
    if PROJECT == "contiki-ng":
        checks = [f"{key} {'✓' if v.get(key) == exp.get(key) else '≠ reference'}"
                  for key in ("profile", "oracle")]
        for key in ("fail_to_pass", "pass_to_pass"):
            known, chosen = set(exp.get(key, [])), set(v.get(key, []))
            checks.append(f"{key} {len(known & chosen)}/{len(chosen)} known"
                          + (f", {len(chosen - known)} unverified" if chosen - known else ""))
        return "; ".join(checks) + "; plan validation required"
    norm = lambda b: SAME_BOARD.get(b, b)
    mine = (norm(v.get("platform")), v.get("test_path"), v.get("scenario"))
    if mine == (norm(exp["platform"]), exp["test_path"], exp["scenario"]):
        pair = "✓ pair"
    else:
        dr = load_dry_run(pr) or {"pairs": []}
        sel = any(p["status"] == "selected" and (norm(p["board"]), p["suite"], p["scenario"]) == mine
                  for p in dr["pairs"])
        pair = (f"{'≠ selected' if sel else '✗ not selected'}: {v.get('platform')} "
                f"{v.get('scenario')} (validated: {exp['platform']} {exp['scenario']})")
    want, got = set(exp.get("fail_to_pass") or []), set(v.get("fail_to_pass") or [])
    f2p = (f"f2p {len(want & got)}/{len(want)} measured"
           + (f", +{len(got - want)} not measured" if got - want else ""))
    return f"{pair} / {f2p}"


def previous_verdicts(run_dir: Path) -> dict[int, str]:
    """Latest earlier verdict for each PR, across all earlier runs."""
    prev = {}
    for d in sorted(p for p in RUNS.iterdir() if p.is_dir() and p.name < run_dir.name):
        for f in d.glob("[0-9]*.json"):
            if not f.name.endswith(".trace.json"):
                prev[int(f.stem)] = json.loads(f.read_text()).get("verdict")
    return prev


def cmd_eval(args) -> None:
    use_project(getattr(args, "project", PROJECT))
    run_dir = (Path(args.run_dir) if args.run_dir
               else RUNS if layout.run_dir() else max(RUNS.iterdir(), key=lambda p: p.name))
    gold, prev = load_gold(), previous_verdicts(run_dir)
    rows, scored, correct, changed = [], 0, 0, []
    verdicts = [f for f in run_dir.glob("[0-9]*.json") if not f.name.endswith(".trace.json")]
    for f in sorted(verdicts, key=lambda p: int(p.stem)):
        pr, v = int(f.stem), json.loads(f.read_text())
        trace = json.loads((run_dir / f"{pr}.trace.json").read_text())
        want, got = gold.get(pr, "—"), v.get("verdict")
        stage = gold_stage(pr)
        ok = "" if want in ("info", "—") else ("✓" if got == want else "✗")
        if ok:
            scored += 1
            correct += ok == "✓"
        if pr in prev and prev[pr] != got:
            changed.append(f"{pr}: {prev[pr]} → {got}")
        rows.append({"pr": pr, "expected": want + (f" ({stage})" if stage else ""),
                     "stage": stage, "got": got, "ok": ok,
                     "confidence": v.get("confidence", ""), "secondary": secondary(pr, v),
                     "seconds": trace.get("seconds"), "turns": len(trace.get("turns", [])),
                     "tool_calls": sum(len(t.get("tool_calls") or []) for t in trace.get("turns", [])),
                     "budget_hit": trace.get("budget_hit", False),
                     "issues": trace.get("issues") or []})

    acc = [r for r in rows if r["expected"] == "accept"]
    rej = [r for r in rows if r["expected"].startswith("reject")]
    rej_t = [r for r in rej if r["stage"] == "triage"]
    md = [f"# Scorecard: {run_dir.name}", "",
          f"**Verdicts: {correct}/{scored} correct** "
          f"(accepts {sum(r['ok'] == '✓' for r in acc)}/{len(acc)}, "
          f"rejects {sum(r['ok'] == '✓' for r in rej)}/{len(rej)}, of which judged only at "
          f"triage {sum(r['ok'] == '✓' for r in rej_t)}/{len(rej_t)})", "",
          f"Time per PR: mean {sum(r['seconds'] for r in rows) / max(len(rows), 1) / 60:.1f} min, "
          f"max {max((r['seconds'] for r in rows), default=0) / 60:.1f} min. "
          f"Tool calls: {sum(r['tool_calls'] for r in rows)} in total; "
          f"budget hit on {sum(r['budget_hit'] for r in rows)} PR(s).", "",
          "| PR | expected | got | ok | conf | pair / fail_to_pass vs validated | time | tools | unresolved issues |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['pr']} | {r['expected']} | {r['got']} | {r['ok']} | {r['confidence']} | "
                  f"{r['secondary']} | {r['seconds'] / 60:.1f} min | {r['tool_calls']}"
                  f"{' (budget hit)' if r['budget_hit'] else ''} | {'; '.join(r['issues'])} |")
    md += ["", "Changed since earlier runs: " + (", ".join(changed) if changed else "none")]
    if PROJECT == "contiki-ng":
        md += ["", f"Abstentions (hold): {sum(r['got'] == 'hold' for r in rows)}; "
               f"errors: {sum(r['got'] == 'error' for r in rows)}. "
               "Gold label agreement does not establish instance-plan validity or unseen accuracy."]
    (run_dir / "scorecard.md").write_text("\n".join(md) + "\n")
    (run_dir / "scorecard.json").write_text(json.dumps(
        {"run": run_dir.name, "project": PROJECT, "correct": correct, "scored": scored, "rows": rows,
         "abstentions": [r["pr"] for r in rows if r["got"] == "hold"],
         "errors": [r["pr"] for r in rows if r["got"] == "error"],
         "changed": changed}, indent=2) + "\n")
    print("\n".join(md))


# ---------------------------------------------------------------- run


def git_state() -> dict:
    head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    if not head and (REPO_ROOT / ".embedeval-revision").exists():
        revision = (REPO_ROOT / ".embedeval-revision").read_text().strip()
        snapshot = REPO_ROOT / "source-manifest.json"
        return {"commit": revision, "dirty": None, "archive": True,
                "source_sha256": json.loads(snapshot.read_text()).get("sha256") if snapshot.exists() else None}
    return {"commit": head, "dirty": bool(dirty)}


def cmd_run(args) -> None:
    use_project(args.project)
    profiles = json.loads((TRIAGE / "models.json").read_text())
    if args.profile not in profiles or args.profile.startswith("_"):
        sys.exit(f"unknown profile {args.profile!r}")
    prof = profiles[args.profile]

    if args.gold and PROJECT not in ("zephyr", "contiki-ng"):
        sys.exit(f"no gold set for {PROJECT}")
    if args.gold and gold_file().get("_status", "finalized") != "finalized":
        sys.exit("gold reference verification is not finalized")
    prs = list(dict.fromkeys((args.pr or []) + (sorted(load_gold()) if args.gold else [])))
    if not prs:
        sys.exit("give --pr and/or --gold")
    missing = [p for p in prs if not (ENRICHED / f"{p}.json").exists()]
    if missing:
        sys.exit(f"not enriched: {missing}. Run filter_candidates.py enrich --pr ...")
    if PROJECT == "contiki-ng":
        import contiki_ng_candidates
        stale_contiki = [n for n in prs if contiki_ng_candidates.stale_record(json.loads((ENRICHED / f"{n}.json").read_text()))]
        if stale_contiki:
            sys.exit(f"stale Contiki-NG records: {stale_contiki}; re-enrich")
    if PROJECT == "openthread":
        import openthread_candidates
        stale = [p for p in prs if openthread_candidates.stale_record(json.loads((ENRICHED / f"{p}.json").read_text()))]
        if stale:
            sys.exit(f"ERROR: enriched records {stale} are not enrich_version {openthread_candidates.ENRICH_VERSION}. "
                     "Run `python3 scripts/openthread_candidates.py enrich --local`.")
    no_dry = [p for p in prs if PROJECT == "zephyr" and load_dry_run(p) is None]
    if no_dry:
        print(f"  WARNING: no dry run for {no_dry}; the model sees no board/scenario table "
              f"for them. Run scripts/dry_run.py {' '.join(map(str, no_dry))}")

    # Make every commit the tools may read reachable before any thread needs it.
    commits = set()
    for p in prs:
        rec = json.loads((ENRICHED / f"{p}.json").read_text())
        commits |= {c for c in (rec.get("base_commit"), rec.get("merge_commit_sha") or rec.get("fix_commit")) if c}
    # Only what is missing: in a cluster run prepare.py has fetched them all,
    # and many triage calls fetching into one clone at once collide on locks.
    missing = [c for c in sorted(commits) if git("cat-file", "-e", f"{c}^{{commit}}").returncode]
    print(f"Fetching {len(missing)} of {len(commits)} commits into the {PROJECT} clone ...")
    r = git("fetch", "-q", "origin", *missing, timeout=1800) if missing else None
    if r is not None and r.returncode:
        print(f"  warning: fetch failed; tools may not see every commit:\n  {r.stderr.strip()[:300]}")

    stamp = f"{datetime.now():%Y-%m-%d_%H%M%S}"
    if layout.run_dir():
        # A run folder: verdicts go straight into <run>/triage/, one file per
        # PR, so any number of triage calls (nodes, slots) can share it. Each
        # call keeps its own manifest and copy of the instructions.
        run_dir = RUNS
        run_dir.mkdir(parents=True, exist_ok=True)
        tag = f"-{stamp}-{os.getpid()}"
    else:
        run_dir = RUNS / f"{datetime.now():%Y-%m-%d_%H%M}_{args.profile}"
        run_dir.mkdir(parents=True, exist_ok=False)
        tag = ""
    prompt = instruction_text()
    (run_dir / f"{INSTRUCTIONS.stem}{tag}.md").write_text(prompt)
    manifest = {"project": PROJECT, "instructions": str(INSTRUCTIONS.relative_to(REPO_ROOT)),
                "profile": args.profile, "api_base": args.api_base,
                "sampling": prof.get("sampling"), "max_tokens": prof.get("max_tokens"),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()[:16],
                "git": git_state(), "prs": prs, "parallel": args.parallel,
                "started": datetime.now().isoformat(timespec="seconds")}
    (run_dir / f"manifest{tag}.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Triage of {len(prs)} PRs with {args.profile}, {args.parallel} at a time "
          f"-> {layout.show(run_dir)}\n")

    with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(triage_one, p, args.profile, prof, args.api_base, run_dir): p
                   for p in prs}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            s = fut.result()
            note = s["error"] or ("; ".join(s["issues"]) if s["issues"] else "")
            print(f"  [{i}/{len(prs)}] {s['pr']:>6}  {str(s['verdict']):<7} "
                  f"{s['seconds'] / 60:>5.1f} min  {s['tool_calls']} tool calls"
                  f"{' (budget hit)' if s['budget_hit'] else ''}  {note[:120]}", flush=True)

    manifest["finished"] = datetime.now().isoformat(timespec="seconds")
    (run_dir / f"manifest{tag}.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print()
    if layout.run_dir():
        # Other triage calls may be writing to the same folder: the scorecard
        # is made once all are done, with `triage_prs.py --run-dir R eval`.
        print(f"  verdicts in {layout.show(run_dir)}; scorecard: triage_prs.py --run-dir "
              f"{layout.run_dir()} eval")
        return
    if PROJECT not in ("zephyr", "contiki-ng"):
        print(f"  verdicts in {layout.show(run_dir)}")
        return
    args.run_dir = str(run_dir)
    cmd_eval(args)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="triage PRs with a served model, then score the run")
    r.add_argument("--project", choices=sorted(PROJECTS), default="zephyr",
                   help="repository being triaged: picks the instructions, clone, record and checks")
    r.add_argument("--profile", required=True, help="entry in triage/models.json")
    r.add_argument("--pr", type=int, nargs="+", help="specific PR numbers")
    r.add_argument("--gold", action="store_true", help="every PR in the project's gold set")
    r.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    r.add_argument("--parallel", type=int, default=4,
                   help="PRs in flight at once; the server takes 4 (--max-num-seqs)")

    e = sub.add_parser("eval", help="score a run against the project's gold set")
    e.add_argument("--project", choices=("zephyr", "contiki-ng"), default="zephyr")
    e.add_argument("run_dir", nargs="?", help="default: the latest run")

    st = sub.add_parser("statement", help="model-written problem statement for a validated "
                                          "instance")
    st.add_argument("--project", choices=("contiki-ng",), default="contiki-ng")
    st.add_argument("--profile", required=True, help="entry in triage/models.json")
    st.add_argument("--instance", required=True, help="the validated instance folder")
    st.add_argument("--recheck", action="store_true",
                    help="check an existing statement under the current rules; replace it if it fails")
    st.add_argument("--api-base", default="http://127.0.0.1:8000/v1")

    args = p.parse_args()
    {"run": cmd_run, "eval": cmd_eval, "statement": cmd_statement}[args.cmd](args)


if __name__ == "__main__":
    main()
