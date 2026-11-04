#!/usr/bin/env python3
"""
The LLM stage for RIOT: on PRs that passed the quick check, reject poor tasks, tag
each as fix or feature, and write the problem statement.

    python scripts/riot_triage.py --run-dir DIR enrich [--pr N ...]    # needs gh
    python scripts/riot_triage.py --run-dir DIR run --api-base URL --profile P [--pr N ...]
    python scripts/riot_triage.py eval DIR [DIR ...]

docs/riot_triage.md is the model's instructions. The record it reads holds what
GitHub has (description, issues, discussion; `enrich`) and what the earlier stages
found (the source and test diffs, how each test went; DIR/check/). It uses no tools:
the record is enough, and each tool round costs a full pass of thinking.

The verdict's form is checked by code, and so is whether the problem statement gives
the fix away: a name the source change touches and the tests never use, or wording
that says how to change the code. A failed check goes back to the model up to
MAX_REPAIRS times; a statement still failing is kept with its `statement_issues`, for
a person to judge. Writes DIR/triage/<pr>.json and <pr>.trace.json; a PR with a
verdict is skipped (--redo: again). `eval` scores against triage/riot_gold.json.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au  # noqa: E402,F401
import riot_env  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTRUCTIONS = REPO_ROOT / "docs" / "riot_triage.md"
GOLD = REPO_ROOT / "triage" / "riot_gold.json"
PROFILES = REPO_ROOT / "triage" / "models.json"
REJECT_REASONS = {"no_behaviour_change", "build_only", "tests_do_not_check_it",
                  "not_describable", "test_only"}
MAX_REPAIRS = 2
REQUEST_TIMEOUT = 3600      # seconds
DIFF_CAP = 200_000          # characters of each diff; the rest is said to be left out
TEXT_CAP = 20_000           # of one comment or issue
COMPILE_ERROR_LINES = 12
# Wording that tells the agent how to change the code rather than what must be true.
HOW_NOT_WHAT = re.compile(r"\b(modify|refactor|rewrite)\b|\b(must|should|to) call\b|\binternally\b",
                          re.I)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def passing(run: Path, prs: list[int] | None) -> list[dict]:
    recs = [json.loads(f.read_text()) for f in sorted((run / "check").glob("*.json"))]
    return [r for r in recs if r["outcome"] in ("strong", "weak") and (not prs or r["pr"] in prs)]


def git_diff(check: dict, *paths: str) -> str:
    out = subprocess.run(["git", "-C", str(riot_env.clone()), "diff", check["base"], check["head"],
                          "--", *paths], capture_output=True, text=True, errors="replace").stdout
    if len(out) > DIFF_CAP:
        return out[:DIFF_CAP] + f"\n[... {len(out) - DIFF_CAP} more characters left out]"
    return out


def cap(text: str) -> str:
    text = text or ""
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + "\n[... left out]"


def compile_error(run: Path, pr: int, target: str) -> str:
    log = run / "check" / "logs" / str(pr) / f"before-{target.replace('/', '_')}-build.log"
    if not log.exists():
        return ""
    lines = [l for l in log.read_text(errors="replace").splitlines()
             if re.search(r"error|undefined reference|unknown modules|\*\*\*", l)]
    return "\n".join(l.replace("/testbed/", "") for l in lines[:COMPILE_ERROR_LINES])


def how_tests_went(run: Path, check: dict) -> str:
    out = []
    for name, t in check["targets"].items():
        out.append(f"### {name}: {t['kind']} (without the change: {t['before']}, with it: {t['after']})")
        if t.get("fail_to_pass"):
            shown = t["fail_to_pass"][:40]
            more = len(t["fail_to_pass"]) - len(shown)
            out.append("Failed or absent without the change, passed with it: " + ", ".join(shown)
                       + (f" and {more} more" if more > 0 else ""))
        if t.get("pass_to_pass"):
            out.append(f"Passed both ways: {len(t['pass_to_pass'])} tests")
        if t["kind"] == "weak":
            out += ["Compile error without the change:", "```", compile_error(run, check["pr"], name), "```"]
    return "\n".join(out)


def record(run: Path, check: dict, gh: dict) -> str:
    out = [f"# PR #{check['pr']}: {gh['title']}", f"Labels: {', '.join(gh['labels']) or '(none)'}", "",
           "## Description", cap(gh.get("body")) or "(empty)", ""]
    for i in gh.get("linked_issues", []):
        out += [f"## Linked issue #{i['number']}: {i['title']}", cap(i["body"]), ""]
        out += [f"**{c['author']}**: {cap(c['body'])}\n" for c in i["comments"]]
    out += ["## Commit messages"] + [c["message"] for c in gh.get("commits", [])] + [""]
    if gh.get("comments") or gh.get("review_comments"):
        out += ["## Discussion"]
        out += [f"**{c['author']}**: {cap(c['body'])}\n" for c in gh.get("comments", [])]
        out += [f"**{c['author']}** on {c['path']}: {cap(c['body'])}\n"
                for c in gh.get("review_comments", [])]
    out += ["## How the tests went on the native board", how_tests_went(run, check), "",
            "## The source change", "```diff", git_diff(check, ".", ":!tests/"), "```", "",
            "## The test change", "```diff", git_diff(check, "tests/"), "```"]
    return "\n".join(out)


def parse(content: str | None) -> dict | None:
    if not content:
        return None
    a, b = content.find("{"), content.rfind("}")
    if a < 0 or b < a:
        return None
    try:
        v = json.loads(content[a:b + 1])
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def issues(v: dict | None, pr: int) -> list[str]:
    if v is None:
        return ["the reply is not a JSON object"]
    out = []
    if v.get("pr") != pr:
        out.append(f"`pr` must be {pr}")
    if v.get("verdict") not in ("accept", "reject"):
        out.append('`verdict` must be "accept" or "reject"')
    if not str(v.get("reason") or "").strip():
        out.append("`reason` is required")
    if v.get("confidence") not in ("high", "medium", "low"):
        out.append("`confidence` must be high, medium or low")
    if v.get("change_type") not in ("fix", "feature"):
        out.append("`change_type` must be fix or feature")
    if v.get("verdict") == "reject" and v.get("reject_reason") not in REJECT_REASONS:
        out.append(f"`reject_reason` must be one of {sorted(REJECT_REASONS)}")
    if v.get("verdict") == "accept" and not str(v.get("problem_statement") or "").strip():
        out.append("`problem_statement` is required when accepting")
    return out


def names_in(text: str) -> set[str]:
    """Words that can only be code: with an underscore, or called like a function."""
    return {m.group() for m in IDENTIFIER.finditer(text)
            if "_" in m.group() or text[m.end():m.end() + 1] == "("}


def test_code(check: dict) -> str:
    """The tests the instance runs, as the agent will have them, and the PR's test diff."""
    out = [git_diff(check, "tests/")]
    for target in check["targets"]:
        folder = target if "/" in target else f"tests/unittests/{target}"
        files = riot_env.git("ls-tree", "-r", "--name-only", check["head"], "--", folder).split()
        out += [riot_env.show(check["head"], f) or "" for f in files]
    return "\n".join(out)


