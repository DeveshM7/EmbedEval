"""The problem statement of a NuttX instance: written, or rechecked, by the
served open-weight model, never by hand.

    python scripts/triage_nuttx_prs.py statement --profile qwen38-nvfp4 \
        --instance <run>/instances/nuttx__nuttx-11889-a2327 [--recheck]

The agent sees only this statement and the repositories at their base
commits, so the statement may describe the symptom, what triggers it and the
required observable behavior, and name what the tests use; it must not give
the fix away. A statement passes when

    - it quotes no line the fix adds;
    - it names no identifier of the fix's changed lines that the test patch
      does not use (fix_identifiers);
    - a review by the same model, in a fresh conversation, finds no phrase
      that gives the fix away.

The writer sees the kernel and apps PR texts, the test patch, the production
fix and the observed before state (validation.json), to describe the bug
correctly; none of it reaches the agent. --recheck keeps a statement that
passes and rewrites one that does not (the old one stays under
superseded_statements). metadata.json records the result under
statement_check with STATEMENT_CHECK, the version of these rules;
promote_instance.py accepts only the current version.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

# 1: line, identifier and model-review checks (2026-11-07).
STATEMENT_CHECK = 1
MAX_ATTEMPTS = 3
DIFF_CAP = 60_000
TEXT_CAP = 6_000
IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

WRITE_PROMPT = """You write the problem statement of one coding benchmark instance from
Apache NuttX. An agent gets only your statement and the kernel and apps
repositories at their base commits (with the apps test change below applied);
it must find the defect in the kernel and fix it. The upstream fix and the PR
texts below are for you alone, so you describe the bug correctly.

Write it as an issue report against the kernel would: what a caller of the
kernel observes (a crash, an assertion, a hang, a wrong result, a missing
interface), the conditions that trigger it, and the required behavior. You may
name the kernel interfaces and configuration options the test change uses.

Describe the kernel's behavior, not the test. Do not mention ostest, the test
change, test programs, test files, test functions or tasks, or lines the test
prints, and do not say that a test fails or must pass: an application calling
the same interfaces would see the same problem, so describe it as that
application would. When the test needs an interface the kernel lacks, say which
interface is missing and how it must behave.

Never say how the code goes wrong inside -- which check is missing, which
lock, counter or field is wrong, where in the code the defect is -- or what
the code must add, track or change. Name no function, macro, type, variable or
file of the production fix unless the test change uses it. Quote no code of
the fix.

Kernel PR (the fix): {kernel_title}
{kernel_body}

Apps PR (the test): {apps_title}
{apps_body}

The test change (the agent sees it):
{test_patch}

The production fix (the agent does not see it):
{fix}

Observed before the fix: {before}

Reply with the JSON object only:
{{"problem_statement": "..."}}"""

REVIEW_PROMPT = """You review one problem statement of a coding benchmark. An agent is
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
{fix}

Names the tests use: {test_names}

The statement:
{statement}

