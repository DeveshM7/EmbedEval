#!/usr/bin/env python3
"""Run Apache Mynewt PR triage against a served open-weight model and score it.

    python scripts/triage_mynewt_prs.py run --profile qwen38-nvfp4 --gold
    python scripts/triage_mynewt_prs.py run --profile qwen38-nvfp4 --case 3680 2157
    python scripts/triage_mynewt_prs.py eval triage/mynewt_runs/<run>

Records are the enriched PRs from `filter_candidates.py enrich --repo mynewt`,
the same files generate_instance.py reads, so a verdict and the instance built
from it always describe one record. On Scholar, copy them up; don't re-enrich.
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

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "docs" / "mynewt_pr_triage.md"
GOLD = ROOT / "triage" / "mynewt_gold.json"
MODELS = ROOT / "triage" / "models.json"
RUNS = ROOT / "triage" / "mynewt_runs"
ENRICHED = ROOT / "candidates" / "mynewt" / "enriched"
# Full bare clone (generate_instance.py's path); the tools read it at the PR's
# commits. PR branch commits that it lacks are fetched by ID.
CLONE = ROOT / "candidates" / "mynewt.git"
CLONE_URL = "https://github.com/apache/mynewt-core.git"
GIT_LOCK = threading.Lock()
_REF = {"ref": {"type": "string",
                "description": "'base' (before the PR), 'head' (its last commit), or the "
                               "SHA of one of the PR's commits"}}
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file in apache/mynewt-core at one of this PR's commits.",
        "parameters": {"type": "object", "properties": {
            **_REF,
            "path": {"type": "string", "description": "Repository-relative file path"},
            "start_line": {"type": "integer", "description": "1-based; default 1"},
            "max_lines": {"type": "integer", "description": "Default 800"}},
            "required": ["path", "ref"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List a directory in apache/mynewt-core at one of this PR's commits.",
        "parameters": {"type": "object", "properties": {
            **_REF,
            "path": {"type": "string", "description": "Repository-relative directory; empty for the top level"}},
            "required": ["path", "ref"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents with an extended regular expression in "
                       "apache/mynewt-core at one of this PR's commits, under a directory or "
                       "across the whole tree. Returns up to 300 matching lines as path:line:text.",
        "parameters": {"type": "object", "properties": {
            **_REF,
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Repository-relative directory; empty for the whole tree"}},
            "required": ["pattern", "path", "ref"]}}},
    {"type": "function", "function": {
        "name": "commit_diff",
        "description": "Show the diff one of this PR's commits made, optionally limited to "
                       "one path. Use it to see which commit changed tests and which changed "
                       "production code.",
        "parameters": {"type": "object", "properties": {
            "sha": {"type": "string", "description": "SHA of one of the PR's commits"},
            "path": {"type": "string", "description": "Optional repository-relative path"}},
            "required": ["sha"]}}},
]
MAX_TOOL_ROUNDS = 8
MAX_REPAIRS = 2
NAME = re.compile(r"[A-Za-z_]\w*/[A-Za-z_]\w*")


def load_gold() -> dict[str, str]:
    data = json.loads(GOLD.read_text())
    return {**{case: "accept" for case in data["accept"]},
            **{case: "reject" for case in data["reject"]}}


def load_record(case: str) -> dict:
    if not case.isdecimal():
        raise ValueError(f"invalid case ID {case!r}; use the PR number")
    path = ENRICHED / f"{case}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path}: run filter_candidates.py enrich --repo mynewt --pr {case}")
    return json.loads(path.read_text())


def is_test_path(path: str) -> bool:
    return any(part == "selftest" or part.startswith("selftest-") for part in path.split("/"))


def suite_sources(record: dict) -> dict[str, str]:
    """Every selftest package file at the PR's merged state, by repository path."""
    return {f"{root}/{name}": entry["content"]
            for root, suite in (record.get("test_suites") or {}).items()
            for name, entry in suite.get("files", {}).items()
            if isinstance(entry.get("content"), str)}


def render_record(case: str, record: dict, field_limit: int = 20000) -> str:
    """Bound individual blobs while keeping every changed path and suite file."""
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
            "head_commit", "commits", "files", "files_truncated", "linked_issues")
    view = {key: record[key] for key in keys if key in record}
    for key in ("comments", "reviews", "review_comments"):
        view[key] = [{field: item.get(field) for field in
                      ("body", "state", "path", "diff_hunk") if item.get(field)}
                     for item in record.get(key, [])[:30]]
    view["test_suites"] = {root: {name: entry.get("content") for name, entry in
                                  suite.get("files", {}).items()}
                           for root, suite in (record.get("test_suites") or {}).items()}
    return json.dumps({"case": case, **bounded(view)}, ensure_ascii=False,
                      separators=(",", ":"))


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
                f"test_suites sources: {unknown}. Use only TEST_SUITE and TEST_CASE names "
                f"defined in those sources"]
    return []


