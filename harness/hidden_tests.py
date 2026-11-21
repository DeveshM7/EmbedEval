"""
Hidden tests: the agent works without the PR's tests, as in SWE-bench; grading
still runs them.

Every Zephyr and RIOT image holds the PR's tests in four places the agent could
see (the images themselves are never changed; all of this happens in the run's
own overlay, before the agent starts):

  1. /testbed: the PR's test changes, as uncommitted edits on the base commit.
     Undone with `git apply -R` of the test diff; build output under tests/ and,
     for Zephyr, twister-out/ (the image's first build of the PR's scenario) are
     removed, and the ctags index loses the lines of the PR's test files. The
     edits were never committed, so the tree is then the base commit: `git
     status` clean, the history the one base commit.
  2. /opt/embedeval/test_patch.diff, the test diff the image was built from:
     deleted.
  3. /opt/embedeval/instance.json, the runner's settings: cut to the board and
     the era's options (Zephyr: no test path or scenario; RIOT: no test names).
  4. run_tests, fixed to the PR's tests: moved to
     /opt/embedeval/run_tests_base.py, and replaced by one that runs the tests
     the agent names (harness/agent_run_tests/), on the same board, with the
     same build-and-run code.

The task message has the problem statement only (no test names), and the
prompt says the tests are not given and how to run tests. Grading is unchanged:
a fresh overlay on the image as built, the PR's tests in place, the original
run_tests.

NuttX (ported from feature/nuttx-pr-filtering, 2026-11-21): the PR's tests are
uncommitted edits to /testbed/apps (its own repository) and its test diff; the
agent's run_tests is the image's own with the changed tests' banners cleared, and
it relinks a stale /testbed/nuttx first (nuttx_setup_command below).

On by default on this branch; EMBEDEVAL_HIDE_TESTS=0 turns it off.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

ENABLED = os.environ.get("EMBEDEVAL_HIDE_TESTS", "1") != "0"
RUNNERS = Path(__file__).resolve().parent / "agent_run_tests"


def project_of(meta: dict) -> str:
    return "riot" if meta.get("pipeline") == "riot-layer" else meta["project"]


def setup_command(meta: dict) -> str:
    """One bash script, run in the agent's overlay before it starts. Fails (and
    so stops the run as an infrastructure error) if anything is left to see."""
    project = project_of(meta)
    if project == "nuttx":
        return nuttx_setup_command()
    if project not in ("zephyr", "riot"):
        raise ValueError(f"hidden tests: not supported for {project}")
    runner = base64.b64encode((RUNNERS / f"{project}.py").read_bytes()).decode()
    keep = ["platform", "twister_args"] if project == "zephyr" else ["board"]
    zephyr = project == "zephyr"
    return f"""set -e
cd /testbed
P=/opt/embedeval/test_patch.diff
[ -f "$P" ] || {{ echo "no test diff at $P"; exit 1; }}
git apply --numstat "$P" | cut -f3 > /tmp/pr-test-files
git apply -R "$P"
git clean -fdxq -- tests/
{'rm -rf /testbed/twister-out /testbed/twister-out.log' if zephyr else ''}
# The ctags index: drop the PR's test files' lines (the filtered copy goes to the
# overlay: the container's /tmp is too small for it).
if [ -f /testbed/tags ]; then
    awk -F'\\t' 'NR==FNR {{ f[$0]; next }} {{ p = $2; sub(/^\\.\\//, "", p) }} !(p in f)' \\
        /tmp/pr-test-files /testbed/tags > /opt/embedeval/tags.new
    mv /opt/embedeval/tags.new /testbed/tags
    if awk -F'\\t' 'NR==FNR {{ f[$0]; next }} {{ p = $2; sub(/^\\.\\//, "", p) }} p in f {{ found = 1 }} END {{ exit !found }}' \\
            /tmp/pr-test-files /testbed/tags; then
        echo "the ctags index still lists the PR's test files"; exit 1
    fi
fi
rm -f "$P"
python3 -c 'import json, sys
c = json.load(open(sys.argv[1]))
json.dump({{k: c[k] for k in {json.dumps(keep)} if k in c}}, open(sys.argv[1], "w"), indent=2)' \\
    /opt/embedeval/instance.json
