#!/usr/bin/env python3
"""
The quick check for RIOT: does the PR's fix turn its tests from failing to passing?

    python scripts/riot_check.py --run-dir DIR                # every runnable PR
    python scripts/riot_check.py --run-dir DIR --pr 20857     # just this PR
    python scripts/riot_check.py --selftest

For each PR test selection found runnable (DIR/select/), in the build image of
its era, with no image of its own:

    before = the PR's base + its changes under tests/
    after  = before + the rest of the PR

Every selected unit-test group and test program is built and run in both, once.
A group is built alone, so one that does not compile cannot hide another's
result, with embUnit's XML output, which names every test, passing ones too. A
test program has one result: its test script's.

    strong            a target builds and fails before, and passes after
    weak              a target does not compile before, and passes after
    no_signal         targets pass before and after
    after_not_passing no target passes after

A target that passes after is what counts: another of the PR's targets failing
after does not reject it, and is recorded. Writes DIR/check/<pr>.json, which the
later stages read, and the end of each log to DIR/check/logs/<pr>/; a PR that
already has a record is skipped (--redo: again). A PR interrupted has no record
and is tried on the next run.

Temporary files: each PR works only in <work>/checks/<pr>/, deleted when it ends
however it ends. Whatever a killed job left there is deleted at the next start,
so one work folder must serve one job at a time. Each container is in its own
process group, and is killed with what it started on a timeout or SIGTERM. A
PR's folder is not allowed past DISK_LIMIT_GB, and no PR starts with less than
MIN_FREE_GB free on the disk.

What every PR downloads is shared, under <shared>/riot/: Rust crates (cargo/)
and the external code of packages (gitcache/).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au  # noqa: E402
import riot_env  # noqa: E402
from riot_select import UNIT_TESTS, unpack  # noqa: E402

BUILD_TIMEOUT = 900     # seconds
TEST_TIMEOUT = 600      # a test program, through its script
UNIT_TIMEOUT = 120      # a group's binary
JOBS = 8                # make -j, per PR
DISK_LIMIT_GB = 20      # one PR's folder
MIN_FREE_GB = 50
LOG_TAIL = 64 * 1024    # bytes of each log kept
MAX_LINES = 200_000     # of one command's output, kept to read its results
END_OF_UNITS = "</TestRun>"
CARGO_TRIES = 3         # one at a time, after a build failed fetching Rust packages
# Cargo's own locks do not hold on this disk: builds filling the shared folder at
# once fail, which must not read as the code not compiling. Not cargo's compile errors.
CARGO_FETCH_FAILED = re.compile(
    r"^(error: |\s*)(failed to (load source for dependency|download|fetch|get|update|"
    r"unpack package|clone into)|Unable to update|unable to get packages)",
    re.M)

# XML elements that are not a test suite's name
_NOT_SUITE = {"TestRun", "Statistics", "Tests", "Failures", "Location", "File", "Line", "Message"}
_stop = threading.Event()
_running: set[subprocess.Popen] = set()
_lock = threading.Lock()
_cargo_lock = threading.Lock()


class Interrupted(Exception):
    """The job is being stopped: this PR gets no record."""


class TooBig(Exception):
    """The PR's folder passed DISK_LIMIT_GB."""


class Downloads(Exception):
    """Rust packages could not be fetched: says nothing about the PR, so no record."""