def statement_issues(statement: str, source_diff: str, tests: str) -> list[str]:
    out = []
    changed = "\n".join(l[1:] for l in source_diff.splitlines()
                        if l[:1] in "+-" and l[:3] not in ("+++", "---"))
    test_words = set(IDENTIFIER.findall(tests))
    changed_words = set(IDENTIFIER.findall(changed))
    leaks = sorted(n for n in names_in(statement) if n in changed_words and n not in test_words)
    if leaks:
        out.append(f"`problem_statement` names {leaks}: the source change touches them and the "
                   "tests never use them. Describe the behaviour without them.")
    how = sorted({m.group().lower() for m in HOW_NOT_WHAT.finditer(statement)})
    if how:
        out.append(f"`problem_statement` says how to change the code ({how}). Say what a caller "
                   "must observe, not how the code gets there.")
    return out


def triage_one(run: Path, check: dict, profile: str, prof: dict, api_base: str) -> dict:
    import litellm
    pr = check["pr"]
    folder = run / "triage"
    gh = json.loads((layout.enriched() / f"{pr}.json").read_text())
    sampling = dict(prof.get("sampling", {}))
    extra = {k: sampling.pop(k) for k in ("top_k", "min_p") if k in sampling}
    kw = dict(model=f"openai/{profile}", api_base=api_base, api_key="local",
              timeout=REQUEST_TIMEOUT, num_retries=0,
              max_tokens=prof.get("max_tokens", 32768), **sampling)
    if extra:
        kw["extra_body"] = extra
    messages = [{"role": "system", "content": INSTRUCTIONS.read_text()},
                {"role": "user", "content": record(run, check, gh)}]
    trace = {"pr": pr, "profile": profile, "turns": [], "error": None}
    verdict, problems, wording, t0 = None, ["no reply"], [], time.time()
    source, tests = git_diff(check, ".", ":!tests/"), test_code(check)
    try:
        for attempt in range(MAX_REPAIRS + 1):
            t = time.time()
            r = litellm.completion(messages=messages, **kw)
            msg = r.choices[0].message
            trace["turns"].append({"seconds": round(time.time() - t, 1),
                                   "finish_reason": r.choices[0].finish_reason,
                                   "prompt_tokens": r.usage.prompt_tokens,
                                   "completion_tokens": r.usage.completion_tokens,
                                   "reasoning": getattr(msg, "reasoning_content", None),
                                   "content": msg.content})
            verdict = parse(msg.content)
            problems = issues(verdict, pr)
            wording = []
            if not problems and verdict["verdict"] == "accept":
                wording = statement_issues(verdict["problem_statement"], source, tests)
            if not problems and not wording:
                break
            if attempt == MAX_REPAIRS:
                break
            messages += [{"role": "assistant", "content": msg.content or ""},
                         {"role": "user", "content": "Your verdict does not meet the output "
                          "requirements:\n- " + "\n- ".join(problems + wording)
                          + "\nReturn the corrected JSON object only."}]
    except Exception as e:      # one PR's failure must not stop the rest
        trace["error"] = f"{type(e).__name__}: {e}"
    trace["seconds"], trace["issues"] = round(time.time() - t0, 1), problems + wording
    trace["repairs"] = len(trace["turns"]) - 1
    (folder / f"{pr}.trace.json").write_text(json.dumps(trace, indent=2, default=str))
    if problems:
        return {"pr": pr, "verdict": "error", "reason": trace["error"] or "; ".join(problems),
                "seconds": trace["seconds"]}
    verdict["outcome"], verdict["seconds"] = check["outcome"], trace["seconds"]
    verdict["repairs"] = trace["repairs"]
    if wording:
        verdict["statement_issues"] = wording
    (folder / f"{pr}.json").write_text(json.dumps(verdict, indent=2) + "\n")
    return verdict


