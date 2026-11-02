"""
Per-PR progress of a run: <run>/status/<pr>.json, one file per PR.

    {"pr": 65697,
     "triage":   {"state": "done", "verdict": "accept", "minutes": 10.6, "note": "", "node": 0},
     "validate": {"state": "done", "outcome": "VALID", "minutes": 24.0, "note": "", "node": 1}}

A stage appears only once it has finished, one way or another:

    triage.state    done            a verdict (accept / reject / error)
                    cap             stopped at the per-PR cap, no verdict
                    server failed   the node's model server never came up
                    failed          no verdict for another reason (see note)
    validate.state  done            outcome "VALID", or "INVALID: <reason>"
                    skipped         not started (disk guard, ...; see note)
                    failed          generate or build failed, or the cap hit

Only one worker handles a PR, and a stage is written once, all at once
(written to a temporary file and renamed), so a file is never half-written,
and a PR whose stage has no entry yet is simply still to do -- which is all
that resuming needs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

TRIAGE_FINAL = {"done", "cap", "server failed", "failed"}


def path(run: Path, pr: int) -> Path:
    return Path(run) / "status" / f"{pr}.json"


def read(run: Path, pr: int) -> dict:
    p = path(run, pr)
    return json.loads(p.read_text()) if p.exists() else {"pr": pr}


def write_stage(run: Path, pr: int, stage: str, info: dict) -> None:
    p = path(run, pr)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = read(run, pr)
    data[stage] = info
    tmp = p.with_name(f".{p.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(p)


def manifest(run: Path) -> dict:
    return json.loads((Path(run) / "manifest.json").read_text())


def triage_needed(run: Path, pr: int) -> bool:
    """No triage outcome yet. "server failed" was written by workers before
    the queue (claims.py): a node whose server fails now just takes no PRs."""
    t = read(run, pr).get("triage")
    return not t or t["state"] == "server failed"


def validate_needed(run: Path, pr: int) -> bool:
    """No validation outcome yet ("skipped" was the disk guard's, before the
    queue: workers now wait for space instead). A PR that ran out of time
    ("... hit the cap", "no time left") has none either: the next job tries
    it again, as do jobs with longer caps or fewer builds per node."""
    v = read(run, pr).get("validate")
    return (not v or v["state"] == "skipped"
            or (v["state"] == "failed" and ("hit the cap" in v.get("note", "")
                                            or "no time left" in v.get("note", ""))))


def triage_todo(run: Path) -> list[int]:
    """PRs that reached triage and have no triage outcome yet."""
    return [e["pr"] for e in manifest(run)["prs"]
            if e["stage"] == "to triage" and triage_needed(run, e["pr"])]


def accepted(run: Path) -> list[int]:
    return [e["pr"] for e in manifest(run)["prs"]
            if (read(run, e["pr"]).get("triage") or {}).get("verdict") == "accept"]


def unfinished(runs: Path, but: Path | None = None) -> dict[int, str]:
    """PRs some run has taken and not finished -- what a resubmit of that run
    would still do -- and the run's name. Every run under `runs` but `but`."""
    out = {}
    for m in sorted(Path(runs).glob("*/manifest.json")):
        other = m.parent
        if but and other.resolve() == Path(but).resolve():
            continue
        for pr in triage_todo(other) + validate_todo(other):
            out.setdefault(pr, other.name)
    return out


def validate_todo(run: Path) -> list[int]:
    """Accepted PRs with no validation outcome yet."""
    return [pr for pr in accepted(run) if validate_needed(run, pr)]