mv /usr/local/bin/run_tests /opt/embedeval/run_tests_base.py
echo {runner} | base64 -d > /usr/local/bin/run_tests
chmod 755 /usr/local/bin/run_tests
# Nothing of the PR's tests left: a clean tree (ignored files included under tests/).
left=$(git status --porcelain; git status --porcelain --ignored -- tests/)
[ -z "$left" ] || {{ echo "left after hiding the tests:"; echo "$left" | head -20; exit 1; }}
rm -f /tmp/pr-test-files
"""


def nuttx_setup_command() -> str:
    """NuttX: undo the PR's test diff in /testbed/apps (its own repository), delete
    the diff, and give the agent run_tests without the changed tests' banners (it
    boots the board, runs ostest and passes on a clean exit), which also relinks
    /testbed/nuttx first when a library in staging/ is newer: an incremental make
    recompiles a changed library but does not relink the sim binary, so without it
    the agent's changes would never reach run_tests."""
    return r"""set -e
P=/opt/embedeval/test_patch.diff
A=/testbed/apps
[ -f "$P" ] || { echo "no test diff at $P"; exit 1; }
git -C "$A" apply --numstat "$P" | cut -f3 > /tmp/pr-test-files
git -C "$A" apply -R "$P"
# the folders of files the test diff added, if now empty
while read -r f; do d=$(dirname "$A/$f"); rmdir -p "$d" 2>/dev/null || true; done < /tmp/pr-test-files
rm -f "$P"
# The agent's run_tests: the image's own, without the changed tests' banners.
sed -i 's/^BANNERS=(.*)$/BANNERS=()/' /usr/local/bin/run_tests
grep -qx 'BANNERS=()' /usr/local/bin/run_tests || { echo "run_tests: no BANNERS line to clear"; exit 1; }
# ... and it relinks a stale binary first.
cat > /tmp/relink <<'RELINK'
if [ -e nuttx ] && [ -n "$(find staging -name '*.a' -newer nuttx 2>/dev/null)" ]; then
    echo "run_tests: libraries are newer than /testbed/nuttx; relinking it"
    rm -f nuttx
    make -j"$(nproc)" >/tmp/relink.log 2>&1 || { tail -30 /tmp/relink.log; echo "run_tests: relink failed"; exit 1; }
fi
RELINK
sed -i '/^cd \/testbed || exit 1$/r /tmp/relink' /usr/local/bin/run_tests
rm -f /tmp/relink
grep -q 'relinking it' /usr/local/bin/run_tests || { echo "run_tests: could not add the relink step"; exit 1; }
# Nothing of the PR's tests left: the apps tree clean, the diff gone.
left=$(git -C "$A" status --porcelain)
[ -z "$left" ] || { echo "left in apps after hiding the tests:"; echo "$left" | head -20; exit 1; }
[ ! -e "$P" ] || { echo "test diff still present"; exit 1; }
rm -f /tmp/pr-test-files
"""


# -- the prompt --------------------------------------------------------------

SYSTEM_RULE = (
    "- After a successful build you MUST still run tests with `run_tests` before\n"
    "  submitting. A passing build does NOT mean the code works.\n"
)

PROTECTED_RULE = (
    "The tests that grade your work are hidden from you (see \"Testing and "
    "submitting\" below). You may add or change tests under {protected} to check "
    "your work, but those changes are discarded before grading."
)

SUBMIT_RULE = (
    "6. Your work is graded only if you submit it (step 4 below). Time is limited: a\n"
    "   run that is stopped before you submit is graded as a fail, so submit once your\n"
    "   change is done and tested."
)

TESTING_AND_SUBMITTING = """\
## Testing and submitting

Your change is graded by hidden tests written for this task. They check the
behaviour the task describes, including every case it lists, and the project's
existing tests must keep passing.

Existing tests passing does not show that new behaviour works. Write a test for
each case the task describes, run it, and also run the existing tests of the code
you changed. The quickest way is usually to add test cases to the nearest existing
test suite rather than create a new one.{hint}

Submit as soon as your change does everything the task asks, your tests for it
pass, and the existing tests of the code you changed pass. Further checking will
not improve your grade.

"""

TEST_HINT = {
    "zephyr": ("\nEach `run_tests` call builds and boots the board, so run the smallest test "
               "folders that cover your change."),
    "riot": "",
    "nuttx": ("\nostest runs every test compiled into it: add your test cases to the "
              "existing ostest suite in /testbed/apps/testing/ostest."),
}

