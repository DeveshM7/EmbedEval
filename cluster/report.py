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


def observation_outcome(run: Path, pr: int, st: dict) -> tuple[str, str]:
    """An observation-only run's PR: observed, finalized, then given a model
    problem statement. VALID only once it is promotable. None of the other
    outcomes settle the PR in the ledger: a triage run may still find another
    oracle for it."""
    o = st.get("observe")
    if not o:
        return "observation: not yet", ""
    if o["state"] != "done":
        return f"observation: {o['state']}", o.get("note", "")
    observed = status.receipt(run, "instances", pr, "observed.json")
    if not observed:
        return "observation: not observable", o.get("note", "")
    if not observed.get("valid"):
        return "observation: no fail-to-pass", observed.get("reason", "")
    f = st.get("finalize")
    if not f:
        return "observed, finalize not yet", ""
    if f["state"] != "done":
        return "finalize: failed", f.get("outcome", "")
    if f["outcome"] != "VALID":
        return ("finalize: hold" if f["outcome"] == "HOLD" else "finalize: INVALID"), f["outcome"]
    meta = status.receipt(run, "finalized", pr, "metadata.json")
    if not (meta.get("problem_statement") or "").strip():
        s = st.get("statement")
        return ("VALID, statement failed" if s and s["state"] != "done" else "VALID, statement not yet"), ""
    return "VALID", f"observation-backed; statement by {(meta.get('statement_provenance') or {}).get('model', '?')}"


def outcome(entry: dict, st: dict, run: Path | None = None, observe_only: bool = False) -> tuple[str, str]:
    """(where the PR ended up, detail)."""
    if entry["stage"] == "to validate":                 # a revalidation (prepare.py --instances)
        return validation_outcome(st)
    if entry["stage"] != "to triage":
        return entry["stage"], entry.get("note", "")
    if observe_only:
        return observation_outcome(run, entry["pr"], st)
    t = st.get("triage")
    if not t:
        return "triage: not yet", ""
    if t["state"] != "done":
        return f"triage: {t['state']}", t.get("note", "")
    if not isinstance(t.get("verdict"), str):          # the model nested its answer
        return "triage: malformed verdict", "the verdict field is not accept/reject/hold"
    if t.get("verdict") != "accept":
        return f"triage: {t.get('verdict')}", t.get("note", "")
    return validation_outcome(st)


def validation_outcome(st: dict) -> tuple[str, str]:
    v = st.get("validate")
    if not v:
        return "accepted, validation not yet", ""
    if v["state"] != "done":
        return f"validation: {v['state']}", v.get("note", "")
    if v["outcome"] == "HOLD":
        return "validation: hold", v.get("note", "")
    if v["outcome"] == "VALID":
        return "VALID", v.get("note", "")             # e.g. retried on another board
    return "INVALID", "; ".join(x for x in (v["outcome"], v.get("note")) if x)


ORDER = ["in ledger", "in another run", "fetch failed", "hard filter", "enrich failed", "dry run error", "dry run: no runnable pair", "enrich: no runnable test", "triage: server failed",
         "triage: cap", "triage: failed", "triage: error", "triage: malformed verdict", "triage: reject", "triage: not yet",
         "accepted, validation not yet", "validation: skipped", "validation: failed",
         "observation: not yet", "observation: cap", "observation: failed", "observation: not observable",
         "observation: no fail-to-pass", "observed, finalize not yet", "finalize: failed",
         "finalize: hold", "finalize: INVALID", "VALID, statement not yet", "VALID, statement failed",
         "INVALID", "VALID"]


def evaluations(st: dict) -> dict:
    """A PR's agent outcome per evaluation label (status.eval_label); the
    unlabeled stage of the first evaluate jobs is labeled from its limits
    ("unlabeled" if it recorded none)."""
    out = {}
    for key, ev in sorted(st.items()):        # "evaluate" first: a labeled stage overrides it
        if key == "evaluate":
            lim = ev.get("limits")
            label = status.eval_label({"model": ev.get("model"), "step_limit": lim["steps"],
                                       "time_limit": lim["minutes"]}) if lim else "unlabeled"
        elif key.startswith("evaluate:"):
            label = key.split(":", 1)[1]
        else:
            continue
        out[label] = ("harness fault" if status.harness_fault(ev)
                      else "unfinished" if ev.get("unfinished")      # not a pass or a fail
                      else "steps" if ev.get("grade") == "fail" and ev.get("step_limit_hit")
                      else ev.get("grade") or ev["state"])
    return out


