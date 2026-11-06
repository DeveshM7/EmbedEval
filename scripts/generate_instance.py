#!/usr/bin/env python3
"""
Build a complete Zephyr (Apptainer recipe) or Apache Mynewt (Docker) instance
from a triage verdict.

    python scripts/generate_instance.py --pr 65697 --triage triage/ideal_verdicts/65697.json
    python scripts/generate_instance.py --pr 65697 --triage verdict.json --out /tmp/check
    python scripts/generate_instance.py --repo mynewt --pr 3299 \
        --triage triage/mynewt-3299.json --out /tmp/check

Zephyr: writes an Apptainer recipe and everything it copies in, so none of it is
hand-written. Mynewt: writes Dockerfile, run_tests.sh, test_patch.diff and
metadata.json.

Writes an Apptainer recipe and everything it copies in, so none of it is
hand-written:

    instance.def      the image recipe, FROM the shared SDK image of the PR's era
    instance.json     board, scenario and twister options, read by run_tests
    run_tests.py      becomes /usr/local/bin/run_tests in the image
    test_patch.diff   the PR's test changes, written by git
    metadata.json     the instance's description

Nothing is built here; scripts/build_instances.py builds the image and
scripts/validate_instance.py validates it.

Deterministic on purpose. The verdict supplies the judgements -- the board and
scenario, the problem statement, the test lists -- and everything else comes
from the enriched record's `twister` block (scripts/zephyr_env.py): the base
and head commits, the era's Python and SDK, and which twister options exist.

Twister replaces `west build` throughout. It applies the scenario's own
settings (extra_configs, extra_args), so there is no extra_configs field to
copy by hand, and it reports a status per test.

Output goes to generated/ by default, never to docker/instances/, so this
cannot disturb an existing instance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENRICHED = layout.enriched()
DRYRUN = layout.dryrun()
TEMPLATES = REPO_ROOT / "templates" / "zephyr" / "apptainer"
DEFAULT_OUT = layout.generated()

REPO_URL = "https://github.com/zephyrproject-rtos/zephyr"
CLONE_CACHE = layout.zephyr_clone()

PROJECTS = {
    "mynewt": {
        "repo_url": "https://github.com/apache/mynewt-core",
        "enriched": REPO_ROOT / "candidates" / "mynewt" / "enriched",
        "templates": REPO_ROOT / "templates" / "mynewt",
        "clone": REPO_ROOT / "candidates" / "mynewt.git",
    },
}


def run(cmd: list[str], timeout: int = 300, check: bool = True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        sys.exit(f"ERROR: {' '.join(cmd[:4])} ... failed:\n{r.stderr.strip()[:400]}")
    return r


def ensure_clone(project: str = "zephyr") -> Path:
    """A bare upstream mirror, reused across instances (Zephyr: full, shared
    with triage_prs.py; other projects: blobless).

    Full rather than blobless. Blobless (all history, file contents fetched on
    first use) is enough for building a test patch, but triage searches the
    tree with git grep, and in a blobless clone that downloads every file under
    the searched path one at a time -- so searches had to be capped at a few
    hundred files, and the model spent its tool rounds guessing narrower
    directories. A full clone is a few GB, once, and makes any search instant.
    """
    if project != "zephyr":
        cfg = PROJECTS[project]
        clone = (Path(os.environ.get("EMBEDEVAL_MYNEWT_CLONE", cfg["clone"]))
                 if project == "mynewt" else cfg["clone"])
        if not clone.exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            print(f"  cloning {cfg['repo_url']} (blobless, one-time) ...")
            run(["git", "clone", "--filter=blob:none", "--no-checkout", "--bare",
                 f"{cfg['repo_url']}.git", str(clone)], timeout=1800)
        return clone
    if not CLONE_CACHE.exists():
        CLONE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        print(f"  cloning {REPO_URL} (full, one-time; a few GB) ...")
        run(["git", "clone", "--bare", f"{REPO_URL}.git", str(CLONE_CACHE)], timeout=3600)
    return CLONE_CACHE


def is_test_path(path: str, project: str) -> bool:
    if project == "zephyr":
        return path.startswith("tests/")
    # Mynewt: a unittest package is selftest/ since March 2019, test/ before.
    return any(
        part in ("selftest", "test") or part.startswith(("selftest-", "test-"))
        for part in path.split("/")
    )


def is_source_path(path: str, project: str) -> bool:
    if is_test_path(path, project):
        return False
    if project == "zephyr":
        return not path.startswith(("doc/", ".github/", "samples/"))
    return not path.startswith(("docs/", ".github/", "test/"))


def changed_test_paths(record: dict, project: str) -> list[str]:
    """Every test-side path needed to reproduce additions, edits, and renames."""
    paths = []
    for changed in record["files"]:
        previous = changed.get("previous_filename")
        current = changed["filename"]
        if previous and is_test_path(previous, project):
            paths.append(previous)
        if is_test_path(current, project):
            paths.append(current)
    return list(dict.fromkeys(paths))


def build_test_patch(
    base: str, head: str, project: str = "zephyr", test_files: list[str] | None = None
) -> str:
    """The PR's test-file changes, as a diff written by git.

    Produced by git rather than assembled from GitHub's per-file `patch`
    fields. Those carry the changed lines but not the headers around them, so
    assembling by hand means reproducing git's format exactly -- and a missing
    `new file mode` line silently breaks every PR that creates a test file,
    which is how 43405, 74435 and 82272 came to fail. Letting git write it
    removes that whole class of error rather than the one instance of it.

    All of tests/, including testcase.yaml: a scenario setting the PR adds
    there is part of how its test detects the bug (85079 adds
    CONFIG_RTIO_SUBMIT_SEM=n to its scenario), and twister applies it.

    The range is safe because both ends are on the PR's own branch: base is its
    merge base, head its tip, and the branch contains nothing that landed on
    main after it forked. Contamination came from ranges ending at a *merge
    commit* -- base..merge_commit traverses everyone else's work, which is what
    put seven foreign files into the nuttx-11889 patch.
    """
    clone = ensure_clone(project)
    for sha in (base, head):
        if run(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"],
               check=False).returncode:
            run(["git", "-C", str(clone), "fetch", "-q", "origin", sha], timeout=900)
    paths = ["tests/"] if project == "zephyr" else (test_files or [])
    if not paths:
        return ""
    r = run(["git", "-C", str(clone), "diff", f"{base}..{head}", "--", *paths])
    return r.stdout


def check_applies(
    patch: str, base: str, inst: Path, project: str = "zephyr"
) -> None:
    """Refuse to emit an instance whose patch git will not accept.

    Without this the first sign of a malformed patch is `git apply` failing
    minutes into an image build, and only for instances someone happens to
    build. A dry run against the base commit costs about a second and covers
    every instance, every time.
    """
    clone = ensure_clone(project)
    patch_file = (inst / "test_patch.diff").resolve()
    with tempfile.TemporaryDirectory(prefix="embedeval-applycheck-") as tmp:
        work = Path(tmp) / "tree"
        run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach",
             str(work), base], timeout=300)
        try:
            r = run(["git", "-C", str(work), "apply", "--check", str(patch_file)], check=False)
            if r.returncode != 0:
                sys.exit("ERROR: generated test_patch.diff does not apply at "
                         f"{base[:12]}:\n{r.stderr.strip()[:400]}")
        finally:
            run(["git", "-C", str(clone), "worktree", "remove", "--force", str(work)],
                timeout=120, check=False)


def check_choice(pr: int, facts: dict, platform: str, test_path: str, scenario: str,
                 force: bool) -> None:
    """The verdict's board and scenario must exist at this commit and, when a
    dry run has been done, be a pair twister selects."""
    problems = []
    if platform not in [b for b in facts["boards"].values() if b]:
        problems.append(f"board {platform!r} is not a candidate board at the base commit "
                        f"(have {facts['boards']})")
    suite = facts["suites"].get(test_path)
    if not suite:
        problems.append(f"{test_path!r} is not a suite this PR changes "
                        f"(have {sorted(facts['suites'])})")
    elif scenario not in suite.get("scenarios", {}):
        problems.append(f"scenario {scenario!r} is not in {test_path} "
                        f"(have {sorted(suite.get('scenarios', {}))})")
    dr = DRYRUN / f"{pr}.json"
    if dr.exists():
        pairs = json.loads(dr.read_text())["pairs"]
        hit = next((p for p in pairs if p["suite"] == test_path and p["scenario"] == scenario
                    and p["board"] == platform), None)
        if hit and hit["status"] != "selected":
            problems.append(f"the dry run filtered {scenario} on {platform}: {hit['reason']}")
    else:
        print(f"  note: no dry run for {pr} (candidates/dryrun/{pr}.json); choice not checked")
    if problems:
        msg = "; ".join(problems)
        if not force:
            sys.exit(f"ERROR: verdict for {pr}: {msg}")
        print(f"  WARNING (--force): {msg}")


def generate_zephyr(pr: int, triage: dict, out_root: Path, force: bool = False) -> Path:
    rec_path = ENRICHED / f"{pr}.json"
    if not rec_path.exists():
        sys.exit(f"ERROR: no enriched record at {rec_path}. Run `enrich` first.")
    rec = json.loads(rec_path.read_text())
    facts = rec.get("twister")
    if not facts or "error" in facts:
        sys.exit(f"ERROR: enriched record for {pr} has no twister facts; "
                 f"run `filter_candidates.py enrich-twister --pr {pr}`")

    for field in ("platform", "test_path", "scenario", "problem_statement", "fail_to_pass"):
        if not triage.get(field):
            sys.exit(f"ERROR: triage verdict for {pr} has no {field!r}")
    platform, test_path, scenario = triage["platform"], triage["test_path"], triage["scenario"]
    check_choice(pr, facts, platform, test_path, scenario, force)

    base, head = facts["base_commit"], facts["head_commit"]
    instance_id = f"zephyr__zephyr-{pr}"
    inst = out_root / instance_id
    inst.mkdir(parents=True, exist_ok=True)

    patch = build_test_patch(base, head)
    if not patch.strip():
        sys.exit(f"ERROR: no test-file diff for {pr}; instance would have no tests")
    (inst / "test_patch.diff").write_text(patch)
    check_applies(patch, base, inst)

    # Era-specific twister options. -W in every era: twister turns warnings
    # into errors by default, and a newer compiler than upstream CI's warns
    # about old code that CI built cleanly. --json-report only where that
    # commit's twister has it (2021-01-25 to 2022-05-13); later twister writes
    # the JSON report by default and rejects the option.
    twister_args = ["-W"] + (["--json-report"] if facts["twister"]["json_report"] else [])
    (inst / "instance.json").write_text(json.dumps(
        {"platform": platform, "test_path": test_path, "scenario": scenario,
         "twister_args": twister_args}, indent=2) + "\n")
    shutil.copy(TEMPLATES / "run_tests.py", inst / "run_tests.py")

    definition = (TEMPLATES / "instance.def.tmpl").read_text()
    for key, val in {"@@INSTANCE_ID@@": instance_id, "@@REPO@@": REPO_URL,
                     "@@BASE_COMMIT@@": base, "@@PYTHON@@": facts["python"],
                     "@@SDK@@": facts["sdk"]}.items():
        definition = definition.replace(key, val)
    (inst / "instance.def").write_text(definition)

    cmd = (f"scripts/twister -p {platform} -T {test_path} -s {test_path}/{scenario} -s {scenario} "
           f"-O twister-out --no-clean {' '.join(twister_args)}")
    meta = {
        "instance_id": instance_id,
        "project": "zephyr",
        "pipeline": "twister",
        "repo": REPO_URL,
        # base_commit..fix_commit spans exactly this PR, which is the range
        # validate_instance.py diffs to reconstruct the upstream fix.
        "base_commit": base,
        "fix_commit": head,
        "test_commit": head,
        "problem_statement": triage["problem_statement"],
        "platform": platform,
        "test_path": test_path,
        "scenario": scenario,
        "twister_command": cmd,
        "twister_args": twister_args,
        "run_command": "run_tests",
        "python_version": facts["python"],
        "sdk_version": facts["sdk"],
        "sdk_archive": facts["sdk_archive"],
        "apptainer_image": f"{instance_id}.sif",
        "fail_to_pass": triage["fail_to_pass"],
        "pass_to_pass": triage.get("pass_to_pass", []),
        "files_changed_by_fix": [
            f["filename"] for f in rec["files"]
            if not f["filename"].startswith(("tests/", "doc/", ".github/", "samples/"))
        ],
        "pr_url": rec["url"],
        "change_type": triage.get("change_type"),
        "generated": True,
    }
    (inst / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")
    return inst


def verify_dependent_unittest(clone: Path, package: str, support_roots: list[str],
                              commits: list[str]) -> None:
    """`package` must be a test-side pkg.type: unittest at every commit that
    depends on a test-support root the PR changed."""
    if not is_test_path(package, "mynewt"):
        sys.exit(f"ERROR: dependent test_path {package!r} is not a selftest/test package")
    for commit in dict.fromkeys(commits):
        shown = run(["git", "-C", str(clone), "show", f"{commit}:{package}/pkg.yml"],
                    check=False)
        if shown.returncode:
            sys.exit(f"ERROR: dependent test_path {package!r} does not exist at {commit[:12]}")
        if not re.search(r"^pkg\.type:\s*[\"']?unittest[\"']?\s*(#.*)?$", shown.stdout, re.M):
            sys.exit(f"ERROR: {package!r} is not pkg.type: unittest at {commit[:12]}")
        deps = set(re.findall(r"^\s*-\s*[\"']?@apache-mynewt-core/([^\"'\s#]+)",
                              shown.stdout, re.M))
        if not deps & set(support_roots):
            sys.exit(f"ERROR: {package!r} at {commit[:12]} does not depend on a changed "
                     f"test-support package ({', '.join(support_roots)})")


def mynewt_instance_id(pr: int, triage: dict) -> str:
    if triage.get("kind") == "test_only_detects_earlier_fix":
        fix_pr = triage.get("fix_pr")
        if fix_pr is not None:
            return f"mynewt__mynewt-{fix_pr}-t{pr}"
        fix_commit = triage.get("fix_commit") or ""
        if len(fix_commit) < 8:
            sys.exit("ERROR: test-only verdict without fix_pr requires fix_commit")
        return f"mynewt__mynewt-{pr}-c{fix_commit[:8]}"
    return f"mynewt__mynewt-{pr}"


def diff_rows(diff: str) -> list[tuple[str, str, int, str]]:
    """(file, '+'|'-', line, text) for a `git diff --unified=0`; '-' rows use base numbering."""
    rows, name, old, new = [], None, 0, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[6:] if line.startswith("+++ b/") else None
        elif hunk := re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line):
            old, new = int(hunk[1]), int(hunk[2])
        elif name and line.startswith("-") and not line.startswith("---"):
            rows.append((name, "-", old, line[1:]))
            old += 1
        elif name and line.startswith("+"):
            rows.append((name, "+", new, line[1:]))
            new += 1
    return rows


C_WORDS = {"int", "void", "char", "long", "short", "unsigned", "signed", "struct", "union", "enum",
           "const", "volatile", "static", "return", "sizeof", "if", "else", "for", "while", "do",
           "switch", "case", "break", "continue", "default", "goto", "typedef", "extern", "inline",
           "define", "include", "ifdef", "ifndef", "endif", "defined", "TEST_ASSERT", "TEST_CASE",
           "TEST_SUITE", "TEST_CASE_SELF", "TEST_CASE_DECL", "TEST_ASSERT_FATAL"}
C_SOURCES = (".c", ".cc", ".cpp", ".cxx", ".C", ".h", ".hh", ".hpp")


def enclosing_conditions(text: str, row: int) -> list[str]:
    """The preprocessor conditions (#if/#ifdef/#ifndef/#elif lines) open at line `row`."""
    stack: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if number >= row:
            break
        d = re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)", line)
        if not d:
            continue
        if d[1] in ("if", "ifdef", "ifndef"):
            stack.append(line.strip())
        elif d[1] == "elif" and stack:
            stack[-1] = line.strip()
        elif d[1] == "endif" and stack:
            stack.pop()
    return stack


