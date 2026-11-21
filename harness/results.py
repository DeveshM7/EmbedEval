#!/usr/bin/env python3
"""
Gather the evaluation's results into one table per project, a row per model.

    python harness/results.py                          # every model and project found
    python harness/results.py --model qwen38-nvfp4 --project zephyr
    python harness/results.py --instances trials/zephyr-10.txt --runs
    python harness/results.py --csv results.csv        # one row per run, for analysis

Reads each run's folder, <eval dir>/<model>/<project>/<PR>/ (harness/paths.py):
grade.json for the outcome and run.json for how the agent's run ended. Only
graded runs count. Like the harness, a run with an infrastructure error after
its retry is "not run", not a fail; the pass rate is pass / (pass + fail), and
"to do" counts instances with no grade yet.

Columns: instances (all the project's, or those in --instances), graded,
pass, fail, not run, to do, pass rate; how the graded runs ended (submitted,
step limit, time cap, context overflow, other); of the submitted fails, how
many patches did not apply or did not build; and the agent's median time and
steps per run. Times are the agent's run alone, not its grading.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402


def ending(run: dict) -> str:
    """How the agent's run ended, in the table's terms."""
    status = str(run.get("exit_status") or "")
    if run.get("hit_time_cap"):
        return "time cap"
    if run.get("hit_step_limit"):
        return "step limit"
    if "ContextWindow" in status:
        return "context"
    if status == "Submitted":
        return "submitted"
    return "other"


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def runs_of(model_dir: Path, project: str, ids: list[str]) -> list[dict]:
    """One row per instance in `ids`: its grade and run, or a 'to do' row."""
    rows = []
    for iid in ids:
        d = model_dir / project / paths.split_instance(iid)[1]
        grade, run = load(d / paths.GRADE_NAME), load(d / paths.RUN_NAME) or {}
        row = {"model": model_dir.name, "project": project, "instance_id": iid,
               "grade": grade["grade"] if grade else "to do",
               "ending": ending(run) if grade and grade["grade"] != "not_run" else "",
               "exit_status": run.get("exit_status") or "",
               "steps": run.get("steps"), "minutes": None,
               "patch_applied": grade.get("patch_applied") if grade else None,
               "build_ok": grade.get("build_ok") if grade else None,
               "node": run.get("node") or "",
               "error": ((grade or {}).get("error") or run.get("infra_error") or "")[:200]}
        if run.get("elapsed_seconds") is not None:
            row["minutes"] = round(run["elapsed_seconds"] / 60, 1)
        rows.append(row)
    return rows


def summary(rows: list[dict]) -> dict:
    count = lambda key, value: sum(r[key] == value for r in rows)   # noqa: E731
    p, f = count("grade", "pass"), count("grade", "fail")
    graded = [r for r in rows if r["grade"] in ("pass", "fail")]
    sub_fail = [r for r in graded if r["grade"] == "fail" and r["ending"] == "submitted"]
    med = lambda xs: statistics.median(xs) if xs else None          # noqa: E731
    return {
        "instances": len(rows), "graded": p + f + count("grade", "not_run"),
        "pass": p, "fail": f, "not run": count("grade", "not_run"), "to do": count("grade", "to do"),
        "pass rate": f"{100 * p / (p + f):.1f}%" if p + f else "-",
        **{e: sum(r["ending"] == e for r in graded)
           for e in ("submitted", "step limit", "time cap", "context", "other")},
        "no apply": sum(r["patch_applied"] is False for r in sub_fail),
        "no build": sum(r["patch_applied"] is not False and r["build_ok"] is False for r in sub_fail),
        "median min": med([r["minutes"] for r in graded if r["minutes"] is not None]),
        "median steps": med([r["steps"] for r in graded if r["steps"] is not None]),
    }


def markdown(table: list[dict], first: str) -> str:
    cols = [first] + [k for k in table[0] if k != first]
    fmt = lambda v: "-" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))  # noqa: E731
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(row[c]) for c in cols) + " |" for row in table]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", nargs="+", help="models (default: every one with results)")
    p.add_argument("--project", nargs="+", choices=paths.PROJECTS,
                   help="projects (default: every one with results)")
    p.add_argument("--instances", help="file of instance ids: count only these (a trial)")
    p.add_argument("--runs", action="store_true", help="also list every run")
    p.add_argument("--csv", help="write one row per run to this file")
    a = p.parse_args()

    root = paths.OUTPUTS_DIR
    models = ([root / paths.model_dir(m) for m in a.model] if a.model
              else sorted(d for d in root.iterdir()
                          if d.is_dir() and any((d / pr).is_dir() for pr in paths.PROJECTS)))
    chosen = set(Path(a.instances).read_text().split()) if a.instances else None
    all_rows = []
    for project in a.project or paths.PROJECTS:
        if not any((m / project).is_dir() for m in models):
            continue
        ids = [i for i in paths.discover_instances(project) if chosen is None or i in chosen]
        rows = {m.name: runs_of(m, project, ids) for m in models}
        print(f"\n## {project} ({len(ids)} instances)\n")
        print(markdown([{"model": name, **summary(r)} for name, r in rows.items()], "model"))
        for r in rows.values():
            all_rows += r
        if a.runs:
            for name, r in rows.items():
                print(f"\n### {name}, {project}\n")
                print(markdown([{k: x[k] for k in ("instance_id", "grade", "ending", "minutes",
                                                     "steps", "error")} for x in r], "instance_id"))
    if not all_rows:
        sys.exit(f"no results under {root}")
    print(f"\nFrom {root}. Pass rate = pass / (pass + fail); 'not run' = infrastructure "
          "error after its retry; 'to do' = not graded yet.")
    if a.csv:
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0]))
            w.writeheader()
            w.writerows(all_rows)
        print(f"{len(all_rows)} runs written to {a.csv}")


if __name__ == "__main__":
    main()
