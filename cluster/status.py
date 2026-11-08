"""
Per-PR progress of a run: <run>/status/<pr>.json, one file per PR.

    {"pr": 65697,
     "triage":   {"state": "done", "verdict": "accept", "minutes": 10.6, "note": "", "node": 0},
     "validate": {"state": "done", "outcome": "VALID", "minutes": 24.0, "note": "", "node": 1}}

A NuttX run prepared from existing instances (prepare.py --instances, a
revalidation) has entries {"pr": <apps PR>, "stage": "to validate",
"instance": <id>, "arch": <host architecture>} and no triage; its phases are
separate jobs (submit.py statement / validate / evaluate):

    statement.state done            "result" kept / written / failed (nuttx_statement.py)
                    failed          no result (the server, the cap; see note): done again
    evaluate.state  done            "grade" pass / fail / ..., "exit_status", "timed_out",
                                    "model", "steps", "limits"
                    failed          not the agent's outcome (build, server, cap): done again

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


def triage_todo(run: Path) -> list[int]:
    """PRs that reached triage and have no triage outcome yet. A PR whose
    model server failed is tried again on a resubmit."""
    out = []
    for e in manifest(run)["prs"]:
        if e["stage"] != "to triage":
            continue
        t = read(run, e["pr"]).get("triage")
        if not t or t["state"] == "server failed":
            out.append(e["pr"])
    return out


def accepted(run: Path) -> list[int]:
    """Accepted PRs, and those prepared straight for validation."""
    return [e["pr"] for e in manifest(run)["prs"]
            if e["stage"] == "to validate"
            or (read(run, e["pr"]).get("triage") or {}).get("verdict") == "accept"]


def entry(run: Path, pr: int) -> dict:
    """The PR's line of the manifest."""
    return next(e for e in manifest(run)["prs"] if e["pr"] == pr)


def statement_todo(run: Path) -> list[int]:
    """Instances of a revalidation run with no statement result yet."""
    return [e["pr"] for e in manifest(run)["prs"] if e.get("instance")
            and (read(run, e["pr"]).get("statement") or {}).get("state") != "done"]


def evaluate_todo(run: Path) -> list[int]:
    """VALID instances with no agent outcome yet; one that failed for a
    reason not the agent's (build, model server, cap) is run again."""
    out = []
    for e in manifest(run)["prs"]:
        st = read(run, e["pr"])
        if e.get("instance") and (st.get("validate") or {}).get("outcome") == "VALID" and \
                (st.get("evaluate") or {}).get("state") != "done":
            out.append(e["pr"])
    return out


def validate_todo(run: Path) -> list[int]:
    """Accepted PRs with no validation outcome yet. One skipped (by the disk
    guard, say) is tried again on a resubmit."""
    out = []
    for pr in accepted(run):
        v = read(run, pr).get("validate")
        if not v or v["state"] == "skipped":
            out.append(pr)
    return out
