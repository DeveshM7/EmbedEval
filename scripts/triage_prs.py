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
requires, a platform we can run, and fail_to_pass names that actually appear
in the suite's sources. A failed check goes back to the model with the reason,
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
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_instance import ensure_clone  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICHED = REPO_ROOT / "candidates" / "enriched"
INSTRUCTIONS = REPO_ROOT / "docs" / "pr_triage.md"
TRIAGE = REPO_ROOT / "triage"
RUNS = TRIAGE / "runs"
INSTANCES = REPO_ROOT / "docker" / "instances"

# Board names pr_triage.md allows. native_sim_64 is the pre-2024 spelling of
# native_sim/native/64; the hand-built instances use both.
PLATFORMS = {"native_sim", "native_sim_64", "native_sim/native/64", "qemu_x86"}
SAME_BOARD = {"native_sim_64": "native_sim/native/64"}

MAX_TOOL_ROUNDS = 8     # model replies that call tools; then it must answer.
                        # 43405 took 24 rounds and 41 minutes reading log_core.c
                        # in chunks before that file was put in the record.
MAX_REPAIRS = 2         # times a failed verdict is sent back with the reasons
MAX_TURNS = MAX_TOOL_ROUNDS + MAX_REPAIRS + 2   # hard stop on requests per PR
REQUEST_TIMEOUT = 3600  # seconds. LiteLLM's default of 600 silently retried a
                        # 10-minute Qwen3.8 request three times over.
TOOL_OUTPUT_CAP = 60_000


# ---------------------------------------------------------------- the record


def render_record(rec: dict) -> str:
    """The enriched record as readable markdown: the same substance as the
    GitHub page, with diffs and test sources in fenced blocks rather than
    escaped inside JSON strings."""
    out = [f"# PR #{rec['number']}: {rec['title']}", "",
           f"- URL: {rec['url']}",
           f"- Merged: {rec.get('merged_at')}",
           f"- Labels: {', '.join(rec.get('labels') or []) or '(none)'}",
           f"- Base commit (repo before this PR): {rec.get('base_commit')}",
           f"- Merge commit (repo after this PR): {rec.get('merge_commit_sha')}",
           f"- Size: +{rec.get('additions')} -{rec.get('deletions')} "
           f"in {len(rec['files'])} files", "",
           "## Description", "", rec.get("body") or "(empty)", ""]

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
        if f.get("patch"):
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
            out += [f"**{c['author']}** on {c['path']}:{c['line']}:",
                    "```diff", c.get("diff_hunk") or "", "```", "", c["body"], ""]

    suites = rec.get("test_suites") or {}
    out += ["## Test suites touched by this PR, in full, at the merge commit", ""]
    if not suites:
        out += ["(this PR touches no test suite)", ""]
    for root, suite in suites.items():
        out += [f"### Suite {root} (config: {suite['config']})", ""]
        for name, f in sorted(suite["files"].items()):
            if f["content"] is None:
                out += [f"#### {root}/{name}", f"(binary, {f['size']} bytes)", ""]
            else:
                out += [f"#### {root}/{name}", "```", f["content"], "```", ""]

    out += ["## Our platforms at the base commit", ""]
    for name, p in (rec.get("platforms") or {}).items():
        if name.startswith("_"):
            out += [f"Note: {p}", ""]
        elif p.get("exists"):
            out += [f"### {name}: exists ({p['path']})", "```yaml", p["content"], "```", ""]
        else:
            out += [f"### {name}: does not exist at this commit", ""]

    sources = rec.get("base_sources") or {}
    out += ["## Source files before this PR, in full, at the base commit", ""]
    if not sources:
        out += ["(this PR modifies no existing non-test file)", ""]
    for path, f in sources.items():
        if f.get("content") is None:
            out += [f"### {path}", f"(not included: {f.get('omitted')}; {f.get('size')} bytes)", ""]
        else:
            out += [f"### {path}", "```", f["content"], "```", ""]
    return "\n".join(out)


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
        "description": "Read a file from the Zephyr repository, before (base) or after "
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
        "description": "List the entries of a directory in the Zephyr repository.",
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
        return subprocess.run(["git", "-C", str(ensure_clone()), *args],
                              capture_output=True, text=True, timeout=timeout)


def run_tool(name: str, args: dict, rec: dict) -> str:
    refs = {"base": rec.get("base_commit"), "merge": rec.get("merge_commit_sha")}
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
    if v.get("change_type") not in ("fix", "feature"):
        issues.append("`change_type` must be fix or feature")
    if v.get("platform") not in PLATFORMS:
        issues.append(f"`platform` must be exactly one of {sorted(PLATFORMS)}")
    if not str(v.get("problem_statement") or "").strip():
        issues.append("`problem_statement` is required")
    if not isinstance(v.get("extra_configs", []), list):
        issues.append("`extra_configs` must be a list")
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