def check_verdict(verdict: dict | None, case: str, record: dict) -> list[str]:
    """Schema of docs/mynewt_pr_triage.md plus every check generate_instance.py
    would otherwise fail on, each with the exact change to make."""
    if verdict is None:
        return ["reply must contain exactly one JSON object and nothing else"]
    issues = []
    if verdict.get("pr") != int(case):
        issues.append(f"pr must be the integer {case}")
    if verdict.get("verdict") not in ("accept", "reject"):
        issues.append("verdict must be \"accept\" or \"reject\"")
    if not str(verdict.get("reason") or "").strip():
        issues.append("reason is required: one or two sentences")
    if verdict.get("confidence") not in ("high", "medium", "low"):
        issues.append("confidence must be \"high\", \"medium\", or \"low\"")
    if verdict.get("verdict") != "accept":
        return issues

    if not record.get("linked_issues"):
        issues.append("this record's linked_issues is empty, and an accept requires an "
                      "originating issue there; change verdict to \"reject\"")
    if verdict.get("change_type") not in ("fix", "feature"):
        issues.append("change_type must be \"fix\" or \"feature\"")
    if verdict.get("platform") != "native":
        issues.append("platform must be \"native\"; if the test cannot run on the native "
                      "BSP, change verdict to \"reject\"")
    for field in ("problem_statement", "detecting_test"):
        if not isinstance(verdict.get(field), str) or not verdict[field].strip():
            issues.append(f"{field} is required and must be a non-empty string")

    sources = "\n".join(suite_sources(record).values())
    issues += check_names("fail_to_pass", verdict.get("fail_to_pass"), sources, True)
    issues += check_names("pass_to_pass", verdict.get("pass_to_pass"), sources, False)
    baseline = verdict.get("baseline_tests")
    if not isinstance(baseline, list) or not all(isinstance(n, str) and NAME.fullmatch(n)
                                                  for n in baseline):
        issues.append("baseline_tests must be a list (empty if none) of suite/testcase "
                      "names that pass before the test patch is applied")
    f2p, p2p = verdict.get("fail_to_pass"), verdict.get("pass_to_pass")
    if isinstance(f2p, list) and isinstance(p2p, list):
        both = sorted(set(map(str, f2p)) & set(map(str, p2p)))
        if both:
            issues.append(f"remove {both} from pass_to_pass: a testcase belongs in "
                          f"fail_to_pass or pass_to_pass, not both")

    mode = verdict.get("failure_mode", "runtime")
    if mode not in ("runtime", "compile"):
        issues.append("failure_mode must be \"runtime\" or \"compile\"")
    changed = {f["filename"] for f in record.get("files", [])}
    if mode == "compile":
        source, api = verdict.get("compile_test_source"), verdict.get("missing_api")
        content = suite_sources(record).get(source) if isinstance(source, str) else None
        if not isinstance(source, str) or source not in changed or not is_test_path(source) \
                or Path(source).suffix not in (".c", ".cc", ".cpp"):
            issues.append("compile_test_source must be the repository path of a C source "
                          "under a selftest directory that this PR changed")
        if not isinstance(api, str) or not api.strip() or \
                (isinstance(content, str) and api not in content):
            issues.append("missing_api must be the exact identifier or header name, as "
                          "written in compile_test_source, that the unfixed code lacks")

    commits = [c["sha"] for c in record.get("commits", [])]
    test_commit = verdict.get("test_commit")
    support = verdict.get("test_support_paths") or []
    if test_commit is not None and test_commit not in commits:
        issues.append("test_commit must be the full SHA of one of the PR's commits, or be "
                      f"omitted; the PR's commits are {commits}")
    if not isinstance(support, list) or not all(isinstance(p, str) for p in support):
        issues.append("test_support_paths must be a list of paths, or be omitted")
    else:
        if support and test_commit is None:
            issues.append("test_support_paths requires test_commit; set test_commit to the "
                          "last commit before the production fix, or remove test_support_paths")
        unknown = [p for p in support if p not in changed]
        if unknown:
            issues.append(f"remove {unknown} from test_support_paths: list only files this "
                          f"PR changed outside its selftest directories")
    flags = verdict.get("compatibility_cflags", [])
    if not isinstance(flags, list) or not all(isinstance(f, str) and f.startswith("-")
                                              for f in flags):
        issues.append("compatibility_cflags must be a list of compiler flags (usually [])")
    if not isinstance(verdict.get("gold_patch_three_way", False), bool):
        issues.append("gold_patch_three_way must be true or false, or be omitted")
    return issues


def git(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    with GIT_LOCK:
        if not CLONE.exists():
            CLONE.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--bare", CLONE_URL, str(CLONE)],
                           capture_output=True, text=True, timeout=3600, check=True)
        return subprocess.run(["git", "-C", str(CLONE), *args],
                              capture_output=True, text=True, timeout=timeout)


