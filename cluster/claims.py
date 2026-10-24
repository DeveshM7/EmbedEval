"""
The work queue of one phase of a run: any node takes the next PR left.

    <run>/claims/<phase>.json          {pr: {"owner", "state", "since"}}
    <run>/claims/alive/<job>-node<N>   touched every minute by each worker

A worker takes a PR by marking it "working" with its name, and marks it
"done" once the PR has its outcome (or frees it, if it was cut short). The
whole table is one file, read and written only while holding a lock on that
same file: on NFS, taking the lock is what makes a node see the other nodes'
latest writes (a node's cached view of other files, status/ included, can be
seconds behind, which let two nodes take the same PR). A "working" PR whose
worker has not touched its alive file for STALE seconds -- a node that died
-- is taken over by the next worker that asks.

What is left to do comes from status/ (status.triage_todo, validate_todo),
read once when a phase starts; the table only says who is doing it, and
which PRs this job has finished since.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

BEAT = 60          # seconds between touches of a worker's alive file
STALE = 300        # a PR whose worker has been silent this long is free


def keep_alive(run: Path, me: str) -> None:
    """Touch this worker's alive file now and every BEAT seconds after, from
    a daemon thread, for as long as the worker runs."""
    f = Path(run) / "claims" / "alive" / me
    f.parent.mkdir(parents=True, exist_ok=True)
    f.touch()

    def loop():
        while True:
            time.sleep(BEAT)
            try:
                f.touch()
            except OSError:
                pass
    threading.Thread(target=loop, daemon=True).start()


class Queue:
    def __init__(self, run: Path, phase: str, me: str, needed):
        """me: this worker's name (<job>-node<N>); needed(pr): whether a PR
        still has no outcome, as status/ says."""
        self.file = Path(run) / "claims" / f"{phase}.json"
        self.alive = Path(run) / "claims" / "alive"
        self.alive.mkdir(parents=True, exist_ok=True)
        self.file.touch()
        self.me, self.needed = me, needed
        self.held: set[int] = set()
        self._mutex = threading.Lock()     # POSIX locks do not exclude a process's own threads

    def _live(self, owner: str) -> bool:
        if owner == self.me:
            return True
        try:
            silent = (self.alive / self.me).stat().st_mtime - (self.alive / owner).stat().st_mtime
        except FileNotFoundError:
            return False
        return silent < STALE       # both times from the file server's clock

    @contextmanager
    def _table(self, timeout: float | None = None):
        """The table, locked; what the block leaves in it is written back."""
        if not self._mutex.acquire(timeout=-1 if timeout is None else timeout):
            raise TimeoutError
        try:
            with open(self.file, "r+") as fh:
                fcntl.lockf(fh, fcntl.LOCK_EX)
                text = fh.read()
                table = {int(k): v for k, v in json.loads(text).items()} if text.strip() else {}
                before = dict(table)
                yield table
                if table != before:
                    fh.seek(0)
                    fh.truncate()
                    fh.write(json.dumps({str(k): v for k, v in sorted(table.items())}))
                    fh.flush()
                    os.fsync(fh.fileno())
        finally:
            self._mutex.release()

    def take(self, todo: list[int]) -> int | None:
        """Claim the first PR of `todo` that no live worker has and no
        worker has finished."""
        with self._table() as t:
            for pr in todo:
                e = t.get(pr)
                if e and (e["state"] == "done" or self._live(e["owner"])):
                    continue
                if not self.needed(pr):
                    continue
                t[pr] = {"owner": self.me, "state": "working",
                         "since": time.strftime("%Y-%m-%dT%H:%M:%S")}
                if e:
                    t[pr]["took_over"] = e["owner"]
                self.held.add(pr)
                return pr
        return None

    def release(self, pr: int, done: bool) -> None:
        """done: the PR has its outcome; otherwise it is free again."""
        with self._table() as t:
            if (t.get(pr) or {}).get("owner") == self.me:
                if done:
                    t[pr] = t[pr] | {"state": "done"}
                else:
                    del t[pr]
            self.held.discard(pr)

    def release_all(self) -> None:
        """Free every PR this worker holds (it is being stopped). Waits only
        briefly: a PR left marked lives on as stale and is taken over."""
        try:
            with self._table(timeout=10) as t:
                for pr in list(self.held):
                    if (t.get(pr) or {}).get("owner") == self.me:
                        del t[pr]
        except (TimeoutError, OSError, ValueError):
            pass
        self.held.clear()

    def busy_elsewhere(self, todo: list[int]) -> bool:
        """Some PR of `todo` is being worked on by another live worker: it
        may still come free, if that worker dies."""
        with self._table() as t:
            return any((e := t.get(pr)) and e["state"] == "working" and e["owner"] != self.me
                       and self._live(e["owner"]) for pr in todo)
