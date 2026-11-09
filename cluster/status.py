"""
Per-PR progress of a run: <run>/status/<pr>.json, one file per PR.

    {"pr": 65697,
     "triage":   {"state": "done", "verdict": "accept", "minutes": 10.6, "note": "", "node": 0},
     "validate": {"state": "done", "outcome": "VALID", "minutes": 24.0, "note": "", "node": 1}}

Mynewt runs (manifest "project": "mynewt") run triage and validation as
separate jobs (triage on a GPU node, validation on an x86 one). Triage records
the verdict as accept (an accepting kind at high or medium confidence) or
reject, with "kind", "confidence" and "failure_mode"; validation adds
"instance" and "failure_mode". A PR prepared from an existing instance
(prepare.py --instances: a revalidation) has stage "to validate" and no
triage at all.

The evaluate phase (Mynewt) adds "evaluate:<label>" (eval_label: model and
limits), {"state": "done", "grade": "pass" | "fail" | ..., "model", "limits",
"steps", "error"}, or "failed" with a note. Jobs before labels wrote the
stage "evaluate".

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


def project(run: Path) -> str:
    """The run's project; manifests from before Mynewt are Zephyr's."""
    return manifest(run).get("project", "zephyr")


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
    """Accepted PRs, and those prepared straight for validation."""
    return [e["pr"] for e in manifest(run)["prs"]
            if e["stage"] == "to validate"
            or (read(run, e["pr"]).get("triage") or {}).get("verdict") == "accept"]


def unfinished(runs: Path, but: Path | None = None, of: str = "zephyr") -> dict[int, str]:
    """PRs some run of project `of` has taken and not finished -- what a
    resubmit of that run would still do -- and the run's name. Every such run
    under `runs` but `but`."""
    out = {}
    for m in sorted(Path(runs).glob("*/manifest.json")):
        other = m.parent
        if (but and other.resolve() == Path(but).resolve()) or project(other) != of:
            continue
        for pr in triage_todo(other) + validate_todo(other):
            out.setdefault(pr, other.name)
    return out


def validate_todo(run: Path) -> list[int]:
    """Accepted PRs with no validation outcome yet."""
    return [pr for pr in accepted(run) if validate_needed(run, pr)]


def eval_label(ev_cfg: dict) -> str:
    """One evaluation: a model under its limits (config "evaluate"), e.g.
    qwen38-nvfp4-50steps-40min. Each label has its own status stage
    ("evaluate:<label>") and outputs (<run>/outputs/<label>/), so models and
    limits are compared on the same instances without overwriting each other."""
    return f"{ev_cfg['model']}-{ev_cfg['step_limit']}steps-{ev_cfg['time_limit']:g}min"


def evaluation(st: dict, label: str) -> dict:
    """A PR's outcome for `label`; also the unlabeled "evaluate" stage of the
    first evaluate jobs, when its model and limits are this label's. (That
    stage records its limits; one without them belongs to no label.)"""
    ev = st.get(f"evaluate:{label}")
    old = st.get("evaluate")
    if ev is None and old and old.get("limits"):
        lim = old["limits"]
        if eval_label({"model": old.get("model"), "step_limit": lim["steps"],
                       "time_limit": lim["minutes"]}) == label:
            return old
    return ev or {}


def harness_fault(ev: dict) -> bool:
    """A grade the harness, not the agent, caused: git apply calling the
    git-generated diff corrupt (patch capture before the byte-exact capture
    read the diff as text, and a lone carriage return broke it)."""
    return ev.get("state") == "done" and "corrupt patch" in (ev.get("error") or "")


def evaluate_needed(run: Path, pr: int, label: str) -> bool:
    """No outcome for evaluation `label` yet, or one cut short for a reason
    that is not the agent's (a cap, no time left, the model server, a
    harness_fault): the next evaluate job runs it again."""
    e = evaluation(read(run, pr), label)
    return not e or harness_fault(e) or (e["state"] == "failed" and any(
        x in e.get("note", "") for x in ("cap", "no time left", "server")))


def evaluate_todo(run: Path, label: str) -> list[int]:
    """VALID instances still to evaluate under `label` (Mynewt evaluate phase)."""
    return [e["pr"] for e in manifest(run)["prs"]
            if (read(run, e["pr"]).get("validate") or {}).get("outcome") == "VALID"
            and evaluate_needed(run, e["pr"], label)]


def entry(run: Path, pr: int) -> dict:
    """The PR's line of the manifest."""
    return next(e for e in manifest(run)["prs"] if e["pr"] == pr)