def derive_compile_expectations(clone: Path, base_commit: str, test_commit: str, fix_commit: str,
                                fix_paths: list[str], changed: list[str], project: str,
                                source: str, api: str) -> tuple[dict, dict] | None:
    """When triage names one (source, API) pair, every pair the before-build
    can fail on: each identifier the added lines of a changed test source use
    that the production fix either adds (in its added lines, absent from the
    base's production tree) or enables (defined at the base inside a
    preprocessor condition on a macro the fix's production lines change, e.g.
    Mbed TLS self-tests behind MBEDTLS_ARIA_C). A new build then fails on each
    of them, at compile or at link, in an order a parallel build does not fix.
    (expectations, gate proofs), or None when the named pair is neither."""
    rows = diff_rows(run(["git", "-C", str(clone), "diff", "--unified=0",
                          f"{base_commit}..{fix_commit}", "--", *fix_paths]).stdout)
    words = lambda text: set(re.findall(r"\b[A-Za-z_]\w{3,}\b", text)) - C_WORDS
    added = words("\n".join(t for _, sign, _, t in rows if sign == "+"))
    macros = {w for w in words("\n".join(t for *_, t in rows)) if re.fullmatch(r"[A-Z][A-Z0-9_]+", w)}

    used: dict[str, set[str]] = {}
    for path in changed:
        if is_test_path(path, project) and Path(path).suffix in C_SOURCES:
            test_rows = diff_rows(run(["git", "-C", str(clone), "diff", "--unified=0",
                                       f"{base_commit}..{test_commit}", "--", path]).stdout)
            lines = "\n".join(t for _, sign, _, t in test_rows if sign == "+")
            # what can fail the build: what the fix adds, and functions the tests call
            used[path] = words(lines) & (added | set(re.findall(r"\b([A-Za-z_]\w+)\s*\(", lines)))
    names = sorted(set().union(*used.values()) | {api})

    def production_hits(*grep: str) -> list[tuple[str, int, str]]:
        """(path, line, text) of one `git grep` over the base's production files."""
        r = subprocess.run(["git", "-C", str(clone), "grep", "-n", "-I", *grep, base_commit],
                           capture_output=True, text=True, errors="replace", timeout=300)
        hits = []
        for line in r.stdout.splitlines():
            _, path, number, text = line.split(":", 3)
            if is_source_path(path, project):
                hits.append((path, int(number), text))
        return hits

    # one grep for every name at once; definitions are among the lines it finds
    mentioned = set()
    definitions: dict[str, list[tuple[str, int]]] = {}
    pattern = re.compile(r"^[A-Za-z_][^;=]*?\b(\w+)\s*\(")
    for path, number, text in production_hits("-w", "-F", *[a for n in names for a in ("-e", n)]):
        mentioned |= words(text) & set(names)
        if (Path(path).suffix in (".c", ".cc", ".cpp") and not text.rstrip().endswith(";")
                and (m := pattern.match(text)) and m[1] in names):
            definitions.setdefault(m[1], []).append((path, number))
    texts: dict[str, str] = {}
    known: dict[str, tuple[str, dict | None] | None] = {}
    for name in names:
        known[name] = None
        if name in added and name not in mentioned:
            known[name] = ("added", None)
            continue
        for path, number in definitions.get(name, []):
            if path not in texts:
                texts[path] = subprocess.run(["git", "-C", str(clone), "show", f"{base_commit}:{path}"],
                                             capture_output=True, text=True, errors="replace").stdout
            gate = next(((c, sorted(words(c) & macros)) for c in enclosing_conditions(texts[path], number)
                         if words(c) & macros), None)
            if gate:
                known[name] = ("enabled", {"path": path, "line": number, "condition": gate[0],
                                           "macro": gate[1][0]})
                break

    if known[api] is None:
        return None
    expectations, gates = {}, {}
    for path, ids in used.items():
        apis = sorted(n for n in ids if known[n])
        if apis:
            expectations[path] = apis
            gates.update({n: known[n][1] for n in apis if known[n][0] == "enabled"})
    if api not in expectations.get(source, []):
        return None
    return expectations, gates