RUNNING_TESTS = {
    "zephyr": (
        "Use `run_tests` with one or more test folders (folders under /testbed with a "
        "testcase.yaml), for example `run_tests tests/kernel/common`. It builds "
        "incrementally and runs every scenario in them that supports this instance's "
        "board, and prints a status for every test case. You can run the project's "
        "existing tests and tests you write yourself. A test that hangs is stopped and "
        "reported as failed."
    ),
    "riot": (
        "Use `run_tests` with one or more tests: a unit-test group (a folder in "
        "tests/unittests, for example `run_tests tests-core`) or a test program (a folder "
        "under tests/, for example `run_tests tests/thread_basic`). It builds "
        "incrementally, runs them and prints a status for every test. You can run the "
        "project's existing tests and tests you write yourself. A test that hangs is "
        "stopped and reported as failed."
    ),
    "nuttx": (
        "Use `run_tests` after building (`cd /testbed && make -j$(nproc) && run_tests`). It boots "
        "the board, runs the whole ostest suite in /testbed/apps/testing/ostest and waits for "
        "results: every test compiled into ostest runs, including tests you add to it (for "
        "example a new test function in its own file, called from ostest_main.c and listed in "
        "ostest's Makefile and CMakeLists.txt). If a library is newer than /testbed/nuttx (an "
        "incremental make does not always relink it), run_tests relinks it first, so it always "
        "tests your current code. ostest stops at the first test that crashes or "
        "fails an assertion, so call a new test early -- at the start of user_main() in "
        "ostest_main.c -- so that it runs even if a later test crashes."
    ),
}

ORIENTATION = {
    "zephyr": (
        "This instance builds and tests with Zephyr's twister for one board, fixed. "
        "`run_tests <test folder>...` rebuilds incrementally (only what you changed is "
        "recompiled), runs the tests and prints a status for every test case. Do not "
        "run `west build`, `west build -t run` or `scripts/twister` yourself, and do not "
        "pass a board or Kconfig options anywhere: the configuration is already applied. "
        "Build output is under /testbed/twister-out."
    ),
    "riot": (
        "Builds use GNU Make and RIOT's native board, which compiles RIOT into an "
        "ordinary Linux process: there is no emulator. Tests are unit-test groups under "
        "tests/unittests or test programs under tests/. `run_tests <test>...` builds "
        "them incrementally and runs them; do not construct make invocations by hand."
    ),
    # NuttX: the project's own (harness/projects.py, with the board of the instance).
    "nuttx": None,
}

WARNINGS = {
    "zephyr": (
        "`run_tests` exit codes: 0 = all tests that ran passed, 1 = a test, the build or "
        "the configuration failed, 2 = the test runner did not start or no valid folder "
        "was given, 3 = nothing ran (filtered or skipped on this board). A run takes one "
        "to several minutes; tests under QEMU include the emulator's boot."
    ),
    "riot": (
        "`run_tests` exit codes: 0 = all tests passed, 1 = a test or a build failed, "
        "2 = no valid test was given."
    ),
    "nuttx": (
        "`run_tests` exit codes: 0 = ostest finished cleanly with no ERROR in its output, "
        "1 = any other completed run (a test failed or reported an error), 2 = timed out. "
        "If `run_tests` times out with no test results, your change has introduced a hang "
        "or an infinite loop: do not submit, investigate further."
    ),
}

def workflow(project: str) -> str:
    """Step 3 of the workflow: NuttX builds with make before run_tests, which takes no tests."""
    if project != "nuttx":
        return WORKFLOW
    return WORKFLOW.replace("`cd /testbed && run_tests <test> ...`",
                            "`cd /testbed && make -j$(nproc) && run_tests`")


WORKFLOW = (
    "3. Build and test (see \"Testing and submitting\"): `cd /testbed && run_tests <test> ...`\n"
    "   - If the build fails: fix the error and repeat\n"
    "   - If a test FAILS: fix your change (or your test, if the test is wrong) and go\n"
    "     back to step 3\n"
    "   - When your change does everything the task asks, your tests for it pass and\n"
    "     the existing tests of the code you changed pass: go to step 4 at once\n"
)
