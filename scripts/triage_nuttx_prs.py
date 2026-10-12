#!/usr/bin/env python3
"""Run NuttX PR-pair triage against a served open-weight model and score it.

    python scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --gold
    python scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --case apps-2327
    python scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --apps-pr 2327 --kernel-pr 11889
    python scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --pairs-file candidates/nuttx/pairs.jsonl
    python scripts/triage_nuttx_prs.py eval triage/nuttx_runs/<run>
    python scripts/triage_nuttx_prs.py leads     # collect QEMU-only leads from all runs
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from nuttx_candidates import DATA, ENRICHED, KERNEL, enrich, full_pr

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "docs" / "nuttx_pr_triage.md"
GOLD = ROOT / "triage" / "nuttx_gold.json"
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
        keys = ("repo", "number", "url", "title", "body", "merged_at", "labels",
                "base_commit", "head_commit", "merge_commit_sha", "commits",
                "files", "files_truncated", "linked_issues", "base_sources",
                "sim_nsh_defconfig")
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
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


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


def git(repo: str, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    clone = CLONES[repo]
    with GIT_LOCKS[repo]:
        if not clone.exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--bare", CLONE_URLS[repo], str(clone)],
                           capture_output=True, text=True, timeout=3600, check=True)
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


def run_tool(name: str, args: dict, record: dict) -> str:
    path = args.get("path") or ""
    if not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts:
        return "error: invalid repository-relative path"
    path = path.strip().strip("/")
    if name not in ("read_file", "list_dir", "grep"):
        return f"error: unknown tool {name}"
    if name == "read_file" and not path:
        return "error: read_file needs a file path"
    where = pinned_commit(args, record)
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


def triage_one(case: str, record: dict, profile: str, settings: dict,
               api_base: str, run_dir: Path) -> dict:
    import litellm

    sampling = dict(settings.get("sampling", {}))
    extra = {key: sampling.pop(key) for key in ("top_k", "min_p") if key in sampling}
    options = {"model": f"openai/{profile}", "api_base": api_base, "api_key": "local",
               "timeout": 3600, "num_retries": 0,
               "max_tokens": settings.get("max_tokens", 32768), **sampling}
    if extra:
        options["extra_body"] = extra
    messages = [{"role": "system", "content": PROMPT.read_text()},
                {"role": "user", "content": "Enriched PR record:\n" + render_record(case, record)}]
    trace = {"case": case, "turns": [], "repairs": [], "error": None}
    verdict, issues, tool_rounds, repairs = None, ["no reply"], 0, 0
    start = time.time()
    try:
        for _ in range(MAX_TOOL_ROUNDS + MAX_REPAIRS + 2):
            t = time.time()
            response = litellm.completion(messages=messages, **options,
                                          **({"tools": TOOLS} if tool_rounds < MAX_TOOL_ROUNDS else {}))
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
                        result = run_tool(call.function.name,
                                          json.loads(call.function.arguments or "{}"), record)
                    except (ValueError, RuntimeError) as exc:
                        result = f"error: {exc}"
                    turn["tool_results"].append(result)
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": result})
                tool_rounds += 1
                if tool_rounds == MAX_TOOL_ROUNDS:
                    messages.append({"role": "user", "content": "Tool budget reached. Return the JSON verdict now."})
                continue
            verdict = parse_verdict(msg.content)
            issues = check_verdict(verdict, case, record)
            if not issues or repairs >= MAX_REPAIRS:
                break
            repairs += 1
            trace["repairs"].append(issues)
            messages.append({"role": "user", "content": "Correct the JSON verdict:\n- "
                             + "\n- ".join(issues)})
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


def collect_leads() -> list[dict]:
    """Latest QEMU-only lead per case across all runs, written to QEMU_LEADS."""
    latest = {}
    for path in sorted(RUNS.glob("*/qemu_leads.jsonl")):   # run names sort by time
        for line in path.read_text().splitlines():
            if line.strip():
                lead = json.loads(line)
                latest[lead["case"]] = {**lead, "run": path.parent.name}
    leads = [latest[case] for case in sorted(latest)]
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
    eval_parser = commands.add_parser("eval")
    eval_parser.add_argument("run_dir", nargs="?")
    commands.add_parser("leads")
    args = parser.parse_args()
    if args.command == "run":
        run(args)
    elif args.command == "leads":
        collect_leads()
    else:
        path = Path(args.run_dir) if args.run_dir else max(RUNS.iterdir())
        eval_run(path)


if __name__ == "__main__":
    main()