def build(run: Path) -> dict:
    m = status.manifest(run)
    nuttx = m.get("project") == "nuttx"
    observe_only = bool(m.get("observe_only"))
    rows = []
    for e in m["prs"]:
        st = status.read(run, e["pr"])
        where, detail = outcome(e, st, run, observe_only)
        v = st.get("validate") or {}
        row = {"pr": e["pr"], "outcome": where, "detail": detail,
               "triage_min": (st.get("triage") or {}).get("minutes"),
               "validate_min": v.get("minutes"),
               "pair": (st.get("triage") or {}).get("pair")
               or " ".join(x for x in (v.get("instance"), v.get("failure_mode")) if x) or None,
               "observe": (st.get("observe") or {}).get("state")}
        if m.get("project") == "contiki-ng" and where == "VALID":   # where the instance is
            row["instance"] = str(run / ("finalized" if observe_only else "instances")
                                  / f"contiki-ng__contiki-ng-{e['pr']}")
        if nuttx and e.get("instance"):     # a NuttX instance prepared for revalidation
            row |= {"pair": row["pair"] or e["instance"], "instance": e["instance"],
                    "arch": e.get("arch"),
                    "statement": (st.get("statement") or {}).get("result")
                    or (st.get("statement") or {}).get("state")}
        rows.append(row | ({"agent": evaluations(st)} if evaluations(st) else {}))
    counts = Counter(r["outcome"] for r in rows)
    statements = (dict(Counter(r.get("statement") or "not yet" for r in rows if "arch" in r))
                  if any("arch" in r for r in rows) else None)
    observations = Counter(r["observe"] for r in rows if r["observe"])
    agents = {label: dict(Counter(r["agent"][label] for r in rows if label in r.get("agent", {})))
              for label in sorted({k for r in rows for k in r.get("agent", {})})}
    return {"run": m["name"], "project": m.get("project", "zephyr"),
            "generated": datetime.now().isoformat(timespec="seconds"),
            "prs": len(rows), "funnel": {k: counts[k] for k in ORDER if counts[k]}
            | {k: v for k, v in counts.items() if k not in ORDER},
            "valid": [r["pr"] for r in rows if r["outcome"] == "VALID"],
            "observations": dict(observations),
            **({"statements": statements} if statements else {}),
            **({"agents": dict(agents)} if agents else {}), "rows": rows}


def to_ledger(run: Path, prs: set[int] | None = None) -> tuple[int, Path]:
    """Record the settled outcomes of a run's PRs -- all of them, or these --
    in the ledger. Workers call it as each PR settles; report and prepare
    for the whole run. Returns (how many were new, the ledger)."""
    m = status.manifest(run)
    rows = []
    for e in m["prs"]:
        if prs is None or e["pr"] in prs:
            where, detail = outcome(e, status.read(run, e["pr"]), run, bool(m.get("observe_only")))
            rows.append({"pr": e["pr"], "outcome": where, "detail": detail})
    # A NuttX run names its own ledger (config.py), whose lines carry no project;
    # Zephyr and Mynewt share one, a Mynewt line tagged with its project.
    book = Path(m.get("ledger") or Path(m["environment"]["EMBEDEVAL_SHARED"]) / "ledger.jsonl")
    project = "zephyr" if m.get("project") == "nuttx" else m.get("project", "zephyr")
    n = ledger.record(book, m["name"], m["code"]["commit"],
                      datetime.now().isoformat(timespec="seconds"), rows, project)
    return n, book


def markdown(rep: dict) -> str:
    out = [f"# Run {rep['run']}", "", f"{rep['prs']} PRs; report made {rep['generated']}.", "",
           "| Outcome | PRs |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rep["funnel"].items()]
    if rep.get("observations"):
        out += ["", "Observation: " + ", ".join(f"{v} {k}" for k, v in rep["observations"].items())]
    if "statements" in rep:
        out += ["", "**Statements:** " + ", ".join(f"{k} {v}" for k, v in rep["statements"].items())]
    out += ["", f"**Valid instances ({len(rep['valid'])}):** "
            + (", ".join(map(str, rep["valid"])) or "none"), "",
            *[f"**Agent grades, {label}:** " + ", ".join(f"{k} {v}" for k, v in counts.items()) + "\n"
              for label, counts in rep.get("agents", {}).items()],
            "| PR | outcome | detail | " + ("instance, oracle" if rep.get("project") == "mynewt" else "pair")
            + " | triage min | validation min |" + (" arch | statement |" if "statements" in rep else ""),
            "|---|---|---|---|---|---|" + ("---|---|" if "statements" in rep else "")]
    for r in rep["rows"]:
        out.append(f"| {r['pr']} | {r['outcome']} | {r['detail'][:100]} | {r['pair'] or ''} | "
                   f"{r['triage_min'] if r['triage_min'] is not None else ''} | "
                   f"{r['validate_min'] if r['validate_min'] is not None else ''} |"
                   + (f" {r.get('arch')} | {r.get('statement') or ''} |" if "arch" in r else ""))
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
    if rep.get("observations"):
        print("  observed: " + ", ".join(f"{v} {k}" for k, v in rep["observations"].items()))
    print(f"  valid: {', '.join(map(str, rep['valid'])) or 'none'}")
    if rep.get("agents"):
        for label, counts in rep["agents"].items():
            print(f"  agent grades, {label}: {counts}")
    if "statements" in rep:
        print(f"  statements: {rep['statements']}")
    if rep["project"] == "contiki-ng" and rep["valid"]:
        source = "finalized" if status.manifest(run).get("observe_only") else "instances"
        print(f"  promote (after review): python scripts/promote_instance.py --source "
              f"{run / source} " + " ".join(f"contiki-ng__contiki-ng-{p}" for p in rep["valid"]))
    print(f"  -> {run / 'report.md'}")
    n, book = to_ledger(run)      # normally already there: workers record as PRs settle
    print(f"  ledger: {n} new settled outcomes -> {book}")
    if args.scorecard and (run / "triage").exists():
        project_args = ["--project", "contiki-ng"] if rep["project"] == "contiki-ng" else []
        subprocess.run([sys.executable, str(REPO / "scripts" / "triage_prs.py"), "--run-dir",
                        str(run), "eval", *project_args], cwd=REPO, env=os.environ, stdout=subprocess.DEVNULL)
        print(f"  scorecard -> {run / 'triage' / 'scorecard.md'}")


if __name__ == "__main__":
    main()