def cmd_enrich(args) -> None:
    import filter_candidates as fc
    for check in passing(layout.run_dir(), args.pr):
        rec = fc.fetch_full(check["pr"])
        print(f"  {check['pr']}: {'ok' if rec else 'FAILED'}", flush=True)


def cmd_run(args) -> None:
    run = layout.run_dir()
    (run / "triage").mkdir(exist_ok=True)
    prof = json.loads(PROFILES.read_text())[args.profile]
    todo = [c for c in passing(run, args.pr)
            if args.redo or not (run / "triage" / f"{c['pr']}.json").exists()]
    print(f"{len(todo)} PRs, {args.parallel} at a time", flush=True)

    def one(c: dict) -> None:
        v = triage_one(run, c, args.profile, prof, args.api_base)
        print(f"  {c['pr']}: {v['verdict']} {v.get('reject_reason') or v.get('change_type') or ''} "
              f"{v.get('confidence', '')} {v.get('seconds')}s, {v.get('repairs', 0)} repairs"
              + (" STATEMENT ISSUES" if v.get("statement_issues") else ""), flush=True)

    with ThreadPoolExecutor(args.parallel) as pool:
        list(pool.map(one, todo))


def cmd_eval(args) -> None:
    gold = {int(k): v for k, v in json.loads(GOLD.read_text()).items() if not k.startswith("_")}
    got = {}
    for d in args.dirs:
        for f in Path(d, "triage").glob("*.json"):
            if not f.name.endswith(".trace.json"):
                v = json.loads(f.read_text())
                got[v["pr"]] = v
    rows, verdict_ok, type_ok, types = [], 0, 0, 0
    for pr, g in sorted(gold.items()):
        v = got.get(pr)
        if not v:
            rows.append(f"  {pr}: no verdict (gold {g['verdict']})")
            continue
        same = v["verdict"] == g["verdict"]
        verdict_ok += same
        line = f"  {pr}: {'ok   ' if same else 'WRONG'} gold {g['verdict']:6} model {v['verdict']:6} " \
               f"{v.get('reject_reason') or ''}"
        if g["verdict"] == "accept" and v["verdict"] == "accept":
            types += 1
            type_ok += v.get("change_type") == g["change_type"]
            if v.get("change_type") != g["change_type"]:
                line += f" type: gold {g['change_type']}, model {v.get('change_type')}"
        rows.append(line)
    print("\n".join(rows))
    print(f"\nverdicts right: {verdict_ok} of {len(gold)}; types right: {type_ok} of {types}")
    print("all verdicts:", dict(Counter(v["verdict"] for v in got.values())))
    print("statements still flagged:", sorted(v["pr"] for v in got.values() if v.get("statement_issues")))
    secs = [v["seconds"] for v in got.values() if "seconds" in v]
    if secs:
        print(f"seconds per PR: mean {sum(secs) / len(secs):.0f}, max {max(secs):.0f}")


def selftest() -> None:
    src = "+++ b/a.c\n+static int hidden_helper(void);\n-int old_name;\n+int new_api(void);\n"
    tests = "new_api();"
    assert statement_issues("Add new_api() so it returns 1.", src, tests) == []
    assert "hidden_helper" in statement_issues("Fix hidden_helper.", src, tests)[0]
    assert statement_issues("Unrelated_name is not touched.", src, tests) == []
    assert "how to change" in statement_issues("new_api() must call something.", src, tests)[0]
    assert "how to change" in statement_issues("Modify the parser.", src, tests)[0]
    assert statement_issues("The caller sees an error.", src, tests) == []
    print("selftest ok")


def main() -> None:
    if sys.argv[1:] == ["selftest"]:
        return selftest()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enrich")
    e.add_argument("--pr", type=int, nargs="+")
    r = sub.add_parser("run")
    r.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    r.add_argument("--profile", default="qwen38-nvfp4")
    r.add_argument("--parallel", type=int, default=4)
    r.add_argument("--pr", type=int, nargs="+")
    r.add_argument("--redo", action="store_true")
    v = sub.add_parser("eval")
    v.add_argument("dirs", nargs="+")
    args = p.parse_args()
    if args.cmd != "eval" and not layout.run_dir():
        sys.exit("needs --run-dir")
    {"enrich": cmd_enrich, "run": cmd_run, "eval": cmd_eval}[args.cmd](args)


if __name__ == "__main__":
    main()
