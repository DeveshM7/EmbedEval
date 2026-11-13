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

The ports get the same four steps: Mynewt, NuttX on Apptainer (the PR's tests are
uncommitted edits in /testbed/apps, a repository of its own; /opt/embedeval/
test_patch.diff, and a runner whose BANNERS list, which names the PR's tests, is
emptied) and Contiki-NG (like Zephyr and RIOT). Before them, history_command
makes the history of each repository one commit: the images hold the base commit
and the recipe's commits, a remote and a shallow boundary, and the one-commit
check of harness/run.py wants a single root. For Mynewt, whose images commit the
PR's tests, it runs after the tests are taken out of the tree, so that no object
of the final repository holds them; for the others the tests are uncommitted
edits and never enter the history.

Only the part of the test diff under the protected paths is undone. A file of
the diff outside them (Mynewt's test support in a production file) stays as the
image has it: the agent's patch, which excludes the protected paths, is graded on
the image as built and has to apply there.

On by default on this branch; EMBEDEVAL_HIDE_TESTS=0 turns it off.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
from pathlib import Path

import projects

ENABLED = os.environ.get("EMBEDEVAL_HIDE_TESTS", "1") != "0"
RUNNERS = Path(__file__).resolve().parent / "agent_run_tests"


def project_of(meta: dict) -> str:
    return "riot" if meta.get("pipeline") == "riot-layer" else meta["project"]


def supported(meta: dict) -> bool:
    """Whether this instance can hide the PR's tests: Zephyr, RIOT, Mynewt,
    Contiki-NG, and NuttX only as an Apptainer image (Docker NuttX and OpenThread
    keep them)."""
    project = project_of(meta)
    if project == "nuttx":
        return meta.get("runtime") == "apptainer"
    return project in ("zephyr", "riot", "mynewt", "contiki-ng")


def _includes(meta: dict, apps: bool = False) -> str:
    """`git apply` options selecting the part of the test diff under the protected
    paths. For NuttX's apps diff (paths relative to apps/): the protected paths
    under apps/, without the prefix."""
    patterns = []
    for path in projects.setting(meta, "protected_paths"):
        if apps:
            if not path.startswith("apps/"):
                continue
            path = path[len("apps/"):]
        patterns.append(path + "*" if path.endswith("/") else path)
    if not patterns:
        raise ValueError(f"hidden tests: no protected path to hide for {meta['instance_id']}")
    return " ".join("--include=" + shlex.quote(p) for p in patterns)


# One commit, no history, as scripts/zephyr_gitlayer.py checks a layer: one
# commit object, no ref but main, no remote, nothing in the object store that the
# commit does not reach. The reflogs and the index's old objects would otherwise
# keep the images' commits alive for `git gc`.
_ONE_ROOT = r"""one_root() {   # one_root <repo> <tree>: the repository's history becomes one commit of <tree>
    cd "$1"
    d=$(git rev-parse --git-dir)
    c=$(GIT_AUTHOR_NAME=EmbedEval GIT_AUTHOR_EMAIL=embedeval@localhost GIT_AUTHOR_DATE='946684800 +0000' \
        GIT_COMMITTER_NAME=EmbedEval GIT_COMMITTER_EMAIL=embedeval@localhost GIT_COMMITTER_DATE='946684800 +0000' \
        git commit-tree "$2" -m 'The starting point of this task' </dev/null)
    for r in $(git remote); do git config --remove-section "remote.$r"; done
    rm -rf "$d/refs" "$d/packed-refs" "$d/logs" "$d/shallow" "$d/FETCH_HEAD" "$d/ORIG_HEAD" \
        "$d/MERGE_HEAD" "$d/CHERRY_PICK_HEAD" "$d/COMMIT_EDITMSG"
    mkdir -p "$d/refs/heads" "$d/refs/tags"
    git update-ref refs/heads/main "$c"
    git symbolic-ref HEAD refs/heads/main
    git reset -q
    git -c gc.reflogExpire=now -c gc.reflogExpireUnreachable=now -c gc.pruneExpire=now -c gc.auto=0 gc -q --prune=now
    rm -rf "$d/logs"
    [ "$(git rev-list --all | wc -l)" -eq 1 ] || { echo "$1: more than one commit"; exit 1; }
    [ "$(git cat-file --batch-all-objects --batch-check='%(objecttype)' | grep -c '^commit$')" -eq 1 ] \
        || { echo "$1: more than one commit object"; exit 1; }
    [ "$(git rev-list --objects --all | cut -d' ' -f1 | sort -u | wc -l)" -eq \
        "$(git cat-file --batch-all-objects --batch-check='%(objectname)' | sort -u | wc -l)" ] \
        || { echo "$1: objects the commit does not reach"; exit 1; }
    [ "$(git for-each-ref --format='%(refname)')" = refs/heads/main ] \
        || { echo "$1: a ref other than main"; exit 1; }
    [ -z "$(git remote)" ] || { echo "$1: a remote"; exit 1; }
    [ ! -e "$d/shallow" ] || { echo "$1: a shallow boundary"; exit 1; }
    [ "$(git rev-parse 'HEAD^{tree}')" = "$2" ] || { echo "$1: not the tree it was made from"; exit 1; }
}
"""


def history_command(meta: dict, hide: bool) -> str:
    """One bash script for Mynewt, Apptainer NuttX and Contiki-NG, run before the
    one-commit check, hidden or not: each repository's history becomes one commit.
    Mynewt's images commit the PR's tests, so when they are hidden they come out
    of the tree first and no object of the new history holds them."""
    project = project_of(meta)
    if project not in ("mynewt", "nuttx", "contiki-ng"):
        raise ValueError(f"hidden tests: no history preparation for {project}")
    head = '"$(cd /testbed && git rev-parse \'HEAD^{tree}\')"'
    if project == "mynewt" and hide:
        body = f"""cd /testbed
git update-index -q --refresh || true
git apply -R --index {_includes(meta)} /opt/benchmark/test_patch.diff
rm -rf /project/bin
one_root /testbed "$(cd /testbed && git write-tree)"
"""
    else:
        body = f"one_root /testbed {head}\n"
        if project == "nuttx":
            body += 'one_root /testbed/apps "$(cd /testbed/apps && git rev-parse \'HEAD^{tree}\')"\n'
    return "set -e\n" + _ONE_ROOT + body


def instance_warnings(meta: dict) -> str:
    """The instance's own warnings (metadata), without the lines that only make
    sense when the PR's tests are visible."""
    text = meta.get("extra_warnings")
    if not text:
        return text
    return "\n".join(l for l in text.split("\n") if l not in VISIBLE_TESTS_WARNINGS)


# Instance warning lines about the PR's tests being there.
VISIBLE_TESTS_WARNINGS = (
    "Use the supplied tests as the acceptance oracle. Do not add, remove, or modify tests "
    "or test helpers.",
)


def _runner_b64(project: str) -> str:
    return base64.b64encode((RUNNERS / f"{project.replace('-', '_')}.py").read_bytes()).decode()


_FILTER_TAGS = r"""# The ctags index: drop the PR's test files' lines (the filtered copy goes to the
# overlay: the container's /tmp is too small for it).
if [ -f /testbed/tags ]; then
    awk -F'\t' 'NR==FNR { f[$0]; next } { p = $2; sub(/^\.\//, "", p) } !(p in f)' \
        /tmp/pr-test-files /testbed/tags > /opt/embedeval/tags.new
    mv /opt/embedeval/tags.new /testbed/tags
    if awk -F'\t' 'NR==FNR { f[$0]; next } { p = $2; sub(/^\.\//, "", p) } p in f { found = 1 } END { exit !found }' \
            /tmp/pr-test-files /testbed/tags; then
        echo "the ctags index still lists the PR's test files"; exit 1
    fi
fi
"""

_INSTALL = """echo {runner} | base64 -d > /usr/local/bin/run_tests
chmod 755 /usr/local/bin/run_tests
"""


def _mynewt_setup(meta: dict) -> str:
    inc = _includes(meta)
    paths = " ".join(shlex.quote(p) for p in projects.setting(meta, "protected_paths"))
    return f"""set -e
cd /testbed
P=/opt/benchmark/test_patch.diff
[ -f "$P" ] || {{ echo "no test diff at $P"; exit 1; }}
# history_command took the protected part out of the tree: it applies forward again.
git apply --check {inc} "$P" || {{ echo "the PR's tests are still in the tree"; exit 1; }}
rm -f "$P"
python3 -c 'import json, sys
c = json.load(open(sys.argv[1]))
json.dump({{k: c[k] for k in ["mcumgr_commit", "test_observer"] if k in c}}, open(sys.argv[1], "w"), indent=2)' \\
    /opt/benchmark/metadata.json
mkdir -p /opt/embedeval
mv /usr/local/bin/run_tests /opt/embedeval/run_tests_base.sh
""" + _INSTALL.format(runner=_runner_b64("mynewt")) + f"""left=$(git status --porcelain; git status --porcelain --ignored -- {paths})
[ -z "$left" ] || {{ echo "left after hiding the tests:"; echo "$left" | head -20; exit 1; }}
"""


def _nuttx_setup(meta: dict) -> str:
    inc = _includes(meta, apps=True)
    return f"""set -e
cd /testbed/apps
P=/opt/embedeval/test_patch.diff
[ -f "$P" ] || {{ echo "no test diff at $P"; exit 1; }}
git apply -R {inc} "$P"
git apply --check {inc} "$P" || {{ echo "the PR's tests are still in the tree"; exit 1; }}
# Kconfig symbols that only the PR's tests define: the .config has lines for
# them (the image was configured with the tests in place); nothing may name them.
for S in $(grep -E '^\\+[[:space:]]*(menu)?config[[:space:]]+[A-Za-z0-9_]+[[:space:]]*$' "$P" \\
        | sed -E 's/^\\+[[:space:]]*(menu)?config[[:space:]]+([A-Za-z0-9_]+)[[:space:]]*$/\\2/'); do
    if grep -rqsE --include='Kconfig*' "^[[:space:]]*(menu)?config[[:space:]]+$S[[:space:]]*$" /testbed; then
        continue
    fi
    for f in /testbed/.config /testbed/.config.old; do
        if [ -f "$f" ]; then
            grep -v -e "^CONFIG_$S=" -e "^# CONFIG_$S is not set\\$" "$f" > "$f.new" || [ $? -eq 1 ]
            cat "$f.new" > "$f"
            rm -f "$f.new"
        fi
    done
    if [ -n "$(grep -rIlw --exclude-dir=.git "CONFIG_$S" /testbed)" ]; then
        echo "CONFIG_$S, defined only by the PR's tests, is still named in /testbed"; exit 1
    fi
done
rm -f "$P"
mkdir -p /opt/embedeval
mv /usr/local/bin/run_tests /opt/embedeval/run_tests_base.sh
# The runner's BANNERS list names the PR's changed ostest tests.
B=/opt/embedeval/run_tests_base.sh
sed 's/^BANNERS=(.*)$/BANNERS=()/' "$B" > "$B.new"
cat "$B.new" > "$B"
rm -f "$B.new"
[ "$(grep -c '^BANNERS=' "$B")" -eq 1 ] && grep -qx 'BANNERS=()' "$B" \\
    || {{ echo "the runner still names the PR's tests"; exit 1; }}
""" + _INSTALL.format(runner=_runner_b64("nuttx")) + """left=$(git -C /testbed status --porcelain; git -C /testbed/apps status --porcelain)
[ -z "$left" ] || { echo "left after hiding the tests:"; echo "$left" | head -20; exit 1; }
"""


def _contiki_setup(meta: dict) -> str:
    inc = _includes(meta)
    return f"""set -e
cd /testbed
P=/opt/embedeval/test_patch.diff
[ -f "$P" ] || {{ echo "no test diff at $P"; exit 1; }}
git apply --numstat {inc} "$P" | cut -f3 > /tmp/pr-test-files
git apply -R {inc} "$P"
git apply --check {inc} "$P" || {{ echo "the PR's tests are still in the tree"; exit 1; }}
git clean -fdxq -- tests/ regression-tests/
# The logs of the image's build-only run name the PR's units.
rm -rf /testbed/.embedeval/logs
mkdir -p /testbed/.embedeval/logs
""" + _FILTER_TAGS + """rm -f "$P"
python3 -c 'import json, sys
c = json.load(open(sys.argv[1]))
json.dump({k: c[k] for k in ["submodules"] if k in c}, open(sys.argv[1], "w"), indent=2)' \\
    /opt/embedeval/bench.json
mv /usr/local/bin/run_tests /opt/embedeval/run_tests_base.py
""" + _INSTALL.format(runner=_runner_b64("contiki-ng")) + """# Nothing of the PR's tests left: a clean tree (ignored files included under tests/).
left=$(git status --porcelain --ignore-submodules=untracked; git status --porcelain --ignored -- tests/ regression-tests/)
[ -z "$left" ] || { echo "left after hiding the tests:"; echo "$left" | head -20; exit 1; }
rm -f /tmp/pr-test-files
"""


def setup_command(meta: dict) -> str:
    """One bash script, run in the agent's overlay before it starts. Fails (and
    so stops the run as an infrastructure error) if anything is left to see."""
    project = project_of(meta)
    if project == "mynewt":
        return _mynewt_setup(meta)
    if project == "nuttx":
        return _nuttx_setup(meta)
    if project == "contiki-ng":
        return _contiki_setup(meta)
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
    "mynewt": "",
    "contiki-ng": "",
    "nuttx": "\nostest (apps/testing/ostest) is the only test program in this build, so add your "
             "test cases to it.",
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
    "mynewt": (
        "Use `run_tests` with one or more test packages (folders under /testbed with a "
        "pkg.yml), for example `run_tests kernel/os/selftest` (`kernel/os/test` in older "
        "trees). It builds incrementally, runs each package's test program and prints a "
        "status for every test case. You can run the project's existing tests and tests "
        "you write yourself. A test that hangs is stopped and reported as failed."
    ),
    "nuttx": (
        "Use `run_tests` to build and test: it rebuilds incrementally, boots the build, "
        "runs ostest to the end and prints a status for each ostest test. Name ostest "
        "tests to require that they ran, for example `run_tests semaphore` (a name matches "
        "every ostest test whose banner contains it, such as `user_main: semaphore test`). "
        "You can run the project's existing tests and tests you add to ostest. A run that "
        "hangs is stopped and reported as failed."
    ),
    "contiki-ng": (
        "Use `run_tests` with one or more tests: a native unit-test folder under tests/ "
        "(for example a folder under tests/08-native-runs; name `<folder>/<program>` to run "
        "one of several programs, and append `:VAR=value` for make options, such as "
        "`:DEFINES=FOO=1`), or a Cooja scenario (a .csc file in a tests/ suite folder). It "
        "builds incrementally, runs them and prints a status for every test case. You can "
        "run the project's existing tests and tests you write yourself. A test that hangs "
        "is stopped and reported as failed."
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
    "mynewt": (
        "The Apache Mynewt (mynewt-core) source is at /testbed; its tests are unit-test "
        "packages (folders with a pkg.yml, usually named selftest, or test in older "
        "trees), built with `newt` for the native simulator through a newt project at "
        "/project. `run_tests <test package>...` builds them incrementally, runs them and "
        "prints a status for every test case. Do not run `newt` yourself and do not edit "
        "/project. The fix belongs in library source: changes to test packages are for "
        "your own checking only."
    ),
    "nuttx": (
        "Two repos: the kernel at /testbed and the applications at /testbed/apps. Builds "
        "use GNU Make and are already configured for this instance's board (on most "
        "instances the simulator, sim:nsh, which builds a native binary at /testbed/nuttx). "
        "Never run distclean or configure.sh -- reconfiguring discards the container's "
        "setup and breaks the test runner. The configuration's test program is ostest "
        "(apps/testing/ostest): `run_tests` rebuilds incrementally with `make -j$(nproc)`, "
        "boots the build, runs ostest to the end and prints a status for each ostest test. "
        "Only your changes to the kernel are graded: changes under /testbed/apps are not "
        "kept."
    ),
    "contiki-ng": (
        "Builds use GNU Make. Tests are native unit tests (folders under tests/ whose "
        "Makefile builds a test program for the native target) and Cooja simulation "
        "scenarios (.csc files in a tests/ suite; these run only where tools/cooja is "
        "checked out). `run_tests <test>...` builds them incrementally, runs them and "
        "prints a status for every test case; do not construct make invocations or start "
        "Cooja by hand. The target and configuration are fixed by this instance."
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
    "mynewt": (
        "`run_tests` exit codes: 0 = every test case passed, 1 = a test case failed or a "
        "test program crashed, 2 = a build or a test run timed out, 3 = a build failed or "
        "a package ran no test case, 4 = no valid test package was given."
    ),
    "nuttx": (
        "`run_tests` exit codes: 0 = the build succeeded, ostest ran to the end with no "
        "error and every test you named ran, 1 = the build failed, a test reported an "
        "error or an assertion failed, ostest did not finish, or a test you named did not "
        "run, 2 = timed out. If `run_tests` times out (exit code 2), your change has "
        "introduced a hang or an infinite loop. Do NOT submit -- investigate further."
    ),
    "contiki-ng": (
        "`run_tests` exit codes: 0 = every test passed, 1 = a test case failed, crashed or "
        "timed out, 2 = a build failed, 3 = a test did not run to completion or reported "
        "no test case, 4 = no valid test was given."
    ),
}

WORKFLOW = (
    "3. Build and test (see \"Testing and submitting\"): `cd /testbed && run_tests <test> ...`\n"
    "   - If the build fails: fix the error and repeat\n"
    "   - If a test FAILS: fix your change (or your test, if the test is wrong) and go\n"
    "     back to step 3\n"
    "   - When your change does everything the task asks, your tests for it pass and\n"
    "     the existing tests of the code you changed pass: go to step 4 at once\n"
)