# ---------------------------------------------------------------- one PR


def triage_one(pr: int, profile: str, prof: dict, api_base: str, run_dir: Path) -> dict:
    import litellm

    rec = json.loads((ENRICHED / f"{pr}.json").read_text())
    sampling = dict(prof.get("sampling", {}))
    extra = {k: sampling.pop(k) for k in ("top_k", "min_p") if k in sampling}
    kw = dict(model=f"openai/{profile}", api_base=api_base, api_key="local",
              timeout=REQUEST_TIMEOUT, num_retries=0,
              max_tokens=prof.get("max_tokens", 32768), **sampling)
    if extra:
        kw["extra_body"] = extra

    messages = [{"role": "system", "content": INSTRUCTIONS.read_text()},
                {"role": "user", "content": "Enriched PR record:\n\n" + render_record(rec)
                 + f"\n\n---\nYou may use up to {MAX_TOOL_ROUNDS} tool rounds for this PR."}]
    trace = {"pr": pr, "profile": profile, "turns": [], "repairs": [], "error": None,
             "tool_calls": 0, "budget_hit": False}
    verdict, issues, repairs, rounds, t0 = None, ["no reply"], 0, 0, time.time()

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
            issues = check_verdict(verdict, rec)
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


# ---------------------------------------------------------------- scoring


def load_gold() -> dict[int, str]:
    g = json.loads((TRIAGE / "gold.json").read_text())
    return {**{int(p): "accept" for p in g["accept"]},
            **{int(p): "reject" for p in g["reject"]},
            **{int(p): "info" for p in g.get("info_only", {})}}


def secondary(pr: int, v: dict) -> str:
    """Accepted gold PRs only: how the instance fields compare to the ones
    built by hand. Reported, never scored."""
    meta_path = INSTANCES / f"zephyr__zephyr-{pr}" / "metadata.json"
    if not meta_path.exists() or v.get("verdict") != "accept":
        return ""
    meta = json.loads(meta_path.read_text())
    norm = lambda p: SAME_BOARD.get(p, p)
    plat = "✓" if norm(v.get("platform")) == norm(meta.get("platform")) else \
        f"✗ {v.get('platform')} (gold {meta.get('platform')})"
    want, got = set(meta.get("fail_to_pass") or []), set(v.get("fail_to_pass") or [])
    f2p = f"{len(want & got)}/{len(want)} gold" + (f", +{len(got - want)} extra" if got - want else "")
    ec = "✓" if sorted(v.get("extra_configs") or []) == sorted(meta.get("extra_configs") or []) else \
        f"✗ {v.get('extra_configs')} (gold {meta.get('extra_configs') or []})"
    return f"{plat} / {f2p} / {ec}"


def previous_verdicts(run_dir: Path) -> dict[int, str]:
    """Latest earlier verdict for each PR, across all earlier runs."""
    prev = {}
    for d in sorted(p for p in RUNS.iterdir() if p.is_dir() and p.name < run_dir.name):
        for f in d.glob("[0-9]*.json"):
            if not f.name.endswith(".trace.json"):
                prev[int(f.stem)] = json.loads(f.read_text()).get("verdict")
    return prev


