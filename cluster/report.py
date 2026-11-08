#!/usr/bin/env python3
"""
A run's results: the funnel from PRs to valid instances, and each PR's outcome.

    python cluster/report.py --run 2025-09

Reads only the manifest (the plan) and status/ (what happened), so it can be
run at any time -- during a job it shows progress so far. Writes
<run>/report.md and <run>/report.json, prints the funnel, and (with
--scorecard) refreshes triage's scorecard of every verdict in the run.
Settled outcomes also go into the ledger, <shared>/ledger.jsonl
(cluster/ledger.py), so later runs skip those PRs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
REPO = CLUSTER.parent
sys.path.insert(0, str(CLUSTER))
import ledger  # noqa: E402
import status  # noqa: E402


def outcome(entry: dict, st: dict) -> tuple[str, str]:
    """(where the PR ended up, detail)."""
    if entry["stage"] == "to validate":          # a prepared instance: no triage
        t = {"state": "done", "verdict": "accept"}
    elif entry["stage"] != "to triage":
        return entry["stage"], entry.get("note", "")
    else:
        t = st.get("triage")
    if not t:
        return "triage: not yet", ""
    if t["state"] != "done":
        return f"triage: {t['state']}", t.get("note", "")
    if t.get("verdict") != "accept":
        return f"triage: {t.get('verdict')}", t.get("note", "")
    v = st.get("validate")
    if not v:
        return "accepted, validation not yet", ""
    if v["state"] != "done":
        return f"validation: {v['state']}", v.get("note", "")
    if v["outcome"] in ("needs_x86", "arm64_env_failure", "qemu_pending"):
        return v["outcome"], v.get("note", "")        # not INVALID, not settled
    if v["outcome"] == "VALID":
        return "VALID", v.get("note", "")             # e.g. retried on another board
    return "INVALID", "; ".join(x for x in (v["outcome"], v.get("note")) if x)


ORDER = ["in ledger", "hard filter", "enrich failed", "dry run error", "dry run: no runnable pair", "triage: server failed",
         "triage: cap", "triage: failed", "triage: error", "triage: reject", "triage: not yet",
         "accepted, validation not yet", "validation: skipped", "validation: failed",
         "qemu_pending", "needs_x86", "arm64_env_failure", "INVALID", "VALID"]


def evaluation(ev: dict | None) -> str | None:
    """An agent outcome in a word: pass, fail, timeout (the time limit, not
    the patch), steps (the step limit), or the failure note."""
    if not ev:
        return None
    if ev["state"] != "done":
        return f"not run: {ev.get('note', '')}"
    if ev.get("grade") == "pass":
        return "pass"
    return "timeout" if ev.get("timed_out") else "steps" if ev.get("step_limit_hit") else "fail"


def build(run: Path) -> dict:
    m = status.manifest(run)
    rows = []
    for e in m["prs"]:
        st = status.read(run, e["pr"])
        where, detail = outcome(e, st)
        row = {"pr": e["pr"], "outcome": where, "detail": detail,
               "triage_min": (st.get("triage") or {}).get("minutes"),
               "validate_min": (st.get("validate") or {}).get("minutes"),
               "pair": (st.get("triage") or {}).get("pair") or e.get("instance")}
        if e.get("instance"):
            row |= {"instance": e["instance"], "arch": e.get("arch"),
                    "statement": (st.get("statement") or {}).get("result")
                    or (st.get("statement") or {}).get("state"),
                    "evaluate": evaluation(st.get("evaluate"))}
        rows.append(row)
    counts = Counter(r["outcome"] for r in rows)
    extra = {}
    if any("instance" in r for r in rows):
        extra = {"statements": dict(Counter(r.get("statement") or "not yet" for r in rows)),
                 "evaluations": dict(Counter(r.get("evaluate") or "not yet" for r in rows
                                             if r["outcome"] == "VALID"))}
    return extra | {"run": m["name"], "generated": datetime.now().isoformat(timespec="seconds"),
            "prs": len(rows), "funnel": {k: counts[k] for k in ORDER if counts[k]}
            | {k: v for k, v in counts.items() if k not in ORDER},
            "valid": [r["pr"] for r in rows if r["outcome"] == "VALID"], "rows": rows}


def markdown(rep: dict) -> str:
    out = [f"# Run {rep['run']}", "", f"{rep['prs']} PRs; report made {rep['generated']}.", "",
           "| Outcome | PRs |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rep["funnel"].items()]
    for key in ("statements", "evaluations"):
        if key in rep:
            out += ["", f"**{key.capitalize()}:** " + ", ".join(f"{k} {v}" for k, v in rep[key].items())]
    out += ["", f"**Valid instances ({len(rep['valid'])}):** "
            + (", ".join(map(str, rep["valid"])) or "none"), "",
            "| PR | outcome | detail | pair | triage min | validation min |"
            + (" arch | statement | agent |" if "statements" in rep else ""),
            "|---|---|---|---|---|---|" + ("---|---|---|" if "statements" in rep else "")]
    for r in rep["rows"]:
        out.append(f"| {r['pr']} | {r['outcome']} | {r['detail'][:100]} | {r['pair'] or ''} | "
                   f"{r['triage_min'] if r['triage_min'] is not None else ''} | "
                   f"{r['validate_min'] if r['validate_min'] is not None else ''} |"
                   + (f" {r.get('arch')} | {r.get('statement') or ''} | {r.get('evaluate') or ''} |"
                      if "instance" in r else ""))
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True, help="run name (folder under runs/) or path")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--scorecard", action="store_true",
                   help="also refresh triage's scorecard (triage_prs.py eval)")
    args = p.parse_args()
    if "/" in args.run:
        run = Path(args.run)
    else:
        import config as cluster_config
        run = Path(cluster_config.load_or_exit(args.config)["derived"]["runs"]) / args.run
    rep = build(run)
    (run / "report.json").write_text(json.dumps(rep, indent=2) + "\n")
    (run / "report.md").write_text(markdown(rep))
    print(f"run {rep['run']}: {rep['prs']} PRs")
    for k, v in rep["funnel"].items():
        print(f"  {v:>5}  {k}")
    print(f"  valid: {', '.join(map(str, rep['valid'])) or 'none'}")
    for key in ("statements", "evaluations"):
        if key in rep:
            print(f"  {key}: {rep[key]}")
    print(f"  -> {run / 'report.md'}")
    m = status.manifest(run)
    book = Path(m.get("ledger") or Path(m["environment"]["EMBEDEVAL_SHARED"]) / "ledger.jsonl")
    n = ledger.record(book, rep["run"], m["code"]["commit"], rep["generated"], rep["rows"])
    print(f"  ledger: {n} new settled outcomes -> {book}")
    if args.scorecard and (run / "triage").exists():
        subprocess.run([sys.executable, str(REPO / "scripts" / "triage_prs.py"), "--run-dir",
                        str(run), "eval"], cwd=REPO, env=os.environ, stdout=subprocess.DEVNULL)
        print(f"  scorecard -> {run / 'triage' / 'scorecard.md'}")


if __name__ == "__main__":
    main()