def pinned_commit(ref, record: dict) -> str:
    """The commit a tool call names, fetched if missing. Raises ValueError."""
    commits = [c["sha"] for c in record.get("commits", [])]
    sha = {"base": record.get("base_commit"), "head": record.get("head_commit")}.get(ref)
    if sha is None and isinstance(ref, str) and len(ref) >= 7:
        sha = next((c for c in commits if c.startswith(ref)), None)
    if not sha:
        raise ValueError("ref must be 'base', 'head', or the SHA of one of the PR's commits: "
                         + ", ".join(commits))
    if git("cat-file", "-e", f"{sha}^{{commit}}").returncode:
        git("fetch", "-q", "origin", sha, timeout=600)
        if git("cat-file", "-e", f"{sha}^{{commit}}").returncode:
            raise ValueError(f"commit {sha[:12]} is unavailable")
    return sha


def run_tool(name: str, args: dict, record: dict) -> str:
    path = args.get("path") or ""
    if not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts:
        return "error: invalid repository-relative path"
    path = path.strip().strip("/")
    if name not in ("read_file", "list_dir", "grep", "commit_diff"):
        return f"error: unknown tool {name}"
    if name == "read_file" and not path:
        return "error: read_file needs a file path"
    try:
        sha = pinned_commit(args.get("sha") if name == "commit_diff" else args.get("ref"),
                            record)
    except ValueError as exc:
        return f"error: {exc}"

    if name == "commit_diff":
        r = git("show", "--format=%H%n%s%n", "--stat", "--patch", sha, "--", *([path] if path else []))
        if r.returncode:
            return f"error: {r.stderr.strip()[:300]}"
        return r.stdout[:60000] + ("\n... (diff truncated; pass path to narrow it)"
                                   if len(r.stdout) > 60000 else "")

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


def score(gold: dict[str, str], verdicts: dict[str, dict]) -> dict:
    rows = []
    for case, expected in gold.items():
        verdict = verdicts.get(case, {})
        got = verdict.get("verdict", "missing")
        rows.append({"case": case, "expected": expected, "got": got,
                     "correct": got == expected, "reason": verdict.get("reason", "")})
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
    verdicts = {case: json.loads((run_dir / f"{case}.json").read_text())
                for case in gold if (run_dir / f"{case}.json").exists()}
    result = score(gold, verdicts)
    lines = [f"# Mynewt triage: {run_dir.name}", "",
             f"Verdicts: {result['correct']}/{result['scored']} correct", "",
             "An `error` verdict is a miss even when the intended verdict was right.", "",
             "| PR | Expected | Got | Correct | Reason |", "|---|---|---|---|---|"]
    for row in result["rows"]:
        reason = row["reason"].replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {row['case']} | {row['expected']} | {row['got']} | "
                     f"{'yes' if row['correct'] else 'no'} | {reason} |")
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
        "pr": int(case), "verdict": "error", "reason": trace["error"] or "; ".join(issues)}
    (run_dir / f"{case}.json").write_text(json.dumps(output, indent=2) + "\n")
    return {"case": case, "verdict": output.get("verdict"), "seconds": trace["seconds"],
            "error": trace["error"], "issues": issues}


def run(args) -> None:
    import litellm  # noqa: F401 - fail before starting a run if the client is absent

    profiles = json.loads(MODELS.read_text())
    if args.profile not in profiles or args.profile.startswith("_"):
        raise SystemExit(f"unknown profile: {args.profile}")
    cases = list(dict.fromkeys([str(c) for c in args.case or []]
                               + (list(load_gold()) if args.gold else [])))
    if not cases:
        raise SystemExit("give --case and/or --gold")
    records = {case: load_record(case) for case in cases}
    try:
        with urlopen(args.api_base.rstrip("/") + "/models", timeout=8) as response:
            served = {model["id"] for model in json.load(response).get("data", [])}
    except (OSError, URLError, ValueError) as exc:
        raise SystemExit(f"model server unavailable at {args.api_base}: {exc}") from exc
    if args.profile not in served:
        raise SystemExit(f"model server does not serve {args.profile}; available: {sorted(served)}")
    run_dir = RUNS / f"{datetime.now():%Y-%m-%d_%H%M%S}_{args.profile}"
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt = PROMPT.read_text()
    (run_dir / PROMPT.name).write_text(prompt)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    manifest = {"profile": args.profile, "api_base": args.api_base,
                "sampling": profiles[args.profile].get("sampling"),
                "max_tokens": profiles[args.profile].get("max_tokens"),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "git_commit": commit, "cases": cases,
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--profile", required=True)
    run_parser.add_argument("--case", nargs="+", help="PR numbers")
    run_parser.add_argument("--gold", action="store_true")
    run_parser.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    run_parser.add_argument("--parallel", type=int, default=4)
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