def package_texts(clone: Path, revision: str, path: str) -> list[str]:
    """The C sources and headers of a test package at a revision."""
    names = run(["git", "-C", str(clone), "ls-tree", "-r", "--name-only", revision, "--", path],
                check=False).stdout.split()
    return [subprocess.run(["git", "-C", str(clone), "show", f"{revision}:{n}"], capture_output=True,
                           text=True, errors="replace").stdout
            for n in names if Path(n).suffix in (".c", ".h")]


def attached_note_proof(clone: Path, base_commit: str, test_commit: str, fix_commit: str,
                        fix_paths: list[str], path: str, api: str) -> dict:
    """Attest the test rows where GCC may attach a note naming `api`.

    A macro must be defined by a line the production fix removes, and is attested
    at the test lines the test patch adds. Otherwise `api` must be a struct member
    whose typedef the production fix changes, initialized from a function whose
    signature the test patch changes. Returns {test row: entry}."""
    def git(*args):
        return run(["git", "-C", str(clone), *args]).stdout
    content = git("show", f"{test_commit}:{path}").splitlines()
    added = {row for name, sign, row, _ in diff_rows(
        git("diff", "--unified=0", f"{base_commit}..{test_commit}", "--", path)) if sign == "+"}
    production = diff_rows(git("diff", "--unified=0", f"{base_commit}..{fix_commit}", "--", *fix_paths))
    word = rf"(?<![A-Za-z0-9_]){re.escape(api)}(?![A-Za-z0-9_])"
    rows = {}
    definitions = [(name, row, text) for name, sign, row, text in production
                   if sign == "-" and re.match(rf"\s*#\s*define\s+{re.escape(api)}\b", text)]
    if definitions:
        if len(definitions) != 1:
            sys.exit(f"ERROR: {api!r} must be defined by exactly one removed production line")
        name, row, text = definitions[0]
        if git("show", f"{base_commit}:{name}").splitlines()[row - 1] != text:
            sys.exit(f"ERROR: {api!r} definition row does not match the base file")
        for number in sorted(added):
            line = content[number - 1]
            if re.search(word, line) and not line.lstrip().startswith(("/*", "*", "//")):
                rows[str(number)] = {"api": api, "kind": "macro", "text": line,
                                     "definition": {"path": name, "line": row, "text": text}}
        if not rows:
            sys.exit(f"ERROR: test patch adds no use of macro {api!r} in {path}")
        return rows
    member = None
    for name in fix_paths:
        if Path(name).suffix != ".h":
            continue
        pattern = rf"(?m)^\s*([A-Za-z_]\w*)\s+{re.escape(api)}\s*;"
        after = run(["git", "-C", str(clone), "show", f"{fix_commit}:{name}"], check=False).stdout
        before = run(["git", "-C", str(clone), "show", f"{base_commit}:{name}"], check=False).stdout
        declared, previous = re.search(pattern, after), re.search(pattern, before)
        if not declared or not previous or declared[1] != previous[1]:
            continue
        # The typedef is one declaration spanning several lines: a removed row
        # anywhere inside it changes the member's type.
        old = before.splitlines()
        start = next((i for i, text in enumerate(old, 1)
                      if re.search(rf"typedef[^;]*\(\*\s*{declared[1]}\s*\)", text)), None)
        if start is not None:
            end = next((i for i in range(start, len(old) + 1) if ";" in old[i - 1]), start)
            if any(file == name and sign == "-" and start <= row <= end for file, sign, row, _ in production):
                member = declared[1]
    if member is None:
        sys.exit(f"ERROR: {api!r} is neither a removed production macro nor a member of a changed function-pointer typedef")
    for number, line in enumerate(content, 1):
        initializer = re.fullmatch(rf"\s*\.{re.escape(api)}\s*=\s*([A-Za-z_]\w*)\s*,?\s*", line)
        if not initializer:
            continue
        owner = None
        for back in range(number - 2, -1, -1):
            if content[back].lstrip().startswith("};"):
                break
            if declared := re.fullmatch(r"\s*(?:static\s+)?(?:const\s+)?struct\s+\w+\s+([A-Za-z_]\w*)\s*=\s*\{\s*", content[back]):
                owner = declared[1]
                break
        start = next((i for i, text in enumerate(content, 1)
                      if re.match(rf"{re.escape(initializer[1])}\s*\(", text)), None)
        if owner is None or start is None:
            continue
        end = next((i for i in range(start, len(content) + 1) if "{" in content[i - 1]), start)
        if any(i in added for i in range(start, end + 1)):
            rows[str(number)] = {"api": api, "kind": "initializer", "text": line,
                                 "object": owner, "value": initializer[1]}
    if not rows:
        sys.exit(f"ERROR: no initializer of {api!r} from a test-patch-changed function in {path}")
    return rows


