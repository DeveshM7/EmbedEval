"""
Every PR's settled outcome, across all runs: <shared>/ledger.jsonl.

One line per PR per run, appended as the PR settles (node_worker.py,
prepare.py; report.py fills in anything missing):

    {"pr": 65697, "outcome": "VALID", "detail": "", "run": "2025-09",
     "commit": "c5e170e...", "date": "2026-10-15T14:26:42"}

Only settled outcomes are recorded -- the ones another run would only
repeat: a hard filter, no runnable pair, a triage reject, VALID, and
INVALID unless twister itself failed to run. Failures of the machinery
(server, caps, disk guard, unfinished work) are not, so the next run tries
those PRs again. prepare.py skips every PR the ledger has settled, unless
given --redo. A PR's latest line wins; deleting a run folder does not
touch the ledger. prepare.py also skips PRs another run has taken and not
finished (status.unfinished), so two runs never work on the same PR.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path

SETTLED = {"hard filter", "dry run: no runnable pair", "triage: reject", "INVALID", "VALID"}


def settled(outcome: str, detail: str) -> bool:
    return outcome in SETTLED and not (outcome == "INVALID" and "twister did not run" in detail)


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read(path: Path) -> dict[int, dict]:
    latest = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                latest[e["pr"]] = e
    return latest


def latest(path: Path) -> dict[int, dict]:
    """Each PR's latest settled entry."""
    with _locked(path):
        return _read(path)


def record(path: Path, run: str, commit: str, date: str, rows: list[dict]) -> int:
    """Append the settled rows of a report; a row already recorded for this
    run with the same outcome is not repeated. Returns how many were added."""
    with _locked(path):
        have = _read(path)
        new = []
        for r in rows:
            if not settled(r["outcome"], r["detail"]):
                continue
            old = have.get(r["pr"])
            if old and old["run"] == run and old["outcome"] == r["outcome"]:
                continue
            new.append({"pr": r["pr"], "outcome": r["outcome"], "detail": r["detail"],
                        "run": run, "commit": commit, "date": date})
        if new:
            with open(path, "a") as fh:
                fh.writelines(json.dumps(e) + "\n" for e in new)
        return len(new)
