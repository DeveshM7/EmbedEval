#!/usr/bin/env python3
"""
Verify and repair the problem statements of the Zephyr and RIOT instances, in the
manner of SWE-bench Verified: is each statement specified well enough to pass the
hidden tests without seeing them? If not, add what is missing, without giving away
the fix.

    python scripts/verify_statements.py prepare [--only <id>...]   # name check + task files
    python scripts/verify_statements.py review --api-base URL      # judges + leak checks (a node)
    python scripts/verify_statements.py combine                    # verdicts, report
    python scripts/verify_statements.py apply                      # write into metadata.json

Steps (docs: the plan's "Statement verification and cleaning"):

  1. prepare: for every instance, its statement, its hidden tests (test_patch.diff),
     the fix (the PR's source diff without tests/, from the local clones), and the
     NAME CHECK: names the fix introduces that the tests use and the statement never
     mentions. One task file per instance in <work>/tasks/.
  2. review: JUDGES independent reviews per task, by the local model, with
     scripts/verify_prompts/judge.md: a verdict (fine / fixable / unfair), the
     problems, and for "fixable" a new statement. Then, for each new statement, a
     separate LEAK CHECK with scripts/verify_prompts/leak.md: does it give away the
     fix, is it accurate, is it complete. One result file per call in
     <work>/results/<id>/, so a cut-off job resumes where it stopped. A node takes
     the tasks with index % nodes == its node index.
  3. combine: the verdict by majority (2 of JUDGES); for a fixable majority, the
     best new statement that the leak check accepts and that passes the mechanical
     checks (no names the fix changes and the tests never use; no "how" wording).
     Writes <work>/report.md and <work>/verdicts.json; tasks to review by hand are
     listed first.
  4. apply: into each instance's metadata.json: "verification" (verdict, votes,
     reason, problems, missing names, leak check) and, when accepted,
     "problem_statement_fixed". The original "problem_statement" is never changed.

The work folder is <EMBEDEVAL_VERIFY_DIR> (default /scratch/scholar/$USER/embedeval/verify).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTANCES = REPO / "apptainer" / "instances"
PROMPTS = Path(__file__).resolve().parent / "verify_prompts"
WORK = Path(os.environ.get("EMBEDEVAL_VERIFY_DIR",
                           f"/scratch/scholar/{os.environ.get('USER', 'user')}/embedeval/verify"))
SHARED = Path(os.environ.get("EMBEDEVAL_SHARED",
                             f"/scratch/scholar/{os.environ.get('USER', 'user')}/embedeval/shared"))
# Local clones the fix diffs come from (NuttX: the kernel, where its fixes are).
CLONES = {"zephyr": SHARED / "zephyr.git", "riot": SHARED / "riot" / "riot.git",
          "nuttx": SHARED / "nuttx.git", "mynewt": SHARED / "mynewt-core.git"}
GITHUB_PR_FILES_CAP = 3000   # GitHub lists at most this many files of a PR
PROFILE = "qwen38-nvfp4"
JUDGES = 3
# Characters of each diff given to the model (about 3.5 characters per token): well
# inside the 262,144-token context with room for the reply and its thinking.
TEST_CHARS, FIX_CHARS = 200_000, 250_000
REQUEST_TIMEOUT = 3600
TRIES = 3                   # per call: a reply that is not valid JSON is asked again

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HOW_NOT_WHAT = re.compile(r"\b(modify|refactor|rewrite)\b|\b(must|should|to) call\b|\binternally\b"
                          r"|\bin the file\b|\b(the|a) (hidden )?tests?\b", re.I)
C_KEYWORDS = set("""if else for while do switch case return sizeof static const volatile
    struct union enum typedef extern inline unsigned signed int char short long float double
    void bool true false NULL default break continue goto register auto restrict""".split())


# -- step 1: prepare -----------------------------------------------------------

def git(project: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(CLONES[project]), *args], capture_output=True,
                       text=True, errors="replace")
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr[:300]}")
    return r.stdout


def fix_diff(meta: dict) -> str:
    project = meta["project"]
    if project == "nuttx":
        # The kernel PR (kernel_base_commit..kernel_merge_commit, limited to its files, as
        # the NuttX branch's validate_instance.gold_patch does) and any follow-up kernel
        # PRs it needs (extra_fix_prs). The tests are a change to the apps repository.
        files = meta.get("files_changed_by_fix") or []
        parts = [git(project, "diff", f"{meta['kernel_base_commit']}..{meta['kernel_merge_commit']}",
                     "--", *(files if 0 < len(files) < GITHUB_PR_FILES_CAP else ["."]))]
        for e in meta.get("extra_fix_prs") or []:
            ef = e.get("files_changed") or []
            parts.append(git(project, "diff", f"{e['base_commit']}..{e['head_commit']}", "--",
                             *(ef if 0 < len(ef) < GITHUB_PR_FILES_CAP else ["."])))
        return "\n".join(parts)
    if project == "mynewt":
        # As the Mynewt branch's validate_instance.gold_patch: from gold_base_commit when
        # the PR's test commit precedes its fix, limited to the fix's files.
        base = meta.get("gold_base_commit") or meta["base_commit"]
        files = meta.get("files_changed_by_fix") or []
        excl = [f":(exclude){p}" for p in (meta.get("protected_paths") or ["test/"])]
        return git(project, "diff", f"{base}..{meta['fix_commit']}", "--",
                   *(files if 0 < len(files) < GITHUB_PR_FILES_CAP else [".", *excl]))
    return git(project, "diff", meta["base_commit"], meta["fix_commit"], "--", ".",
               ":(exclude)tests/")


def project_facts(meta: dict) -> str:
    """The facts about the instance's project given to the model with each task (the
    instructions themselves are the same for every project)."""
    project = meta["project"]
    if project == "nuttx":
        cfg = meta.get("config_overrides") or {}
        return ("Project: Apache NuttX. Two repositories: the kernel and the applications (apps). "
                "The fix changes the kernel; the hidden tests are a change to the apps repository "
                f"(under apps/testing/), run through the test application {meta.get('test_app', '?')} "
                f"on the {meta.get('sim_config', '?')} configuration."
                + (f" The agent's build tree is already configured with {json.dumps(cfg)}, which "
                   "the agent can see." if cfg else ""))
    if project == "mynewt":
        prot = ", ".join(meta.get("protected_paths") or ["test/"])
        return ("Project: Apache Mynewt (mynewt-core). One repository; the hidden tests are unit-test "
                f"packages under {prot} (here {meta.get('test_path', '?')}), built and run with newt on "
                f"the {meta.get('platform', 'native')} platform. Configuration settings are declared in "
                "syscfg.yml files (read in C as MYNEWT_VAL(NAME)) and packages and their dependencies "
                "in pkg.yml files.")
    if project == "zephyr":
        return ("Project: Zephyr RTOS. One repository; the hidden tests are under tests/, built and "
                f"run with twister for {meta.get('platform', '?')}.")
    if project == "riot":
        return ("Project: RIOT OS. One repository; the hidden tests are under tests/, built and run "
                "on RIOT's native board.")
    return f"Project: {project}."


def added(diff: str) -> list[tuple[str, str]]:
    """(file, line) for every added line of a diff."""
    out, f = [], ""
    for l in diff.splitlines():
        if l.startswith("+++ "):
            f = l[6:] if l.startswith("+++ b/") else ""
        elif l.startswith("+") and not l.startswith("+++"):
            out.append((f, l[1:]))
    return out


def removed_words(diff: str) -> set[str]:
    return {w for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")
            for w in IDENT.findall(l[1:])}


def new_files(diff: str) -> list[str]:
    out, cur = [], None
    for l in diff.splitlines():
        if l.startswith("diff --git "):
            cur = l.split(" b/", 1)[-1]
        elif l.startswith("new file mode") and cur:
            out.append(cur)
    return out


def defined_names(diff: str) -> dict[str, str]:
    """Names the fix defines or declares on its added lines: name -> kind."""
    names: dict[str, str] = {}
    for f, line in added(diff):
        s = line.strip()
        header = f.endswith(".h")
        if f.endswith(("Kconfig", ".kconfig")) or "/Kconfig" in f or f.startswith("Kconfig"):
            m = re.match(r"(?:menu)?config\s+(\w+)", s)
            if m:
                names["CONFIG_" + m.group(1)] = "Kconfig option"
            continue
        if f.endswith(("Makefile", ".mk", "Makefile.dep", "Makefile.include")) or "/Makefile" in f:
            for m in re.finditer(r"(?:PSEUDOMODULES|USEMODULE|DEFAULT_MODULE)\s*\+?=\s*(.+)", s):
                for w in m.group(1).split():
                    if re.fullmatch(r"[A-Za-z_][\w-]*", w):
                        names[w] = "build module"
            continue
        if f.endswith("syscfg.yml"):        # Mynewt settings: keys under syscfg.defs
            m = re.match(r"\s{2,6}([A-Z][A-Z0-9_]{2,}):\s*$", line)
            if m:
                names[m.group(1)] = "syscfg setting"
            continue
        if f.endswith("pkg.yml"):           # Mynewt packages
            m = re.match(r"pkg\.name:\s*[\"']?([\w./-]+)", s)
            if m:
                names[m.group(1)] = "package"
            continue
        if not f.endswith((".c", ".h")):
            continue
        m = re.match(r"#\s*define\s+(\w+)", s)
        if m:
            names[m.group(1)] = "macro"
            continue
        m = re.match(r"(?:typedef\s+)?(struct|union|enum)\s+(\w+)\s*\{?", s)
        if m and not s.endswith(";") or (m and "{" in s):
            names.setdefault(m.group(2), m.group(1))
        m = re.match(r"\}\s*(\w+)\s*;", s)
        if m:
            names[m.group(1)] = "type"
        m = re.match(r"typedef\b.*?\b(\w+)\s*(?:\[[^\]]*\])?\s*;", s)
        if m:
            names[m.group(1)] = "type"
        # function declarations or definitions, not static (static ones are internal)
        if not s.startswith(("static", "return", "#", "//", "/*", "*")) and not line[:1].isspace():
            m = re.match(r"(?:[\w\*\s]+?)\b(\w+)\s*\(", s)
            if m and m.group(1) not in C_KEYWORDS:
                names[m.group(1)] = "function"
        if header:
            m = re.match(r"([A-Z][A-Z0-9_]{2,})\s*(?:=[^,]*)?,?\s*(?:/\*.*)?$", s)
            if m:
                names.setdefault(m.group(1), "enum value")
            if line[:1].isspace():
                m = re.match(r"(?:const\s+)?[\w\s\*]+?\b(\w+)\s*(?:\[[^\]]*\])?\s*;", s)
                if m and m.group(1) not in C_KEYWORDS and not s.startswith("return"):
                    names.setdefault(m.group(1), "field")
    for p in new_files(diff):
        if p.endswith(".h"):
            names[p.split("/include/", 1)[-1] if "/include/" in p else p.rsplit("/", 1)[-1]] = "header"
    return names


def test_uses(diff: str) -> set[str]:
    words = set()
    for _, line in added(diff):
        words |= set(IDENT.findall(line))
        for m in re.finditer(r'#\s*include\s*[<"]([^>"]+)[>"]', line):
            words.add(m.group(1))
        for m in re.finditer(r"USEMODULE\s*\+?=\s*(.+)", line):
            words |= set(m.group(1).split())
        for m in re.finditer(r"[\w-]+(?:/[\w.-]+)+", line):     # package paths in pkg.yml deps
            words.add(m.group().lstrip("@").split("/", 1)[-1] if m.group().startswith("@") else m.group())
    return words


def name_check(statement: str, tests: str, fix: str) -> list[dict]:
    """Names the fix introduces that the tests use and the statement never mentions."""
    defined, used, old = defined_names(fix), test_uses(tests), removed_words(fix)
    out = []
    for name, kind in sorted(defined.items()):
        if len(name) < 3 or name in old or name not in used:
            continue
        bare = name[7:] if name.startswith("CONFIG_") else name
        if name in statement or (kind == "Kconfig option" and bare in statement) \
                or (kind == "header" and name.rsplit("/", 1)[-1] in statement):
            continue
        out.append({"name": name, "kind": kind})
    return out


def shorten(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n[... {len(text) - limit} more characters not shown ...]\n", True


def prepare(only: list[str] | None, source: Path) -> None:
    """`source`: the folder holding the instance folders (default apptainer/instances; for
    another project's branch, that branch's folder, e.g. in a git worktree)."""
    (WORK / "tasks").mkdir(parents=True, exist_ok=True)
    ids = sorted(d.name for d in source.iterdir()
                 if (d / "metadata.json").exists() and d.name.split("__")[0] in CLONES)
    if only:
        ids = [i for i in ids if i in only]
    bad = []
    for n, iid in enumerate(ids, 1):
        d = source / iid
        meta = json.loads((d / "metadata.json").read_text())
        tests = (d / "test_patch.diff").read_text(errors="replace")
        try:
            fix = fix_diff(meta)
        except RuntimeError as e:
            bad.append(f"{iid}: {e}")
            continue
        t, t_cut = shorten(tests, TEST_CHARS)
        f, f_cut = shorten(fix, FIX_CHARS)
        task = {"instance_id": iid, "project": meta["project"], "folder": str(d.resolve()),
                "facts": project_facts(meta),
                "statement": meta["problem_statement"], "tests": t, "fix": f,
                "tests_shortened": t_cut, "fix_shortened": f_cut,
                "missing_names": name_check(meta["problem_statement"], tests, fix)}
        (WORK / "tasks" / f"{iid}.json").write_text(json.dumps(task, indent=1))
        if n % 50 == 0:
            print(f"  {n}/{len(ids)}", flush=True)
    (WORK / "tasks.txt").write_text("\n".join(i for i in ids if (WORK / "tasks" / f"{i}.json").exists())
                                    + "\n")
    tasks = [json.loads((WORK / "tasks" / f"{i}.json").read_text())
             for i in (WORK / "tasks.txt").read_text().split()]
    print(f"{len(tasks)} tasks in {WORK / 'tasks'}; with missing names: "
          f"{sum(bool(t['missing_names']) for t in tasks)}; shortened: "
          f"{sum(t['tests_shortened'] or t['fix_shortened'] for t in tasks)}")
    for b in bad:
        print("  NOT PREPARED:", b)


# -- step 2: review ------------------------------------------------------------

def judge_message(task: dict) -> str:
    names = "\n".join(f"- {m['name']} ({m['kind']})" for m in task["missing_names"]) or "(none found)"
    return (f"# PROJECT\n\n{task.get('facts', '')}\n\n"
            f"# PROBLEM STATEMENT\n\n{task['statement']}\n\n"
            f"# HIDDEN TESTS (diff){' (shortened)' if task['tests_shortened'] else ''}\n\n"
            f"```diff\n{task['tests']}\n```\n\n"
            f"# FIX (diff, without tests){' (shortened)' if task['fix_shortened'] else ''}\n\n"
            f"```diff\n{task['fix']}\n```\n\n"
            f"# NAMES THE TESTS NEED, NOT IN THE STATEMENT\n\n{names}\n")


def leak_message(task: dict, new: str) -> str:
    return (f"# PROJECT\n\n{task.get('facts', '')}\n\n"
            f"# ORIGINAL STATEMENT\n\n{task['statement']}\n\n# NEW STATEMENT\n\n{new}\n\n"
            f"# HIDDEN TESTS (diff)\n\n```diff\n{task['tests']}\n```\n\n"
            f"# FIX (diff, without tests)\n\n```diff\n{task['fix']}\n```\n")


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


def judge_ok(v: dict | None) -> bool:
    if not v or v.get("verdict") not in ("fine", "fixable", "unfair"):
        return False
    return v["verdict"] != "fixable" or bool(str(v.get("new_statement") or "").strip())


def leak_ok(v: dict | None) -> bool:
    return bool(v) and v.get("decision") in ("accept", "reject", "unsure") \
        and v.get("leak") in ("none", "minor", "major")


class Client:
    def __init__(self, api_base: str):
        prof = json.loads((REPO / "triage" / "models.json").read_text())[PROFILE]
        self.url = api_base.rstrip("/") + "/chat/completions"
        self.sampling = dict(prof.get("sampling", {}))
        self.max_tokens = prof.get("max_tokens", 32768)

    def call(self, system: str, user: str) -> dict:
        body = {"model": PROFILE, "messages": [{"role": "system", "content": system},
                                               {"role": "user", "content": user}],
                "max_tokens": self.max_tokens, **self.sampling}
        req = urllib.request.Request(self.url, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        t = time.time()
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            out = json.loads(r.read())
        msg = out["choices"][0]["message"]
        return {"content": msg.get("content"), "reasoning": msg.get("reasoning_content")
                or msg.get("reasoning"), "finish_reason": out["choices"][0].get("finish_reason"),
                "usage": out.get("usage"), "seconds": round(time.time() - t, 1)}


def one_call(client: Client, path: Path, system: str, user: str, ok) -> dict:
    """A call whose result is saved to `path`; skipped if already there."""
    if path.exists():
        return json.loads(path.read_text())
    record = {"tries": []}
    for _ in range(TRIES):
        try:
            r = client.call(system, user)
        except Exception as e:                          # the server, a timeout: try again
            record["tries"].append({"error": f"{type(e).__name__}: {e}"[:500]})
            time.sleep(10)
            continue
        v = parse(r["content"])
        record["tries"].append({k: r[k] for k in ("finish_reason", "usage", "seconds")})
        if ok(v):
            record.update(result=v, raw=r)
            break
        record.setdefault("bad_replies", []).append(r)
    record.setdefault("result", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    tmp.replace(path)
    return record


def review_task(client: Client, iid: str, log) -> None:
    task = json.loads((WORK / "tasks" / f"{iid}.json").read_text())
    out = WORK / "results" / iid
    judge_sys, leak_sys = (PROMPTS / "judge.md").read_text(), (PROMPTS / "leak.md").read_text()
    msg = judge_message(task)
    with ThreadPoolExecutor(JUDGES) as ex:
        judges = list(ex.map(lambda k: one_call(client, out / f"judge{k}.json", judge_sys, msg,
                                                judge_ok), range(1, JUDGES + 1)))
    verdicts = [(j["result"] or {}).get("verdict") for j in judges]
    top, n = Counter(v for v in verdicts if v).most_common(1)[0] if any(verdicts) else (None, 0)
    if top == "fixable" and n * 2 > JUDGES:
        for k, j in enumerate(judges, 1):
            v = j["result"] or {}
            if v.get("verdict") == "fixable":
                one_call(client, out / f"leak{k}.json", leak_sys,
                         leak_message(task, v["new_statement"]), leak_ok)
    log(f"{iid}: {verdicts}")


def review(api_base: str, share: str, parallel: int) -> None:
    i, n = (int(x) for x in share.split("/"))
    ids = [iid for k, iid in enumerate((WORK / "tasks.txt").read_text().split()) if k % n == i]
    todo = [iid for iid in ids if not done(iid)]
    lock = threading.Lock()

    def log(m: str) -> None:
        with lock:
            print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

    log(f"share {share}: {len(ids)} tasks, {len(todo)} to do, {parallel} at a time")
    client = Client(api_base)

    def safe(iid: str) -> None:
        try:
            review_task(client, iid, log)
        except Exception as e:
            log(f"{iid}: ERROR {type(e).__name__}: {e}")

    with ThreadPoolExecutor(parallel) as ex:
        list(ex.map(safe, todo))
    log(f"share {share} finished; not done: {[x for x in ids if not done(x)]}")


def done(iid: str) -> bool:
    out = WORK / "results" / iid
    judges = [out / f"judge{k}.json" for k in range(1, JUDGES + 1)]
    if not all(p.exists() for p in judges):
        return False
    vs = [(json.loads(p.read_text())["result"] or {}).get("verdict") for p in judges]
    if Counter(vs)["fixable"] * 2 > JUDGES:
        return all((out / f"leak{k}.json").exists() for k, v in enumerate(vs, 1) if v == "fixable")
    return True


# -- step 3: combine -----------------------------------------------------------

def mechanical_issues(new: str, task: dict) -> tuple[list[str], list[str]]:
    """(blocking, notes). Blocking: names the FIX DEFINES (new functions, macros, fields,
    types, modules...) that the tests never use and the original statement does not
    have: an internal of the fix, a hint at the implementation. Notes: 'how' or test
    wording, for the report only (words like "modify" also occur innocently)."""
    # Only names the fix defines in source files and nowhere public (headers, Kconfig,
    # Makefiles): a public header's names are interface, a .c file's are implementation.
    fix = task["fix"]
    public = defined_names("\n".join(_files(fix, lambda f: not f.endswith(".c"))))
    defined = set(defined_names("\n".join(_files(fix, lambda f: f.endswith(".c"))))) - set(public)
    test_words = set(IDENT.findall(task["tests"])) | test_uses(task["tests"])
    old = set(IDENT.findall(task["statement"]))
    leaks = sorted(w for w in set(IDENT.findall(new))
                   if w in defined and w not in test_words and w not in old and len(w) > 2)
    blocking = [f"names the fix defines in source files that the tests never use: {leaks}"] if leaks else []
    how = sorted({m.group().lower() for m in HOW_NOT_WHAT.finditer(new)}
                 - {m.group().lower() for m in HOW_NOT_WHAT.finditer(task["statement"])})
    notes = [f"'how' or test wording: {how}"] if how else []
    return blocking, notes


def _files(diff: str, keep) -> list[str]:
    """The parts of a diff for the files `keep` accepts."""
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    return [p for p in parts if p.startswith("diff --git ") and keep(p.split(" b/", 1)[-1].split("\n", 1)[0])]


def combine() -> dict:
    ids = (WORK / "tasks.txt").read_text().split()
    final = {}
    for iid in ids:
        task = json.loads((WORK / "tasks" / f"{iid}.json").read_text())
        out = WORK / "results" / iid
        judges = [json.loads(p.read_text()) if p.exists() else {"result": None}
                  for p in (out / f"judge{k}.json" for k in range(1, JUDGES + 1))]
        res = [j["result"] or {} for j in judges]
        votes = [r.get("verdict") for r in res]
        counts = Counter(v for v in votes if v)
        top, n = counts.most_common(1)[0] if counts else (None, 0)
        rec = {"instance_id": iid, "project": task["project"], "votes": votes,
               "missing_names": task["missing_names"],
               "shortened": task["tests_shortened"] or task["fix_shortened"],
               "verdict": top if n * 2 > JUDGES else "split", "status": None,
               "reason": None, "problems": [], "new_statement": None, "leak_check": None}
        agree = [r for r in res if r.get("verdict") == rec["verdict"]]
        if agree:
            rec["reason"] = agree[0].get("reason")
            rec["problems"] = agree[0].get("problems") or []
        if rec["verdict"] == "fine":
            rec["status"] = "fine" if not task["missing_names"] else "review: fine, but names missing"
        elif rec["verdict"] == "unfair":
            rec["status"] = "unfair"
        elif rec["verdict"] == "split" or rec["verdict"] is None:
            rec["status"] = "review: judges disagree" if counts else "review: no valid reply"
        else:   # fixable: the best candidate the leak check accepts
            best = None
            for k, r in enumerate(res, 1):
                if r.get("verdict") != "fixable":
                    continue
                p = out / f"leak{k}.json"
                lk = (json.loads(p.read_text())["result"] if p.exists() else None) or {}
                new = r["new_statement"]
                if lk.get("leak") == "minor" and lk.get("suggested_statement"):
                    new = lk["suggested_statement"]
                mech, notes = mechanical_issues(new, task)
                cand = {"judge": k, "new_statement": new, "leak_check": lk, "mechanical": mech,
                        "notes": notes, "added": r.get("added") or []}
                good = lk.get("decision") == "accept" and lk.get("accurate") is True \
                    and lk.get("leak") in ("none", "minor") and not mech
                score = (good, lk.get("complete") or 0, -len(new))
                if best is None or score > best[0]:
                    best = (score, cand)
            cand = best[1] if best else None
            rec["new_statement"] = cand["new_statement"] if cand else None
            rec["leak_check"] = cand
            rec["status"] = "fixed" if best and best[0][0] else "review: no fix passed the checks"
        final[iid] = rec
    (WORK / "verdicts.json").write_text(json.dumps(final, indent=1))
    report(final)
    return final


def report(final: dict) -> None:
    by = Counter(r["status"].split(":")[0] for r in final.values())
    lines = ["# Statement verification", "",
             f"{len(final)} tasks: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())), "",
             "| project | fine | fixed | unfair | review |", "|---|---|---|---|---|"]
    for p in sorted({r["project"] for r in final.values()}):
        c = Counter(r["status"].split(":")[0] for r in final.values() if r["project"] == p)
        lines.append(f"| {p} | {c['fine']} | {c['fixed']} | {c['unfair']} | {c['review']} |")
    order = lambda r: (not r["status"].startswith("review"), r["status"], r["instance_id"])  # noqa: E731
    for r in sorted(final.values(), key=order):
        lines += ["", f"## {r['instance_id']}: {r['status']}", "",
                  f"Votes {r['votes']}. {r['reason'] or ''}"]
        if r["missing_names"]:
            lines.append("Names missing from the original: "
                         + ", ".join(f"`{m['name']}` ({m['kind']})" for m in r["missing_names"]))
        for p in r["problems"]:
            lines.append(f"- {p.get('category')}: {p.get('detail')}")
        if r["new_statement"]:
            lk = r["leak_check"] or {}
            c = lk.get("leak_check") or {}
            lines += ["", f"Leak check: leak {c.get('leak')}, accurate {c.get('accurate')}, "
                          f"complete {c.get('complete')}, decision {c.get('decision')}"
                          + (f"; {c.get('leak_detail')}" if c.get("leak_detail") else "")
                          + (f"; mechanical: {lk['mechanical']}" if lk.get("mechanical") else "")
                          + (f"; notes: {lk['notes']}" if lk.get("notes") else ""),
                      "", "Original:", "", "> " + r_orig(r["instance_id"]).replace("\n", "\n> "),
                      "", "New:", "", "> " + r["new_statement"].replace("\n", "\n> ")]
    (WORK / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:9]))
    print(f"report: {WORK / 'report.md'}")


def folder_of(iid: str) -> Path:
    """The instance's folder: where `prepare` found it (recorded in its task file)."""
    t = json.loads((WORK / "tasks" / f"{iid}.json").read_text())
    return Path(t["folder"]) if t.get("folder") else INSTANCES / iid


