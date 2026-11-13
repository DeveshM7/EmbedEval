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

A NuttX run prepared from existing instances (prepare.py --instances, a
revalidation) has entries {"pr": <apps PR>, "stage": "to validate",
"instance": <id>, "arch": <host architecture>} and no triage; its phases are
separate jobs (submit.py statement / validate):

    statement.state done            "result" kept / written / failed (nuttx_statement.py)
                    failed          no result (the server, the cap; see note): done again

Contiki-NG runs add observe (observation runs, observe_todo) and finalize
(finalize_todo) stages; their statement phase writes or rechecks the problem
statement of each VALID instance (statement_todo).

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
    """PRs that reached triage and have no triage outcome yet (none in an
    observation-only run)."""
    m = manifest(run)
    if m.get("observe_only"):
        return []
    return [e["pr"] for e in m["prs"]
            if e["stage"] == "to triage" and triage_needed(run, e["pr"])]


def observe_todo(run: Path) -> list[int]:
    """Contiki candidates without a completed observation."""
    return [e["pr"] for e in manifest(run)["prs"]
            if e["stage"] == "to triage"
            and read(run, e["pr"]).get("observe", {}).get("state") != "done"]


def receipt(run: Path, folder: str, pr: int, name: str) -> dict:
    """A Contiki instance file of the run (instances/ or finalized/), or {}."""
    f = Path(run) / folder / f"contiki-ng__contiki-ng-{pr}" / name
    return json.loads(f.read_text()) if f.exists() else {}


def finalize_todo(run: Path) -> list[int]:
    """Observed-valid Contiki PRs not yet finalized (generated from the
    observation into <run>/finalized/, built and validated with repeats)."""
    return [e["pr"] for e in manifest(run)["prs"]
            if e["stage"] == "to triage"
            and read(run, e["pr"]).get("observe", {}).get("state") == "done"
            and receipt(run, "instances", e["pr"], "observed.json").get("valid") is True
            and read(run, e["pr"]).get("finalize", {}).get("state") != "done"]


# The statement check in force: keep equal to scripts/triage_prs.STATEMENT_CHECK.
STATEMENT_CHECK = 2


def instance_folder(run: Path, pr: int) -> str:
    """Where a run keeps its validated instance: finalized/ for an
    observation run, instances/ for a triage run."""
    return "finalized" if (Path(run) / "finalized" / f"contiki-ng__contiki-ng-{pr}").exists() else "instances"


def accepted(run: Path) -> list[int]:
    """Accepted PRs, and those prepared straight for validation."""
    return [e["pr"] for e in manifest(run)["prs"]
            if e["stage"] == "to validate"
            or (read(run, e["pr"]).get("triage") or {}).get("verdict") == "accept"]


def unfinished(runs: Path, but: Path | None = None, of: str = "zephyr",
               project: str | None = None) -> dict[int, str]:
    """PRs some run of project `of` (`project`, as Contiki-NG's callers name
    it) has taken and not finished -- what a resubmit of that run would still
    do -- and the run's name. Every such run under `runs` but `but`."""
    of = project or of
    out = {}
    for m in sorted(Path(runs).glob("*/manifest.json")):
        other = m.parent
        if (but and other.resolve() == Path(but).resolve()) or manifest(other).get("project", "zephyr") != of:
            continue
        for pr in triage_todo(other) + validate_todo(other):
            out.setdefault(pr, other.name)
    return out


def revalidation(run: Path) -> list[int]:
    """A Contiki-NG revalidation's PRs (prepare.py --instances): each has an
    existing instance in <run>/sources/, rendered again under the current runner."""
    return [e["pr"] for e in manifest(run)["prs"] if e["stage"] == "to validate"]


def validate_todo(run: Path) -> list[int]:
    """Accepted PRs with no validation outcome yet."""
    return [pr for pr in accepted(run) if validate_needed(run, pr)]


def valid(run: Path) -> list[int]:
    """PRs of the run whose validation outcome is VALID."""
    return [e["pr"] for e in manifest(run)["prs"]
            if (read(run, e["pr"]).get("validate") or {}).get("outcome") == "VALID"]


def entry(run: Path, pr: int) -> dict:
    """The PR's line of the manifest."""
    return next(e for e in manifest(run)["prs"] if e["pr"] == pr)
def nuttx_statement_todo(run: Path) -> list[int]:
    """Instances of a revalidation run with no statement result yet."""
    return [e["pr"] for e in manifest(run)["prs"] if e.get("instance")
            and (read(run, e["pr"]).get("statement") or {}).get("state") != "done"]


def contiki_statement_todo(run: Path, recheck: bool = False) -> list[int]:
    """VALID instances whose metadata has no problem statement yet; with
    recheck, also those whose statement has not been checked under the
    current STATEMENT_CHECK."""
    out = []
    for e in manifest(run)["prs"]:
        folder = instance_folder(run, e["pr"])
        if receipt(run, folder, e["pr"], "validation.json").get("valid") is not True:
            continue
        meta = receipt(run, folder, e["pr"], "metadata.json")
        if (not (meta.get("problem_statement") or "").strip()
                or (recheck and (meta.get("statement_check") or {}).get("version") != STATEMENT_CHECK)):
            out.append(e["pr"])
    return out


def statement_todo(run: Path, recheck: bool = False) -> list[int]:
    """The statement phase's work: a NuttX run's (its instances' statements),
    or a Contiki-NG run's (VALID instances; `recheck` is Contiki-NG's)."""
    return nuttx_statement_todo(run) if project(run) == "nuttx" else contiki_statement_todo(run, recheck)


