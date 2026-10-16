#!/usr/bin/env python3
"""Run NuttX PR-pair triage against a served open-weight model and score it.

    python scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --gold
    python scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --case apps-2327
    python scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --apps-pr 2327 --kernel-pr 11889
    python scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --pairs-file candidates/nuttx/pairs.jsonl
    python scripts/triage_nuttx_prs.py eval triage/nuttx_runs/<run>
    python scripts/triage_nuttx_prs.py leads     # collect QEMU-only leads from all runs

Per-apps-PR search: the model classifies one apps PR and finds its kernel
PRs itself (docs/nuttx_apps_triage.md, gold triage/nuttx_apps_gold.json).

    python scripts/triage_nuttx_prs.py apps --profile qwen38-nvfp4 --gold
    python scripts/triage_nuttx_prs.py apps --profile qwen38-nvfp4 --gold --blind
    python scripts/triage_nuttx_prs.py apps --profile qwen38-nvfp4 --apps-pr 3790 2327
    python scripts/triage_nuttx_prs.py apps --profile qwen38-nvfp4 --all-ostest
    python scripts/triage_nuttx_prs.py gold-promote generated/nuttx__nuttx-20112
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import contextlib
import copy
import hashlib
import json
import re
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import nuttx_candidates as candidates
import nuttx_config
from nuttx_candidates import (APPS, DATA, ENRICHED, KERNEL, KERNEL_LINK, enrich,
                              full_pr)

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "docs" / "nuttx_pr_triage.md"
GOLD = ROOT / "triage" / "nuttx_gold.json"
APPS_PROMPT = ROOT / "docs" / "nuttx_apps_triage.md"
APPS_GOLD = ROOT / "triage" / "nuttx_apps_gold.json"
MODELS = ROOT / "triage" / "models.json"
RUNS = ROOT / "triage" / "nuttx_runs"
# Pairs rejected only because their test needs a QEMU target, kept for when a
# QEMU runner exists.
QEMU_LEADS = ROOT / "triage" / "nuttx_qemu_leads.jsonl"
QEMU_PLATFORM = re.compile(r"qemu-[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+")
LEADS_LOCK = threading.Lock()
# Full bare clones; the tools read and search them at a PR's pinned commits.
CLONES = {"kernel": DATA / "kernel.git", "apps": DATA / "apps.git"}
CLONE_URLS = {"kernel": "https://github.com/apache/nuttx.git",
              "apps": "https://github.com/apache/nuttx-apps.git"}
GIT_LOCKS = {repo: threading.Lock() for repo in CLONES}
_WHERE = {
    "repo": {"type": "string", "enum": ["apps", "kernel"]},
    "kernel_pr": {"type": "integer", "description": "Required if several kernel PRs are candidates"},
    "ref": {"type": "string", "enum": ["base", "head"],
            "description": "The PR's base commit (before it) or head commit (its last commit)"}}
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file in the apps or kernel repository at this PR's base or head commit.",
        "parameters": {"type": "object", "properties": {
            **_WHERE,
            "path": {"type": "string", "description": "Repository-relative file path, e.g. testing/ostest/ostest_main.c"},
            "start_line": {"type": "integer", "description": "1-based; default 1"},
            "max_lines": {"type": "integer", "description": "Default 800"}},
            "required": ["repo", "path", "ref"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List a directory in the apps or kernel repository at this PR's base or head commit.",
        "parameters": {"type": "object", "properties": {
            **_WHERE,
            "path": {"type": "string", "description": "Repository-relative directory; empty for the top level"}},
            "required": ["repo", "path", "ref"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents with an extended regular expression in the apps or "
                       "kernel repository at this PR's base or head commit, under a directory or "
                       "across the whole tree. Returns up to 300 matching lines as path:line:text.",
        "parameters": {"type": "object", "properties": {
            **_WHERE,
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Repository-relative directory; empty for the whole tree"}},
            "required": ["repo", "pattern", "path", "ref"]}}},
]
MAX_TOOL_ROUNDS = 8
MAX_REPAIRS = 2

# The per-apps-PR search reads either repository at any revision, searches
# history and follows references; no kernel PR is known up front.
_ANYWHERE = {
    "repo": {"type": "string", "enum": ["apps", "kernel"]},
    "ref": {"type": "string",
            "description": "base or head (the apps PR's commits, or a kernel PR's with "
                           "kernel_pr), a commit SHA (full or short), or a date YYYY-MM-DD "
                           "(the default branch as of the end of that day, UTC)"},
    "kernel_pr": {"type": "integer",
                  "description": "With repo kernel and ref base or head: the kernel PR"}}
APPS_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file in the apps or kernel repository at any revision.",
        "parameters": {"type": "object", "properties": {
            **_ANYWHERE,
            "path": {"type": "string", "description": "Repository-relative file path"},
            "start_line": {"type": "integer", "description": "1-based; default 1"},
            "max_lines": {"type": "integer", "description": "Default 800"}},
            "required": ["repo", "path", "ref"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List a directory in the apps or kernel repository at any revision.",
        "parameters": {"type": "object", "properties": {
            **_ANYWHERE,
            "path": {"type": "string", "description": "Repository-relative directory; empty for the top level"}},
            "required": ["repo", "path", "ref"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents with an extended regular expression in the apps or "
                       "kernel repository at any revision, under a directory or across the "
                       "whole tree. Returns up to 300 matching lines as path:line:text.",
        "parameters": {"type": "object", "properties": {
            **_ANYWHERE,
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Repository-relative directory; empty for the whole tree"}},
            "required": ["repo", "pattern", "path", "ref"]}}},
    {"type": "function", "function": {
        "name": "git_log",
        "description": "Search the default-branch history of the kernel (or apps) repository, "
                       "newest first. Combine any of: since/until dates; symbol (commits that "
                       "add or remove that exact string, like git log -S); regex (commits whose "
                       "diff adds or removes a matching line, like git log -G); message (a "
                       "case-insensitive extended regex over commit messages); paths. Returns "
                       "'<sha> <commit date> <author> | <subject>' and the changed files per commit. "
                       "symbol and regex searches are slow over the whole history: narrow them "
                       "with dates or paths. Open a commit with open_ref to see its diff and PR.",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string", "enum": ["kernel", "apps"], "description": "Default kernel"},
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
        "description": "Follow a reference: a PR or issue in apache/nuttx or apache/nuttx-apps "
                       "(apache/nuttx#123, a github.com URL, or a bare number with repo), a "
                       "commit URL, or a commit SHA (full or short). A PR or issue returns its "
                       "title, body, comments, changed files and patches, the links it mentions "
                       "and its cross-references; a commit returns its message, files, diff and "
                       "the merged PR(s) it belongs to. Output is bounded.",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "repo": {"type": "string", "enum": ["kernel", "apps"],
                     "description": "For a bare number, or a SHA found in both repositories"}},
            "required": ["ref"]}}},
    {"type": "function", "function": {
        "name": "check_config",
        "description": "Configure sim:nsh exactly as the benchmark does (ostest on), add your "
                       "config_overrides, run make olddefconfig, and report for the kernel PR's "
                       "head (fixed) and base (old), each with the apps PR's head: which "
                       "overrides stuck, which were dropped and the dependencies they lack, and "
                       "whether each changed ostest source is built and its entry function "
                       "called. Takes about a minute.",
        "parameters": {"type": "object", "properties": {
            "kernel_pr": {"type": "integer"},
            "config_overrides": {"type": "object", "additionalProperties": {"type": "string"},
                                 "description": "e.g. {\"CONFIG_FOO\": \"y\"}; {} for none"},
            "test_paths": {"type": "array", "items": {"type": "string"},
                           "description": "Default: the apps PR's changed testing/ostest/ files"},
            "platform": {"type": "string",
                         "description": "board:config; default sim:nsh, or the QEMU pair's board"}},
            "required": ["kernel_pr", "config_overrides"]}}},
]
REPO_NAMES = {"kernel": KERNEL, "apps": APPS}
KINDS = ("kernel_test", "test_only", "apps_fix", "qemu_only")
LEVELS = ("low", "medium", "high")
TAGS = ("bug_test", "behavior_change")
# Prompt tokens after which the search must stop calling tools and answer:
# vLLM serves 262144 tokens and the reply may need 32768.
CONTEXT_LIMIT = 190_000
FINAL_GRACE = 600        # seconds past the wall-time budget for the final answer
RECORD_LIMIT = 160_000   # characters of the rendered apps record
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA = re.compile(r"[0-9a-fA-F]{7,40}")
# A hex word with a digit and a letter: a commit hash, not a number or word.
HASH = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b")
ITEM_URL = re.compile(r"github\.com/apache/(nuttx|nuttx-apps)/(?:pull|pulls|issues)/(\d+)")
COMMIT_URL = re.compile(r"github\.com/apache/(nuttx|nuttx-apps)/commit/([0-9a-fA-F]{7,40})")
SCOPED_REF = re.compile(r"#?(?:apache/)?(nuttx|nuttx-apps)[#/](\d+)")


def load_gold() -> dict[str, str]:
    data = json.loads(GOLD.read_text())
    return {**{case: "accept" for case in data["accept"]},
            **{case: "reject" for case in data["reject"]}}


def pair_cases(path: Path) -> list[str]:
    cases = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            apps, kernel = row["apps_pr"], row["kernel_pr"]
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"{path}: line {number}: invalid pair row") from exc
        if any(type(value) is not int or value <= 0 for value in (apps, kernel)):
            raise ValueError(f"{path}: line {number}: PR numbers must be positive integers")
        case = f"apps-{apps}-kernel-{kernel}"
        if row.get("case", case) != case:
            raise ValueError(f"{path}: line {number}: case does not match PR numbers")
        cases.append(case)
    if not cases:
        raise ValueError(f"{path}: no PR pairs")
    return list(dict.fromkeys(cases))


def load_record(case: str) -> dict:
    pair = re.fullmatch(r"apps-(\d+)-kernel-(\d+)", case)
    if pair:
        return enrich(int(pair[1]), kernel_number=int(pair[2]))
    path = ENRICHED / f"{case}.json"
    if path.exists():
        return json.loads(path.read_text())
    kind, _, number = case.partition("-")
    if not number.isdecimal() or kind not in ("apps", "kernel"):
        raise ValueError(f"invalid case ID: {case}")
    if kind == "apps":
        old_path = ENRICHED / f"{number}.json"
        if not old_path.exists():
            raise FileNotFoundError(f"{old_path}: run nuttx_candidates.py enrich --pr {number}")
        return json.loads(old_path.read_text())
    # Kernel-only negatives do not enter the apps-first discovery stream.
    record = {"project": "nuttx", "apps": None,
              "kernel_candidates": [full_pr(KERNEL, int(number))],
              "test_contents": {}, "pairing_source": "kernel_only"}
    ENRICHED.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def render_record(case: str, record: dict, field_limit: int = 8000) -> str:
    """Bound individual blobs while preserving both PRs and every changed path."""
    def bounded(value):
        if isinstance(value, str):
            return (value[:field_limit] + f"\n[truncated {len(value) - field_limit} characters]"
                    if len(value) > field_limit else value)
        if isinstance(value, list):
            return [bounded(item) for item in value]
        if isinstance(value, dict):
            return {key: bounded(item) for key, item in value.items()}
        return value

    # These API fields are redundant with changed paths and comments, and can
    # contain long user profile payloads unrelated to the PR.
    def pr_view(pr):
        if pr is None:
            return None
        keys = ("repo", "number", "url", "title", "body", "created_at", "merged_at", "labels",
                "base_commit", "head_commit", "merge_commit_sha", "commits",
                "files", "files_truncated", "linked_issues", "base_sources",
                "sim_nsh_defconfig", "timeline")
        view = {key: pr[key] for key in keys if key in pr}
        for key in ("comments", "reviews", "review_comments"):
            view[key] = [{field: item.get(field) for field in
                          ("body", "state", "path", "diff_hunk") if item.get(field)}
                         for item in pr.get(key, [])[:30]]
        return bounded(view)

    data = {"case": case, "pairing_source": record.get("pairing_source"),
            "apps": pr_view(record.get("apps")),
            "kernel_candidates": [pr_view(pr) for pr in record.get("kernel_candidates", [])],
            "changed_test_contents": bounded(record.get("test_contents") or {})}
    if "kernel_candidates" not in record:   # an apps-only record: no partner chosen
        del data["pairing_source"], data["kernel_candidates"]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def render_apps_record(case: str, record: dict) -> str:
    """An apps-only record, shrinking long fields until it fits RECORD_LIMIT."""
    for limit in (8000, 3000, 1000, 300):
        text = render_record(case, record, limit)
        if len(text) <= RECORD_LIMIT:
            return text
    # A PR with very many files: keep the paths, drop patches outside testing/.
    apps = {**record["apps"], "files": [
        {**f, "patch": f.get("patch") if f["filename"].startswith("testing/") else None}
        for f in record["apps"]["files"]]}
    return render_record(case, {**record, "apps": apps}, 300)[:RECORD_LIMIT]


def parse_verdict(content: str | None) -> dict | None:
    if not content:
        return None
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def check_verdict(verdict: dict | None, case: str, record: dict) -> list[str]:
    if verdict is None:
        return ["reply must contain one JSON object"]
    issues = []
    if verdict.get("case") != case:
        issues.append(f"case must be {case}")
    if verdict.get("verdict") not in ("accept", "reject"):
        issues.append("verdict must be accept or reject")
    if not str(verdict.get("reason") or "").strip():
        issues.append("reason is required")
    if verdict.get("verdict") != "accept":
        lead = verdict.get("qemu_lead")
        if lead is not None:
            kernels = {pr["number"] for pr in record.get("kernel_candidates", [])}
            if not isinstance(lead, dict) or not QEMU_PLATFORM.fullmatch(str(lead.get("platform"))):
                issues.append("qemu_lead must be null or name a concrete qemu-<board>:<config> platform")
            elif lead.get("kernel_pr") not in kernels:
                issues.append("qemu_lead.kernel_pr must select a supplied kernel candidate")
            elif lead.get("confidence") not in ("high", "medium", "low") or \
                    not str(lead.get("why") or "").strip():
                issues.append("qemu_lead needs confidence (high, medium, or low) and why")
        return issues
    apps = record.get("apps")
    kernels = {pr["number"]: pr for pr in record.get("kernel_candidates", [])}
    selected = verdict.get("kernel_pr")
    kernel = kernels.get(selected) if isinstance(selected, int) else None
    if apps is None:
        issues.append("accept requires an apps test PR")
    if kernel is None:
        issues.append("kernel_pr must select a supplied kernel candidate")
    for name, pr in (("apps", apps), ("kernel", kernel)):
        if pr and not pr.get("merged_at"):
            issues.append(f"{name} PR must be merged")
        if pr and (not pr.get("base_commit") or not pr.get("head_commit")):
            issues.append(f"{name} PR requires base and head commits")
    if verdict.get("platform") != "sim:nsh":
        issues.append("accept only pairs whose test runs on sim:nsh; if only a QEMU target "
                      "can run it, reject and describe that target in qemu_lead")
    if verdict.get("evidence_status") not in ("observed", "needs_validation"):
        issues.append("evidence_status must be observed or needs_validation")
    elif verdict["evidence_status"] == "observed" and not record.get("validation"):
        issues.append("no validation trace was supplied; use needs_validation")
    if "issue_url" not in verdict:
        issues.append("issue_url is required (null if unknown)")
    if verdict.get("confidence") not in ("high", "medium", "low"):
        issues.append("confidence must be high, medium, or low")
    if verdict.get("change_type") not in ("fix", "feature"):
        issues.append("change_type must be fix or feature")
    for field in ("problem_statement", "detecting_test"):
        if not isinstance(verdict.get(field), str) or not verdict[field].strip():
            issues.append(f"{field} is required")
    return issues + check_recipe(verdict, apps, kernel, record)


def check_recipe(verdict: dict, apps: dict | None, kernel: dict | None,
                 record: dict) -> list[str]:
    """The generator fields of one apps/kernel pair: paths, detecting tests,
    failure type, config and readiness."""
    issues = []
    for field, pr, name in (("test_paths", apps, "apps"), ("fix_paths", kernel, "kernel")):
        paths = verdict.get(field)
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            issues.append(f"{field} must be a non-empty list of paths")
            continue
        changed = {f["filename"] for f in (pr or {}).get("files", [])}
        missing = [p for p in paths if p not in changed]
        if missing:
            # Say which paths are wrong and which are allowed: a bare list of the
            # wrong paths was once read as the list to use.
            issues.append(f"{field} lists paths the {name} PR did not change; remove "
                          f"{missing}. Choose only from the files the {name} PR changed: "
                          f"{sorted(changed)}")
    names = verdict.get("fail_to_pass")
    if not isinstance(names, list) or not names or not all(
            isinstance(name, str) and name.strip() for name in names):
        issues.append("fail_to_pass must name at least one detecting test function")
    else:
        contents = record.get("test_contents") or {}
        paths = verdict.get("test_paths")
        paths = paths if isinstance(paths, list) else []
        sources = "\n".join(contents[path] for path in paths
                            if isinstance(path, str) and isinstance(contents.get(path), str))
        if sources:
            missing = [name for name in names
                       if not re.fullmatch(r"[A-Za-z_]\w*", name)
                       or not re.search(rf"\b{re.escape(name)}\b", sources)]
            if missing:
                issues.append(f"fail_to_pass names not found in selected test sources: {missing}")
    if verdict.get("fail_type") not in ("runtime_assert", "compile_error", "runtime_other"):
        issues.append("fail_type must be runtime_assert, compile_error, or runtime_other")
    if verdict.get("fail_type") == "compile_error":
        source = verdict.get("compile_test_source")
        token = verdict.get("missing_api")
        content = ((record.get("test_contents") or {}).get(source)
                   if isinstance(source, str) else None)
        if not isinstance(source, str) or not isinstance(verdict.get("test_paths"), list) or \
                source not in verdict["test_paths"]:
            issues.append("compile_test_source must be a selected changed test path")
        if not isinstance(token, str) or not token.strip() or \
                (isinstance(content, str) and token not in content):
            issues.append("missing_api must name an interface used by compile_test_source")
    overrides = verdict.get("config_overrides")
    if not isinstance(overrides, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in overrides.items()):
        issues.append("config_overrides must be a mapping of config names to values")
    supported = (verdict.get("platform") == "sim:nsh"
                 and isinstance(verdict.get("test_paths"), list)
                 and all(path.startswith("testing/ostest/")
                         for path in verdict["test_paths"] if isinstance(path, str))
                 and verdict.get("fail_type") in ("runtime_assert", "compile_error"))
    readiness = verdict.get("readiness")
    if not supported:
        if readiness != "needs_fixture":
            issues.append("readiness must be needs_fixture for this test and platform")
    elif readiness not in ("needs_reviewed_recipe", "ready_to_generate"):
        issues.append("readiness must be needs_reviewed_recipe or ready_to_generate")
    return issues


def proposed(verdict: dict) -> list[dict]:
    """The pairs a per-apps-PR verdict sends to generation: high or medium."""
    pairs = verdict.get("pairs") if isinstance(verdict, dict) else None
    return [pair for pair in pairs if isinstance(pair, dict)
            and pair.get("confidence") in ("high", "medium")] if isinstance(pairs, list) else []


def check_apps_verdict(verdict: dict | None, case: str, record: dict) -> list[str]:
    """A per-apps-PR verdict: its kind, its search outcome and every pair.
    Low pairs are recorded only, so they need just a kernel PR and evidence;
    high and medium pairs need what the generator needs."""
    if verdict is None:
        return ["reply must contain one JSON object"]
    issues = []
    if verdict.get("case") != case:
        issues.append(f"case must be {case}")
    kind = verdict.get("kind")
    if kind not in KINDS:
        issues.append(f"kind must be one of {', '.join(KINDS)}")
    if not str(verdict.get("reason") or "").strip():
        issues.append("reason is required")
    if verdict.get("kernel_search") not in ("found", "nothing_found"):
        issues.append("kernel_search must be found or nothing_found")
    searched = verdict.get("searched")
    if not isinstance(searched, list) or not searched or not all(
            isinstance(step, str) and step.strip() for step in searched):
        issues.append("searched must list what you searched, one string per search")
    pairs = verdict.get("pairs")
    if not isinstance(pairs, list):
        return issues + ["pairs must be a list (empty if none)"]
    seen = set()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            issues.append(f"pairs[{index}] must be an object")
            continue
        number = pair.get("kernel_pr")
        if type(number) is not int or number <= 0:
            issues.append(f"pairs[{index}].kernel_pr must be a kernel PR number")
            continue
        where = f"pairs[{index}] (kernel #{number})"
        if number in seen:
            issues.append(f"{where}: list each kernel PR once")
        seen.add(number)
        if pair.get("confidence") not in LEVELS:
            issues.append(f"{where}: confidence must be high, medium, or low")
        evidence = pair.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item.strip() for item in evidence):
            issues.append(f"{where}: evidence must list the facts behind the pair")
        if pair.get("confidence") in ("high", "medium"):
            issues.extend(f"{where}: {issue}" for issue in check_pair(pair, record))
    sent = proposed(verdict)
    if kind in ("test_only", "apps_fix") and sent:
        issues.append(f"kind {kind} has no kernel PR to pair; give its kernel leads "
                      "confidence low, or choose another kind")
    if kind == "qemu_only" and any(pair.get("platform") == "sim:nsh" for pair in sent):
        issues.append("kind qemu_only cannot have a sim:nsh pair at high or medium")
    if verdict.get("kernel_search") == "found" and not sent:
        issues.append("kernel_search found needs a high or medium pair; otherwise "
                      "use nothing_found")
    if verdict.get("kernel_search") == "nothing_found" and sent:
        issues.append("kernel_search nothing_found cannot have high or medium pairs")
    return issues


def check_pair(pair: dict, record: dict) -> list[str]:
    number = pair["kernel_pr"]
    kernel = next((pr for pr in record.get("kernel_candidates", [])
                   if pr["number"] == number), None)
    if kernel is None:
        return [f"apache/nuttx#{number} is not a pull request; open your candidate "
                "with open_ref and give its PR number"]
    issues = []
    apps = record.get("apps")
    for name, pr in (("apps", apps), ("kernel", kernel)):
        if not pr.get("merged_at"):
            issues.append(f"{name} PR must be merged")
        if not pr.get("base_commit") or not pr.get("head_commit"):
            issues.append(f"{name} PR requires base and head commits")
    if pair.get("tag") not in TAGS:
        issues.append("tag must be bug_test or behavior_change")
    if pair.get("platform") != "sim:nsh" and \
            not QEMU_PLATFORM.fullmatch(str(pair.get("platform"))):
        issues.append("platform must be sim:nsh or a concrete qemu-<board>:<config>")
    if not isinstance(pair.get("problem_statement"), str) or not pair["problem_statement"].strip():
        issues.append("problem_statement is required")
    if isinstance(pair.get("config_overrides"), dict):
        issues.extend(nuttx_config.check_overrides(pair["config_overrides"]))
    also = pair.get("also_needs", [])
    if not isinstance(also, list):
        issues.append("also_needs must be a list (empty if none)")
        also = []
    for extra in also:
        other = extra.get("kernel_pr") if isinstance(extra, dict) else None
        evidence = extra.get("evidence") if isinstance(extra, dict) else None
        if type(other) is not int or other == number:
            issues.append("each also_needs entry needs another kernel_pr number")
        elif not isinstance(evidence, list) or not evidence or not all(
                isinstance(item, str) and item.strip() for item in evidence):
            issues.append(f"also_needs #{other}: evidence must list why it is needed")
        elif not any(pr["number"] == other and pr.get("merged_at")
                     for pr in record.get("kernel_candidates", [])):
            issues.append(f"also_needs #{other} must be a merged apache/nuttx PR")
    return issues + check_recipe(pair, apps, kernel, record)


def git(repo: str, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    clone = CLONES[repo]
    with GIT_LOCKS[repo]:
        if not clone.exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--bare", CLONE_URLS[repo], str(clone)],
                           capture_output=True, text=True, timeout=3600, check=True)
    # Reads run in parallel: a slow history search must not hold up the
    # other cases. Only fetches write to the clone.
    with GIT_LOCKS[repo] if args[:1] == ("fetch",) else contextlib.nullcontext():
        return subprocess.run(["git", "-C", str(clone), *args],
                              capture_output=True, text=True, timeout=timeout)


def pinned_commit(args: dict, record: dict) -> tuple[str, str] | str:
    """The (repo, commit) a tool call names, fetched if missing, or an error."""
    repo = args.get("repo")
    if repo == "apps":
        pr = record.get("apps")
    elif repo == "kernel":
        candidates = record.get("kernel_candidates", [])
        pr = next((p for p in candidates if p["number"] == args.get("kernel_pr")), None)
        if pr is None and len(candidates) == 1:
            pr = candidates[0]
    else:
        return "error: repo must be apps or kernel"
    if not pr:
        return "error: repository PR is unavailable or ambiguous; pass kernel_pr"
    sha = {"base": pr.get("base_commit"), "head": pr.get("head_commit")}.get(args.get("ref"))
    if not sha:
        return "error: ref must be base or head, and this PR must have that commit"
    # PR branch commits live on forks, not in the clone's branches; GitHub
    # serves them by ID.
    if git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
        git(repo, "fetch", "-q", "origin", sha, timeout=600)
        if git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
            return f"error: commit {sha[:12]} is unavailable"
    return repo, sha


def git_date(value: str, end: bool = False) -> str:
    """A YYYY-MM-DD date as git reads it: the start or the end of the day, UTC."""
    return f"{value} {'23:59:59' if end else '00:00:00'} +0000"


def resolve_commit(repo: str, ref: str) -> str | None:
    """The full SHA of a commit in a clone, fetching a full SHA it lacks."""
    def parse():
        r = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
        return r.stdout.strip() if r.returncode == 0 else None
    sha = parse()
    if sha is None and len(ref) == 40:
        git(repo, "fetch", "-q", "origin", ref, timeout=600)
        sha = parse()
    return sha


def any_commit(args: dict, record: dict) -> tuple[str, str] | str:
    """The (repo, commit) of a free-exploration tool call: a PR's base or
    head, a commit SHA, or the default branch as of a date."""
    repo, ref = args.get("repo"), str(args.get("ref") or "").strip()
    if repo not in CLONES:
        return "error: repo must be apps or kernel"
    if ref in ("base", "head"):
        if repo == "kernel":
            number = args.get("kernel_pr")
            if type(number) is not int:
                return "error: kernel base or head needs kernel_pr"
            if load_kernel(record, number) is None:
                return f"error: apache/nuttx#{number} is not a pull request"
        return pinned_commit(args, record)
    if DATE.fullmatch(ref):
        r = git(repo, "rev-list", "-1", "--first-parent", f"--before={git_date(ref, end=True)}",
                "HEAD")
        return (repo, r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else \
            f"error: no {repo} commit before {ref}"
    if SHA.fullmatch(ref):
        sha = resolve_commit(repo, ref)
        return (repo, sha) if sha else f"error: commit {ref} is not in the {repo} repository"
    return "error: ref must be base, head, a commit SHA, or a date YYYY-MM-DD"


def load_kernel(record: dict, number: int) -> dict | None:
    """A kernel PR the search names, fetched once and kept in the record so
    the tools and the checks can use it; None if it is not a PR."""
    known = record.setdefault("kernel_candidates", [])
    kernel = next((pr for pr in known if pr["number"] == number), None)
    if kernel is None:
        try:
            kernel = candidates.kernel_record(number)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):   # an issue number, or no such PR
                return None
            raise
        known.append(kernel)
    return kernel


def hide_links(text: str, numbers=()) -> str:
    """Text with kernel PR links, kernel URLs, commit hashes and the given
    kernel PR numbers replaced, for a blind search."""
    text = re.sub(r"https?://github\.com/apache/nuttx/\S+", "[hidden]", text)
    text = HASH.sub("[hidden]", KERNEL_LINK.sub("[hidden]", text))
    for number in numbers:
        text = re.sub(rf"(?<!\w){number}(?!\w)", "[hidden]", text)
    return text


def blind_record(record: dict, numbers) -> dict:
    """An apps record without its explicit links to kernel PRs and commits."""
    blind = copy.deepcopy(record)
    apps = blind["apps"]
    for key in ("title", "body"):
        apps[key] = hide_links(apps.get(key) or "", numbers)
    for commit in apps.get("commits", []):
        commit["message"] = hide_links(commit.get("message") or "", numbers)
    for key in ("comments", "review_comments", "reviews"):
        for item in apps.get(key, []):
            item["body"] = hide_links(item.get("body") or "", numbers)
    for file in apps.get("files", []):
        if file.get("patch"):
            file["patch"] = hide_links(file["patch"], numbers)
    apps["timeline"] = [event for event in apps.get("timeline", [])
                        if event.get("event") == "cross-referenced"
                        and event.get("repo") != KERNEL]
    blind["blind"] = sorted(numbers)
    return blind


def clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n[... {len(text) - limit} more characters]"


def mentioned_links(text: str) -> list[str]:
    links = [f"{KERNEL}#{n}" for n in candidates.kernel_pr_refs(text)]
    links += [f"{APPS}#{n}" for n in candidates.apps_pr_refs(text)]
    links += [f"{KERNEL}/issues/{n}" for n in
              re.findall(r"github\.com/apache/nuttx/issues/(\d+)", text)]
    links += [f"commit {sha}" for sha in HASH.findall(text)]
    return list(dict.fromkeys(links))


def render_item(item: dict, budget: int = 20000) -> str:
    """A PR or issue from open_ref, bounded."""
    kind = item["kind"]
    lines = [f"{kind.upper()} {item['repo']}#{item['number']}: {item['title']}", item.get("url", "")]
    if kind == "pr":
        lines.append(f"created_at: {item.get('created_at')}  "
                     f"merged_at: {item.get('merged_at')}  base_commit: {item.get('base_commit')}  "
                     f"head_commit: {item.get('head_commit')}  merge_commit: "
                     f"{item.get('merge_commit_sha')}")
    else:
        lines.append(f"state: {item.get('state')}  created_at: {item.get('created_at')}")
    lines += ["", "Body:", clip(item.get("body"), 6000)]
    texts = [item.get("body") or ""]
    if kind == "pr":
        lines += ["", "Commits:"]
        for commit in item.get("commits", [])[:30]:
            lines.append(f"- {commit['sha'][:12]} {clip(commit.get('message'), 800)}")
            texts.append(commit.get("message") or "")
        for issue in item.get("linked_issues", []):
            lines.append(f"Linked issue {KERNEL}#{issue['number']}: {issue['title']} {issue['url']}")
    comments = [c for key in ("comments", "review_comments", "reviews")
                for c in item.get(key, []) if c.get("body")]
    if comments:
        lines += ["", "Comments:"]
        lines += [f"- {clip(c['body'], 800)}" for c in comments[:15]]
        texts += [c["body"] for c in comments]
    links = mentioned_links("\n".join(texts))
    if links:
        lines += ["", "Links mentioned: " + ", ".join(links[:60])]
    refs = item.get("timeline", [])
    if refs:
        lines += ["", "Cross-references:"]
        for event in refs[:40]:
            if event["event"] == "cross-referenced":
                state = "merged" if event.get("merged_at") else event.get("state")
                lines.append(f"- {(event.get('created_at') or '')[:10]} {event['repo']}#"
                             f"{event['number']} ({'PR' if event.get('is_pr') else 'issue'}, "
                             f"{state}): {event.get('title')}")
            else:
                lines.append(f"- {(event.get('created_at') or '')[:10]} commit "
                             f"{event['commit_id'][:12]} mentions it")
    if kind == "pr":
        lines += ["", "Files:"]
        lines += [f"- {f['status']} {f['filename']} +{f.get('additions')} -{f.get('deletions')}"
                  for f in item.get("files", [])[:150]]
        text = "\n".join(lines)
        for f in item.get("files", []):
            if f.get("patch") and len(text) < budget:
                text += f"\n\n--- {f['filename']}\n" + clip(f["patch"], 4000)
        return clip(text, budget)
    return clip("\n".join(lines), budget)


def render_commit(commit: dict, budget: int = 20000) -> str:
    prs = ", ".join(f"{commit['repo']}#{pr['number']} \"{pr['title']}\" "
                    f"(merged {pr.get('merged_at')})" for pr in commit["prs"])
    lines = [f"COMMIT {commit['repo']} {commit['sha']}", commit.get("url") or "",
             f"date: {commit.get('date')}  parents: {', '.join(p[:12] for p in commit['parents'])}",
             f"Pull requests: {prs or 'none found'}", "", "Message:",
             clip(commit.get("message"), 3000), "", "Files:"]
    lines += [f"- {f['status']} {f['filename']} +{f.get('additions')} -{f.get('deletions')}"
              for f in commit.get("files", [])[:150]]
    text = "\n".join(lines)
    for f in commit.get("files", []):
        if f.get("patch") and len(text) < budget:
            text += f"\n\n--- {f['filename']}\n" + clip(f["patch"], 4000)
    return clip(text, budget)


def open_ref(args: dict, record: dict) -> str:
    ref = str(args.get("ref") or "").strip()
    hint = args.get("repo")
    if hint not in (None, "kernel", "apps"):
        return "error: repo must be kernel or apps"
    found = COMMIT_URL.search(ref)
    is_commit = bool(found)
    found = found or ITEM_URL.search(ref) or SCOPED_REF.fullmatch(ref)
    if found:
        repo = "kernel" if found[1] == "nuttx" else "apps"
        value = found[2]
    elif re.fullmatch(r"#?\d+", ref):
        if hint is None:
            return "error: a bare number needs repo (kernel or apps)"
        repo, value = hint, ref.lstrip("#")
    elif SHA.fullmatch(ref):
        repo, value, is_commit = hint, ref, True
    else:
        return "error: ref must be a PR or issue reference, a commit URL, or a commit SHA"
    if is_commit:   # find which clone has it, else ask GitHub
        repos = [repo] if repo else ["kernel", "apps"]
        local = [(name, sha) for name in repos if (sha := resolve_commit(name, value))]
        if len(local) > 1:
            return "error: this short SHA is in both repositories; pass repo"
        try:
            if local:
                repo, sha = local[0]
                commit = candidates.commit_record(REPO_NAMES[repo], sha)
            else:
                for repo in repos:
                    with contextlib.suppress(RuntimeError):
                        commit = candidates.commit_record(REPO_NAMES[repo], value)
                        break
                else:
                    return f"error: commit {value} is in neither repository"
        except RuntimeError as exc:
            return f"error: {exc}"
        text = render_commit(commit)
    else:
        try:
            item = candidates.issue_or_pr(REPO_NAMES[repo], int(value))
        except RuntimeError as exc:
            return f"error: {exc}"
        if repo == "kernel" and item["kind"] == "pr":
            load_kernel(record, item["number"])
        text = render_item(item)
    # A blind search must find kernel PRs by search, not by apps-side text.
    return hide_links(text, record["blind"]) if "blind" in record and repo == "apps" else text


def git_log(args: dict, record: dict) -> str:
    repo = args.get("repo") or "kernel"
    if repo not in CLONES:
        return "error: repo must be kernel or apps"
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
            command.append(f"--{key}={git_date(args[key], end)}")
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
    if command == base and not paths:
        return "error: give at least one of since, until, symbol, regex, message, paths"
    try:
        r = git(repo, *command, "HEAD", "--", *[p.strip("/") for p in paths if p.strip("/")],
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
    text = clip(text, 16000)
    return hide_links(text, record["blind"]) if "blind" in record and repo == "apps" else text


def check_config(args: dict, record: dict) -> str:
    number = args.get("kernel_pr")
    if type(number) is not int:
        return "error: kernel_pr must be a kernel PR number"
    kernel = load_kernel(record, number)
    if kernel is None:
        return f"error: apache/nuttx#{number} is not a pull request"
    overrides = args.get("config_overrides")
    overrides = {} if overrides is None else overrides
    problems = nuttx_config.check_overrides(overrides)
    if problems:
        return "error: " + "; ".join(problems)
    paths = args.get("test_paths") or [f["filename"] for f in record["apps"]["files"]
                                       if f["filename"].startswith("testing/ostest/")
                                       and f["status"] != "removed"]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return "error: test_paths must be a list of paths"
    platform = args.get("platform") or "sim:nsh"
    return nuttx_config.describe(nuttx_config.check_pair(
        record["apps"], kernel, overrides, paths, record.get("test_contents") or {},
        platform=platform))


def run_tool(name: str, args: dict, record: dict, free: bool = False) -> str:
    """One tool call. Pair mode pins reads to the supplied PRs' commits; free
    (per-apps-PR) mode reads any revision and adds git_log and open_ref."""
    if free and name == "git_log":
        return git_log(args, record)
    if free and name == "open_ref":
        return open_ref(args, record)
    if free and name == "check_config":
        return check_config(args, record)
    path = args.get("path") or ""
    if not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts:
        return "error: invalid repository-relative path"
    path = path.strip().strip("/")
    if name not in ("read_file", "list_dir", "grep"):
        return f"error: unknown tool {name}"
    if name == "read_file" and not path:
        return "error: read_file needs a file path"
    where = any_commit(args, record) if free else pinned_commit(args, record)
    if isinstance(where, str):
        return where
    repo, sha = where

    if name == "read_file":
        r = git(repo, "show", f"{sha}:{path}")
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
        r = git(repo, "ls-tree", f"{sha}:{path}" if path else sha)
        if r.returncode:
            return f"error: {r.stderr.strip()[:300]}"
        return "\n".join(f"{line.split()[1]:<5} {line.split(chr(9))[1]}"
                         for line in r.stdout.splitlines()) or "(empty)"

    r = git(repo, "grep", "-n", "-E", "-e", args.get("pattern") or "", sha, "--", path or ".")
    if r.returncode not in (0, 1):
        return f"error: {r.stderr.strip()[:300]}"
    hits = [line.split(":", 1)[1] for line in r.stdout.splitlines()]   # drop "<sha>:"
    return ("\n".join(hits[:300]) + (f"\n... {len(hits) - 300} more" if len(hits) > 300 else "")
            or "(no matches)")


def score(gold: dict[str, str], verdicts: dict[str, dict]) -> dict:
    rows = []
    for case, expected in gold.items():
        verdict = verdicts.get(case, {})
        got = verdict.get("verdict", "missing")
        rows.append({"case": case, "expected": expected, "got": got,
                     "correct": got == expected,
                     "kernel_pr": verdict.get("kernel_pr"),
                     "reason": verdict.get("reason", ""),
                     "platform": verdict.get("platform", "")})
    return {"correct": sum(row["correct"] for row in rows),
            "scored": len(rows), "rows": rows}


def eval_run(run_dir: Path) -> dict:
    gold = load_gold()
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        cases = set(json.loads(manifest_path.read_text()).get("cases", []))
        gold = {case: expected for case, expected in gold.items() if case in cases}
    if not gold:
        raise ValueError("no gold cases in this run")
    expected_kernels = json.loads(GOLD.read_text())["accept"]
    verdicts = {case: json.loads((run_dir / f"{case}.json").read_text())
                for case in gold if (run_dir / f"{case}.json").exists()}
    result = score(gold, verdicts)
    for row in result["rows"]:
        if row["case"] in expected_kernels:
            kernel_number = expected_kernels[row["case"]]["kernel_pr"]
            row["expected_kernel_pr"] = kernel_number
            row["kernel_match"] = (row["kernel_pr"] == row["expected_kernel_pr"]
                                   if row["got"] == "accept" else None)
    lines = [f"# NuttX triage: {run_dir.name}", "",
             f"Verdicts: {result['correct']}/{result['scored']} correct", "",
             "Kernel PR is reported for accepted gold cases but does not affect "
             "the verdict score.",
             "", "| Case | Expected | Got | Correct | Kernel PR (expected) |",
             "|---|---|---|---|---|"]
    for row in result["rows"]:
        lines.append(f"| {row['case']} | {row['expected']} | {row['got']} | "
                     f"{'yes' if row['correct'] else 'no'} | {row['kernel_pr'] or ''}"
                     f"{' (' + str(row['expected_kernel_pr']) + ')' if 'expected_kernel_pr' in row else ''} |")
    result["run"] = run_dir.name
    (run_dir / "scorecard.json").write_text(json.dumps(result, indent=2) + "\n")
    (run_dir / "scorecard.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return result


def model_options(profile: str, settings: dict, api_base: str) -> dict:
    sampling = dict(settings.get("sampling", {}))
    extra = {key: sampling.pop(key) for key in ("top_k", "min_p") if key in sampling}
    options = {"model": f"openai/{profile}", "api_base": api_base, "api_key": "local",
               "timeout": 3600, "num_retries": 0,
               "max_tokens": settings.get("max_tokens", 32768), **sampling}
    if extra:
        options["extra_body"] = extra
    return options


def converse(messages: list, options: dict, tools: list, call_tool, check, trace: dict,
             max_rounds: int | None = None, max_calls: int | None = None,
             deadline: float | None = None) -> tuple[dict | None, list[str]]:
    """Let the model call tools, then check its JSON reply and ask for
    repairs. Tools are withdrawn once a budget runs out: tool rounds, tool
    calls, wall time (a time.time() deadline) or context."""
    import litellm

    verdict, issues, rounds, calls_made, repairs, tools_open = None, ["no reply"], 0, 0, 0, True
    trace["tool_calls"] = 0
    for _ in range((max_rounds or max_calls) + MAX_REPAIRS + 2):
        t = time.time()
        if deadline:   # no single call may run far past the wall-time budget
            options = {**options, "timeout": max(60, deadline + FINAL_GRACE - t)}
        response = litellm.completion(messages=messages, **options,
                                      **({"tools": tools} if tools_open else {}))
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
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": result})
            rounds += 1
            calls_made += len(calls)
            trace["tool_calls"] = calls_made
            if tools_open:
                spent = ("Tool budget" if (max_rounds and rounds >= max_rounds)
                         or (max_calls and calls_made >= max_calls)
                         else "Time budget" if deadline and time.time() >= deadline
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
    messages = [{"role": "system", "content": PROMPT.read_text()},
                {"role": "user", "content": "Enriched PR record:\n" + render_record(case, record)}]
    trace = {"case": case, "turns": [], "repairs": [], "error": None}
    verdict, issues = None, ["no reply"]
    start = time.time()
    try:
        verdict, issues = converse(messages, options, TOOLS,
                                   lambda name, args: run_tool(name, args, record),
                                   lambda reply: check_verdict(reply, case, record), trace,
                                   max_rounds=MAX_TOOL_ROUNDS)
    except Exception as exc:
        trace["error"] = f"{type(exc).__name__}: {exc}"
    trace.update({"seconds": round(time.time() - start, 1), "issues": issues,
                  "messages": messages})
    (run_dir / f"{case}.trace.json").write_text(json.dumps(trace, indent=2, default=str))
    output = verdict if isinstance(verdict, dict) and not issues else {
        "case": case, "verdict": "error", "reason": trace["error"] or "; ".join(issues)}
    (run_dir / f"{case}.json").write_text(json.dumps(output, indent=2) + "\n")
    if output.get("verdict") == "reject" and isinstance(output.get("qemu_lead"), dict):
        with LEADS_LOCK, open(run_dir / "qemu_leads.jsonl", "a") as leads:
            leads.write(json.dumps({"case": case, **output["qemu_lead"],
                                    "reason": output.get("reason")}) + "\n")
    return {"case": case, "verdict": output.get("verdict"), "seconds": trace["seconds"],
            "error": trace["error"], "issues": issues}


def run(args) -> None:
    import litellm  # noqa: F401 - fail before starting a run if the client is absent

    profiles = json.loads(MODELS.read_text())
    if args.profile not in profiles or args.profile.startswith("_"):
        raise SystemExit(f"unknown profile: {args.profile}")
    if (args.apps_pr is None) != (args.kernel_pr is None):
        raise SystemExit("--apps-pr and --kernel-pr must be supplied together")
    pair_case = ([f"apps-{args.apps_pr}-kernel-{args.kernel_pr}"]
                 if args.apps_pr is not None else [])
    try:
        pool_cases = pair_cases(args.pairs_file) if args.pairs_file else []
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    cases = list(dict.fromkeys((args.case or []) + pair_case + pool_cases
                              + (list(load_gold()) if args.gold else [])))
    if not cases:
        raise SystemExit("give --case, --apps-pr with --kernel-pr, --pairs-file, and/or --gold")
    try:
        with urlopen(args.api_base.rstrip("/") + "/models", timeout=8) as response:
            served = {model["id"] for model in json.load(response).get("data", [])}
    except (OSError, URLError, ValueError) as exc:
        raise SystemExit(f"model server unavailable at {args.api_base}: {exc}") from exc
    if args.profile not in served:
        raise SystemExit(f"model server does not serve {args.profile}; available: {sorted(served)}")
    records = {case: load_record(case) for case in cases}
    run_dir = RUNS / f"{datetime.now():%Y-%m-%d_%H%M%S}_{args.profile}"
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt = PROMPT.read_text()
    (run_dir / "nuttx_pr_triage.md").write_text(prompt)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    manifest = {"profile": args.profile, "api_base": args.api_base,
                "sampling": profiles[args.profile].get("sampling"),
                "max_tokens": profiles[args.profile].get("max_tokens"),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "git_commit": commit, "cases": cases,
                "pairs_file": str(args.pairs_file) if args.pairs_file else None,
                "started": datetime.now().isoformat(timespec="seconds")}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        jobs = [pool.submit(triage_one, case, records[case], args.profile,
                            profiles[args.profile], args.api_base, run_dir) for case in cases]
        for job in futures.as_completed(jobs):
            result = job.result()
            print(f"{result['case']}: {result['verdict']} ({result['seconds']} s) "
                  f"{result['error'] or '; '.join(result['issues'])}", flush=True)
    manifest["finished"] = datetime.now().isoformat(timespec="seconds")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.gold:
        eval_run(run_dir)


def triage_apps_one(case: str, record: dict, profile: str, settings: dict, api_base: str,
                    run_dir: Path, max_calls: int, max_minutes: float) -> dict:
    """Classify one apps PR and search for its kernel PRs."""
    options = model_options(profile, settings, api_base)
    messages = [{"role": "system", "content": APPS_PROMPT.read_text()},
                {"role": "user", "content": "Apps PR record:\n" + render_apps_record(case, record)}]
    record.setdefault("kernel_candidates", [])   # kernel PRs opened during the search
    trace = {"case": case, "turns": [], "repairs": [], "error": None}

    def check(reply):
        for pair in proposed(reply or {}):   # fetch the PRs the checks compare against
            extras = pair.get("also_needs") if isinstance(pair.get("also_needs"), list) else []
            for number in [pair.get("kernel_pr")] + [e.get("kernel_pr") for e in extras
                                                      if isinstance(e, dict)]:
                if type(number) is int:
                    with contextlib.suppress(RuntimeError):
                        load_kernel(record, number)
        return check_apps_verdict(reply, case, record)

    verdict, issues = None, ["no reply"]
    start = time.time()
    try:
        verdict, issues = converse(messages, options, APPS_TOOLS,
                                   lambda name, args: run_tool(name, args, record, free=True),
                                   check, trace, max_calls=max_calls,
                                   deadline=start + max_minutes * 60)
    except Exception as exc:
        trace["error"] = f"{type(exc).__name__}: {exc}"
        if time.time() >= start + max_minutes * 60:   # a call ran past the budget
            trace["budget_exhausted"] = "time"
    trace.update({"seconds": round(time.time() - start, 1), "issues": issues,
                  "messages": messages})
    (run_dir / f"{case}.trace.json").write_text(json.dumps(trace, indent=2, default=str))
    budget = {"tool_calls": trace.get("tool_calls"), "seconds": trace["seconds"],
              "exhausted": trace.get("budget_exhausted")}
    if isinstance(verdict, dict) and not issues:
        output = {**verdict, "budget": budget}
    else:
        # No valid verdict: an explicit, recorded outcome rather than a crash.
        output = {"case": case, "kind": "budget_exhausted" if budget["exhausted"] else "error",
                  "pairs": [], "reason": trace["error"] or "; ".join(issues), "budget": budget}
    (run_dir / f"{case}.json").write_text(json.dumps(output, indent=2) + "\n")
    leads = [{"case": case, "platform": pair["platform"], "kernel_pr": pair["kernel_pr"],
              "confidence": pair["confidence"], "why": " ".join(pair["evidence"]),
              "reason": output.get("reason")}
             for pair in output["pairs"] if QEMU_PLATFORM.fullmatch(str(pair.get("platform")))]
    if leads:
        with LEADS_LOCK, open(run_dir / "qemu_leads.jsonl", "a") as out:
            out.writelines(json.dumps(lead) + "\n" for lead in leads)
    return {"case": case, "kind": output.get("kind"), "seconds": trace["seconds"],
            "tool_calls": trace.get("tool_calls"), "error": trace["error"], "issues": issues,
            "pairs": [f"#{p.get('kernel_pr')} {p.get('confidence')}"
                      for p in output["pairs"] if isinstance(p, dict)]}


def load_apps_gold(path: Path | None = None) -> dict:
    return json.loads((path or APPS_GOLD).read_text())


def run_apps(args) -> None:
    import litellm  # noqa: F401 - fail before starting a run if the client is absent

    profiles = json.loads(MODELS.read_text())
    if args.profile not in profiles or args.profile.startswith("_"):
        raise SystemExit(f"unknown profile: {args.profile}")
    gold = load_apps_gold()["cases"]
    # A blind gold run needs a pair to find: the cases with a must_find.
    gold_cases = [case for case, entry in gold.items()
                  if not args.blind or entry.get("must_find")]
    numbers = list(args.apps_pr or [])
    if args.all_ostest:
        numbers += candidates.ostest_apps_prs()
    cases = list(dict.fromkeys([f"apps-{n}" for n in numbers]
                               + (gold_cases if args.gold else [])))
    if not cases:
        raise SystemExit("give --apps-pr, --all-ostest and/or --gold")
    try:
        with urlopen(args.api_base.rstrip("/") + "/models", timeout=8) as response:
            served = {model["id"] for model in json.load(response).get("data", [])}
    except (OSError, URLError, ValueError) as exc:
        raise SystemExit(f"model server unavailable at {args.api_base}: {exc}") from exc
    if args.profile not in served:
        raise SystemExit(f"model server does not serve {args.profile}; available: {sorted(served)}")
    records = {}
    for case in cases:
        record = candidates.apps_record(int(case.split("-")[1]))
        if args.blind:   # hide general link forms, and the known partners by number
            record = blind_record(record, [number for item in gold.get(case, {}).get("must_find", [])
                                           for number in [item["kernel_pr"],
                                                          *item.get("also_needs", [])]])
        records[case] = record
    run_dir = RUNS / (f"{datetime.now():%Y-%m-%d_%H%M%S}_{args.profile}_apps"
                      + ("_blind" if args.blind else ""))
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt = APPS_PROMPT.read_text()
    (run_dir / APPS_PROMPT.name).write_text(prompt)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    manifest = {"mode": "apps", "blind": args.blind, "profile": args.profile,
                "api_base": args.api_base,
                "sampling": profiles[args.profile].get("sampling"),
                "max_tokens": profiles[args.profile].get("max_tokens"),
                "budget": {"max_tool_calls": args.max_tool_calls,
                           "max_minutes": args.max_minutes},
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "git_commit": commit, "cases": cases,
                "started": datetime.now().isoformat(timespec="seconds")}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        jobs = [pool.submit(triage_apps_one, case, records[case], args.profile,
                            profiles[args.profile], args.api_base, run_dir,
                            args.max_tool_calls, args.max_minutes) for case in cases]
        for job in futures.as_completed(jobs):
            result = job.result()
            print(f"{result['case']}: {result['kind']} {result['pairs']} "
                  f"({result['seconds']} s, {result['tool_calls']} tool calls) "
                  f"{result['error'] or '; '.join(result['issues'])}", flush=True)
    # One line per proposed pair: high and medium go on to generation and
    # validation, low ones are only recorded.
    with open(run_dir / "pairs.jsonl", "w") as out:
        for case in cases:
            verdict = json.loads((run_dir / f"{case}.json").read_text())
            for pair in verdict.get("pairs", []):
                out.write(json.dumps({"case": case, "apps_pr": int(case.split("-")[1]),
                                      **{key: pair.get(key) for key in (
                                          "kernel_pr", "confidence", "tag", "platform",
                                          "fail_type", "readiness")}}) + "\n")
    manifest["finished"] = datetime.now().isoformat(timespec="seconds")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if set(cases) & set(gold):
        eval_apps_run(run_dir)


def pair_matches(pair: dict | None, expected: dict) -> bool:
    if pair is None:
        return False
    platform = expected.get("platform")
    if platform == "qemu":
        return bool(QEMU_PLATFORM.fullmatch(str(pair.get("platform"))))
    # The tag is recorded, not scored.
    return not platform or pair.get("platform") == platform


def score_apps(gold: dict, verdicts: dict[str, dict], config_problems=None) -> dict:
    """Score per-apps-PR verdicts against a partial gold: each case needs the
    right kind, every must_find pair at high or medium (with its also_needs
    PRs, and a config that gives the listed settings and runs the changed
    tests), and no must_not_propose pair at high or medium. Other high or
    medium pairs are unknown: listed for validation, never penalized.
    config_problems(case, pair, required) lists a pair's config problems."""
    rows, extras = [], []
    for case, entry in gold.items():
        verdict = verdicts.get(case, {})
        sent = {pair.get("kernel_pr"): pair for pair in proposed(verdict)}
        kinds = entry.get("kind")
        kinds = [kinds] if isinstance(kinds, str) else kinds
        must_find = entry.get("must_find", [])
        missing = [item["kernel_pr"] for item in must_find
                   if not pair_matches(sent.get(item["kernel_pr"]), item)]
        config = {}
        for item in must_find:
            pair = sent.get(item["kernel_pr"])
            if item["kernel_pr"] in missing:
                continue
            found = []
            listed = {e.get("kernel_pr") for e in pair.get("also_needs") or []
                      if isinstance(e, dict)}
            found += [f"also_needs lacks #{number}" for number in item.get("also_needs", [])
                      if number not in listed]
            if "config" in item and config_problems:
                found += config_problems(case, pair, item["config"],
                                         item.get("config_platform") or pair.get("platform"))
            if found:
                config[item["kernel_pr"]] = found
        must_not = [item["kernel_pr"] for item in entry.get("must_not_propose", [])]
        violations = [number for number in must_not if number in sent]
        known = {item["kernel_pr"] for item in must_find} | set(must_not)
        unknown = [{"case": case, "kernel_pr": number, "confidence": pair.get("confidence"),
                    "platform": pair.get("platform")}
                   for number, pair in sent.items() if number not in known]
        extras.extend(unknown)
        kind_ok = not kinds or verdict.get("kind") in kinds
        rows.append({"case": case, "expected_kind": kinds, "got_kind": verdict.get("kind", "missing"),
                     "kind_ok": kind_ok, "found": len(must_find) - len(missing),
                     "must_find": len(must_find), "missing": missing, "violations": violations,
                     "unknown": [item["kernel_pr"] for item in unknown],
                     "pairs": [f"#{p.get('kernel_pr')} {p.get('confidence')}"
                               for p in verdict.get("pairs", []) if isinstance(p, dict)],
                     "budget": verdict.get("budget") or {}, "config": config,
                     "tags": {p.get("kernel_pr"): p.get("tag") for p in sent.values()},
                     "correct": kind_ok and not missing and not violations and not config})
    return {"correct": sum(row["correct"] for row in rows), "scored": len(rows),
            "must_find_found": sum(row["found"] for row in rows),
            "must_find_total": sum(row["must_find"] for row in rows),
            "violations": sum(len(row["violations"]) for row in rows),
            "unknown_extras": extras, "rows": rows}