def cmd_eval(args) -> None:
    run_dir = Path(args.run_dir) if args.run_dir else max(RUNS.iterdir(), key=lambda p: p.name)
    gold, prev = load_gold(), previous_verdicts(run_dir)
    rows, scored, correct, changed = [], 0, 0, []
    verdicts = [f for f in run_dir.glob("[0-9]*.json") if not f.name.endswith(".trace.json")]
    for f in sorted(verdicts, key=lambda p: int(p.stem)):
        pr, v = int(f.stem), json.loads(f.read_text())
        trace = json.loads((run_dir / f"{pr}.trace.json").read_text())
        want, got = gold.get(pr, "—"), v.get("verdict")
        ok = "" if want in ("info", "—") else ("✓" if got == want else "✗")
        if ok:
            scored += 1
            correct += ok == "✓"
        if pr in prev and prev[pr] != got:
            changed.append(f"{pr}: {prev[pr]} → {got}")
        rows.append({"pr": pr, "expected": want, "got": got, "ok": ok,
                     "confidence": v.get("confidence", ""), "secondary": secondary(pr, v),
                     "seconds": trace.get("seconds"), "turns": len(trace.get("turns", [])),
                     "tool_calls": sum(len(t.get("tool_calls") or []) for t in trace.get("turns", [])),
                     "budget_hit": trace.get("budget_hit", False),
                     "issues": trace.get("issues") or []})

    acc = [r for r in rows if r["expected"] == "accept"]
    rej = [r for r in rows if r["expected"] == "reject"]
    md = [f"# Scorecard: {run_dir.name}", "",
          f"**Verdicts: {correct}/{scored} correct** "
          f"(accepts {sum(r['ok'] == '✓' for r in acc)}/{len(acc)}, "
          f"rejects {sum(r['ok'] == '✓' for r in rej)}/{len(rej)})", "",
          f"Time per PR: mean {sum(r['seconds'] for r in rows) / max(len(rows), 1) / 60:.1f} min, "
          f"max {max((r['seconds'] for r in rows), default=0) / 60:.1f} min. "
          f"Tool calls: {sum(r['tool_calls'] for r in rows)} in total; "
          f"budget hit on {sum(r['budget_hit'] for r in rows)} PR(s).", "",
          "| PR | expected | got | ok | conf | platform / fail_to_pass / extra_configs | time | tools | unresolved issues |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['pr']} | {r['expected']} | {r['got']} | {r['ok']} | {r['confidence']} | "
                  f"{r['secondary']} | {r['seconds'] / 60:.1f} min | {r['tool_calls']}"
                  f"{' (budget hit)' if r['budget_hit'] else ''} | {'; '.join(r['issues'])} |")
    md += ["", "Changed since earlier runs: " + (", ".join(changed) if changed else "none")]
    (run_dir / "scorecard.md").write_text("\n".join(md) + "\n")
    (run_dir / "scorecard.json").write_text(json.dumps(
        {"run": run_dir.name, "correct": correct, "scored": scored, "rows": rows,
         "changed": changed}, indent=2) + "\n")
    print("\n".join(md))


# ---------------------------------------------------------------- run


def git_state() -> dict:
    head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    return {"commit": head, "dirty": bool(dirty)}


def cmd_run(args) -> None:
    profiles = json.loads((TRIAGE / "models.json").read_text())
    if args.profile not in profiles or args.profile.startswith("_"):
        sys.exit(f"unknown profile {args.profile!r}")
    prof = profiles[args.profile]

    prs = list(dict.fromkeys((args.pr or []) + (sorted(load_gold()) if args.gold else [])))
    if not prs:
        sys.exit("give --pr and/or --gold")
    missing = [p for p in prs if not (ENRICHED / f"{p}.json").exists()]
    if missing:
        sys.exit(f"not enriched: {missing}. Run filter_candidates.py enrich --pr ...")

    # Make every commit the tools may read reachable before any thread needs it.
    commits = set()
    for p in prs:
        rec = json.loads((ENRICHED / f"{p}.json").read_text())
        commits |= {c for c in (rec.get("base_commit"), rec.get("merge_commit_sha")) if c}
    print(f"Fetching {len(commits)} commits into the Zephyr clone ...")
    r = git("fetch", "-q", "origin", *sorted(commits), timeout=1800)
    if r.returncode:
        print(f"  warning: fetch failed; tools may not see every commit:\n  {r.stderr.strip()[:300]}")

    run_dir = RUNS / f"{datetime.now():%Y-%m-%d_%H%M}_{args.profile}"
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt = INSTRUCTIONS.read_text()
    (run_dir / "pr_triage.md").write_text(prompt)
    manifest = {"profile": args.profile, "api_base": args.api_base,
                "sampling": prof.get("sampling"), "max_tokens": prof.get("max_tokens"),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()[:16],
                "git": git_state(), "prs": prs, "parallel": args.parallel,
                "started": datetime.now().isoformat(timespec="seconds")}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Triage of {len(prs)} PRs with {args.profile}, {args.parallel} at a time "
          f"-> {run_dir.relative_to(REPO_ROOT)}\n")

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
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print()
    args.run_dir = str(run_dir)
    cmd_eval(args)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="triage PRs with a served model, then score the run")
    r.add_argument("--profile", required=True, help="entry in triage/models.json")
    r.add_argument("--pr", type=int, nargs="+", help="specific PR numbers")
    r.add_argument("--gold", action="store_true", help="every PR in triage/gold.json")
    r.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    r.add_argument("--parallel", type=int, default=4,
                   help="PRs in flight at once; the server takes 4 (--max-num-seqs)")

    e = sub.add_parser("eval", help="score a run against triage/gold.json")
    e.add_argument("run_dir", nargs="?", help="default: the latest run")

    args = p.parse_args()
    {"run": cmd_run, "eval": cmd_eval}[args.cmd](args)


if __name__ == "__main__":
    main()
