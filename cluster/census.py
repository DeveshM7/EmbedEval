#!/usr/bin/env python3
"""
Where every candidate of a campaign ended: one row per PR across its runs.

    python cluster/census.py --runs RUN... [--valid RUN] [--out FILE.md]

Each RUN is a run folder (or name under runs/). A PR's layer is the furthest
any run took it, in this order: an instance VALID in --valid (the
revalidation of the promoted set), VALID in a run, a validation verdict,
an observation that ran its units, a triage verdict, a preparation stage.
A later triage run over a PR (a re-triage) replaces what an earlier triage
run found for it; observation runs only add. The cause is that layer's own reason (the verdict's or receipt's), grouped
by `bucket` so that one cause shared by unrelated PRs shows as one row of
the summary -- the first place to look for a shared fix.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
sys.path.insert(0, str(CLUSTER))
import report  # noqa: E402
import status  # noqa: E402

# (bucket, pattern on the lower-cased cause); first match wins
BUCKETS = [
    ("compile-only before state, not credited", r"compile-only before state"),
    ("unsupported setup: cross-compiled port", r"arm|nxp|cross-compil|02-compile|compile-arm|platform .*absent|cc26|cc13|nrf|simplelink|zoul|openmote"),
    ("unsupported setup: network/service fixture", r"tun|tap|mqtt|coap|lwm2m|snmp|border router|rpl-br|fixture"),
    ("unsupported setup: renode/doxygen/script", r"renode|doxygen|documentation"),
    ("release or merge rollup", r"rollup|release|merged-pr|context|nested merge"),
    ("compile oracle not formed", r"compile oracle|source-blocked|compile_requirements|absent at base|not a compile"),
    ("refactor or no behaviour change", r"refactor|rename|cleanup|clean-up|reorgani|no behavio|behavior-preserving|cosmetic|style"),
    ("no detecting test", r"no .*(detect|oracle|fail)|does not (detect|fail)|cannot fail|passes before|no case failed|no fail-to-pass"),
    ("validation: before state", r"before|repeatable|attribut"),
    ("validation: after state", r"after|with the fix|does not apply"),
    ("infrastructure", r"cap|timeout|time limit|server|infrastructure|hold:"),
]


def bucket(cause: str) -> str:
    c = (cause or "").lower()
    return next((b for b, pat in BUCKETS if re.search(pat, c)), "other")


def verdict_reason(run: Path, pr: int) -> str:
    f = run / "triage" / f"{pr}.json"
    try:
        v = json.loads(f.read_text())
    except (OSError, ValueError):
        return ""
    return str(v.get("reason") or v.get("hold_reason") or "")[:300]


def observation(run: Path, pr: int) -> tuple[str, str] | None:
    o = status.receipt(run, "instances", pr, "observed.json")
    if not o:
        return None
    b, a = (o.get("before") or {}).get("rc"), (o.get("after") or {}).get("rc")
    if o.get("valid"):
        return "observed fail-to-pass", ""
    if b == 2 and a == 0:
        return "observed: base build fails, fix builds", "compile-only before state (observation cannot credit it)"
    return "observed: no fail-to-pass", str(o.get("reason") or "")


RANK = ["prepare", "triage", "observation", "validation", "VALID", "promotable"]


def census(runs: list[Path], valid_run: Path | None = None) -> list[dict]:
    rows: dict[int, dict] = {}

    triaged: dict[int, str] = {}      # PR -> the last triage run that took it

    def offer(pr: int, layer: str, outcome: str, cause: str, run: Path) -> None:
        old = rows.get(pr)
        replaces = (not observe_only and pr in triaged and triaged[pr] != run.name
                    and old is not None and old["run"] == triaged[pr])
        if old is None or replaces or RANK.index(layer) > RANK.index(old["layer"]):
            rows[pr] = {"pr": pr, "layer": layer, "outcome": outcome, "cause": cause, "run": run.name}

    for run in runs:
        m = status.manifest(run)
        observe_only = bool(m.get("observe_only"))
        for e in m["prs"]:
            pr, st = e["pr"], status.read(run, e["pr"])
            where, detail = report.outcome(e, st, run, bool(m.get("observe_only")))
            if where == "in another run":
                continue
            if where == "VALID":
                offer(pr, "VALID", where, detail, run)
            elif where.startswith(("INVALID", "validation:", "finalize:", "VALID,")):
                offer(pr, "validation", where, detail, run)
            elif where.startswith("triage:") or where.startswith("accepted"):
                offer(pr, "triage", where, verdict_reason(run, pr) or detail, run)
            elif where.startswith(("observation", "observed")):
                o = observation(run, pr)
                if o:
                    offer(pr, "observation", o[0], o[1] or detail, run)
                else:
                    offer(pr, "prepare", where, detail, run)
            else:
                offer(pr, "prepare", where, detail, run)
            if not observe_only:
                triaged[pr] = run.name
    if valid_run is not None:
        for pr in status.valid(valid_run):
            rows[pr] = {"pr": pr, "layer": "promotable", "outcome": "VALID (revalidated)", "cause": "",
                        "run": valid_run.name}
    for r in rows.values():
        r["bucket"] = "" if r["layer"] in ("promotable", "VALID") else bucket(f"{r['outcome']} {r['cause']}")
    return [rows[pr] for pr in sorted(rows)]


def markdown(rows: list[dict]) -> str:
    out = ["| Layer | Bucket | PRs |", "|---|---|---|"]
    groups = Counter((r["layer"], r["bucket"]) for r in rows)
    for (layer, b), n in sorted(groups.items(), key=lambda kv: (-RANK.index(kv[0][0]), -kv[1])):
        prs = [str(r["pr"]) for r in rows if (r["layer"], r["bucket"]) == (layer, b)]
        out.append(f"| {layer} | {b or '-'} | {n}: {', '.join(prs[:40])}{' ...' if len(prs) > 40 else ''} |")
    out += ["", "| PR | layer | outcome | bucket | cause | run |", "|---|---|---|---|---|---|"]
    for r in rows:
        cause = r["cause"].replace("|", "/").replace("\n", " ")[:160]
        out.append(f"| {r['pr']} | {r['layer']} | {r['outcome']} | {r['bucket']} | {cause} | {r['run']} |")
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--valid", help="the revalidation run of the promoted set")
    p.add_argument("--config", help="default: cluster/config.yaml")
    p.add_argument("--out", type=Path, help="write the table here (markdown) and beside it .json")
    args = p.parse_args()

    def resolve(name: str) -> Path:
        if "/" in name:
            return Path(name)
        import config as cluster_config
        return Path(cluster_config.load_or_exit(args.config)["derived"]["runs"]) / name

    rows = census([resolve(r) for r in args.runs], resolve(args.valid) if args.valid else None)
    text = markdown(rows)
    if args.out:
        args.out.write_text(text)
        args.out.with_suffix(".json").write_text(json.dumps(rows, indent=2) + "\n")
    print(text.split("\n\n")[0])


if __name__ == "__main__":
    main()
