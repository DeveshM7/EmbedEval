#!/usr/bin/env python3
"""Per-PR Apache Mynewt triage against a served open-weight model, scored on a partial gold.

    python scripts/triage_mynewt_prs.py run --profile qwen38-nvfp4 --gold
    python scripts/triage_mynewt_prs.py run --profile qwen38-nvfp4 --gold --blind
    python scripts/triage_mynewt_prs.py run --profile qwen38-nvfp4 --case 3680 251
    python scripts/triage_mynewt_prs.py run --profile qwen38-nvfp4 --all      # every enriched record
    python scripts/triage_mynewt_prs.py eval triage/mynewt_runs/<run>

One call triages ONE merged PR that changes a unit-test package: the model
classifies it (fix_with_test, feature_with_test, test_only_detects_earlier_fix,
maintenance, needs_hw), finds the production change its test detects (in the
same PR, or earlier in history for a test-only PR) and returns generator-ready
fields (docs/mynewt_triage.md). Records are the enriched PRs in
candidates/mynewt/enriched/<pr>.json (the files generate_instance.py reads).

Environment: MYNEWT_CLONE (bare mynewt-core clone the tools read; on Scholar
/scratch/scholar/$USER/repos/mynewt-core.git), EMBEDEVAL_MYNEWT_RUNS (run
output directory), MYNEWT_ENRICHED (record directory).
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import contextlib
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "docs" / "mynewt_triage.md"
GOLD = ROOT / "triage" / "mynewt_gold.json"
MODELS = ROOT / "triage" / "models.json"
RUNS = Path(os.environ.get("EMBEDEVAL_MYNEWT_RUNS") or ROOT / "triage" / "mynewt_runs")
ENRICHED = Path(os.environ.get("MYNEWT_ENRICHED") or ROOT / "candidates" / "mynewt" / "enriched")
CLONE = Path(os.environ.get("MYNEWT_CLONE") or ROOT / "candidates" / "mynewt.git")
CLONE_URL = "https://github.com/apache/mynewt-core.git"
GH_REPO = "apache/mynewt-core"
GIT_LOCK = threading.Lock()

ACCEPT_KINDS = ("fix_with_test", "feature_with_test", "test_only_detects_earlier_fix")
REJECT_KINDS = ("maintenance", "needs_hw")
LEVELS = ("low", "medium", "high")
MAX_TOOL_CALLS = 40
MAX_TOOL_SECONDS = 2700        # tools are withdrawn after this much wall time
FINAL_GRACE = 600
CONTEXT_LIMIT = 190_000
MAX_REPAIRS = 2
NAME = re.compile(r"[A-Za-z_]\w*/[A-Za-z_]\w*")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA = re.compile(r"[0-9a-fA-F]{7,40}")

_REF = {"ref": {"type": "string",
                "description": "'base' (before the PR), 'head' (its last commit), a commit SHA "
                               "(full or short), or a date YYYY-MM-DD (the default branch as of "
                               "the end of that day, UTC)"}}
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file in apache/mynewt-core at a revision.",
        "parameters": {"type": "object", "properties": {
            **_REF,
            "path": {"type": "string", "description": "Repository-relative file path"},
            "start_line": {"type": "integer", "description": "1-based; default 1"},
            "max_lines": {"type": "integer", "description": "Default 800"}},
            "required": ["path", "ref"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List a directory in apache/mynewt-core at a revision.",
        "parameters": {"type": "object", "properties": {
            **_REF,
            "path": {"type": "string", "description": "Repository-relative directory; empty for the top level"}},
            "required": ["path", "ref"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents with an extended regular expression in "
                       "apache/mynewt-core at a revision, under a directory or across the "
                       "whole tree. Returns up to 300 matching lines as path:line:text.",
        "parameters": {"type": "object", "properties": {
            **_REF,
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Repository-relative directory; empty for the whole tree"}},
            "required": ["pattern", "path", "ref"]}}},
    {"type": "function", "function": {
        "name": "commit_diff",
        "description": "Show the diff one commit made (message, files, patch), optionally "
                       "limited to one path. Use it to see which commit of the PR changed "
                       "tests and which changed production code, or to read a candidate fix.",
        "parameters": {"type": "object", "properties": {
            "sha": {"type": "string", "description": "A commit SHA (full or short)"},
            "path": {"type": "string", "description": "Optional repository-relative path"}},
            "required": ["sha"]}}},
    {"type": "function", "function": {
        "name": "git_log",
        "description": "Search the default-branch history of apache/mynewt-core, newest first. "
                       "Combine any of: since/until dates; symbol (commits that add or remove "
                       "that exact string, like git log -S); regex (commits whose diff adds or "
                       "removes a matching line, like git log -G); message (a case-insensitive "
                       "extended regex over commit messages); paths. Returns "
                       "'<sha> <commit date> <author> | <subject>' and the changed files per "
                       "commit. symbol and regex searches are slow over the whole history: "
                       "narrow them with dates or paths. Open a commit with open_ref to see "
                       "its diff and PR.",
        "parameters": {"type": "object", "properties": {
            "since": {"type": "string", "description": "YYYY-MM-DD"},
            "until": {"type": "string", "description": "YYYY-MM-DD (inclusive)"},
            "symbol": {"type": "string"},
            "regex": {"type": "string"},
            "message": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "max_results": {"type": "integer", "description": "Default 30, at most 100"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "open_ref",
        "description": "Follow a reference: a PR or issue in apache/mynewt-core "
                       "(apache/mynewt-core#123, '#123', a bare number, or a github.com URL), "
                       "a commit URL, or a commit SHA. A PR or issue returns its title, body, "
                       "comments, commits, changed files and patches, and the links it "
                       "mentions; a commit returns its message, files, diff and the merged "
                       "PR(s) it belongs to. Output is bounded.",
        "parameters": {"type": "object", "properties": {"ref": {"type": "string"}},
                       "required": ["ref"]}}},
]


# ----------------------------------------------------------------- records

def load_record(case: str) -> dict:
    if not case.isdecimal():
        raise ValueError(f"invalid case ID {case!r}; use the PR number")
    path = ENRICHED / f"{case}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path}: run filter_candidates.py enrich --repo mynewt --pr {case}")
    return json.loads(path.read_text())


def suite_sources(record: dict) -> dict[str, str]:
    """Every unit-test package file at the PR's merged state, by repository path."""
    return {f"{root}/{name}": entry["content"]
            for root, suite in (record.get("test_suites") or {}).items()
            for name, entry in suite.get("files", {}).items()
            if isinstance(entry.get("content"), str)}


