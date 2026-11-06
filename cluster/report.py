#!/usr/bin/env python3
"""
A run's results: the funnel from PRs to valid instances, and each PR's outcome.

    python cluster/report.py --run 2025-09

Reads only the manifest (the plan) and status/ (what happened), so it can be
run at any time -- during a job it shows progress so far. Writes
<run>/report.md and <run>/report.json, prints the funnel, and (with
--scorecard) refreshes triage's scorecard of every verdict in the run.
Settled outcomes also go into the ledger, <shared>/ledger.jsonl
(cluster/ledger.py), so later runs skip those PRs; the workers write them as
each PR settles, and this fills in anything missing.
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
    """(where the PR ended up, detail). A PR prepared "to validate" (a
    revalidation of an existing instance) has no triage."""
    if entry["stage"] not in ("to triage", "to validate"):
        return entry["stage"], entry.get("note", "")
    if entry["stage"] == "to triage":
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
    if v["outcome"] == "VALID":
        return "VALID", v.get("note", "")             # e.g. retried on another board
    return "INVALID", "; ".join(x for x in (v["outcome"], v.get("note")) if x)


ORDER = ["in ledger", "in another run", "fetch failed", "hard filter", "enrich failed", "dry run error", "dry run: no runnable pair", "enrich: no runnable test", "triage: server failed",
         "triage: cap", "triage: failed", "triage: error", "triage: reject", "triage: not yet",
         "accepted, validation not yet", "validation: skipped", "validation: failed",
         "INVALID", "VALID"]


def build(run: Path) -> dict:
    m = status.manifest(run)
    rows = []
    for e in m["prs"]:
        st = status.read(run, e["pr"])
        where, detail = outcome(e, st)
        v = st.get("validate") or {}
        rows.append({"pr": e["pr"], "outcome": where, "detail": detail,
                     "triage_min": (st.get("triage") or {}).get("minutes"),
                     "validate_min": v.get("minutes"),
                     "pair": (st.get("triage") or {}).get("pair")
                     or " ".join(x for x in (v.get("instance"), v.get("failure_mode")) if x) or None}
                    | ({"agent": st["evaluate"].get("grade") or st["evaluate"]["state"]} if st.get("evaluate") else {}))
    counts = Counter(r["outcome"] for r in rows)
    agents = Counter(r["agent"] for r in rows if "agent" in r)
    return {"run": m["name"], "project": m.get("project", "zephyr"),
            "generated": datetime.now().isoformat(timespec="seconds"),
            "prs": len(rows), "funnel": {k: counts[k] for k in ORDER if counts[k]}
            | {k: v for k, v in counts.items() if k not in ORDER},
            "valid": [r["pr"] for r in rows if r["outcome"] == "VALID"],
            **({"agents": dict(agents)} if agents else {}), "rows": rows}


def to_ledger(run: Path, prs: set[int] | None = None) -> tuple[int, Path]:
    """Record the settled outcomes of a run's PRs -- all of them, or these --
    in the ledger. Workers call it as each PR settles; report and prepare
    for the whole run. Returns (how many were new, the ledger)."""
    m = status.manifest(run)
    rows = []
    for e in m["prs"]:
        if prs is None or e["pr"] in prs:
            where, detail = outcome(e, status.read(run, e["pr"]))
            rows.append({"pr": e["pr"], "outcome": where, "detail": detail})
    book = Path(m["environment"]["EMBEDEVAL_SHARED"]) / "ledger.jsonl"
    n = ledger.record(book, m["name"], m["code"]["commit"],
                      datetime.now().isoformat(timespec="seconds"), rows, m.get("project", "zephyr"))
    return n, book


def markdown(rep: dict) -> str:
    out = [f"# Run {rep['run']}", "", f"{rep['prs']} PRs; report made {rep['generated']}.", "",
           "| Outcome | PRs |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rep["funnel"].items()]
    out += ["", f"**Valid instances ({len(rep['valid'])}):** "
            + (", ".join(map(str, rep["valid"])) or "none"), "",
            *([f"**Agent grades:** " + ", ".join(f"{k} {v}" for k, v in rep["agents"].items()), ""]
              if rep.get("agents") else []),
            "| PR | outcome | detail | " + ("instance, oracle" if rep.get("project") == "mynewt" else "pair")
            + " | triage min | validation min |",
            "|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        out.append(f"| {r['pr']} | {r['outcome']} | {r['detail'][:100]} | {r['pair'] or ''} | "
                   f"{r['triage_min'] if r['triage_min'] is not None else ''} | "
                   f"{r['validate_min'] if r['validate_min'] is not None else ''} |")
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
    if rep.get("agents"):
        print(f"  agent grades: {rep['agents']}")
    print(f"  -> {run / 'report.md'}")
    n, book = to_ledger(run)      # normally already there: workers record as PRs settle
    print(f"  ledger: {n} new settled outcomes -> {book}")
    if args.scorecard and (run / "triage").exists():
        subprocess.run([sys.executable, str(REPO / "scripts" / "triage_prs.py"), "--run-dir",
                        str(run), "eval"], cwd=REPO, env=os.environ, stdout=subprocess.DEVNULL)
        print(f"  scorecard -> {run / 'triage' / 'scorecard.md'}")


if __name__ == "__main__":
    main()
