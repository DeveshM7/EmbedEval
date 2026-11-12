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


# -- the prompt --------------------------------------------------------------

SYSTEM_RULE = (
    "- After a successful build you MUST still run tests with `run_tests` before\n"
    "  submitting. A passing build does NOT mean the code works.\n"
)

PROTECTED_RULE = (
    "The tests that grade your work are not given to you. You may add or change "
    "tests under {protected} to check your work, but those changes are discarded "
    "before grading."
)

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
}

WORKFLOW = (
    "3. Build and test: `cd /testbed && run_tests <test> ...` with the tests that\n"
    "   exercise your change (existing ones, or ones you add)\n"
    "   - If the build fails: fix the error and repeat\n"
    "   - If tests FAIL: fix your change and go back to step 3\n"
    "   - When your change builds and your tests pass: go to step 4\n"
)