def inventory(record: dict) -> dict[str, list[str]]:
    """suite/testcase names per test package, computed from the sources: each
    TEST_SUITE body calls the TEST_CASE functions it runs."""
    out = {}
    for root, suite in (record.get("test_suites") or {}).items():
        texts = [e["content"] for e in suite.get("files", {}).values()
                 if isinstance(e.get("content"), str)]
        text = "\n".join(texts)
        cases = set(re.findall(r"\bTEST_CASE(?:_SELF|_TASK)?\(\s*(\w+)\s*\)", text))
        names = []
        for m in re.finditer(r"\bTEST_SUITE\(\s*(\w+)\s*\)\s*\{", text):
            depth, i = 1, m.end()
            while i < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[i], 0)
                i += 1
            for call in re.findall(r"\b(\w+)\s*\(\s*\)\s*;", text[m.end():i]):
                if call in cases:
                    names.append(f"{m.group(1)}/{call}")
        out[root] = list(dict.fromkeys(names))
    return out


def render_record(case: str, record: dict, field_limit: int = 20000) -> str:
    """Bound individual blobs while keeping every changed path and package file."""
    def bounded(value):
        if isinstance(value, str):
            return (value[:field_limit] + f"\n[truncated {len(value) - field_limit} characters]"
                    if len(value) > field_limit else value)
        if isinstance(value, list):
            return [bounded(item) for item in value]
        if isinstance(value, dict):
            return {key: bounded(item) for key, item in value.items()}
        return value

    keys = ("number", "url", "title", "body", "merged_at", "labels", "base_commit",
            "base_date", "head_commit", "commits", "files", "files_truncated", "linked_issues")
    view = {key: record[key] for key in keys if key in record}
    files = record.get("files") or []
    patch_limit = min(field_limit, 250000 // max(len(files), 1))
    view["files"] = [{**item, "patch": (
        item["patch"][:patch_limit] + "\n[Diff excerpt; use diff with this path for the full patch.]"
        if isinstance(item.get("patch"), str) and len(item["patch"]) > patch_limit
        else item.get("patch"))} for item in files]
    for key in ("comments", "reviews", "review_comments"):
        view[key] = [{field: item.get(field) for field in
                      ("body", "state", "path", "diff_hunk") if item.get(field)}
                     for item in record.get(key, [])[:30]]
    source_files = sum(len(suite.get("files", {}))
                       for suite in (record.get("test_suites") or {}).values())
    source_limit = min(field_limit, 120000 // max(source_files, 1))
    def source_excerpt(content):
        if not isinstance(content, str) or len(content) <= source_limit:
            return content
        return (content[:source_limit] +
                "\n[Source excerpt; use read_file at head for the remaining lines.]")

    view["test_suites"] = {root: {name: source_excerpt(entry.get("content")) for name, entry in
                                  suite.get("files", {}).items()}
                           for root, suite in (record.get("test_suites") or {}).items()}
    text = json.dumps({"case": case, **bounded(view)}, ensure_ascii=False,
                      separators=(",", ":"))
    lines = ["", "", "Test package roots (test_path is one of these) and their testcases, computed "
             "from the sources at the PR's head (use these names exactly; names the sources "
             "cannot give are missing):"]
    for root, names in inventory(record).items():
        lines.append(f"- {root}: " + (", ".join(names) or "(none computed; read the sources)"))
    return text + "\n".join(lines)


def hide_links(text: str) -> str:
    """Blind variant: no issue, PR or commit references."""
    text = re.sub(r"https?://github\.com/\S+", "[link hidden]", text or "")
    text = re.sub(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#\d+", "[ref hidden]", text, flags=re.I)
    text = re.sub(r"(?<![\w/])#\d+\b", "[ref hidden]", text)
    text = re.sub(r"\bMYNEWT-\d+\b", "[ref hidden]", text)
    return re.sub(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b", "[hash hidden]", text)


def blind_record(record: dict) -> dict:
    """Same PR with its explicit links hidden: the model must find the fix by
    search, not by reference. The issue text stays (it is the problem statement)."""
    rec = json.loads(json.dumps(record))
    rec["blind"] = True
    rec["body"] = hide_links(rec.get("body"))
    for c in rec.get("commits", []):
        c["message"] = hide_links(c.get("message"))
    for key in ("comments", "review_comments", "reviews"):
        for item in rec.get(key, []):
            item["body"] = hide_links(item.get("body"))
    for issue in rec.get("linked_issues", []):
        issue["comments"] = []
        issue["body"] = hide_links(issue.get("body"))
        issue.pop("url", None)
    return rec


# ---------------------------------------------------------------- checking

def parse_verdict(content: str | None) -> dict | None:
    """The last top-level JSON object in the reply. Prose before it can contain
    braces, so first-{ to last-} is not enough."""
    if not content:
        return None
    decoder, found, i = json.JSONDecoder(), None, content.find("{")
    while i >= 0:
        try:
            value, end = decoder.raw_decode(content, i)
        except json.JSONDecodeError:
            i = content.find("{", i + 1)
            continue
        if isinstance(value, dict):
            found = value
        i = content.find("{", end)
    return found


def is_accept(verdict: dict) -> bool:
    """High or medium accepting kind: built and run. Low is only recorded."""
    return verdict.get("kind") in ACCEPT_KINDS and verdict.get("confidence") in ("high", "medium")


def check_names(field: str, names, sources: str, required: bool) -> list[str]:
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names) or \
            (required and not names):
        need = "a non-empty list" if required else "a list (empty if none)"
        return [f"{field} must be {need} of suite/testcase names, e.g. \"my_suite/my_case\""]
    malformed = [n for n in names if not NAME.fullmatch(n)]
    if malformed:
        return [f"{field} has names not in suite/testcase form: {malformed}. Write each as "
                f"the TEST_SUITE name, a slash, and the TEST_CASE name"]
    unknown = [n for n in names if not all(re.search(rf"\b{re.escape(part)}\b", sources)
                                           for part in n.split("/"))]
    if unknown:
        return [f"{field} names a suite or testcase that does not appear in the record's "
                f"test package sources: {unknown}. Use only TEST_SUITE and TEST_CASE names "
                f"defined in those sources"]
    return []


def check_verdict(verdict: dict | None, case: str, record: dict) -> list[str]:
    """Schema of docs/mynewt_triage.md plus every check generate_instance.py
    would otherwise fail on, each with the exact change to make."""
    if verdict is None:
        return ["reply must contain exactly one JSON object and nothing else"]
    issues = []
    if verdict.get("pr") != int(case):
        issues.append(f"pr must be the integer {case}")
    kind = verdict.get("kind")
    if kind not in ACCEPT_KINDS + REJECT_KINDS:
        issues.append(f"kind must be one of {list(ACCEPT_KINDS + REJECT_KINDS)}")
    if verdict.get("confidence") not in LEVELS:
        issues.append("confidence must be \"high\", \"medium\", or \"low\"")
    if not str(verdict.get("reason") or "").strip():
        issues.append("reason is required: one or two sentences")
    if kind not in ACCEPT_KINDS:
        return issues

    roots = list((record.get("test_suites") or {}))
    if verdict.get("test_path") not in roots:
        issues.append(f"test_path must be one of the record's test package roots: {roots}")
    for field in ("problem_statement", "detecting_test"):
        if not isinstance(verdict.get(field), str) or not verdict[field].strip():
            issues.append(f"{field} is required and must be a non-empty string")
    if not isinstance(verdict.get("searched"), list) or not verdict.get("searched"):
        issues.append("searched must be a non-empty list of what you looked at")
    paths = verdict.get("fix_paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        issues.append("fix_paths must be a non-empty list of the production files the fix changes")

    sources = "\n".join(suite_sources(record).values())
    issues += check_names("fail_to_pass", verdict.get("fail_to_pass"), sources, True)
    issues += check_names("pass_to_pass", verdict.get("pass_to_pass"), sources, False)
    baseline = verdict.get("baseline_tests")
    if not isinstance(baseline, list) or not all(isinstance(n, str) and NAME.fullmatch(n)
                                                  for n in baseline):
        issues.append("baseline_tests must be a list (empty if none) of suite/testcase "
                      "names present before the test patch is applied")
    f2p, p2p = verdict.get("fail_to_pass"), verdict.get("pass_to_pass")
    if isinstance(f2p, list) and isinstance(p2p, list):
        both = sorted(set(map(str, f2p)) & set(map(str, p2p)))
        if both:
            issues.append(f"remove {both} from pass_to_pass: a testcase belongs in "
                          f"fail_to_pass or pass_to_pass, not both")

    own = int(case)
    if kind == "test_only_detects_earlier_fix":
        fix_pr, fix_commit = verdict.get("fix_pr"), verdict.get("fix_commit")
        if fix_pr is not None and (type(fix_pr) is not int or fix_pr == own):
            issues.append("fix_pr must be the number of the EARLIER PR that holds the fix, "
                          "or null if it has none")
        if not isinstance(fix_commit, str) or not SHA.fullmatch(fix_commit):
            issues.append("fix_commit must be the SHA of the earlier fix commit (open the fix "
                          "with open_ref to get it)")
        else:
            sha = resolve(fix_commit)
            if sha is None:
                issues.append(f"fix_commit {fix_commit} is not a commit of apache/mynewt-core")
            elif record.get("base_commit") and git(
                    "merge-base", "--is-ancestor", sha, record["base_commit"]).returncode:
                issues.append("fix_commit must already be in the history before this PR's base "
                              "commit; otherwise the PR itself holds the fix and the kind is "
                              "fix_with_test or feature_with_test")
    elif verdict.get("fix_pr") != own:
        issues.append(f"fix_pr must be {case} (the fix is this PR's own production change)")

    mode = verdict.get("failure_mode", "runtime")
    if mode not in ("runtime", "compile", "config", "dependency"):
        issues.append("failure_mode must be runtime, compile, config or dependency")
    if mode == "config" and not isinstance(verdict.get("config_expectation"), dict):
        issues.append("config mode requires an observed, source-attributed config_expectation")
    if mode == "dependency" and (verdict.get("change_type") != "feature_with_test"
                                  or not isinstance(verdict.get("dependency_expectation"), dict)):
        issues.append("dependency mode requires a labeled functionality addition and source-attributed dependency expectation")
    changed = {f["filename"] for f in record.get("files", [])}
    if mode == "compile":
        source, api = verdict.get("compile_test_source"), verdict.get("missing_api")
        content = suite_sources(record).get(source) if isinstance(source, str) else None
        if not isinstance(source, str) or source not in changed or \
                Path(source).suffix not in (".c", ".cc", ".cpp") or content is None:
            issues.append("compile_test_source must be the repository path of a C source in a "
                          "test package that this PR changed")
        if not isinstance(api, str) or not api.strip() or \
                (isinstance(content, str) and api not in content):
            issues.append("missing_api must be the exact identifier or header name, as "
                          "written in compile_test_source, that the unfixed code lacks")

    commits = [c["sha"] for c in record.get("commits", [])]
    test_commit = verdict.get("test_commit")
    support = verdict.get("test_support_paths") or []
    if test_commit is not None and test_commit not in commits:
        issues.append("test_commit must be the full SHA of one of the PR's commits, or null; "
                      f"the PR's commits are {commits}")
    if not isinstance(support, list) or not all(isinstance(p, str) for p in support):
        issues.append("test_support_paths must be a list of paths, or be omitted")
    else:
        if support and test_commit is None:
            issues.append("test_support_paths requires test_commit; set test_commit to the "
                          "last commit before the production fix, or remove test_support_paths")
        unknown = [p for p in support if p not in changed]
        if unknown:
            issues.append(f"remove {unknown} from test_support_paths: list only files this "
                          f"PR changed outside its test packages")
    flags = verdict.get("compatibility_cflags", [])
    if not isinstance(flags, list) or not all(isinstance(f, str) and f.startswith("-")
                                              for f in flags):
        issues.append("compatibility_cflags must be a list of compiler flags (usually [])")
    if not isinstance(verdict.get("gold_patch_three_way", False), bool):
        issues.append("gold_patch_three_way must be true or false, or be omitted")
    return issues


# ------------------------------------------------------------------- tools

def git(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    with GIT_LOCK:
        if not CLONE.exists():
            CLONE.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--bare", CLONE_URL, str(CLONE)],
                           capture_output=True, text=True, timeout=3600, check=True)
    # Reads run in parallel; only fetches write to the clone.
    with GIT_LOCK if args[:1] == ("fetch",) else contextlib.nullcontext():
        return subprocess.run(["git", "-C", str(CLONE), *args],
                              capture_output=True, text=True, timeout=timeout)


def gh(path: str):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"gh api {path}: {(r.stderr or r.stdout).strip()[:200]}")
    return json.loads(r.stdout)


def resolve(ref: str) -> str | None:
    """The full SHA of a commit in the clone, fetching a full SHA it lacks."""
    def parse():
        r = git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
        return r.stdout.strip() if r.returncode == 0 else None
    sha = parse()
    if sha is None and len(ref) == 40:
        git("fetch", "-q", "origin", ref, timeout=600)
        sha = parse()
    return sha


def pinned(ref, record: dict) -> str:
    """The commit a tool call names. Raises ValueError."""
    ref = str(ref or "").strip()
    sha = {"base": record.get("base_commit"), "head": record.get("head_commit")}.get(ref)
    if sha:
        if not resolve(sha):
            raise ValueError(f"commit {sha[:12]} is unavailable")
        return sha
    if DATE.fullmatch(ref):
        r = git("rev-list", "-1", "--first-parent", f"--before={ref} 23:59:59 +0000", "HEAD")
        if r.returncode or not r.stdout.strip():
            raise ValueError(f"no commit before {ref}")
        return r.stdout.strip()
    if SHA.fullmatch(ref):
        sha = resolve(ref)
        if sha:
            return sha
        raise ValueError(f"commit {ref} is not in apache/mynewt-core")
    raise ValueError("ref must be base, head, a commit SHA, or a date YYYY-MM-DD")


def clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n[... {len(text) - limit} more characters]"


def git_log(args: dict, record: dict) -> str:
    try:
        limit = min(max(int(args.get("max_results") or 30), 1), 100)
    except (TypeError, ValueError):
        return "error: max_results must be a number"
    base = ["log", "--no-color", "--date=short", f"--max-count={limit}",
            "--format=%x1e%h %cd %an | %s", "--name-only"]
    command = list(base)
    for key, end in (("since", False), ("until", True)):
        if args.get(key):
            if not DATE.fullmatch(str(args[key])):
                return f"error: {key} must be YYYY-MM-DD"
            command.append(f"--{key}={args[key]} {'23:59:59' if end else '00:00:00'} +0000")
    if args.get("symbol"):
        command.append(f"-S{args['symbol']}")
    if args.get("regex"):
        command += ["-G", str(args["regex"])]
    if args.get("message"):
        command += ["-i", "-E", f"--grep={args['message']}"]
    paths = args.get("paths") or []
    paths = [paths] if isinstance(paths, str) else paths
    if not isinstance(paths, list) or any(
            not isinstance(p, str) or p.startswith("/") or ".." in Path(p).parts for p in paths):
        return "error: paths must be repository-relative paths"
    if paths:
        command.append("--full-history")
    if command == base and not paths:
        return "error: give at least one of since, until, symbol, regex, message, paths"
    try:
        r = git(*command, "HEAD", "--", *[p.strip("/") for p in paths if p.strip("/")],
                timeout=600)
    except subprocess.TimeoutExpired:
        return "error: the search took over 10 minutes; narrow it with dates or paths"
    if r.returncode:
        return f"error: {r.stderr.strip()[:300]}"
    commits = []
    for chunk in r.stdout.split("\x1e")[1:]:
        head, *files = [line for line in chunk.splitlines() if line.strip()]
        more = f" (+{len(files) - 8} more)" if len(files) > 8 else ""
        commits.append(head + (f"\n    {', '.join(files[:8])}{more}" if files else ""))
    text = "\n".join(commits) or "(no commits)"
    if len(commits) == limit:
        text += f"\n... stopped at {limit} commits; narrow the search or raise max_results"
    return clip(text, 16000)


def commit_text(sha: str, path: str = "", budget: int = 60000) -> str:
    r = git("show", "--format=%H%nDate: %cd%n%n%B", "--date=short", "--stat", "--patch", sha,
            "--", *([path] if path else []))
    if r.returncode:
        return f"error: {r.stderr.strip()[:300]}"
    return r.stdout[:budget] + ("\n... (diff truncated; pass path to narrow it)"
                                if len(r.stdout) > budget else "")


ITEM_URL = re.compile(r"github\.com/apache/mynewt-core/(?:pull|pulls|issues)/(\d+)")
COMMIT_URL = re.compile(r"github\.com/apache/mynewt-core/(?:pull/\d+/)?commits?/([0-9a-fA-F]{7,40})")
SCOPED = re.compile(r"(?:apache/)?(?:mynewt-core)?#?(\d+)")


def open_ref(args: dict, record: dict) -> str:
    ref = str(args.get("ref") or "").strip()
    found = COMMIT_URL.search(ref)
    if found or (SHA.fullmatch(ref) and not ref.isdecimal()):
        sha_text = found[1] if found else ref
        sha = resolve(sha_text)
        if sha is None:
            return f"error: commit {sha_text} is not in apache/mynewt-core"
        text = commit_text(sha, budget=20000)
        try:
            prs = gh(f"repos/{GH_REPO}/commits/{sha}/pulls")
            text = ("Pull requests: " + (", ".join(
                f"#{p['number']} \"{p['title']}\" (merged {p.get('merged_at')})"
                for p in prs) or "none found") + "\n\n" + text)
        except (RuntimeError, subprocess.SubprocessError) as exc:
            text = f"(pull requests unavailable: {exc})\n\n" + text
        return text
    number = None
    found = ITEM_URL.search(ref)
    if found:
        number = int(found[1])
    elif re.fullmatch(r"(?:apache/mynewt-core)?#?\d+", ref):
        number = int(re.search(r"\d+", ref)[0])
    if number is None:
        return "error: ref must be a PR or issue reference, a commit URL, or a commit SHA"
    try:
        item = gh(f"repos/{GH_REPO}/issues/{number}")
    except RuntimeError as exc:
        return f"error: {exc}"
    kind = "PR" if item.get("pull_request") else "ISSUE"
    lines = [f"{kind} #{number}: {item['title']}", item.get("html_url", ""),
             f"state: {item.get('state')}  created_at: {item.get('created_at')}  "
             f"closed_at: {item.get('closed_at')}", "", "Body:", clip(item.get("body"), 6000)]
    texts = [item.get("body") or ""]
    try:
        comments = gh(f"repos/{GH_REPO}/issues/{number}/comments?per_page=30")
    except RuntimeError:
        comments = []
    if comments:
        lines += ["", "Comments:"] + [f"- {clip(c.get('body'), 800)}" for c in comments[:15]]
        texts += [c.get("body") or "" for c in comments]
    if kind == "PR":
        try:
            pr = gh(f"repos/{GH_REPO}/pulls/{number}")
            commits = gh(f"repos/{GH_REPO}/pulls/{number}/commits?per_page=100")
            files = gh(f"repos/{GH_REPO}/pulls/{number}/files?per_page=100")
        except RuntimeError as exc:
            return "\n".join(lines) + f"\nerror: {exc}"
        lines.insert(3, f"merged_at: {pr.get('merged_at')}  merge_commit: {pr.get('merge_commit_sha')}")
        lines += ["", "Commits:"] + [f"- {c['sha'][:12]} {clip(c['commit']['message'], 600)}"
                                      for c in commits[:30]]
        texts += [c["commit"]["message"] for c in commits]
        lines += ["", "Files:"] + [f"- {f['status']} {f['filename']} +{f['additions']} -{f['deletions']}"
                                    for f in files[:150]]
        text = "\n".join(lines)
        for f in files:
            if f.get("patch") and len(text) < 20000:
                text += f"\n\n--- {f['filename']}\n" + clip(f["patch"], 4000)
        lines = [text]
    mentioned = sorted({m for m in re.findall(r"#\d+|MYNEWT-\d+", "\n".join(texts))})
    if mentioned:
        lines += ["", "Links mentioned: " + ", ".join(mentioned[:60])]
    text = clip("\n".join(lines), 20000)
    return hide_links(text) if record.get("blind") else text


def run_tool(name: str, args: dict, record: dict) -> str:
    if name == "git_log":
        return git_log(args, record)
    if name == "open_ref":
        return open_ref(args, record)
    path = args.get("path") or ""
    if not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts:
        return "error: invalid repository-relative path"
    path = path.strip().strip("/")
    if name not in ("read_file", "list_dir", "grep", "commit_diff"):
        return f"error: unknown tool {name}"
    if name == "read_file" and not path:
        return "error: read_file needs a file path"
    try:
        sha = pinned(args.get("sha") if name == "commit_diff" else args.get("ref"), record)
    except ValueError as exc:
        return f"error: {exc}"
    if name == "commit_diff":
        return commit_text(sha, path)
    if name == "read_file":
        r = git("show", f"{sha}:{path}")
        if r.returncode:
            return f"error: {r.stderr.strip()[:300]}"
        lines = r.stdout.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        count = max(1, int(args.get("max_lines") or 800))
        chunk = lines[start - 1:start - 1 + count]
        body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(chunk, start))
        more = start - 1 + count < len(lines)
        return (body[:60000] + (f"\n... ({len(lines)} lines in total; pass start_line to read on)"
                                if more else "")) or "(empty file)"
    if name == "list_dir":
        r = git("ls-tree", f"{sha}:{path}" if path else sha)
        if r.returncode:
            return f"error: {r.stderr.strip()[:300]}"
        return "\n".join(f"{line.split()[1]:<5} {line.split(chr(9))[1]}"
                         for line in r.stdout.splitlines()) or "(empty)"
    r = git("grep", "-n", "-E", "-e", args.get("pattern") or "", sha, "--", path or ".")
    if r.returncode not in (0, 1):
        return f"error: {r.stderr.strip()[:300]}"
    hits = [line.split(":", 1)[1] for line in r.stdout.splitlines()]   # drop "<sha>:"
    return ("\n".join(hits[:300]) + (f"\n... {len(hits) - 300} more" if len(hits) > 300 else "")
            or "(no matches)")


# ----------------------------------------------------------------- scoring

def load_gold(path: Path | None = None) -> dict:
    return json.loads((path or GOLD).read_text())["cases"]


def score(gold: dict, verdicts: dict[str, dict]) -> dict:
    """Score against a partial gold. A must_accept case needs a high or medium
    accepting kind, and every field the gold is certain of: kind (one of the
    listed), test_path, fix_pr, fail_to_pass containing the listed cases and
    none of not_fail_to_pass. A must_not_propose case must not be accepted at
    high or medium (low is allowed). Other cases are unknown: their accepts
    are listed for validation, never penalized."""
    rows = []
    for case, entry in gold.items():
        v = verdicts.get(case, {})
        accepted = is_accept(v)
        problems = []
        if "must_accept" in entry:
            want = entry["must_accept"]
            if not accepted:
                problems.append(f"not accepted ({v.get('kind', 'missing')} {v.get('confidence', '')})")
            else:
                if v.get("kind") not in want.get("kind", ACCEPT_KINDS):
                    problems.append(f"kind {v.get('kind')}")
                if "test_path" in want and v.get("test_path") != want["test_path"]:
                    problems.append(f"test_path {v.get('test_path')}")
                if "fix_pr" in want and v.get("fix_pr") != want["fix_pr"]:
                    problems.append(f"fix_pr {v.get('fix_pr')}")
                f2p = set(v.get("fail_to_pass") or [])
                lacks = [n for n in want.get("fail_to_pass_includes", []) if n not in f2p]
                if lacks:
                    problems.append(f"fail_to_pass lacks {lacks}")
                bad = [n for n in want.get("not_fail_to_pass", []) if n in f2p]
                if bad:
                    problems.append(f"fail_to_pass has {bad}")
        else:
            if accepted:
                problems.append(f"violation: accepted {v.get('kind')} {v.get('confidence')}")
        rows.append({"case": case, "expect": "accept" if "must_accept" in entry else "not accepted",
                     "got": f"{v.get('kind', 'missing')} {v.get('confidence', '')}".strip(),
                     "problems": problems, "correct": not problems,
                     "reason": str(v.get("reason", ""))[:200]})
    return {"correct": sum(r["correct"] for r in rows), "scored": len(rows), "rows": rows}


def trace_stats(run_dir: Path, case: str) -> dict:
    path = run_dir / f"{case}.trace.json"
    if not path.exists():
        return {}
    t = json.loads(path.read_text())
    return {"repairs": len(t.get("repairs", [])), "tool_calls": t.get("tool_calls", 0),
            "seconds": t.get("seconds"), "exhausted": t.get("budget_exhausted")}


def eval_run(run_dir: Path, gold_path: Path | None = None) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    gold = {c: e for c, e in load_gold(gold_path).items() if c in manifest.get("cases", [])}
    if not gold:
        raise ValueError("no gold cases in this run")
    verdicts = {c: json.loads((run_dir / f"{c}.json").read_text())
                for c in gold if (run_dir / f"{c}.json").exists()}
    result = score(gold, verdicts)
    stats = {c: trace_stats(run_dir, c) for c in gold}
    result["repairs"] = sum(s.get("repairs", 0) for s in stats.values())
    blind = " (blind: explicit links hidden)" if manifest.get("blind") else ""
    lines = [f"# Mynewt per-PR triage: {run_dir.name}{blind}", "",
             f"Cases: {result['correct']}/{result['scored']} correct",
             f"Repairs: {result['repairs']}", "",
             "An `error` verdict is a miss even when the intended one was right.", "",
             "| PR | Expect | Got | Problems | Tool calls / s | Repairs | Correct |",
             "|---|---|---|---|---|---|---|"]
    for row in result["rows"]:
        s = stats.get(row["case"], {})
        lines.append(f"| {row['case']} | {row['expect']} | {row['got']} | "
                     f"{'; '.join(row['problems'])} | {s.get('tool_calls')} / {s.get('seconds')}"
                     f"{' (' + s['exhausted'] + ' budget)' if s.get('exhausted') else ''} | "
                     f"{s.get('repairs')} | {'yes' if row['correct'] else 'no'} |")
    unknown = []
    for p in sorted(run_dir.glob("*.json")):
        if p.stem.isdecimal() and p.stem not in gold:
            v = json.loads(p.read_text())
            if is_accept(v):
                unknown.append(f"- {p.stem}: {v.get('kind')} {v.get('confidence')} test_path "
                               f"{v.get('test_path')} fix_pr {v.get('fix_pr')}")
    if unknown:
        lines += ["", "## Unknown accepts, for validation", ""] + unknown
    result["run"] = run_dir.name
    (run_dir / "scorecard.json").write_text(json.dumps(result, indent=2) + "\n")
    (run_dir / "scorecard.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return result


# ------------------------------------------------------------------ running

def model_options(profile: str, settings: dict, api_base: str) -> dict:
    sampling = dict(settings.get("sampling", {}))
    extra = {key: sampling.pop(key) for key in ("top_k", "min_p") if key in sampling}
    options = {"model": f"openai/{profile}", "api_base": api_base, "api_key": "local",
               "timeout": 3600, "num_retries": 0,
               "max_tokens": settings.get("max_tokens", 32768), **sampling}
    if extra:
        options["extra_body"] = extra
    return options


def converse(messages: list, options: dict, call_tool, check, trace: dict,
             deadline: float) -> tuple[dict | None, list[str]]:
    """Let the model call tools, then check its JSON reply and ask for up to
    MAX_REPAIRS repairs. Tools are withdrawn once the call, time or context
    budget runs out."""
    import litellm

    verdict, issues, calls_made, repairs, tools_open = None, ["no reply"], 0, 0, True
    trace["tool_calls"] = 0
    for _ in range(MAX_TOOL_CALLS + MAX_REPAIRS + 4):
        t = time.time()
        options = {**options, "timeout": max(60, deadline + FINAL_GRACE - t)}
        response = litellm.completion(messages=messages, **options,
                                      **({"tools": TOOLS} if tools_open else {}))
        choice = response.choices[0]
        msg = choice.message
        calls = msg.tool_calls or []
        turn = {"seconds": round(time.time() - t, 1), "content": msg.content,
                "reasoning": getattr(msg, "reasoning_content", None),
                "finish_reason": choice.finish_reason,
                "usage": {"prompt": response.usage.prompt_tokens,
                          "completion": response.usage.completion_tokens},
                "tool_calls": [{"name": call.function.name,
                                "arguments": call.function.arguments} for call in calls]}
        trace["turns"].append(turn)
        assistant = {"role": "assistant", "content": msg.content or ""}
        if calls:
            assistant["tool_calls"] = [call.model_dump() for call in calls]
        messages.append(assistant)
        if calls:
            turn["tool_results"] = []
            for call in calls:
                try:
                    result = call_tool(call.function.name,
                                       json.loads(call.function.arguments or "{}"))
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    result = f"error: {exc}"
                turn["tool_results"].append(result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            calls_made += len(calls)
            trace["tool_calls"] = calls_made
            if tools_open:
                spent = ("Tool budget" if calls_made >= MAX_TOOL_CALLS
                         else "Time budget" if time.time() >= deadline
                         else "Context budget" if response.usage.prompt_tokens > CONTEXT_LIMIT
                         else None)
                if spent:
                    tools_open = False
                    trace["budget_exhausted"] = spent.split()[0].lower()
                    messages.append({"role": "user",
                                     "content": f"{spent} reached. Return the JSON verdict now."})
            continue
        verdict = parse_verdict(msg.content)
        issues = check(verdict)
        if not issues or repairs >= MAX_REPAIRS:
            break
        repairs += 1
        trace["repairs"].append(issues)
        messages.append({"role": "user", "content": "Correct the JSON verdict:\n- "
                         + "\n- ".join(issues)})
    return verdict, issues


def triage_one(case: str, record: dict, profile: str, settings: dict,
               api_base: str, run_dir: Path) -> dict:
    options = model_options(profile, settings, api_base)
    trace = {"case": case, "turns": [], "repairs": [], "error": None}
    verdict, issues = None, ["no reply"]
    start = time.time()
    messages = []
    try:
        if record.get("base_commit") and not record.get("base_date"):
            r = git("log", "-1", "--format=%cs", record["base_commit"])
            if r.returncode and resolve(record["base_commit"]):
                r = git("log", "-1", "--format=%cs", record["base_commit"])
            record = {**record, "base_date": r.stdout.strip()} if r.stdout.strip() else record
        messages = [{"role": "system", "content": PROMPT.read_text()},
                    {"role": "user", "content": "Enriched PR record:\n" + render_record(case, record)}]
        verdict, issues = converse(messages, options,
                                   lambda name, args: run_tool(name, args, record),
                                   lambda reply: check_verdict(reply, case, record), trace,
                                   deadline=start + MAX_TOOL_SECONDS)
    except Exception as exc:
        trace["error"] = f"{type(exc).__name__}: {exc}"
    trace.update({"seconds": round(time.time() - start, 1), "issues": issues,
                  "messages": messages})
    (run_dir / f"{case}.trace.json").write_text(json.dumps(trace, indent=2, default=str))
    output = verdict if isinstance(verdict, dict) and not issues else {
        "pr": int(case), "kind": "error", "reason": trace["error"] or "; ".join(issues)}
    (run_dir / f"{case}.json").write_text(json.dumps(output, indent=2) + "\n")
    return {"case": case, "verdict": f"{output.get('kind')} {output.get('confidence', '')}",
            "seconds": trace["seconds"], "error": trace["error"], "issues": issues}


def run(args) -> None:
    import litellm  # noqa: F401 - fail before starting a run if the client is absent

    profiles = json.loads(MODELS.read_text())
    if args.profile not in profiles or args.profile.startswith("_"):
        raise SystemExit(f"unknown profile: {args.profile}")
    cases = [str(c) for c in args.case or []]
    if args.gold:
        cases += list(load_gold())
    if args.all:
        cases += sorted((p.stem for p in ENRICHED.glob("*.json") if p.stem.isdecimal()), key=int)
    cases = list(dict.fromkeys(cases))
    if not cases:
        raise SystemExit("give --case, --gold and/or --all")
    records = {case: load_record(case) for case in cases}
    if args.blind:
        records = {case: blind_record(rec) for case, rec in records.items()}
    try:
        with urlopen(args.api_base.rstrip("/") + "/models", timeout=8) as response:
            served = {model["id"] for model in json.load(response).get("data", [])}
    except (OSError, URLError, ValueError) as exc:
        raise SystemExit(f"model server unavailable at {args.api_base}: {exc}") from exc
    if args.profile not in served:
        raise SystemExit(f"model server does not serve {args.profile}; available: {sorted(served)}")
    if args.out:
        # cluster/node_worker.py: one PR per call, verdicts side by side in
        # <run>/triage/, a manifest per call
        run_dir = Path(args.out)
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = run_dir / f"manifest-{'-'.join(cases)[:60]}-{datetime.now():%Y%m%d-%H%M%S}.json"
    else:
        run_dir = RUNS / f"{datetime.now():%Y-%m-%d_%H%M%S}_{args.profile}{'_blind' if args.blind else ''}"
        run_dir.mkdir(parents=True, exist_ok=False)
        manifest_file = run_dir / "manifest.json"
    prompt = PROMPT.read_text()
    if not args.out:
        (run_dir / PROMPT.name).write_text(prompt)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    manifest = {"profile": args.profile, "api_base": args.api_base, "blind": args.blind,
                "sampling": profiles[args.profile].get("sampling"),
                "max_tokens": profiles[args.profile].get("max_tokens"),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "git_commit": commit, "cases": cases,
                "started": datetime.now().isoformat(timespec="seconds")}
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"run dir: {run_dir}  prompt sha256: {manifest['prompt_sha256'][:10]}", flush=True)
    with futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        jobs = [pool.submit(triage_one, case, records[case], args.profile,
                            profiles[args.profile], args.api_base, run_dir) for case in cases]
        for job in futures.as_completed(jobs):
            result = job.result()
            print(f"{result['case']}: {result['verdict']} ({result['seconds']} s) "
                  f"{result['error'] or '; '.join(result['issues'])}", flush=True)
    manifest["finished"] = datetime.now().isoformat(timespec="seconds")
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
    if args.gold and not args.out:
        eval_run(run_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--profile", required=True)
    run_parser.add_argument("--case", nargs="+", help="PR numbers")
    run_parser.add_argument("--gold", action="store_true", help="the partial gold's cases, then score")
    run_parser.add_argument("--all", action="store_true", help="every record in the enriched directory")
    run_parser.add_argument("--blind", action="store_true", help="hide explicit links (tracked only)")
    run_parser.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    run_parser.add_argument("--parallel", type=int, default=4)
    run_parser.add_argument("--out", help="write the verdicts straight into this folder "
                            "(cluster/node_worker.py: <run>/triage)")
    eval_parser = commands.add_parser("eval")
    eval_parser.add_argument("run_dir", nargs="?")
    args = parser.parse_args()
    if args.command == "run":
        run(args)
    else:
        path = Path(args.run_dir) if args.run_dir else max(RUNS.iterdir())
        eval_run(path)


if __name__ == "__main__":
    main()
