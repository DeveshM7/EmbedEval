"""
Hidden tests for NuttX: the agent works without the PR's tests, as in SWE-bench;
grading still runs them. Ported from main's harness/hidden_tests.py (Zephyr and
RIOT), for this branch's NuttX images.

A NuttX image holds the PR's tests in three places the agent could see. All of
this happens in the run's own overlay, before the agent starts; the image is
never changed, and grading (harness/evaluate_patches.py) uses a fresh overlay on
the image as built, with the tests and the original run_tests:

  1. /testbed/apps: the PR's test changes, applied as uncommitted edits to the
     apps repository (the image's `cd apps && git apply test_patch.diff`).
     Undone with `git apply -R`; the apps repository never committed them, so
     the tree is then the apps base commit and `git status` is clean. The
     image is not prebuilt, so there is no build output to clean.
  2. /opt/embedeval/test_patch.diff, the test diff the image was built from:
     deleted.
  3. run_tests: it requires every changed test's banner in ostest's output
     (BANNERS, the hidden tests' names). The agent's copy has no banners: it
     builds nothing, boots the board, runs ostest and passes on a clean exit,
     as before. ostest runs every test compiled into it, so tests the agent adds
     to apps/testing/ostest run with the rest.

On by default; EMBEDEVAL_HIDE_TESTS=0 turns it off.
"""

from __future__ import annotations

import os

ENABLED = os.environ.get("EMBEDEVAL_HIDE_TESTS", "1") != "0"


def applies(meta: dict) -> bool:
    return ENABLED and meta.get("project") == "nuttx"


def setup_command(meta: dict) -> str:
    """One bash script, run in the agent's overlay before it starts. Fails (and so
    stops the run as an infrastructure error) if anything of the tests is left."""
    if meta.get("project") != "nuttx":
        raise ValueError(f"hidden tests: not supported for {meta.get('project')}")
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
# Nothing of the PR's tests left: the apps tree clean, the diff gone.
left=$(git -C "$A" status --porcelain)
[ -z "$left" ] || { echo "left in apps after hiding the tests:"; echo "$left" | head -20; exit 1; }
[ ! -e "$P" ] || { echo "test diff still present"; exit 1; }
rm -f /tmp/pr-test-files
"""


# -- the prompt (ported from main's harness/hidden_tests.py) ------------------

SYSTEM_RULE = (
    "- After a successful build you MUST still run tests with `run_tests` before\n"
    "  submitting. A passing build does NOT mean the code works.\n"
)

PROTECTED_RULE = (
    "2. The tests that grade your work are hidden from you (see \"Testing and\n"
    "   submitting\" below). You may add or change tests under\n"
    "   {protected} to check your work, but those changes are\n"
    "   discarded before grading."
)

SUBMIT_RULE = (
    "4. Your work is graded only if you submit it (step 4 of the workflow below).\n"
    "   Time is limited: a run that is stopped before you submit is graded as a fail,\n"
    "   so submit once your change is done and tested."
)

RUNNING_TESTS = (
    "Use `run_tests` after building (`cd /testbed && make -j$(nproc) && run_tests`). It boots\n"
    "the board, runs the whole ostest suite in /testbed/apps/testing/ostest and waits for\n"
    "results: every test compiled into ostest runs, including tests you add to it (for\n"
    "example a new test function in its own file, called from ostest_main.c and listed in\n"
    "ostest's Makefile and CMakeLists.txt). ostest stops at the first test that crashes or\n"
    "fails an assertion, so call a new test early -- at the start of user_main() in\n"
    "ostest_main.c -- so that it runs even if a later test crashes. Exit codes: 0 = ostest\n"
    "finished cleanly with no ERROR in its output, 1 = any other completed run (a test failed\n"
    "or reported an error), 2 = timed out."
)

TESTING_AND_SUBMITTING = """\
## Testing and submitting

Your change is graded by hidden tests written for this task. They check the
behaviour the task describes, including every case it lists, and the project's
existing tests must keep passing.

Existing tests passing does not show that new behaviour works. Write a test for
each case the task describes, run it, and also run the existing tests. The quickest
way is usually to add test cases to the existing ostest suite rather than create a
new test program.

Submit as soon as your change does everything the task asks, your tests for it
pass, and the existing tests pass. Further checking will not improve your grade.

"""

WORKFLOW = (
    "3. Build and test (see \"Testing and submitting\"): `cd /testbed && make -j$(nproc) && run_tests`\n"
    "   - If the build fails: fix the error and repeat\n"
    "   - If a test FAILS: fix your change (or your test, if the test is wrong) and go\n"
    "     back to step 3\n"
    "   - When your change does everything the task asks, your tests for it pass and\n"
    "     the existing tests pass: go to step 4 at once\n"
)