Reply with the JSON object only:
{{"gives_away_fix": true or false, "phrases": ["each phrase of the statement that gives the fix away, verbatim"]}}"""


def _code_like(token: str) -> bool:
    return ("_" in token or (len(token) >= 5 and token.isalpha() and token.isupper())
            or re.search(r"[a-z][A-Z]", token) is not None)


def fix_identifiers(statement: str, fix: str, test_patch: str) -> list[str]:
    """Identifiers of the fix's changed lines that the statement names, other
    than those the test patch uses (an interface may be named as the tests
    use it)."""
    changed = set()
    for line in fix.splitlines():
        if line[:1] in "+-" and line[:3] not in ("+++", "---"):
            changed |= {t for t in IDENT.findall(line) if _code_like(t)}
    tests = test_patch.lower()
    return sorted({t for t in IDENT.findall(statement) if t in changed and t.lower() not in tests})


def check(statement, fix: str, test_patch: str, review=None) -> list[str]:
    """Why `statement` does not pass (empty: it passes). review(statement)
    returns the phrases the model review says give the fix away."""
    if not isinstance(statement, str) or len(statement.strip()) < 40:
        return ["`problem_statement` must be a nonempty description (40 characters or more)"]
    issues = []
    added = {line[1:].strip() for line in fix.splitlines()
             if line.startswith("+") and not line.startswith("+++")}
    if any(len(line) >= 25 and line in statement for line in added):
        issues.append("`problem_statement` quotes the production fix; describe the behavior instead")
    named = fix_identifiers(statement, fix, test_patch)
    if named:
        issues.append("`problem_statement` names code of the fix that the tests do not use ("
                      + ", ".join(named) + "); describe what a test or caller observes instead")
    if not issues and review is not None:
        phrases = review(statement)
        if phrases:
            issues.append("`problem_statement` gives the fix away: " + "; ".join(f'"{x}"' for x in phrases)
                          + ". Describe only the observable symptom, its trigger and the required "
                          "behavior, not what the code checks or must do internally")
    return issues


def before_state(receipt: dict) -> str:
    """What validation saw before the fix, from validation.json."""
    b = receipt.get("before") or {}
    if b.get("diagnostics") or b.get("diagnostic"):
        return "the build fails: " + "; ".join(b.get("diagnostics") or [b["diagnostic"]])[:2000]
    return f"ostest {b.get('meaning', '?')}: " + " | ".join(b.get("summary") or [])[:2000]


def _ask(options: dict, content: str, trace: dict, key: str) -> str:
    import litellm
    r = litellm.completion(messages=[{"role": "user", "content": content}], **options)
    text = r.choices[0].message.content or ""
    trace.setdefault(key, []).append({"request_chars": len(content), "reply": text})
    return text


def review(statement: str, fix: str, test_patch: str, options: dict, parse, trace: dict) -> list[str]:
    """The model's review: the offending phrases, or []. Raises when two
    replies carry no verdict."""
    names = sorted({t for t in IDENT.findall(test_patch) if _code_like(t)})
    content = REVIEW_PROMPT.format(fix=fix[:DIFF_CAP], test_names=", ".join(names)[:4000] or "(none)",
                                   statement=statement)
    for _ in range(2):
        v = parse(_ask(options, content, trace, "reviews"))
        if isinstance(v, dict) and isinstance(v.get("gives_away_fix"), bool):
            if not v["gives_away_fix"]:
                return []
            return [x for x in v.get("phrases") or [] if isinstance(x, str) and x.strip()] \
                or ["(the reviewer quoted no phrase)"]
    raise ValueError("the statement review returned no verdict")


def statement_one(folder: Path, profile: str, options: dict, parse, context: dict, fix: str,
                  trace_dir: Path, recheck: bool = False) -> dict:
    """Write (or, with recheck, check and if needed rewrite) the statement of
    the instance in `folder`, into its metadata.json. context: kernel_title,
    kernel_body, apps_title, apps_body (the PR texts). Returns {"ok", "kept",
    "issues", "error"}."""
    meta_path = folder / "metadata.json"
    meta = json.loads(meta_path.read_text())
    test_patch = (folder / "test_patch.diff").read_text()
    receipt_path = next((p for p in (folder / "validation.json", folder / "previous-validation.json")
                         if p.exists()), None)
    receipt = json.loads(receipt_path.read_text()) if receipt_path else {}
    iid = meta["instance_id"]
    trace = {"instance": iid, "profile": profile, "version": STATEMENT_CHECK, "error": None}
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"{iid}.statement.json"
    t0 = time.time()
    judge = lambda s: review(s, fix, test_patch, options, parse, trace)

    def finish(result: dict) -> dict:
        trace.update(seconds=round(time.time() - t0, 1), result=result)
        trace_path.write_text(json.dumps(trace, indent=2, default=str) + "\n")
        meta_path.write_text(json.dumps(meta, indent=4) + "\n")
        return result

    old = (meta.get("problem_statement") or "").strip()
    try:
        if recheck and old:
            trace["recheck_issues"] = issues = check(old, fix, test_patch, judge)
            if not issues:
                meta["statement_check"] = {"version": STATEMENT_CHECK, "result": "kept",
                                           "model": profile, "trace": str(trace_path)}
                return finish({"ok": True, "kept": True, "issues": [], "error": None})
        prompt = WRITE_PROMPT.format(
            kernel_title=context.get("kernel_title") or "", kernel_body=(context.get("kernel_body") or "")[:TEXT_CAP],
            apps_title=context.get("apps_title") or "", apps_body=(context.get("apps_body") or "")[:TEXT_CAP],
            test_patch=test_patch[:DIFF_CAP], fix=fix[:DIFF_CAP], before=before_state(receipt))
        content, issues, written = prompt, ["no reply"], None
        for _ in range(MAX_ATTEMPTS):
            v = parse(_ask(options, content, trace, "writes"))
            written = v.get("problem_statement") if isinstance(v, dict) else None
            issues = check(written, fix, test_patch, judge)
            if not issues:
                break
            content = (prompt + "\n\nYour previous statement:\n" + str(written)
                       + "\n\nCorrect it:\n- " + "\n- ".join(issues))
    except Exception as e:           # no verdict, the server: decide nothing
        trace["error"] = f"{type(e).__name__}: {e}"
        trace.update(seconds=round(time.time() - t0, 1))
        trace_path.write_text(json.dumps(trace, indent=2, default=str) + "\n")
        return {"ok": False, "kept": False, "issues": [], "error": trace["error"]}
    trace["issues"] = issues
    if old:
        meta.setdefault("superseded_statements", []).append(
            {"problem_statement": old, "check_issues": trace.get("recheck_issues")})
    if issues:
        meta.update(problem_statement="", statement_check={
            "version": STATEMENT_CHECK, "result": "failed", "issues": issues, "model": profile,
            "trace": str(trace_path)})
        return finish({"ok": False, "kept": False, "issues": issues, "error": None})
    meta.update(problem_statement=written.strip(), statement_check={
        "version": STATEMENT_CHECK, "result": "written", "model": profile, "trace": str(trace_path),
        "prompt_sha256": hashlib.sha256(WRITE_PROMPT.encode()).hexdigest()[:16]})
    return finish({"ok": True, "kept": False, "issues": [], "error": None})