def eval_apps_run(run_dir: Path, gold_path: Path | None = None) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    gold = {case: entry for case, entry in load_apps_gold(gold_path)["cases"].items()
            if case in manifest.get("cases", [])}
    if not gold:
        raise ValueError("no gold cases in this run")
    verdicts = {case: json.loads((run_dir / f"{case}.json").read_text())
                for case in gold if (run_dir / f"{case}.json").exists()}
    # The effective configuration of each found pair, kept with the run so
    # a later eval (e.g. off Scholar) does not need the NuttX image.
    checks_path = run_dir / "config_checks.json"
    checks = json.loads(checks_path.read_text()) if checks_path.exists() else {}

    def config_problems(case, pair, required, platform):
        overrides = pair.get("config_overrides") or {}
        invalid = nuttx_config.check_overrides(overrides)
        if invalid:
            return invalid
        key = f"{case}#{pair['kernel_pr']}"
        if key not in checks:
            record = candidates.apps_record(int(case.split("-")[1]))
            checks[key] = nuttx_config.check_pair(
                record["apps"], candidates.kernel_record(pair["kernel_pr"]), overrides,
                pair.get("test_paths") or [], record["test_contents"], report=list(required),
                sides=("fixed",), platform=platform)["fixed"]
            checks_path.write_text(json.dumps(checks, indent=2) + "\n")
        return nuttx_config.problems(checks[key], required)

    result = score_apps(gold, verdicts, config_problems)
    blind = " (blind: explicit links hidden)" if manifest.get("blind") else ""
    lines = [f"# NuttX per-apps-PR triage: {run_dir.name}{blind}", "",
             f"Cases: {result['correct']}/{result['scored']} correct",
             f"must_find recall: {result['must_find_found']}/{result['must_find_total']}",
             f"must_not_propose violations: {result['violations']}",
             f"Config or also_needs problems: {sum(bool(r['config']) for r in result['rows'])}",
             f"Unknown high/medium pairs, for validation: {len(result['unknown_extras'])}",
             f"Budget exhausted: {sum(bool(r['budget'].get('exhausted')) for r in result['rows'])}",
             "", "| Case | Kind (expected) | Proposed | Missing | Violations | Config | Unknown | "
             "Tags | Tool calls / s | Correct |", "|---|---|---|---|---|---|---|---|---|---|"]
    for row in result["rows"]:
        expected = "/".join(row["expected_kind"] or ["any"])
        lines.append(f"| {row['case']} | {row['got_kind']} ({expected}) | "
                     f"{', '.join(row['pairs'])} | {', '.join(map(str, row['missing']))} | "
                     f"{', '.join(map(str, row['violations']))} | "
                     f"{'; '.join(f'#{k}: ' + ', '.join(v) for k, v in row['config'].items())} | "
                     f"{', '.join(map(str, row['unknown']))} | "
                     f"{', '.join(f'#{k} {v}' for k, v in row['tags'].items() if v)} | "
                     f"{row['budget'].get('tool_calls')} / {row['budget'].get('seconds')}"
                     f"{' (' + row['budget']['exhausted'] + ' budget)' if row['budget'].get('exhausted') else ''} | "
                     f"{'yes' if row['correct'] else 'no'} |")
    if result["unknown_extras"]:
        lines += ["", "## Unknown pairs to validate", ""]
        lines += [f"- {item['case']} -> apache/nuttx#{item['kernel_pr']} "
                  f"({item['confidence']}, {item['platform']})" for item in result["unknown_extras"]]
    result["run"] = run_dir.name
    (run_dir / "scorecard.json").write_text(json.dumps(result, indent=2) + "\n")
    (run_dir / "scorecard.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return result


def promote(instance_dirs: list[Path], gold_path: Path | None = None) -> None:
    """Fold validation results into the apps gold. A valid (fails before,
    passes after) pair becomes must_find. An invalid one only goes to
    needs_review: it can fail for config or runner reasons, so a person
    decides whether it belongs in must_not_propose."""
    path = gold_path or APPS_GOLD
    gold = load_apps_gold(path)
    for inst in instance_dirs:
        meta = json.loads((inst / "metadata.json").read_text())
        report = json.loads((inst / "validation.json").read_text())
        apps, kernel = meta["apps_pr"], meta["kernel_pr"]
        case = f"apps-{apps}"
        entry = gold["cases"].setdefault(case, {"kind": "kernel_test", "must_find": [],
                                                "must_not_propose": []})
        note = {"apps_pr": apps, "kernel_pr": kernel, "instance": inst.name,
                "valid": report.get("valid"), "reason": report.get("reason"),
                "date": date.today().isoformat()}
        listed_not = any(item["kernel_pr"] == kernel for item in entry.get("must_not_propose", []))
        if report.get("valid") and not listed_not:
            if not any(item["kernel_pr"] == kernel for item in entry.setdefault("must_find", [])):
                entry["must_find"].append({
                    "kernel_pr": kernel, "platform": meta.get("sim_config", "sim:nsh"),
                    "evidence": f"validated {note['date']} ({inst.name}): {report.get('reason')}"})
            print(f"{case}: #{kernel} -> must_find")
        else:
            if listed_not:
                note["reason"] = f"valid, but listed in must_not_propose: {note['reason']}"
            review = gold.setdefault("needs_review", [])
            if not any(item["apps_pr"] == apps and item["kernel_pr"] == kernel
                       and item["reason"] == note["reason"] for item in review):
                review.append(note)
            print(f"{case}: #{kernel} -> needs_review ({note['reason']})")
    path.write_text(json.dumps(gold, indent=2) + "\n")


def collect_leads() -> list[dict]:
    """Latest QEMU-only lead per case and kernel PR across all runs (an apps PR
    can have several), written to QEMU_LEADS."""
    latest = {}
    for path in sorted(RUNS.glob("*/qemu_leads.jsonl")):   # run names sort by time
        for line in path.read_text().splitlines():
            if line.strip():
                lead = json.loads(line)
                latest[lead["case"], lead["kernel_pr"]] = {**lead, "run": path.parent.name}
    leads = [latest[key] for key in sorted(latest)]
    QEMU_LEADS.write_text("".join(json.dumps(lead) + "\n" for lead in leads))
    for lead in leads:
        print(f"{lead['case']}: {lead['platform']} ({lead['confidence']}) {lead['why']}")
    print(f"{len(leads)} leads -> {QEMU_LEADS.relative_to(ROOT)}")
    return leads


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--profile", required=True)
    run_parser.add_argument("--case", nargs="+")
    run_parser.add_argument("--apps-pr", type=int)
    run_parser.add_argument("--kernel-pr", type=int)
    run_parser.add_argument("--pairs-file", type=Path)
    run_parser.add_argument("--gold", action="store_true")
    run_parser.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    run_parser.add_argument("--parallel", type=int, default=4)
    apps_parser = commands.add_parser("apps", help="per-apps-PR classification and kernel search")
    apps_parser.add_argument("--profile", required=True)
    apps_parser.add_argument("--apps-pr", type=int, nargs="+")
    apps_parser.add_argument("--all-ostest", action="store_true",
                             help="every merged apps PR that changes testing/ostest/")
    apps_parser.add_argument("--gold", action="store_true")
    apps_parser.add_argument("--blind", action="store_true",
                             help="hide explicit kernel links and commit hashes")
    apps_parser.add_argument("--max-tool-calls", type=int, default=40)
    apps_parser.add_argument("--max-minutes", type=float, default=30)
    apps_parser.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    apps_parser.add_argument("--parallel", type=int, default=4)
    eval_parser = commands.add_parser("eval")
    eval_parser.add_argument("run_dir", nargs="?")
    commands.add_parser("leads")
    promote_parser = commands.add_parser(
        "gold-promote", help="add validation results to the per-apps-PR gold")
    promote_parser.add_argument("instance", type=Path, nargs="+",
                                help="instance directories with metadata.json and validation.json")
    args = parser.parse_args()
    if args.command == "run":
        run(args)
    elif args.command == "apps":
        run_apps(args)
    elif args.command == "leads":
        collect_leads()
    elif args.command == "gold-promote":
        promote(args.instance)
    else:
        path = Path(args.run_dir) if args.run_dir else max(RUNS.iterdir())
        manifest = path / "manifest.json"
        if manifest.exists() and json.loads(manifest.read_text()).get("mode") == "apps":
            eval_apps_run(path)
        else:
            eval_run(path)


if __name__ == "__main__":
    main()