def _kill(proc: subprocess.Popen) -> None:
    """The container's process group and anything below it that left the group."""
    pids = au._descendants(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _on_signal(*_) -> None:
    _stop.set()
    with _lock:
        for proc in _running:
            _kill(proc)


def parse_units(output: str) -> tuple[dict[str, str], bool]:
    """{suite.test: pass|fail} from embUnit's XML, and whether the run reached
    its end. A test that crashed the binary is not in the output."""
    tests, suite, kind = {}, "", None
    for line in output.splitlines():
        line = line.strip()
        m = re.fullmatch(r"<(Test|FailedTest) id=\"\d+\">", line)
        if m:
            kind = m.group(1)
            continue
        m = re.fullmatch(r"<Name>(.*)</Name>", line)
        if m and kind:
            tests[f"{suite}.{m.group(1)}"] = "pass" if kind == "Test" else "fail"
            kind = None
            continue
        m = re.fullmatch(r"<(\w+)>", line)
        if m and m.group(1) not in _NOT_SUITE and m.group(1) != "Name":
            suite = m.group(1)
    return tests, END_OF_UNITS in output


def unit_verdict(before: dict, after: dict) -> dict:
    """One group's result, from its two runs."""
    out = {"before": before["status"], "after": after["status"]}
    if after["status"] != "ran" or not after["complete"] or not after["tests"]:
        out["kind"] = "after_not_passing"
        return out
    b, a = before.get("tests", {}), after["tests"]
    out["fail_to_pass"] = sorted(t for t, r in a.items() if r == "pass" and b.get(t) != "pass")
    out["pass_to_pass"] = sorted(t for t, r in a.items() if r == "pass" and b.get(t) == "pass")
    out["fail_after"] = sorted(t for t, r in a.items() if r == "fail")
    if before["status"] == "build_timeout" or not (out["fail_to_pass"] or out["pass_to_pass"]):
        out["kind"] = "after_not_passing"
    elif before["status"] == "no_build":
        out["kind"] = "weak"
    elif out["fail_to_pass"]:
        out["kind"] = "strong"
    else:
        out["kind"] = "no_signal"
    return out


def program_verdict(before: str, after: str) -> dict:
    """One test program's result, from its two runs."""
    out = {"before": before, "after": after}
    if after != "pass" or before == "build_timeout":
        out["kind"] = "after_not_passing"
    elif before == "no_build":
        out["kind"] = "weak"
    elif before == "pass":
        out["kind"] = "no_signal"
    else:
        out["kind"] = "strong"
    return out


def outcome(targets: dict) -> str:
    kinds = {t["kind"] for t in targets.values()}
    return next((k for k in ("strong", "weak", "no_signal") if k in kinds), "after_not_passing")


def diff(rec: dict, *paths: str) -> bytes:
    """Exactly as git wrote it: stripped, or decoded, a patch can stop applying."""
    return subprocess.run(["git", "-C", str(riot_env.clone()), "diff", "--binary", rec["base"],
                           rec["head"], "--", *paths], capture_output=True, check=True).stdout


class Check:
    """One PR's folder and container."""

    def __init__(self, rec: dict, logs: Path) -> None:
        self.rec, self.logs = rec, logs
        self.root = au.path("checks", "x").parent / str(rec["pr"])
        self.tree = self.root / "tree"

    def make_args(self) -> str:
        return f"BOARD={self.rec['board']} BUILD_IN_DOCKER=0"

    def run(self, script: str, log: str, timeout: int, cflags: str = "",
            until: str | None = None) -> tuple[int | str, str]:
        """Run a script in the container: its exit code or 'timeout', and its output."""
        if _stop.is_set():
            raise Interrupted
        env = {"RIOT_CI_BUILD": "1", "CC_NOCOLOR": "1", "CARGO_HOME": "/cargo",
               "GIT_CACHE_DIR": "/gitcache"}
        if cflags:
            env["CFLAGS"] = cflags
        cmd = [*au.exec_args(au.riot_image(self.rec["image"]), home=self.root / "home",
                             binds={self.tree: "/testbed", self.root / "tmp": "/tmp",
                                    au.shared_path("riot", "cargo", "x").parent: "/cargo",
                                    au.shared_path("riot", "gitcache", "x").parent: "/gitcache"},
                             env=env), "bash", "-c", f"cd /testbed && {script}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                text=True, errors="replace")
        with _lock:
            _running.add(proc)
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            _kill(proc)

        timer = threading.Timer(timeout, expire)
        timer.start()
        lines = deque(maxlen=MAX_LINES)     # a binary can print the same line for minutes
        try:
            for line in proc.stdout:
                lines.append(line)
                if until and until in line:
                    _kill(proc)
                    break
            proc.stdout.close()
            rc = proc.wait()
        finally:
            timer.cancel()
            with _lock:
                _running.discard(proc)
            _kill(proc)
        if _stop.is_set():
            raise Interrupted
        text = "".join(lines)
        (self.logs / f"{log}.log").write_text(text[-LOG_TAIL:])
        return ("timeout" if expired.is_set() else 0 if until and until in text else rc), text

    def clear_build(self, folder: str) -> None:
        au.remove(self.tree / folder / "bin")
        used = subprocess.run(["du", "-sk", str(self.root)], capture_output=True, text=True)
        if used.stdout and int(used.stdout.split()[0]) > DISK_LIMIT_GB * 2**20:
            raise TooBig(f"over {DISK_LIMIT_GB} GB")

    def build(self, folder: str, extra: str, log: str, cflags: str = "") -> str | None:
        """None if it built, else why not."""
        script = f"make --no-print-directory -C {folder} {self.make_args()} {extra} -j{JOBS} all"
        rc, text = self.run(script, log, BUILD_TIMEOUT, cflags)
        if rc != 0 and CARGO_FETCH_FAILED.search(text):
            with _cargo_lock:
                for _ in range(CARGO_TRIES):
                    rc, text = self.run(script, log, BUILD_TIMEOUT, cflags)
                    if rc == 0 or not CARGO_FETCH_FAILED.search(text):
                        break
                else:
                    raise Downloads(f"{folder}: Rust packages could not be fetched")
        return None if rc == 0 else "build_timeout" if rc == "timeout" else "no_build"

    def run_unit(self, group: str, phase: str) -> dict:
        folder, tag = UNIT_TESTS, f"{phase}-{group}"
        try:
            why = self.build(folder, f"UNIT_TESTS={group}", f"{tag}-build", "-DOUTPUT=OUTPUT_XML")
            if why:
                return {"status": why}
            elf = f"{folder}/bin/{self.rec['board']}/tests_unittests.elf"
            # 's': the binary may wait to be told to start, as make test does
            rc, text = self.run(f"printf 's\\n' | {elf}", tag, UNIT_TIMEOUT, until=END_OF_UNITS)
            tests, complete = parse_units(text)
            return {"status": "ran" if rc != "timeout" else "timeout",
                    "tests": tests, "complete": complete}
        finally:
            self.clear_build(folder)

    def run_program(self, folder: str, phase: str) -> str:
        tag = f"{phase}-{folder.replace('/', '_')}"
        try:
            why = self.build(folder, "", f"{tag}-build")
            if why:
                return why
            # INSIDE_DOCKER: some test programs otherwise hand "make test" to Docker (PR 21775)
            rc, _ = self.run(f"make --no-print-directory -C {folder} {self.make_args()} "
                             "INSIDE_DOCKER=1 test",
                             tag, TEST_TIMEOUT)
            return "pass" if rc == 0 else "timeout" if rc == "timeout" else "fail"
        finally:
            self.clear_build(folder)

    def run_all(self, phase: str) -> tuple[dict, dict]:
        return ({g: self.run_unit(g, phase) for g in self.rec["units"]},
                {p: self.run_program(p, phase) for p in self.rec["programs"]})

    def apply(self, diff: bytes, name: str) -> None:
        patch = self.root / f"{name}.diff"
        patch.write_bytes(diff)
        r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=self.tree,
                           capture_output=True, text=True, errors="replace")
        if r.returncode:
            raise RuntimeError(f"{name} patch does not apply: {r.stderr.strip()[:200]}")

    def check(self) -> dict:
        r = self.rec
        out = {k: r[k] for k in ("pr", "merge", "base", "head", "board", "image")}
        fix = diff(r, ".", ":!tests/")
        if not fix:
            return {**out, "outcome": "no_fix", "targets": {}}
        tests = diff(r, "tests/")
        if not tests:       # GitHub lists test files, but these commits do not change them
            return {**out, "outcome": "no_test_change", "targets": {}}
        if self.root.exists():
            au.remove(self.root)
        for d in ("home", "tmp"):
            (self.root / d).mkdir(parents=True)
        try:
            unpack(r["base"], self.tree)
            self.apply(tests, "tests")
            units_b, progs_b = self.run_all("before")
            self.apply(fix, "fix")
            units_a, progs_a = self.run_all("after")
        finally:
            au.remove(self.root)
        targets = {g: unit_verdict(units_b[g], units_a[g]) for g in r["units"]}
        targets.update({p: program_verdict(progs_b[p], progs_a[p]) for p in r["programs"]})
        return {**out, "outcome": outcome(targets), "targets": targets}


def selftest() -> None:
    xml = ("noise\n<TestRun>\n<a_tests>\n<Test id=\"1\">\n<Name>t1</Name>\n</Test>\n"
           "<FailedTest id=\"2\">\n<Name>t2</Name>\n<Location>\n<File>f</File>\n<Line>3</Line>\n"
           "</Location>\n<Message>m</Message>\n</FailedTest>\n</a_tests>\n<b_tests>\n"
           "<Test id=\"3\">\n<Name>t1</Name>\n</Test>\n</b_tests>\n<Statistics>\n<Tests>3</Tests>\n"
           "<Failures>1</Failures>\n</Statistics>\n</TestRun>\n")
    tests, done = parse_units(xml)
    assert tests == {"a_tests.t1": "pass", "a_tests.t2": "fail", "b_tests.t1": "pass"}, tests
    assert done and not parse_units(xml[:60])[1]
    good = {"status": "ran", "tests": {"s.a": "pass", "s.b": "pass"}, "complete": True}
    fail = {"status": "ran", "tests": {"s.a": "pass", "s.b": "fail"}, "complete": True}
    crash = {"status": "ran", "tests": {"s.a": "pass"}, "complete": False}
    v = unit_verdict(fail, good)
    assert v["kind"] == "strong" and v["fail_to_pass"] == ["s.b"] and v["pass_to_pass"] == ["s.a"]
    assert unit_verdict(crash, good)["fail_to_pass"] == ["s.b"]
    assert unit_verdict({"status": "no_build"}, good)["kind"] == "weak"
    assert unit_verdict(good, good)["kind"] == "no_signal"
    assert unit_verdict(fail, crash)["kind"] == "after_not_passing"
    assert unit_verdict(fail, {"status": "no_build"})["kind"] == "after_not_passing"
    assert unit_verdict({"status": "build_timeout"}, good)["kind"] == "after_not_passing"
    empty = {"status": "ran", "tests": {}, "complete": True}
    assert unit_verdict(good, empty)["kind"] == "after_not_passing"
    assert program_verdict("fail", "pass")["kind"] == "strong"
    assert program_verdict("timeout", "pass")["kind"] == "strong"
    assert program_verdict("no_build", "pass")["kind"] == "weak"
    assert program_verdict("pass", "pass")["kind"] == "no_signal"
    assert program_verdict("fail", "fail")["kind"] == "after_not_passing"
    assert program_verdict("build_timeout", "pass")["kind"] == "after_not_passing"
    assert outcome({"a": {"kind": "after_not_passing"}, "b": {"kind": "weak"}}) == "weak"
    assert outcome({"a": {"kind": "weak"}, "b": {"kind": "strong"}}) == "strong"
    assert outcome({"a": {"kind": "after_not_passing"}}) == "after_not_passing"
    assert CARGO_FETCH_FAILED.search("x\nerror: failed to load source for dependency `a`\n")
    assert CARGO_FETCH_FAILED.search("Caused by:\n  failed to clone into: /cargo/git/db/a\n")
    assert CARGO_FETCH_FAILED.search("error: failed to unpack package `a`\n")
    assert not CARGO_FETCH_FAILED.search("error: could not compile `a`\n/cargo/git/checkouts/a.rs\n")
    assert not CARGO_FETCH_FAILED.search("main.c:1:1: error: failed to parse\n")
    print("selftest ok")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pr", type=int, nargs="+", help="only these PRs")
    p.add_argument("--parallel", type=int, default=8, help=f"PRs at once, {JOBS} cores each (default 8)")
    p.add_argument("--redo", action="store_true", help="again for PRs that have a record")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    if args.selftest:
        return selftest()
    if not layout.run_dir():
        sys.exit("needs --run-dir")
    if platform.machine() != "x86_64":
        sys.exit("RIOT's build images are x86 only: run this on an x86 node "
                 "(the cpu partition), in <shared>/riot/venv")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    run = layout.run_dir()
    folder, logs = run / "check", run / "check" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    leftovers = au.path("checks", "x").parent
    for old in leftovers.iterdir():
        print(f"deleting what a stopped job left: {old.name}", flush=True)
        au.remove(old)
    recs = [json.loads(f.read_text()) for f in sorted((run / "select").glob("*.json"))]
    recs = [r for r in recs if "reason" not in r and (not args.pr or r["pr"] in args.pr)]
    todo = [r for r in recs if args.redo or not (folder / f"{r['pr']}.json").exists()]
    print(f"{len(recs)} runnable PRs, {len(todo)} to do, {args.parallel} at a time", flush=True)

    def one(r: dict) -> None:
        if _stop.is_set():
            return
        if shutil.disk_usage(au.work_root()).free < MIN_FREE_GB * 2**30:
            print(f"  {r['pr']}: not started, under {MIN_FREE_GB} GB free", flush=True)
            return
        t, pr_logs = time.time(), logs / str(r["pr"])
        if pr_logs.exists():
            shutil.rmtree(pr_logs)
        pr_logs.mkdir()
        try:
            rec = Check(r, pr_logs).check()
        except Interrupted:
            print(f"  {r['pr']}: interrupted", flush=True)
            return
        except Exception as e:      # one PR's failure must not stop the rest; no record: tried again
            print(f"  {r['pr']}: ERROR {type(e).__name__}: {str(e)[:300]}", flush=True)
            return
        rec["seconds"] = round(time.time() - t, 1)
        (folder / f"{r['pr']}.json").write_text(json.dumps(rec, indent=2) + "\n")
        what = " ".join(f"{k}={v['kind']}" for k, v in rec["targets"].items())
        print(f"  {r['pr']}: {rec['outcome']} {rec['seconds']}s: {what[:300]}", flush=True)

    with ThreadPoolExecutor(args.parallel) as pool:
        list(pool.map(one, todo))
    done = [json.loads(f.read_text()) for f in folder.glob("*.json")]
    print(f"\n{len(done)} records: " + ", ".join(
        f"{v} {k}" for k, v in Counter(r["outcome"] for r in done).most_common()), flush=True)
    if _stop.is_set():
        sys.exit("stopped: run again to finish the rest")


if __name__ == "__main__":
    main()