def r_orig(iid: str) -> str:
    return json.loads((folder_of(iid) / "metadata.json").read_text())["problem_statement"]


# -- step 4: apply -------------------------------------------------------------

def apply() -> None:
    """Writes the verdicts into metadata.json. <work>/overrides.json, if present, maps an
    instance id to a decision taken after the automatic checks (by the user or in a manual
    check): {"status": "review: <why>"} holds a fix back (the original statement is kept),
    {"status": "fixed", "new_statement": "...", "note": "..."} sets a corrected or written
    statement, {"status": "fine"} or {"status": "unfair"} sets the verdict."""
    final = json.loads((WORK / "verdicts.json").read_text())
    p = WORK / "overrides.json"
    overrides = json.loads(p.read_text()) if p.exists() else {}
    for iid, o in overrides.items():
        r = final[iid]
        r["auto_status"] = r["status"]
        r["status"] = o["status"]
        if "new_statement" in o:
            r["new_statement"] = o["new_statement"]
        r["override_note"] = o.get("note")
    stamp = time.strftime("%Y-%m-%d")
    n = Counter()
    for iid, r in final.items():
        p = folder_of(iid) / "metadata.json"
        text = p.read_text()
        meta = json.loads(text)
        # keep the file's own layout, so the diff shows only what was added
        m = re.search(r'^( +)"', text, re.M)
        indent = len(m.group(1)) if m else 2
        ascii_only = text.isascii()
        meta["verification"] = {
            "status": r["status"], "verdict": r["verdict"], "votes": r["votes"],
            "reason": r["reason"], "problems": r["problems"], "missing_names": r["missing_names"],
            "leak_check": (r["leak_check"] or {}).get("leak_check"), "model": PROFILE,
            "date": stamp}
        if r.get("override_note") or r.get("auto_status"):
            meta["verification"].update(auto_status=r.get("auto_status"), note=r.get("override_note"))
        if r["status"] == "fixed":
            meta["problem_statement_fixed"] = r["new_statement"]
        else:
            meta.pop("problem_statement_fixed", None)
        p.write_text(json.dumps(meta, indent=indent, ensure_ascii=ascii_only) + "\n")
        n[r["status"].split(":")[0]] += 1
    print(f"applied to {len(final)} instances: {dict(n)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--only", nargs="+")
    p.add_argument("--source", type=Path, default=INSTANCES,
                   help="folder of instance folders (default apptainer/instances)")
    p = sub.add_parser("review")
    p.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    p.add_argument("--share", default="0/1", help="I/N: tasks with index %% N == I")
    p.add_argument("--parallel", type=int, default=6, help="tasks at a time (x3 judges)")
    sub.add_parser("combine")
    sub.add_parser("apply")
    a = ap.parse_args()
    if a.cmd == "prepare":
        prepare(a.only, a.source)
    elif a.cmd == "review":
        review(a.api_base, a.share, a.parallel)
    elif a.cmd == "combine":
        combine()
    else:
        apply()


if __name__ == "__main__":
    main()