def generate_mynewt(
    pr: int,
    triage: dict,
    out_root: Path,
    tag_suffix: str = "",
    enriched_root: Path | None = None,
    runtime: str = "docker",
) -> Path:
    project = "mynewt"
    cfg = PROJECTS[project]
    templates = cfg["templates"]
    rec_path = (enriched_root or cfg["enriched"]) / f"{pr}.json"
    if not rec_path.exists():
        sys.exit(f"ERROR: no enriched record at {rec_path}. Run `enrich` first.")
    rec = json.loads(rec_path.read_text())
    # A linked issue is evidence, not a requirement: without one, the triage
    # writes the problem statement from the PR text and diff.
    linked = rec.get("linked_issues") or []

    for field in ("problem_statement", "fail_to_pass"):
        if not triage.get(field):
            sys.exit(f"ERROR: triage verdict for {pr} has no {field!r}")

    platform = triage.get("platform", "native")
    if platform != "native":
        sys.exit("ERROR: Mynewt platform must be 'native'; QEMU is not supported")
    if "baseline_tests" not in triage:
        sys.exit(f"ERROR: triage verdict for {pr} has no 'baseline_tests'")
    baseline_path = triage.get("baseline_test_path")
    if baseline_path is not None and (not isinstance(baseline_path, str)
            or Path(baseline_path).is_absolute() or ".." in Path(baseline_path).parts
            or not is_test_path(baseline_path, project)):
        sys.exit("ERROR: baseline_test_path must be a repository-relative test package")

    suites = rec.get("test_suites") or {}
    if not suites:
        sys.exit(f"ERROR: enriched record for {pr} has no test_suites")
    # One PR can touch several suites; the one holding the changed tests is
    # the one with the most test files in the diff.
    test_files = changed_test_paths(rec, project)
    test_path = triage.get("test_path") or max(
        suites, key=lambda r: sum(t.startswith(r + "/") for t in test_files))
    # A changed package can be a pkg.type: lib helper (e.g. sys/log/full/selftest/util)
    # that no `newt test` can run. Then test_path may name an existing unittest
    # package that depends on it, verified from Git below.
    dependent_unittest = test_path not in suites and bool(triage.get("test_path"))
    if dependent_unittest and triage.get("kind") == "test_only_detects_earlier_fix":
        sys.exit("ERROR: a dependent unittest test_path is not supported for test-only verdicts")

    base_commit = rec.get("base_commit")
    head_commit = rec.get("head_commit")
    if not base_commit or not head_commit:
        sys.exit(f"ERROR: enriched record for {pr} predates base_commit/head_commit; re-enrich it")
    test_commit = triage.get("test_commit") or head_commit
    support_paths = triage.get("test_support_paths", [])
    if support_paths and not triage.get("test_commit"):
        sys.exit("ERROR: test_support_paths requires test_commit so production fix "
                 "changes cannot leak into the test patch")
    if triage.get("test_commit"):
        commit_shas = {commit["sha"] for commit in rec.get("commits", [])}
        if test_commit not in commit_shas:
            sys.exit(f"ERROR: test_commit {test_commit} is not a commit in PR {pr}")
    if dependent_unittest:
        verify_dependent_unittest(ensure_clone(project), test_path, list(suites),
                                  [base_commit, test_commit])
    elif test_path not in suites:
        sys.exit(f"ERROR: test_path {test_path!r} is not a test package changed by PR {pr}")
    changed_paths = {changed["filename"] for changed in rec["files"]}
    unknown_support = [path for path in support_paths if path not in changed_paths]
    if unknown_support:
        sys.exit("ERROR: test_support_paths not changed by the PR: "
                 + ", ".join(unknown_support))
    test_files = list(dict.fromkeys([*test_files, *support_paths]))
    kind = triage.get("kind")
    paired = kind == "test_only_detects_earlier_fix"
    fix_pr = triage.get("fix_pr", pr)
    fix_commit = head_commit
    fix_paths = triage.get("fix_paths") or [
        f["filename"] for f in rec["files"] if is_source_path(f["filename"], project)]
    if paired:
        if "fix_pr" not in triage:
            sys.exit("ERROR: test-only verdict requires fix_pr (use null if no PR)")
        fix_commit = triage.get("fix_commit")
        if not fix_commit or not triage.get("fix_paths"):
            sys.exit("ERROR: test-only verdict requires fix_commit and fix_paths")
        clone = ensure_clone(project)
        if fix_pr is not None:
            fix_rec_path = (enriched_root or cfg["enriched"]) / f"{fix_pr}.json"
            if fix_rec_path.exists():
                fix_rec = json.loads(fix_rec_path.read_text())
                commits = [c["sha"] for c in fix_rec.get("commits", [])]
            else:
                merge = run(["git", "-C", str(clone), "log", "--all", "--format=%H",
                             "--extended-regexp", "--grep",
                             rf"^Merge pull request #{fix_pr} "]).stdout.splitlines()
                if len(merge) != 1:
                    sys.exit(f"ERROR: no unique local merge for fix PR {fix_pr}; enrich that PR")
                parents = run(["git", "-C", str(clone), "show", "-s", "--format=%P",
                               merge[0]]).stdout.split()
                if len(parents) != 2:
                    sys.exit(f"ERROR: fix PR {fix_pr} merge is not a two-parent commit")
                commits = run(["git", "-C", str(clone), "rev-list", "--reverse",
                               f"{parents[0]}..{parents[1]}"]).stdout.splitlines()
            if not commits or fix_commit not in commits:
                sys.exit(f"ERROR: fix_commit is not a commit in fix PR {fix_pr}")
            first_fix = commits[0]
            instance_id = mynewt_instance_id(pr, triage)
        else:
            first_fix = fix_commit
            instance_id = mynewt_instance_id(pr, triage)
        base_commit = run(["git", "-C", str(clone), "rev-parse",
                           f"{first_fix}^{{commit}}^"]).stdout.strip()
        test_commit = head_commit
        support_paths = []
        test_files = [path for path in changed_test_paths(rec, project)
                      if any(path.startswith(suite + "/") for suite in suites)]
        for suite in suites:
            if not any(path.startswith(suite + "/") for path in test_files):
                continue
            pkg = run(["git", "-C", str(clone), "cat-file", "-e",
                       f"{base_commit}:{suite}/pkg.yml"], check=False)
            if pkg.returncode:
                sys.exit(f"ERROR: test package {suite!r} does not exist at fix base "
                         f"{base_commit[:12]}")
    else:
        instance_id = mynewt_instance_id(pr, triage)
    inst = out_root / instance_id
    inst.mkdir(parents=True, exist_ok=True)

    patch = build_test_patch(base_commit, test_commit, project, test_files)
    if not patch.strip():
        sys.exit(f"ERROR: no test-file diff for {pr}; instance would have no tests")
    # A selected renamed production file must remove its old path too. A diff
    # restricted to the new name otherwise leaves incompatible old code behind.
    rename_rows = run(["git", "-C", str(ensure_clone(project)), "diff", "--name-status", "--find-renames",
                       f"{base_commit}..{fix_commit}"]).stdout.splitlines()
    for row in rename_rows:
        fields = row.split("\t")
        if (len(fields) == 3 and fields[0].startswith("R") and fields[2] in fix_paths
                and is_source_path(fields[1], project) and fields[1] not in fix_paths):
            fix_paths.append(fields[1])

    failure_mode = triage.get("failure_mode", "runtime")
    if failure_mode not in ("runtime", "compile", "config", "dependency"):
        sys.exit("ERROR: Mynewt failure_mode must be runtime, compile, config or dependency")
    def observed_build_output(expectation):
        path = Path(expectation.get("observed_log", ""))
        evidence = (REPO_ROOT / path).resolve()
        if (path.is_absolute() or ".." in path.parts or not evidence.is_relative_to(REPO_ROOT.resolve())
                or not evidence.is_file()):
            sys.exit("ERROR: build-failure expectation requires a saved repository-relative observed_log")
        raw = evidence.read_bytes()
        text = raw.decode("utf-8", errors="backslashreplace")
        marker = '\n{"failure_mode":'
        if marker not in text:
            sys.exit("ERROR: observed_log has no recorded build output and structured result")
        output = text.split(marker, 1)[0].strip()
        if output != expectation.get("output", "").strip():
            sys.exit("ERROR: expected build failure differs from its saved observed_log")
        (inst / "observed_before.log").write_bytes(raw)
        return output, {"observed_log": path.as_posix(), "observed_log_sha256": hashlib.sha256(raw).hexdigest(),
                        "observed_evidence_file": "observed_before.log"}
    dependency_proof = None
    dependency_output_sha256 = None
    if failure_mode == "dependency":
        import yaml
        expectation = triage.get("dependency_expectation", {})
        provider = expectation.get("provider", "")
        output, observed_proof = observed_build_output(expectation)
        provider_file = provider + "/pkg.yml"
        clone = ensure_clone(project)
        if (triage.get("change_type") != "feature_with_test" or provider_file not in fix_paths
                or not is_source_path(provider_file, project)
                or any(path.startswith(provider + "/") and not is_test_path(path, project) for path in test_files)
                or run(["git", "-C", str(clone), "cat-file", "-e", f"{base_commit}:{provider_file}"], check=False).returncode == 0
                or not output):
            sys.exit("ERROR: dependency mode requires a functionality-addition task whose production provider is absent at base and selected in gold")
        def package_at(revision, path):
            return yaml.safe_load(run(["git", "-C", str(clone), "show", f"{revision}:{path}/pkg.yml"]).stdout)
        fixed_provider = package_at(fix_commit, provider)
        test_dependencies = package_at(test_commit, test_path).get("pkg.deps") or []
        dependency = "@apache-mynewt-core/" + provider
        if fixed_provider.get("pkg.name") != provider or fixed_provider.get("pkg.type") == "unittest":
            sys.exit("ERROR: dependency provider name does not match its production package")
        if expectation.get("kind") == "package":
            message = f"Could not resolve package dependency: {dependency}; depender: {test_path}"
            if not any(name in test_dependencies for name in (dependency, provider)) or message not in output:
                sys.exit("ERROR: the upstream test must directly depend on the exact unresolved new package")
        elif expectation.get("kind") == "api_provider":
            api, consumer, selector = expectation["api"], expectation["consumer"], expectation["selector_path"]
            before_selector = yaml.safe_load(run(["git", "-C", str(clone), "show", f"{base_commit}:{selector}"]).stdout)
            after_selector = yaml.safe_load(run(["git", "-C", str(clone), "show", f"{fix_commit}:{selector}"]).stdout)
            if (selector not in fix_paths or selector in test_files or "@apache-mynewt-core/" + consumer not in test_dependencies
                    or api not in (package_at(base_commit, consumer).get("pkg.req_apis") or [])
                    or api not in (fixed_provider.get("pkg.apis") or [])
                    or dependency in (before_selector.get("pkg.deps") or [])
                    or dependency not in (after_selector.get("pkg.deps") or [])
                    or f"* {api}, required by: {consumer}" not in output):
                sys.exit("ERROR: missing API must be required by the test's base dependency and supplied/selected by the production fix")
        else:
            sys.exit("ERROR: unknown dependency expectation kind")
        dependency_output_sha256 = hashlib.sha256(output.encode()).hexdigest()
        dependency_proof = {k: v for k, v in expectation.items() if k != "output"}
        dependency_proof.update(observed_proof)
        dependency_proof["provider_blob"] = run(["git", "-C", str(clone), "rev-parse", f"{fix_commit}:{provider_file}"]).stdout.strip()
    config_proof = None
    config_output_sha256 = None
    if failure_mode == "config":
        import yaml
        expectation = triage.get("config_expectation", {})
        provider = expectation.get("production_path")
        setting = expectation.get("setting")
        test_config = expectation.get("test_config_path")
        test_settings = expectation.get("test_settings")
        output, observed_proof = observed_build_output(expectation)
        if (provider not in fix_paths or not is_source_path(provider, project)
                or provider in test_files or provider == test_config or test_config not in test_files
                or not isinstance(setting, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", setting)
                or not isinstance(test_settings, dict) or not test_settings
                or "Syscfg restriction violations detected:" not in output
                or setting not in output.split("Setting history", 1)[0]):
            sys.exit("ERROR: config mode requires an observed restriction failure, changed production setting and protected test configuration")
        clone = ensure_clone(project)
        def read_config(revision, path):
            text = run(["git", "-C", str(clone), "show", f"{revision}:{path}"]).stdout
            return yaml.safe_load(text) or {}
        old = read_config(base_commit, provider)
        fixed = read_config(fix_commit, provider)
        value_changed = old.get("syscfg.vals", {}).get(setting) != fixed.get("syscfg.vals", {}).get(setting)
        old_restrictions = (old.get("syscfg.defs", {}).get(setting) or {}).get("restrictions")
        fixed_restrictions = (fixed.get("syscfg.defs", {}).get(setting) or {}).get("restrictions")
        if not value_changed and old_restrictions == fixed_restrictions:
            sys.exit("ERROR: the production fix does not change this setting's value or restrictions")
        values = read_config(test_commit, test_config).get("syscfg.vals", {})
        if any(name not in values or values[name] != value for name, value in test_settings.items()):
            sys.exit("ERROR: expected test settings do not match the upstream test configuration")
        config_output_sha256 = hashlib.sha256(output.encode()).hexdigest()
        config_proof = {"production_path": provider, "setting": setting,
                        "test_config_path": test_config, "test_settings": test_settings,
                        "base_blob": run(["git", "-C", str(clone), "rev-parse", f"{base_commit}:{provider}"]).stdout.strip(),
                        "fix_blob": run(["git", "-C", str(clone), "rev-parse", f"{fix_commit}:{provider}"]).stdout.strip(),
                        "value_changed": value_changed, "restrictions_changed": old_restrictions != fixed_restrictions}
        config_proof.update(observed_proof)
    compile_test_source = triage.get("compile_test_source")
    missing_api = triage.get("missing_api")
    compile_expectations = triage.get("compile_expectations")
    base_api_providers = triage.get("base_api_providers", {})
    base_api_provider_proofs = {}
    if not isinstance(base_api_providers, dict):
        sys.exit("ERROR: base_api_providers must map required APIs to existing base providers")
    if base_api_providers:
        import yaml
        clone = ensure_clone(project)
        def base_package(path):
            return yaml.safe_load(run(["git", "-C", str(clone), "show", f"{base_commit}:{path}/pkg.yml"]).stdout)
        original_dependencies = base_package(test_path).get("pkg.deps", [])
        for api, entry in base_api_providers.items():
            consumer, provider = entry["required_by"], entry["provider"]
            if ("@apache-mynewt-core/" + provider not in original_dependencies
                    or api not in (base_package(consumer).get("pkg.req_apis") or [])
                    or api not in (base_package(provider).get("pkg.apis") or [])
                    or any(path.startswith((consumer + "/", provider + "/")) for path in [*fix_paths, *test_files])):
                sys.exit("ERROR: base API fixture must preserve an original test dependency and unchanged provider/consumer")
            base_api_provider_proofs[api] = {**entry,
                "base_provider_blob": run(["git", "-C", str(clone), "rev-parse", f"{base_commit}:{provider}/pkg.yml"]).stdout.strip(),
                "base_consumer_blob": run(["git", "-C", str(clone), "rev-parse", f"{base_commit}:{consumer}/pkg.yml"]).stdout.strip()}
    dependency_headers = triage.get("compile_dependency_headers", {})
    dependency_header_proofs = {}
    if not isinstance(dependency_headers, dict) or (dependency_headers and not compile_expectations):
        sys.exit("ERROR: compile_dependency_headers requires compile expectations")
    compile_declarations = {}
    compile_notes = {}
    attached_notes = triage.get("compile_attached_notes", {})
    if not isinstance(attached_notes, dict) or (attached_notes and not compile_expectations):
        sys.exit("ERROR: compile_attached_notes requires a compile expectation map")
    dependency_constants = triage.get("compile_dependency_constants", {})
    dependency_constant_proofs = {}
    if not isinstance(dependency_constants, dict) or (dependency_constants and not compile_expectations):
        sys.exit("ERROR: compile_dependency_constants requires a compile expectation map")
    test_observer = triage.get("test_observer")
    mcumgr_commit = triage.get("mcumgr_commit")
    dependency_pin = None
    if mcumgr_commit is not None:
        if not isinstance(mcumgr_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", mcumgr_commit):
            sys.exit("ERROR: mcumgr_commit must be an exact commit")
        clone = ensure_clone(project)
        import render_mynewt_instance
        release = render_mynewt_instance.core_release(clone, base_commit)
        core_tag = "mynewt_" + "_".join(map(str, release)) + "_tag"
        repository = run(["git", "-C", str(clone), "show", f"{base_commit}:repository.yml"]).stdout
        block = re.search(r"(?m)^    apache-mynewt-mcumgr:\n((?:[ \t]{8,}[^\n]*(?:\n|$))*)", repository)
        version = re.search(rf"(?m)^\s+{re.escape(core_tag)}:\s+(\d+\.\d+\.\d+)\s*$", block[1]) if block else None
        if (not version or not re.search(r"(?m)^\s+master:\s+0-dev\s*$", block[1])
                or not re.search(r"(?m)^\s+user:\s+apache\s*$", block[1])
                or not re.search(r"(?m)^\s+repo:\s+mynewt-mcumgr\s*$", block[1])):
            sys.exit("ERROR: base does not declare an active mcumgr dependency and compatible release mapping")
        dependency_clone = REPO_ROOT / "candidates/dependency-cache/mynewt-mcumgr.git"
        if not dependency_clone.exists():
            dependency_clone.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "--mirror", "https://github.com/apache/mynewt-mcumgr.git",
                 str(dependency_clone)], timeout=900)
        tag = "mcumgr_" + version[1].replace(".", "_") + "_tag"
        commit = run(["git", "-C", str(dependency_clone), "rev-parse", f"refs/tags/{tag}^{{commit}}"]).stdout.strip()
        if commit != mcumgr_commit:
            sys.exit("ERROR: mcumgr_commit does not match the upstream compatible-release tag")
        dependency_pin = {"commit": commit, "version": version[1], "tag": tag,
                          "core_release_tag": core_tag, "source": f"{base_commit}:repository.yml",
                          "selection": "compatible release fixture; not an author-specified master lock"}
    if test_observer is not None:
        if test_observer != "testutil-gdb":
            sys.exit("ERROR: unsupported Mynewt test_observer")
        observer_source = run(["git", "-C", str(ensure_clone(project)), "show",
                               f"{test_commit}:test/testutil/src/case.c"]).stdout
        if not all(token in observer_source for token in ("tu_case_write_pass_buf(void)", "tu_case_fail(void)", "tu_case_name")):
            sys.exit("ERROR: testutil-gdb requires the upstream testutil result-event symbols")
        observer_config = observer_source + run(["git", "-C", str(ensure_clone(project)), "show",
                                                f"{test_commit}:test/testutil/src/testutil.c"]).stdout
        if not any(config in observer_config for config in ("tu_config.ts_suite_name", "ts_current_config->ts_suite_name")):
            sys.exit("ERROR: testutil-gdb requires a recognized upstream suite configuration")
    compile_gates = {}      # API -> the base definition's condition on a macro the fix changes
    if compile_expectations is not None and failure_mode != "compile":
        sys.exit("ERROR: compile_expectations requires compile mode")
    if failure_mode == "compile":
        if not isinstance(compile_test_source, str) or not compile_test_source.strip():
            sys.exit("ERROR: compile mode requires compile_test_source")
        if not isinstance(missing_api, str) or not missing_api.strip():
            sys.exit("ERROR: compile mode requires missing_api")
        clone = ensure_clone(project)
        changed = run([
            "git", "-C", str(clone), "diff", "--name-only",
            f"{base_commit}..{test_commit}", "--", *test_files,
        ]).stdout.splitlines()
        if dependency_headers:
            import yaml
            for header, spec in dependency_headers.items():
                api, provider = spec["api"], spec["provider"]
                package, flag, test_config = spec["production_path"], spec["flag"], spec["test_config_path"]
                if (not is_test_path(header, project) or Path(header).suffix != ".h"
                        or package not in fix_paths or test_config not in test_files
                        or Path(provider).is_absolute() or ".." in Path(provider).parts
                        or not re.fullmatch(r"[A-Z][A-Z0-9_]*", flag)):
                    sys.exit("ERROR: dependency header requires a protected test header, changed package edge and test configuration")
                def git_text(revision, path):
                    return run(["git", "-C", str(clone), "show", f"{revision}:{path}"]).stdout
                content = git_text(test_commit, header)
                if content != git_text(base_commit, header) or not re.search(
                        rf'^\s*#\s*include\s*[<"]{re.escape(api)}[>"]', content, re.M):
                    sys.exit("ERROR: dependency header must be unchanged and directly include the missing API header")
                old_package = yaml.safe_load(git_text(base_commit, package))
                fixed_package = yaml.safe_load(git_text(fix_commit, package))
                edge, dependency = "pkg.deps." + flag, "@apache-mynewt-core/" + provider
                test_values = yaml.safe_load(git_text(test_commit, test_config)).get("syscfg.vals", {})
                if (dependency in (old_package.get(edge) or [])
                        or dependency not in (fixed_package.get(edge) or [])
                        or test_values.get(flag) != 1):
                    sys.exit("ERROR: production fix must add the provider dependency selected by the upstream test")
                if yaml.safe_load(git_text(base_commit, provider + "/pkg.yml")).get("pkg.name") != provider:
                    sys.exit("ERROR: dependency provider must already exist at base")
                git_text(base_commit, provider + "/include/" + api)
                if compile_expectations.get(header) != [api]:
                    sys.exit("ERROR: dependency header expectation must contain only the proven missing header")
                dependency_header_proofs[header] = {**spec,
                    "header_blob": run(["git", "-C", str(clone), "rev-parse", f"{base_commit}:{header}"]).stdout.strip(),
                    "base_package_blob": run(["git", "-C", str(clone), "rev-parse", f"{base_commit}:{package}"]).stdout.strip(),
                    "fix_package_blob": run(["git", "-C", str(clone), "rev-parse", f"{fix_commit}:{package}"]).stdout.strip()}
        header_source = Path(compile_test_source).suffix in (".h", ".hh", ".hpp")
        if (Path(compile_test_source).suffix not in (".c", ".cc", ".cpp", ".cxx", ".C", ".h", ".hh", ".hpp")
                or not is_test_path(compile_test_source, project)
                or (compile_test_source not in changed and compile_test_source not in dependency_header_proofs)):
            sys.exit("ERROR: compile_test_source must be a changed test-side C/C++ source or header in the test patch")
        source = run([
            "git", "-C", str(clone), "show", f"{test_commit}:{compile_test_source}",
        ]).stdout
        if missing_api not in source:
            sys.exit("ERROR: missing_api is absent from compile_test_source")
        if header_source and compile_test_source not in dependency_header_proofs:
            # GCC locates a missing include in the test header that names it.
            # Accept that location only for a production header this fix adds.
            if not re.search(rf'^\s*#\s*include\s*[<"]{re.escape(missing_api)}[>"]', source, re.M):
                sys.exit("ERROR: header compile_test_source requires missing_api to be an #include path")
            base_files = run(["git", "-C", str(clone), "ls-tree", "-r", "--name-only",
                              base_commit]).stdout.splitlines()
            added = run(["git", "-C", str(clone), "diff", "--name-only", "--diff-filter=A",
                         f"{base_commit}..{fix_commit}", "--", *fix_paths]).stdout.splitlines()
            if any(path.endswith("/" + missing_api) for path in base_files) or not any(
                    path in fix_paths and path.endswith("/" + missing_api) and is_source_path(path, project)
                    for path in added):
                sys.exit("ERROR: included header must be absent at base and added by the production fix")
        if compile_expectations is None and fix_paths:
            derived = derive_compile_expectations(clone, base_commit, test_commit, fix_commit, fix_paths,
                                                  changed, project, compile_test_source, missing_api)
            if derived and derived[0] != {compile_test_source: [missing_api]}:
                compile_expectations, compile_gates = derived
        if compile_expectations is not None:
            if not isinstance(compile_expectations, dict) or not compile_expectations or not fix_paths:
                sys.exit("ERROR: compile_expectations must map changed test sources to production API identifiers")
            production_diff = run(["git", "-C", str(clone), "diff", "--unified=0",
                                   f"{base_commit}..{fix_commit}", "--", *fix_paths]).stdout
            changed_production = "\n".join(line[1:] for line in production_diff.splitlines()
                                           if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
            keywords = {"int", "void", "char", "long", "short", "unsigned", "signed", "struct",
                        "union", "enum", "const", "volatile", "static", "return", "sizeof"}
            for path, apis in compile_expectations.items():
                if (not isinstance(path, str) or (path not in changed and path not in dependency_header_proofs) or not is_test_path(path, project)
                        or Path(path).suffix not in (".c", ".cc", ".cpp", ".cxx", ".C", ".h", ".hh", ".hpp")
                        or not isinstance(apis, list) or not apis):
                    sys.exit("ERROR: each compile expectation needs a changed protected test source and API list")
                content = run(["git", "-C", str(clone), "show", f"{test_commit}:{path}"]).stdout
                for api in apis:
                    if path in dependency_header_proofs:
                        continue
                    if (isinstance(api, str) and "/" in api and ".." not in Path(api).parts
                            and re.fullmatch(r"[A-Za-z_][\w./-]*\.h", api)):
                        if not re.search(rf'^\s*#\s*include\s*[<"]{re.escape(api)}[>"]', content, re.M):
                            sys.exit("ERROR: expected missing header must be included by its changed test source")
                        base_headers = run(["git", "-C", str(clone), "ls-tree", "-r", "--name-only",
                                            base_commit]).stdout.splitlines()
                        added_headers = run(["git", "-C", str(clone), "diff", "--name-only", "--diff-filter=A",
                                             f"{base_commit}..{fix_commit}", "--", *fix_paths]).stdout.splitlines()
                        if any(name.endswith("/" + api) for name in base_headers) or not any(
                                name.endswith("/" + api) and is_source_path(name, project) for name in added_headers):
                            sys.exit("ERROR: expected header must be absent at base and added by the production fix")
                        # Newt writes MYNEWT_VAL() into syscfg/syscfg.h. When the test patch
                        # swaps that include for the new header, the Newt dependency scan
                        # (gcc -MM -MG) skips the missing header and rejects each #if use
                        # of the macro. Attest those rows only if the fix's header
                        # re-exports syscfg/syscfg.h and the test source used to include it.
                        syscfg = r'^\s*#\s*include\s*[<"]syscfg/syscfg\.h[>"]'
                        base_content = run(["git", "-C", str(clone), "show", f"{base_commit}:{path}"], check=False).stdout
                        header = next(name for name in added_headers if name.endswith("/" + api))
                        provided = run(["git", "-C", str(clone), "show", f"{fix_commit}:{header}"]).stdout
                        include = re.search(rf'^\s*#\s*include\s*[<"]{re.escape(api)}[>"]', content, re.M)
                        if (re.search(syscfg, base_content, re.M) and not re.search(syscfg, content, re.M)
                                and re.search(syscfg, provided, re.M)):
                            first = content[:include.start()].count("\n") + 1
                            for number, line in enumerate(content.splitlines(), 1):
                                if number > first and re.match(r"\s*#\s*(?:if|elif)\s+MYNEWT_VAL\(", line):
                                    compile_notes.setdefault(path, {})[str(number)] = {
                                        "api": api, "kind": "preprocessor", "macro": "MYNEWT_VAL",
                                        "provider": "syscfg/syscfg.h", "text": line}
                        continue
                    if (not isinstance(api, str) or not re.fullmatch(r"[A-Za-z_]\w*", api) or api in keywords
                            or not re.search(rf"\b{re.escape(api)}\b", content)):
                        sys.exit(f"ERROR: compile API {api!r} must occur in its test source and changed production lines")
                    if api in (attached_notes.get(path) or []):
                        compile_notes.setdefault(path, {}).update(attached_note_proof(
                            clone, base_commit, test_commit, fix_commit, fix_paths, path, api))
                    elif api not in compile_gates and not re.search(rf"\b{re.escape(api)}\b", changed_production):
                        # This migration supplies an enum through the upstream
                        # dependency pin, rather than a core header definition.
                        header = dependency_constants.get(api)
                        package = "encoding/tinycbor/pkg.yml"
                        if (not isinstance(header, str) or not re.fullmatch(r"src/[A-Za-z0-9_/-]+\.h", header)
                                or package not in fix_paths):
                            sys.exit(f"ERROR: compile API {api!r} is not attested by the production fix")
                        after = run(["git", "-C", str(clone), "show", f"{fix_commit}:{package}"]).stdout
                        before = run(["git", "-C", str(clone), "show", f"{base_commit}:{package}"]).stdout
                        pattern = r"(?m)^repository\.tinycbor:\n((?:[ \t]+[^\n]*(?:\n|$))*)"
                        new = re.search(pattern, after)
                        old = re.search(pattern, before)
                        fields = dict(re.findall(r"(?m)^\s+(\w+):\s*([^\s#]+)", new[1])) if new else {}
                        revision = fields.get("vers", "")
                        if (fields.get("type") != "github" or fields.get("user") != "intel"
                                or fields.get("repo") != "tinycbor" or not re.fullmatch(r"[0-9a-f]{40}-commit", revision)
                                or (old and old[1] == new[1])):
                            sys.exit("ERROR: dependency constant requires a changed immutable upstream TinyCBOR pin")
                        revision = revision[:-len("-commit")]
                        cache = REPO_ROOT / "candidates/dependency-cache/tinycbor.git"
                        if not cache.exists():
                            cache.parent.mkdir(parents=True, exist_ok=True)
                            run(["git", "clone", "--mirror", "https://github.com/intel/tinycbor.git", str(cache)], timeout=900)
                        declaration = run(["git", "-C", str(cache), "show", f"{revision}:{header}"]).stdout
                        if not re.search(rf"(?m)^\s*{re.escape(api)}\s*=", declaration):
                            sys.exit(f"ERROR: {api!r} is not an enum declaration in the pinned dependency header")
                        dependency_constant_proofs[api] = {"package": package, "commit": revision, "header": header,
                            "blob": run(["git", "-C", str(cache), "rev-parse", f"{revision}:{header}"]).stdout.strip()}
                for number, line in enumerate(content.splitlines(), 1):
                    declaration = re.fullmatch(r"\s*(?:static\s+)?struct\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(\[\s*[A-Za-z_0-9]+\s*\])?\s*;\s*", line)
                    if declaration and declaration[1] in apis:
                        compile_declarations.setdefault(path, {})[str(number)] = {
                            "api": declaration[1], "variable": declaration[2], "text": line,
                            "array": bool(declaration[3])}
            if any(path not in compile_expectations or any(api not in compile_expectations[path] for api in apis)
                   for path, apis in attached_notes.items()):
                sys.exit("ERROR: compile_attached_notes must name compile_expectations pairs")
            if missing_api not in compile_expectations.get(compile_test_source, []):
                sys.exit("ERROR: compile_expectations must include the primary source/API pair")
    (inst / "test_patch.diff").write_text(patch)
    check_applies(patch, base_commit, inst, project)

    if runtime == "docker":
        dockerfile = (templates / "Dockerfile.tmpl").read_text()
        for key, val in {"@@BASE_COMMIT@@": base_commit, "@@REPO@@": cfg["repo_url"]}.items():
            dockerfile = dockerfile.replace(key, val)
        (inst / "Dockerfile").write_text(dockerfile)
        (inst / "run_tests.sh").write_text((templates / "run_tests.sh").read_text())

    meta = {
        "instance_id": instance_id,
        "project": project,
        "repo": cfg["repo_url"],
        # base_commit..fix_commit spans exactly this PR. When test-only support
        # precedes the production fix, gold_base_commit below narrows the range
        # validate_instance.py uses to reconstruct that fix.
        "base_commit": base_commit,
        "fix_commit": fix_commit,
        "test_commit": test_commit,
        "fix_pr": fix_pr,
        "test_pr": pr,
        "problem_statement": triage["problem_statement"],
        "platform": platform,
        "test_path": test_path,
        "build_command": "true",
        "run_command": "run_tests",
        "docker_image": f"embedeval:{project}-{pr}{tag_suffix}",
        "fail_to_pass": triage["fail_to_pass"],
        "pass_to_pass": triage.get("pass_to_pass", []),
        "files_changed_by_fix": fix_paths,
        "pr_url": rec["url"],
        "change_type": triage.get("change_type"),
        "generated": True,
    }
    meta.update({
        "issue_url": (f"https://github.com/apache/mynewt-core/issues/{linked[0]['number']}"
                      if linked else None),
        "failure_mode": failure_mode,
        "compile_test_source": compile_test_source,
        "missing_api": missing_api,
        "docker_platform": "linux/amd64",
        "structured_test_results": "/tmp/mynewt-result.json",
        "baseline_tests": triage["baseline_tests"],
        "protected_paths": list(dict.fromkeys(
            path.rstrip("/") + "/"
            for path in ["test", *suites, *([test_path] if dependent_unittest else []),
                         *([baseline_path] if baseline_path else [])])),
        "compatibility_cflags": triage.get("compatibility_cflags", []),
    })
    # Cases a TEST_SUITE calls only inside `#if 0` never run: expecting them
    # would fail every phase. Upstream disables tests this way (e.g. the six
    # iat2_rat2 cases of ble_sm_sc_test.c, 2017-2019).
    from triage_mynewt_prs import disabled_cases
    clone = ensure_clone(project)
    off = set(disabled_cases(package_texts(clone, test_commit, test_path)))
    if not set(meta["fail_to_pass"]) - off:
        sys.exit("ERROR: every fail_to_pass case is one upstream disabled with #if 0")
    off_base = set(disabled_cases(package_texts(clone, base_commit, baseline_path or test_path)))
    dropped = sorted((off & (set(meta["fail_to_pass"]) | set(meta["pass_to_pass"])))
                     | (off_base & set(meta["baseline_tests"])))
    meta["fail_to_pass"] = [n for n in meta["fail_to_pass"] if n not in off]
    meta["pass_to_pass"] = [n for n in meta["pass_to_pass"] if n not in off]
    meta["baseline_tests"] = [n for n in meta["baseline_tests"] if n not in off_base]
    if dropped:
        meta["disabled_tests_dropped"] = dropped
    if dependent_unittest:
        meta["test_support_roots"] = list(suites)
    # Test infrastructure can live outside the discovered unittest packages.
    meta["protected_paths"].extend(path for path in test_files
        if (is_test_path(path, project) or path in support_paths)
        and not any(path.startswith(root) for root in meta["protected_paths"]))
    if compile_expectations is not None:
        meta["compile_expectations"] = compile_expectations
    if compile_gates:
        meta["compile_gates"] = compile_gates      # host metadata only: not in bench.json
        if dependency_header_proofs:
            meta["compile_dependency_header_proofs"] = dependency_header_proofs
        if compile_declarations:
            meta["compile_declarations"] = compile_declarations
        if compile_notes:
            meta["compile_notes"] = compile_notes
        if dependency_constant_proofs:
            meta["compile_dependency_constant_proofs"] = dependency_constant_proofs
    if test_observer is not None:
        meta["test_observer"] = test_observer
    if config_proof is not None:
        meta["config_attribution"] = config_proof
        meta["config_output_sha256"] = config_output_sha256
    if dependency_proof is not None:
        meta["dependency_attribution"] = dependency_proof
        meta["dependency_output_sha256"] = dependency_output_sha256
    if base_api_provider_proofs:
        meta["base_api_provider_proofs"] = base_api_provider_proofs
        meta["base_api_provider_packages"] = list(dict.fromkeys(x["provider"] for x in base_api_provider_proofs.values()))
    if dependency_pin:
        meta["mcumgr_commit"] = mcumgr_commit
        meta["dependency_pin"] = dependency_pin
    if baseline_path:
        meta["baseline_test_path"] = baseline_path
    if triage.get("gold_patch_three_way"):
        meta["gold_patch_three_way"] = True
    if not paired and test_commit != head_commit:
        meta["gold_base_commit"] = test_commit
    if triage.get("simulator_fixture"):
        meta["simulator_fixture"] = triage["simulator_fixture"]
    import render_mynewt_instance
    compatibility_patch, compatibility_proof = render_mynewt_instance.simulator_fixture(meta, ensure_clone(project), patch)
    if compatibility_proof:
        meta["simulator_fixture_proof"] = compatibility_proof
        meta["protected_paths"].append(compatibility_proof["path"])
        (inst / "simulator_compat.diff").write_text(compatibility_patch)
    if runtime == "docker":
        fixture = ("COPY simulator_compat.diff /opt/benchmark/\n"
                   "RUN cd /testbed && git apply /opt/benchmark/simulator_compat.diff "
                   "&& git -c user.name=EmbedEval -c user.email=benchmark@localhost commit -a -m \"sim: pinned upstream taskpool stack sizing\"") if compatibility_patch else ""
        file = inst / "Dockerfile"
        file.write_text(file.read_text().replace("@@SIMULATOR_FIXTURE@@", fixture))
    (inst / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")
    if runtime == "apptainer":
        # An Apptainer recipe instead of the Dockerfile (scripts/render_mynewt_instance.py).
        import render_mynewt_instance
        with tempfile.TemporaryDirectory(prefix="embedeval-render-") as tmp:
            rendered = render_mynewt_instance.render(meta, inst / "test_patch.diff", Path(tmp))
            for f in rendered.iterdir():
                shutil.copy2(f, inst / f.name)
    return inst



def generate(pr: int, triage: dict, out_root: Path, tag_suffix: str = "",
             project: str = "zephyr", force: bool = False,
             enriched_root: Path | None = None, runtime: str = "docker") -> Path:
    if project == "mynewt":
        return generate_mynewt(pr, triage, out_root, tag_suffix, enriched_root, runtime)
    if project == "zephyr":
        return generate_zephyr(pr, triage, out_root, force)
    sys.exit(f"ERROR: unknown project {project!r}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate a Zephyr or Mynewt instance from a triage verdict."
    )
    p.add_argument("--repo", choices=("zephyr", "mynewt"), default="zephyr")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--triage", required=True, help="path to the triage verdict JSON")
    p.add_argument("--enriched-root", type=Path,
                   help="Mynewt only: directory containing enriched <pr>.json records")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="output root (default: generated/, never docker/instances/)")
    p.add_argument("--force", action="store_true",
                   help="generate even if the board/scenario check fails (for testing)")
    p.add_argument("--tag-suffix", default="",
                   help="Mynewt only: append to the image tag, e.g. -gen, so building a "
                        "generated instance cannot overwrite an existing image")
    p.add_argument("--runtime", choices=("docker", "apptainer"), default="docker",
                   help="Mynewt only: write an Apptainer recipe (instance.def, for hosts "
                        "without Docker such as Scholar) instead of a Dockerfile")
    args = p.parse_args()

    triage = json.loads(Path(args.triage).read_text())
    if triage.get("verdict") == "reject" or triage.get("kind") in ("maintenance", "needs_hw"):
        sys.exit(f"triage rejected PR {args.pr}: {triage.get('reason','no reason given')}")

    inst = generate(args.pr, triage, Path(args.out), args.tag_suffix, args.repo, args.force,
                    enriched_root=args.enriched_root, runtime=args.runtime)
    print(f"  wrote {layout.show(inst)}")
    for f in sorted(inst.iterdir()):
        print(f"    {f.name:<20} {f.stat().st_size:>7} B")


if __name__ == "__main__":
    main()
