#!/usr/bin/env python3
"""
Verify an instance is well formed: its tests must FAIL at the base commit and
PASS once the upstream fix is applied.

    python scripts/validate_instance.py nuttx__nuttx-11889
    python scripts/validate_instance.py --repo riot
    python scripts/validate_instance.py zephyr__zephyr-65697 --patch outputs/.../patch.diff

No API key and no LLM involved, so this is the cheapest way to confirm a built
image actually works. Replaces validate_instance.sh and
validate_riot_instance.sh, and gives NuttX a validator for the first time --
previously it had only 16 hand-written per-PR scripts.

Zephyr instances from the twister pipeline (metadata "pipeline": "twister")
are validated in their Apptainer image instead, exactly as built:

    EMBEDEVAL_INSTANCES_DIR=generated python scripts/validate_instance.py zephyr__zephyr-65697

Each step runs in a throwaway container with a fresh writable overlay, so the
image itself is never changed:

    1. run_tests on the image as built     -> must FAIL (exit 1)
    2. apply the upstream fix (base_commit..fix_commit outside tests/)
    3. run_tests again                     -> must PASS (exit 0)

"Fail" is any failure -- a test, the build or the configuration -- but not
"nothing ran": a scenario filtered on the board (exit 3) is invalid, never a
failure to fix. A report is written to <instance>/validation.json.

Mynewt instances (scripts/render_mynewt_instance.py): the image is
unpacked to a writable sandbox on node-local disk (a .sif is very slow to
compile in on ARM64), and in it

    0. baseline: record the original tests as diagnostic evidence, not an acceptance gate
       (recorded not_applicable when the selected package is added by the test
       patch and baseline_tests is []; run at metadata baseline_test_path when
       the package was renamed or split from an older unittest: baseline_proof)
    1. run_tests -> exactly fail_to_pass fail, pass_to_pass pass (exit 1)
    2. apply the upstream fix (gold_base_commit|base_commit..fix_commit)
    3. run_tests -> everything passes (exit 0)

each judged on the runner's structured result (/tmp/mynewt-result.json), as in
Docker. A mynewt-core clone (EMBEDEVAL_MYNEWT_CLONE) supplies the fix. Every
command runs without a network (scripts/mynewt_network.py), as the agent's do.
Before step 0 the image is checked for anything the agent must not find
(scripts/mynewt_leakcheck.py: post-base history, the fix, copies of
mynewt-core) and for a route out; a finding makes the instance invalid. With
--determinism N the before state runs N - 1 more times and must be judged as
in step 1 every time (for a runtime oracle: every fail_to_pass case fails and
every pass_to_pass case passes; for the others, the same attributed failure).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au
import build_config
import instances
import mynewt_leakcheck
from mynewt_network import isolated_command


def sh(cmd: list[str], timeout: int = 120, quiet: bool = True):
    return subprocess.run(cmd, capture_output=quiet, text=True, timeout=timeout)


def compile_line_matches(source: str, api: str, diagnostic) -> bool:
    match = (re.fullmatch(r"(.+?):\d+(?::\d+)?:\s*(?:fatal )?error:\s*(.*)", diagnostic)
             if isinstance(diagnostic, str) else None)
    if match is None and isinstance(diagnostic, str):
        match = re.fullmatch(
            r"(?:/[^:]+/ld:\s*)?(.+?):\d+:\([^\n]+\): undefined reference to [`‘]([^'’]+)['’]",
            diagnostic,
        )
    return bool(match and (match[1] == source or match[1].endswith("/" + source))
                and re.search(rf"(?<![A-Za-z0-9_]){re.escape(api)}(?![A-Za-z0-9_])", match[2]))


def compile_note_matches(meta: dict, source: str, api: str, item: dict) -> bool:
    """Re-check an error plus its attached note against rows attested at generation."""
    entry = meta.get("compile_notes", {}).get(source, {}).get(item.get("line"))
    parts = item["diagnostic"].split("\n")
    error = re.fullmatch(r"(.+?):(\d+):(\d+): error: (.*)", parts[0])
    if not entry or entry["api"] != api or len(parts) != 2 or not error:
        return False
    at = lambda file, wanted: file == wanted or file.endswith("/" + wanted)
    if item["kind"] == "initializer_note":
        return bool(entry["kind"] == "initializer" and at(error[1], source) and error[2] == item["line"]
                    and re.fullmatch(r"initialization of ['‘][^’']+['’] from incompatible pointer type "
                                     r"['‘][^’']+['’] \[-Werror=incompatible-pointer-types\]", error[4])
                    and parts[1] == (f"{error[1]}:{error[2]}:{error[3]}: note: (near initialization for "
                                     f"‘{entry['object']}.{api}’)")
                    and entry["text"][int(error[3]) - 1:].startswith(entry["value"]))
    if item["kind"] == "preprocessor_macro":
        use = re.match(rf"\s*#\s*(?:if|elif)\s+({re.escape(entry.get('macro', ''))})\(", entry["text"])
        return bool(entry["kind"] == "preprocessor" and entry.get("macro") and use and at(error[1], source)
                    and error[2] == item["line"] and error[4] == 'missing binary operator before token "("'
                    and int(error[3]) == use.end(1) + 1 and parts[1].endswith("| " + entry["text"]))
    note = re.fullmatch(r"(.+?):(\d+):(\d+): note: in expansion of macro ['‘]([A-Za-z_]\w*)['’]", parts[1])
    definition = entry.get("definition", {})
    return bool(entry["kind"] == "macro" and note and at(note[1], source) and note[2] == item["line"]
                and note[4] == api and at(error[1], definition.get("path", "")) and error[2] == str(definition.get("line"))
                and error[4].endswith("[-Werror=overflow]")
                and re.match(rf"{re.escape(api)}(?![A-Za-z0-9_])", entry["text"][int(note[3]) - 1:]))


def compile_profile_matches(meta: dict, report: dict) -> bool:
    observed = report.get("compile_diagnostics")
    if not isinstance(observed, list) or not observed:
        return False
    incomplete = set()
    lines = []
    for item in observed:
        if not isinstance(item, dict):
            return False
        source, api, diagnostic = item.get("source"), item.get("api"), item.get("diagnostic")
        if (source not in meta["compile_expectations"] or api not in meta["compile_expectations"][source]
                or not isinstance(diagnostic, str)):
            return False
        if "kind" not in item:
            if any(text in diagnostic for text in (": error: storage size of ",
                    ": error: array type has incomplete element type ", "[-Werror=unused-variable]")):
                return False
            if not compile_line_matches(source, api, diagnostic):
                return False
        elif item["kind"] in ("initializer_note", "macro_note", "preprocessor_macro"):
            if not compile_note_matches(meta, source, api, item):
                return False
        else:
            error = re.fullmatch(r"(.+?):(\d+)(?::\d+)?:\s*(?:fatal )?error:\s*(.*)", diagnostic)
            declaration = meta.get("compile_declarations", {}).get(source, {}).get(item.get("line"))
            if (not error or not declaration or declaration["api"] != api
                    or item.get("declaration") != declaration["text"] or error[2] != item.get("line")
                    or not (error[1] == source or error[1].endswith("/" + source))):
                return False
            variable = declaration["variable"]
            key = source, error[2], variable
            if item["kind"] == "incomplete_type":
                if not re.fullmatch(rf"storage size of ['‘]{re.escape(variable)}['’] isn['’]t known", error[3]):
                    return False
                incomplete.add(key)
            elif item["kind"] == "incomplete_array_type":
                if not declaration.get("array") or not re.fullmatch(
                        rf"array type has incomplete element type ['‘]struct {re.escape(api)}['’]", error[3]):
                    return False
                incomplete.add(key)
            elif item["kind"] == "unused_after_incomplete_type":
                if key not in incomplete or not re.fullmatch(
                        rf"(?:unused variable ['‘]{re.escape(variable)}['’]|['‘]{re.escape(variable)}['’] defined but not used) \[-Werror=unused-variable\]", error[3]):
                    return False
            else:
                return False
        lines.append(diagnostic)
    return report.get("diagnostic") == "\n".join(lines)


def verify_structured_results(meta: dict, report: dict, phase: str) -> tuple[bool, str]:
    mode = meta.get("failure_mode", "runtime") if meta["project"] == "mynewt" else "runtime"
    if phase == "baseline":
        expected_fail = set()
        expected_pass = set(meta["baseline_tests"])
        expected_exit_code = 0
    else:
        expected_fail = set(meta["fail_to_pass"]) if phase == "before" else set()
        expected_pass = set(meta["pass_to_pass"])
        expected_exit_code = 1 if phase == "before" else 0
        if phase == "after":
            expected_pass |= set(meta["fail_to_pass"])
    actual_pass = set(report.get("passed", []))
    actual_fail = set(report.get("failed", []))
    inventory_valid = (
        report.get("build_ok") is True
        and actual_pass == expected_pass
        and actual_fail == expected_fail
        and not report.get("missing")
        and not report.get("unexpected")
        and not report.get("error")
        and report.get("exit_code") == expected_exit_code
    )
    if phase == "baseline":
        valid = (inventory_valid and report.get("failure_mode", "runtime") == "runtime"
                 and report.get("compile_failure", False) is False
                 and not report.get("diagnostic"))
    elif mode in ("config", "dependency") and phase == "before":
        diagnostic = report.get("diagnostic")
        valid = (report.get("failure_mode") == mode
                 and report.get("build_ok") is False and report.get(mode + "_failure") is True
                 and report.get("compile_failure", False) is False
                 and isinstance(diagnostic, str)
                 and hashlib.sha256(diagnostic.encode()).hexdigest() == meta[mode + "_output_sha256"]
                 and report.get(mode + "_output_sha256") == meta[mode + "_output_sha256"]
                 and not actual_pass and not actual_fail and not report.get("error")
                 and not report.get("missing") and not report.get("unexpected")
                 and report.get("exit_code") == 1)
    elif mode == "compile" and phase == "before":
        source = meta["compile_test_source"]
        api = meta["missing_api"]
        diagnostic = report.get("diagnostic")
        matched_diagnostic = (compile_profile_matches(meta, report) if meta.get("compile_expectations")
                              else compile_line_matches(source, api, diagnostic))
        valid = (
            report.get("failure_mode") == "compile"
            and report.get("build_ok") is False
            and report.get("compile_failure") is True
            and report.get("compile_test_source") == source
            and report.get("missing_api") == api
            and matched_diagnostic
            and not actual_pass and not actual_fail
            and not report.get("missing") and not report.get("unexpected")
            and not report.get("error")
            and report.get("exit_code") == 1
        )
    elif mode == "compile":
        valid = (inventory_valid and report.get("failure_mode") == "compile"
                 and report.get("compile_failure") is False
                 and not report.get("diagnostic"))
    else:
        valid = inventory_valid
    detail = (
        f"expected pass={sorted(expected_pass)}, fail={sorted(expected_fail)}; "
        f"got pass={sorted(actual_pass)}, fail={sorted(actual_fail)}, "
        f"missing={report.get('missing', [])}, unexpected={report.get('unexpected', [])}, "
        f"error={report.get('error')}, exit_code={report.get('exit_code')} "
        f"(expected {expected_exit_code})"
    )
    return valid, detail

def baseline_proof(meta: dict, git) -> tuple[str | None, dict | None, str]:
    """(kind, proof, why) for a selected Mynewt test package that is new in the
    test patch, so `run_tests --baseline` cannot use meta["test_path"].
    git(*args) runs git in the benchmark checkout and returns (returncode, stdout).

    kind "not_applicable": baseline_tests == [], pkg.yml is *added* (not renamed)
    by the test patch, and the closest existing ancestor package at the base is
    not a unittest (so no old suite was split out of it).
    kind "relocated": baseline_tests is nonempty and meta["baseline_test_path"]
    names an existing pkg.type unittest at the base and the commit before the test
    patch, which the new package was renamed from (R) or split from (the closest
    existing ancestor package of an added package).
    (None, None, why) otherwise, and always when the selected package already exists
    at the baseline: the caller then runs the strict baseline unchanged. Every git
    call must exit 0; an unreadable object or bad ref is never taken as absence."""
    if meta.get("project") != "mynewt":
        return None, None, "not a Mynewt instance"
    declared = meta.get("baseline_tests")
    old = meta.get("baseline_test_path")
    if declared == [] and old is None:
        kind = "not_applicable"
    elif isinstance(declared, list) and declared and isinstance(old, str):
        kind = "relocated"
    else:
        return None, None, "no baseline_test_path for a nonempty baseline_tests, or one with none"
    base = meta.get("base_commit", "")
    path = str(meta.get("test_path", "")).strip("/")
    old = old.strip("/") if old else None
    if not re.fullmatch(r"[0-9a-f]{40}", base) or not path or old == path:
        return None, None, "no full base_commit or test_path, or baseline_test_path is test_path"
    pkg = f"{path}/pkg.yml"

    def run(*args):
        rc, out = git(*args)
        if rc != 0:
            raise ValueError(f"git {' '.join(args)[:80]} exited {rc}")
        return out.strip()

    def entry(commit: str, package: str) -> str:
        return run("ls-tree", commit, "--", package)

    def is_unittest(commit: str, package: str) -> bool:
        return re.search(r"(?m)^pkg\.type:\s*[\"']?unittest[\"']?\s*(#.*)?$",
                         run("show", f"{commit}:{package}")) is not None

    try:
        run("cat-file", "-e", f"{base}^{{commit}}")
        snapshot = run("rev-parse", "HEAD^{commit}")
        parent = run("rev-parse", "HEAD~1^{commit}")
        proof = {"kind": kind, "base_commit": base, "baseline_commit": parent,
                 "snapshot_commit": snapshot, "upstream_test_commit": meta.get("test_commit"),
                 "arm_port_commit": None, "package": pkg}
        if parent != base:
            # The aarch64 recipe commits a simulator port between the base and the
            # snapshot commit; it must touch neither package.
            if run("rev-parse", "HEAD~2^{commit}") != base:
                return None, None, "HEAD^ is neither the recorded base nor its simulator port"
            if run("diff", "--name-only", base, parent, "--", path, *([old] if old else [])):
                return None, None, "the commit before the test patch changes a test package"
            proof["arm_port_commit"] = parent
        proof["tree_entries"] = {"selected_at_base": entry(base, pkg),
                                 "selected_at_baseline": entry(parent, pkg)}
        if proof["tree_entries"]["selected_at_base"] or proof["tree_entries"]["selected_at_baseline"]:
            return None, None, f"{pkg} exists at the baseline"
        proof["tree_entries"]["selected_at_snapshot"] = entry("HEAD", pkg)
        if not re.fullmatch(r"100(?:644|755) blob [0-9a-f]{40}\t" + re.escape(pkg),
                            proof["tree_entries"]["selected_at_snapshot"]):
            return None, None, f"{pkg} is not a file at the snapshot commit"
        change = None
        renames = []
        for line in run("diff-tree", "-r", "-M", "--name-status", "--no-commit-id",
                        parent, "HEAD").splitlines():
            cols = line.split("\t")
            if cols[0].startswith("R"):
                renames.append(cols)
            if cols[-1] == pkg and (cols[0] == "A" or cols[0].startswith("R")):
                change = cols
        if change is None:
            return None, None, f"the test patch does not add {pkg}"
        proof["change_status"] = change[0]
        if change[0] != "A":
            proof["renamed_from"] = change[1]
        # The closest existing package above the selected one at the base.
        ancestor = None
        for parent_dir in Path(path).parents:
            if str(parent_dir) != "." and entry(base, f"{parent_dir}/pkg.yml"):
                ancestor = str(parent_dir)
                break
        proof["closest_ancestor_package"] = ancestor
        if kind == "not_applicable":
            if change[0] != "A":
                return None, None, f"{pkg} was renamed from {change[1]}: its old tests are the baseline"
            if ancestor and is_unittest(base, f"{ancestor}/pkg.yml"):
                return None, None, f"{pkg} is split from the unittest package {ancestor}"
            for moved in renames:
                if (moved[1].endswith("/pkg.yml")
                        and Path(moved[1]).parent.parent == Path(path).parent
                        and Path(moved[-1]).parent.parent == Path(path).parent
                        and is_unittest(base, moved[1])):
                    return None, None, "a sibling unittest was renamed; declare its baseline path and tests"
            return kind, proof, ""
        old_pkg = f"{old}/pkg.yml"
        proof["baseline_test_path"] = old
        proof["tree_entries"]["baseline_package_at_base"] = entry(base, old_pkg)
        proof["tree_entries"]["baseline_package_at_baseline"] = entry(parent, old_pkg)
        for where, commit in (("base", base), ("baseline", parent)):
            if not re.fullmatch(r"100(?:644|755) blob [0-9a-f]{40}\t" + re.escape(old_pkg),
                                proof["tree_entries"][f"baseline_package_at_{where}"]):
                return None, None, f"{old_pkg} is not a file at the {where} commit"
            if not is_unittest(commit, old_pkg):
                return None, None, f"{old_pkg} is not pkg.type unittest at the {where} commit"
        if change[0] != "A":
            if change[1] != old_pkg:
                return None, None, f"{pkg} was renamed from {change[1]}, not {old_pkg}"
            proof["relation"] = "renamed"
        elif ancestor == old:
            proof["relation"] = "split from the closest existing ancestor package"
        elif any(cols[1] == old_pkg and Path(cols[-1]).parent.parent == Path(path).parent
                 and Path(old).parent == Path(path).parent for cols in renames):
            # A test backend can split into sibling packages while the original
            # package is renamed. The original suite must still actually run.
            proof["relation"] = "split alongside a renamed sibling package"
            proof["sibling_renames"] = [cols for cols in renames if cols[1] == old_pkg]
        else:
            return None, None, f"{old} is not the closest existing ancestor package of {path}"
        return kind, proof, ""
    except ValueError as e:
        return None, None, str(e)

# GitHub lists at most this many files of a pull request.


def gold_patch(meta: dict, workdir: Path) -> str:
    """
    Fetch the upstream fix as a diff, restricted to the files the fix should
    touch so test changes never leak in.

    Zephyr and RIOT are single-repo (base_commit..fix_commit). NuttX is two
    repos and the fix lives in the kernel one, between kernel_base_commit and
    kernel_merge_commit.
    """
    project = meta["project"]
    if project == "nuttx":
        repo, base, fix = meta["kernel_repo"], meta["kernel_base_commit"], meta["kernel_merge_commit"]
    else:
        repo, base, fix = meta["repo"], meta.get("gold_base_commit", meta["base_commit"]), meta["fix_commit"]

    local = au.mynewt_clone() if project == "mynewt" else None
    if local:
        clone = local
        for sha in (base, fix):
            if sh(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode:
                sh(["git", "-C", str(clone), "fetch", "-q", "origin", sha], timeout=900)
    else:
        clone = workdir / "repo"
        print(f"  cloning {repo} (blobless) ...")
        sh(["git", "clone", "--filter=blob:none", "--no-checkout", repo, str(clone)], timeout=900)
        sh(["git", "-C", str(clone), "fetch", "origin", base, fix], timeout=900)

    files = meta.get("files_changed_by_fix", [])
    cmd = ["git", "-C", str(clone), "diff", f"{base}..{fix}", "--"]
    cmd += files if files else [".", ":(exclude)tests/"]
    # Bytes as git wrote them: text mode would turn a CRLF file's \r\n into \n
    # and the patch would no longer apply.
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    out = r.stdout.decode("utf-8", "surrogateescape")
    if r.returncode != 0 or not out.strip():
        raise RuntimeError(f"could not produce a gold patch: {r.stderr.decode(errors='replace').strip()[:200]}")
    return out








def local_fix_patch(meta: dict) -> str:
    """The upstream fix from the local Zephyr clone: every change of the PR
    outside tests/. The same range the test patch comes from, so the two
    never overlap."""
    clone = layout.zephyr_clone()
    base, fix = meta["base_commit"], meta["fix_commit"]
    for sha in (base, fix):
        if sh(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode:
            sh(["git", "-C", str(clone), "fetch", "-q", "origin", sha], timeout=900)
    r = sh(["git", "-C", str(clone), "diff", f"{base}..{fix}", "--", ".", ":(exclude)tests/"],
           timeout=300)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"could not produce the fix patch: {r.stderr.strip()[:200]}")
    return r.stdout


RUN_TESTS_MEANING = {0: "passed", 1: "failed", 2: "twister did not run", 3: "nothing ran"}


def summary(output: str) -> list[str]:
    """The per-test lines and verdict from run_tests' output."""
    keep, on = [], False
    for line in output.splitlines():
        if line.startswith("--- twister:"):
            on = True
        if on or line.startswith("==> run_tests"):
            keep.append(line)
        if line.startswith("--- suite:"):
            on = False
    if len(keep) <= 40:
        return keep
    # Large suites: keep what matters -- every test that did not pass, the
    # assertion lines, and the verdict -- and count the passes.
    passed = [l for l in keep if l.strip().startswith("passed")]
    rest = [l for l in keep if not l.strip().startswith(("passed", "| PASS"))]
    return rest[:1] + [f"  ({len(passed)} tests passed)"] + rest[1:40]


def validate_apptainer(instance_id: str, patch_override: Path | None, timeout: int,
                       verbose: bool) -> bool:
    meta = instances.load_metadata(instance_id)
    image = au.instance_image(instance_id)
    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")
    print(f"  {meta['test_path']}/{meta['scenario']} on {meta['platform']}")
    report = {"instance_id": instance_id, "image": str(image), "platform": meta["platform"],
              "scenario": f"{meta['test_path']}/{meta['scenario']}", "valid": False,
              # what was validated: promote_instance.py refuses a recipe that
              # changed after this point
              "recipe_sha256": au.recipe_fingerprint(instances.instance_dir(instance_id)),
              "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not image.exists():
        print(f"  ERROR: image not built: {image}")
        report["reason"] = "image not built"
        return _finish(instance_id, report)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = au.path("runs", f"{instance_id}-{stamp}", "x").parent
    overlay = run_dir / "overlay"
    overlay.mkdir(parents=True)
    logdir = au.path("logs", instance_id, f"validate-{stamp}", "x").parent
    logdir.mkdir(parents=True, exist_ok=True)
    report["logs"] = str(logdir)
    container = au.exec_args(image, binds={run_dir: "/embedeval-run"}, overlay=overlay)

    def step(label: str, cmd: list[str], log: str, t: int):
        print(f"    {label} ...", end="", flush=True)
        t0 = time.time()
        try:
            r = subprocess.run(container + cmd, capture_output=True, text=True, timeout=t)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired as e:
            rc, out = "timeout", str(e.stdout or "")
        (logdir / log).write_text(out)
        secs = time.time() - t0
        print(f" {secs:.0f}s (rc={rc})")
        if verbose:
            print(out)
        return rc, out, secs

    try:
        print("\n  Step 1: expecting run_tests to FAIL on the image as built")
        rc1, out1, s1 = step("run_tests", ["run_tests"], "before.log", timeout)
        report["before"] = {"rc": rc1, "meaning": RUN_TESTS_MEANING.get(rc1, str(rc1)),
                            "seconds": round(s1), "summary": summary(out1)}
        for line in summary(out1):
            print(f"      {line}")
        if rc1 != 1:
            why = {0: "tests PASSED before the fix -- the instance does not detect the bug",
                   3: "nothing ran: the scenario is filtered or skipped on this board",
                   2: "twister did not run (environment problem)"}.get(
                       rc1, f"run_tests ended with {rc1}")
            print(f"  INVALID: {why}")
            report["reason"] = why
            return _finish(instance_id, report)

        print("\n  Step 2: applying the upstream fix")
        patch_text = (Path(patch_override).read_text() if patch_override
                      else local_fix_patch(meta))
        (run_dir / "fix.diff").write_text(patch_text)
        rca, outa, _ = step("git apply", ["git", "-C", "/testbed", "apply",
                                          "/embedeval-run/fix.diff"], "apply.log", 300)
        if rca != 0:
            print(f"  INVALID: the fix does not apply: {outa.strip()[:300]}")
            report["reason"] = "the fix does not apply"
            return _finish(instance_id, report)

        print("\n  Step 3: expecting run_tests to PASS with the fix")
        rc2, out2, s2 = step("run_tests", ["run_tests"], "after.log", timeout)
        report["after"] = {"rc": rc2, "meaning": RUN_TESTS_MEANING.get(rc2, str(rc2)),
                           "seconds": round(s2), "summary": summary(out2)}
        for line in summary(out2):
            print(f"      {line}")
        if rc2 != 0:
            why = {1: "tests still fail with the fix",
                   3: "nothing ran with the fix",
                   2: "twister did not run (environment problem)"}.get(
                       rc2, f"run_tests ended with {rc2}")
            print(f"  INVALID: {why}")
            report["reason"] = why
            return _finish(instance_id, report)

        print("\n  VALID: fails before the fix, passes after")
        report["valid"] = True
        report["reason"] = "fails before the fix, passes after"
        return _finish(instance_id, report)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        report["reason"] = f"error: {type(e).__name__}: {e}"
        return _finish(instance_id, report)
    finally:
        au.remove(run_dir)


def _finish(instance_id: str, report: dict) -> bool:
    (instances.instance_dir(instance_id) / "validation.json").write_text(
        json.dumps(report, indent=2) + "\n")
    return report["valid"]


def error_lines(output: str) -> str:
    """The lines of a failed build that look like errors, indented. A parallel
    build's last lines are usually other files still compiling."""
    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output)
    lines = [line.strip() for line in re.split(r"[\r\n]+", out)
             if re.search(r"error:|undefined reference|Illegal instruction|\*\*\*",
                          line, re.IGNORECASE)]
    text = "\n".join(lines[-15:]) if lines else out[-400:]
    return "  " + text.strip().replace("\n", "\n  ")


def compile_diagnostic(meta: dict, output: str):
    """A NuttX compile_error instance's expected diagnostic in a build's
    output, or None.

    Covers both a compiler diagnostic (missing symbol in the test source) and
    a linker diagnostic (missing symbol at link time, e.g. a test calling a
    kernel API the unfixed kernel doesn't implement yet) -- same bucket: the
    unfixed tree can't build.
    """
    source, missing_api = meta.get("compile_test_source"), meta.get("missing_api")
    if not source or not missing_api:
        return None
    prefix = (rf"(?:^|[\s/]){re.escape(Path(source).name)}:\d+(?::\d+)?:\s*"
              r"(?:\([^\n)]*\):\s*)?")
    diagnostic = re.search(
        prefix + rf"(?:(?:fatal )?error:|undefined reference to)[^\n]*{re.escape(missing_api)}",
        output,
    )
    if diagnostic:
        return diagnostic
    # A newly added header can be absent altogether before the fix, so the
    # compiler names that header instead of any API it declares.
    header = re.search(prefix + r"fatal error:\s*([^\s:]+): No such file or directory", output)
    if header and f"include/{header.group(1)}" in meta.get("files_changed_by_fix", []):
        return header
    return None


def test_build_error(meta: dict, output: str):
    """A compile or link error located in one of the changed test sources, or None."""
    for path in meta.get("files_changed_by_test_patch", []):
        if path.endswith(".c"):
            diagnostic = re.search(
                rf"(?:^|[\s/]){re.escape(Path(path).name)}:\d+(?::\d+)?:\s*(?:\([^\n)]*\):\s*)?"
                r"(?:(?:fatal )?error:|undefined reference to)[^\n]*", output)
            if diagnostic:
                return diagnostic
    return None


NUTTX_RUN_TESTS_MEANING = {0: "passed", 1: "failed", 2: "timed out"}








NO_ROUTE = ("python3 -c \"import socket; socket.create_connection(('140.82.112.3', 443), 5)\" "
            "2>/dev/null && echo NETWORK-REACHABLE; "
            "timeout 20 git -C /testbed ls-remote origin >/dev/null 2>&1 && echo ORIGIN-REACHABLE; true")


def validate_mynewt_apptainer(instance_id: str, patch_override: Path | None, timeout: int,
                              verbose: bool, determinism: int = 0) -> bool:
    """A Mynewt instance in its Apptainer image (metadata "runtime": "apptainer")."""
    meta = instances.load_metadata(instance_id)
    au.require_host_arch(meta)
    image = au.instance_image(instance_id)
    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")
    idir = instances.instance_dir(instance_id)
    report = {"instance_id": instance_id, "image": str(image), "host": platform.machine(),
              "failure_mode": meta.get("failure_mode", "runtime"), "valid": False,
              "acceptance_policy": "attributable-fail-before-pass-after-v1",
              "recipe_sha256": au.recipe_fingerprint(idir, au.mynewt_recipe_files(idir)),
              "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not image.exists():
        print(f"  ERROR: image not built: {image}")
        report["reason"] = "image not built"
        return _finish(instance_id, report)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = au.path("runs", f"{instance_id}-{stamp}", "x").parent
    logdir = au.path("logs", instance_id, f"validate-{stamp}", "x").parent
    logdir.mkdir(parents=True, exist_ok=True)
    report["logs"] = str(logdir)
    sandbox = None

    def step(label: str, cmd: str, log: str, t: int):
        """Run cmd in the sandbox; returns (rc, output, structured result)."""
        print(f"    {label} ...", end="", flush=True)
        t0 = time.time()
        full = (f"cd /testbed && rm -f /tmp/mynewt-result.json && {cmd}; "
                "rc=$?; echo ==MYNEWT-RESULT==; "
                "cat /tmp/mynewt-result.json 2>/dev/null; exit $rc")
        try:
            r = subprocess.run(isolated_command(au.sandbox_exec_args(sandbox, binds={run_dir: "/embedeval-run"})
                                                + ["bash", "-c", full], meta.get("network_fixture")),
                               capture_output=True, text=True, timeout=t)
            rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc = "timeout"
        (logdir / log).write_text(out)
        result = None
        if "==MYNEWT-RESULT==" in out:
            try:
                result = json.loads(out.split("==MYNEWT-RESULT==", 1)[1])
            except ValueError:
                pass
        print(f" {time.time() - t0:.0f}s (rc={rc})")
        if verbose:
            print(out)
        return rc, out, result

    def invalid(why: str, detail: str = "") -> bool:
        print(f"  INVALID: {why}")
        if detail:
            print(detail)
        report["reason"] = why
        return _finish(instance_id, report)

    def judge(phase: str, rc, result, want_rc: int) -> str | None:
        """None if the phase matches expectations, else why not."""
        if rc == "timeout":
            return f"{phase}: run_tests timed out"
        if result is None:
            return f"{phase}: no structured result (rc={rc})"
        ok, detail = verify_structured_results(meta, result, phase)
        report[phase] = {"rc": rc, "passed": result.get("passed", []),
                         "failed": result.get("failed", []), "error": result.get("error"),
                         "missing": result.get("missing", []),
                         "unexpected": result.get("unexpected", [])}
        if meta.get("failure_mode") in ("config", "dependency"):
            mode = meta["failure_mode"]
            report[phase].update({mode + "_failure": result.get(mode + "_failure", False),
                                  mode + "_output_sha256": result.get(mode + "_output_sha256")})
        if not ok or rc != want_rc:
            return f"{phase}: {detail}"
        return None

    try:
        t0 = time.time()
        sandbox = au.unpack_sandbox(image)
        print(f"    unpacked sandbox in {time.time() - t0:.0f}s")
        if meta["project"] != "mynewt":
            return invalid("not a Mynewt instance")

        def sandbox_git(*args):
            r = subprocess.run(isolated_command(au.sandbox_exec_args(sandbox) + ["git", "-C", "/testbed", *args],
                                                meta.get("network_fixture")),
                               capture_output=True, text=True, timeout=120)
            return r.returncode, r.stdout

        print("\n  Leak check: the image as the agent gets it")
        gold = gold_patch(meta, run_dir)
        patch = patch_override.read_text() if patch_override else gold
        facts = mynewt_leakcheck.fix_facts(au.mynewt_clone(), meta, gold,
                                           (idir / "test_patch.diff").read_text())
        report["leak_check"] = leak = mynewt_leakcheck.scan(sandbox, facts)
        r = subprocess.run(isolated_command(au.sandbox_exec_args(sandbox) + ["bash", "-c", NO_ROUTE],
                                            meta.get("network_fixture")),
                           capture_output=True, text=True, timeout=120)
        leak["network"] = "reachable" if "REACHABLE" in r.stdout else "isolated"
        if leak["network"] != "isolated" or r.returncode:
            leak["ok"] = False
            leak["findings"].append(f"network: {r.stdout.strip() or r.stderr.strip()[-200:]}")
        print(f"    {'clean' if leak['ok'] else 'FOUND: ' + '; '.join(leak['findings'])} "
              f"({leak['commits']} commits, {leak['fix_blobs_checked']} fixed blobs, "
              f"{leak['fix_lines_checked']} fix lines checked; network {leak['network']})")
        if not leak["ok"]:
            return invalid("leak: " + "; ".join(leak["findings"]))

        kind, proof, not_proven = baseline_proof(meta, sandbox_git)
        if meta.get("baseline_test_path") is not None and kind != "relocated":
            return invalid(f"baseline_test_path is not proven: {not_proven}")
        if kind == "not_applicable":
            print("\n  Baseline: not applicable, baseline_tests is [] and "
                  f"{proof['package']} is added by the test patch")
            report["baseline"] = {
                "status": "not_applicable", "tests_run": False, "passed": [], "failed": [],
                "reason": "the selected test package is added by the test patch; "
                          "it does not exist at the baseline commit",
                "proof": proof}
            (logdir / "baseline.log").write_text(
                "baseline NOT APPLICABLE: no baseline tests were run.\n"
                + json.dumps(proof, indent=2) + "\n")
        else:
            print("\n  Baseline: recording original-test health (diagnostic only)")
            old = ""
            if kind == "relocated":
                print(f"    (at {proof['baseline_test_path']}, the package "
                      f"{proof['package']} was {proof['relation']})")
                old = " --baseline-test-path " + shlex.quote(proof["baseline_test_path"])
            snapshot_rc, snapshot = sandbox_git("rev-parse", "HEAD")
            if snapshot_rc != 0:
                return invalid("cannot identify the test-patched snapshot")
            rc, out, result = step(
                "run_tests --baseline",
                "ORIG=$(git rev-parse HEAD) && git checkout -q --detach HEAD^ && "
                f"{{ run_tests --baseline{old}; r=$?; git checkout -q --detach $ORIG; (exit $r); }}",
                "baseline.log", timeout + 300)
            # A timeout can kill the shell before its checkout-back command.
            # Restore independently before evaluating the PR's tests.
            restore_rc, _ = sandbox_git("checkout", "-q", "--detach", snapshot.strip())
            if restore_rc != 0:
                return invalid("cannot restore the test-patched snapshot after baseline")
            why = judge("baseline", rc, result, 0)
            report.setdefault("baseline", {"rc": rc, "tests_run": True})
            report["baseline"].update({"diagnostic_only": True,
                                       "status": "failed" if why else "passed",
                                       "reason": why})
            if kind == "relocated" and "baseline" in report:
                report["baseline"]["relocated_proof"] = proof
            if why:
                print(f"  Baseline diagnostic: {why}; continuing to before/after")

        print("\n  Step 1: expecting exactly fail_to_pass to FAIL at the base commit")
        rc, out, result = step("run_tests", "run_tests", "before.log", timeout + 300)
        why = judge("before", rc, result, 1)
        if why:
            return invalid(why, error_lines(out))

        if determinism > 1:
            # Every oracle: a compile or dependency failure can depend on build
            # order (which file a parallel build stops at), a runtime one on
            # timing or memory layout. Each repeat is judged as step 1 was.
            print(f"\n  Determinism: the before state {determinism - 1} more times")
            tally = {n: {} for n in meta["fail_to_pass"] + meta["pass_to_pass"]}
            first = dict(report["before"])
            failures = []
            for k in range(1, determinism + 1):
                if k > 1:
                    rc, out, result = step("run_tests", "run_tests", f"before-{k}.log", timeout + 300)
                    why = judge("before", rc, result, 1)
                    if why:
                        failures.append(f"run {k}: {why[:300]}")
                passed, failed = set((result or {}).get("passed", [])), set((result or {}).get("failed", []))
                for n in tally:
                    st = "passed" if n in passed else "failed" if n in failed else "not_run"
                    tally[n][st] = tally[n].get(st, 0) + 1
            report["before"] = first
            mode = meta.get("failure_mode", "runtime")
            flaky = [n for n in meta["fail_to_pass"] if mode == "runtime" and tally[n].get("failed", 0) != determinism]
            flaky += [n for n in meta["pass_to_pass"] if mode == "runtime" and tally[n].get("passed", 0) != determinism]
            report["determinism"] = {"runs": determinism, "ok": not flaky and not failures, "flaky": flaky,
                                     "failures": failures, "tally": {n: t for n, t in tally.items() if n in flaky}}
            if flaky or failures:
                return invalid(f"flaky before state: {(flaky[:5] or failures[:1])} in {determinism} runs")
            print(f"    deterministic: {determinism}/{determinism}")

        print("\n  Step 2: applying the upstream fix, expecting everything to PASS")
        (run_dir / "fix.diff").write_bytes(patch.encode("utf-8", "surrogateescape"))
        three_way = " --3way" if meta.get("gold_patch_three_way") else ""
        rc, out, _ = step("git apply", f"git apply{three_way} /embedeval-run/fix.diff",
                          "apply.log", 120)
        if rc != 0:
            return invalid("the fix does not apply", "  " + out.strip()[:300])
        rc, out, result = step("run_tests", "run_tests", "after.log", timeout + 300)
        why = judge("after", rc, result, 0)
        if why:
            return invalid(why, error_lines(out))

        print("\n  VALID: fails before the fix, passes after")
        report["valid"] = True
        report["reason"] = "fails before the fix, passes after" + (
            " (baseline not applicable: the package is added by the test patch)"
            if kind == "not_applicable" else "")
        return _finish(instance_id, report)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        report["reason"] = f"error: {type(e).__name__}: {e}"
        return _finish(instance_id, report)
    finally:
        if sandbox:
            au.remove_sandbox(sandbox)
        au.remove(run_dir)


def validate(instance_id: str, patch_override: Path | None = None, timeout: int = 600,
             verbose: bool = False, determinism: int = 0) -> bool:
    meta = instances.load_metadata(instance_id)
    if meta.get("pipeline") == "twister":
        return validate_apptainer(instance_id, patch_override, max(timeout, 1800), verbose)
    if meta["project"] == "mynewt" and meta.get("runtime") == "apptainer":
        return validate_mynewt_apptainer(instance_id, patch_override, timeout, verbose, determinism)
    if meta["project"] == "zephyr":
        print(f"  SKIP: {instance_id} is a Docker Zephyr instance, kept as reference "
              f"metadata only; Zephyr instances are validated from the twister pipeline "
              f"(EMBEDEVAL_INSTANCES_DIR=generated).")
        return False
    cfg = build_config.config(meta["project"])
    image = meta["docker_image"]
    build_cmd = meta.get("build_command")
    if not build_cmd:
        print(f"  ERROR: {instance_id} has no build_command")
        return False

    print(f"\n{'=' * 70}\n{instance_id}  ({image})\n{'=' * 70}")

    run_args = ["docker", "run", "-d", "--rm"]
    if cfg["platform"]:
        run_args += ["--platform", cfg["platform"]]
    run_args += [image, "sleep", "1800"]

    r = sh(run_args, timeout=120)
    if r.returncode != 0:
        print(f"  ERROR: docker run failed: {r.stderr.strip()[:200]}")
        return False
    cid = r.stdout.strip()
    workdir = Path(tempfile.mkdtemp())

    step_log = "/tmp/embedeval_step.log"

    def dexec(cmd: str, t: int, label: str | None = None, capture: bool = False):
        # With --verbose the container output streams live; otherwise it is
        # captured and only shown when a step fails. Either way each step
        # announces itself and reports how long it took, so a step that is
        # silently skipped or suspiciously fast is visible.
        if label:
            print(f"    {label} ...", end="", flush=True)
        t0 = time.time()
        stream = verbose and not capture
        if stream:
            # Streamed output isn't captured, so keep a copy in the container
            # for step_output() to read back if this step fails.
            cmd = f"set -o pipefail; ( {cmd} ) 2>&1 | tee {step_log}"
        r = sh(["docker", "exec", cid, "bash", "-c", cmd], timeout=t,
               quiet=not stream)
        if capture and verbose:
            print()
            if r.stdout:
                print(r.stdout, end="")
            if r.stderr:
                print(r.stderr, end="", file=sys.stderr)
        if label:
            print(f" {time.time() - t0:.1f}s (rc={r.returncode})")
        return r

    def structured_results_match(phase: str, container_id: str = cid) -> bool:
        result_path = meta.get("structured_test_results")
        if not result_path:
            return True
        result = sh(["docker", "exec", container_id, "cat", result_path], timeout=30)
        if result.returncode != 0:
            print(f"  FAIL: could not read structured test results at {result_path}")
            return False
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"  FAIL: invalid structured test results at {result_path}: {e}")
            return False
        valid, detail = verify_structured_results(meta, report, phase)
        if not valid:
            print(f"  FAIL: {phase} structured test results do not match: {detail}")
            return False
        print(f"  ok: {phase} structured test results match the expected cases")
        return True

    def step_output(r) -> str:
        """Full output of the step that produced r; call before the next dexec."""
        out = (r.stdout or "") + (r.stderr or "")
        if not out and verbose:
            out = sh(["docker", "exec", cid, "cat", step_log], timeout=60).stdout or ""
        return out

    def build_errors(r) -> str:
        return error_lines(step_output(r))

    try:
        if meta["project"] == "mynewt":
            print("\n  Baseline: recording original-test health (diagnostic only)")
            baseline_run = sh(run_args, timeout=120)
            if baseline_run.returncode != 0:
                print(f"  Baseline unavailable: {baseline_run.stderr.strip()[:200]}")
            else:
                baseline_cid = baseline_run.stdout.strip()
                try:
                    checkout = sh(["docker", "exec", baseline_cid, "bash", "-c",
                                   "cd /testbed && git checkout --detach HEAD^"], timeout=120,
                                  quiet=not verbose)
                    if checkout.returncode != 0:
                        print(f"  Baseline checkout failed (rc={checkout.returncode}); "
                              "continuing in the separate before/after container")
                    else:
                        baseline = sh(["docker", "exec", baseline_cid, "bash", "-c",
                                       "cd /testbed && run_tests --baseline"], timeout=timeout,
                                      quiet=not verbose)
                        matched = structured_results_match("baseline", baseline_cid)
                        print("  Baseline passed" if matched and baseline.returncode == 0
                              else "  Baseline failed; continuing to before/after")
                finally:
                    sh(["docker", "rm", "-f", baseline_cid], timeout=60)

        # --- Step 1: tests must FAIL on the unfixed code -------------------
        print("\n  Step 1: expecting tests to FAIL at the base commit")
        if meta["project"] == "mynewt":
            dexec(f"cd /testbed && {build_cmd}", 900, "build")
            r1 = dexec("cd /testbed && run_tests", timeout, "run_tests")
            if not structured_results_match("before"):
                return False
            if r1.returncode != 1:
                print(f"  FAIL: expected run_tests exit 1 before the fix, got "
                      f"{r1.returncode} -- the regression did not fail cleanly")
                return False
            print(f"  ok: run_tests exited {r1.returncode} as expected")
        else:
            dexec(f"cd /testbed && {build_cmd}", 900, "build")
            r1 = dexec("cd /testbed && run_tests", timeout, "run_tests")
            if r1.returncode == 0:
                print("  FAIL: tests PASSED before the fix -- the instance does not "
                      "isolate the bug (test patch may not apply, or the bug is absent)")
                return False
            print(f"  ok: run_tests exited {r1.returncode} as expected")

        # --- Step 2: tests must PASS with the fix applied ------------------
        print("\n  Step 2: applying the fix, expecting tests to PASS")
        if patch_override:
            patch_text = Path(patch_override).read_text()
            print(f"  using supplied patch {patch_override}")
        else:
            patch_text = gold_patch(meta, workdir)
        pf = workdir / "fix.diff"
        pf.write_text(patch_text)

        sh(["docker", "cp", str(pf), f"{cid}:/tmp/fix.diff"], timeout=60)
        three_way = " --3way" if meta.get("gold_patch_three_way") else ""
        ra = dexec(f"cd /testbed && git apply{three_way} /tmp/fix.diff", 120, "git apply")
        if ra.returncode != 0:
            print(f"  FAIL: git apply failed: {step_output(ra).strip()[:300]}")
            return False

        cleaned = cfg["clean_paths"]
        if cleaned:
            dexec("rm -rf " + " ".join(cleaned), 60, f"clean {' '.join(cleaned)}")
        else:
            print(f"    clean: nothing to remove for {meta['project']}")
        rb = dexec(f"cd /testbed && {build_cmd}", 900, "rebuild")
        if rb.returncode != 0:
            print(f"  FAIL: build failed after applying the fix (rc={rb.returncode})")
            print(build_errors(rb))
            return False

        r2 = dexec("cd /testbed && run_tests", timeout, "run_tests")
        if not structured_results_match("after"):
            return False
        if r2.returncode != 0:
            print(f"  FAIL: tests still failing after the fix (rc={r2.returncode})")
            print("  " + step_output(r2)[-400:].replace("\n", "\n  "))
            return False

        print("\n  VALID: fails before the fix, passes after")
        return True

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return False
    finally:
        sh(["docker", "rm", "-f", cid], timeout=60)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Validate instance fail-then-pass behaviour.")
    p.add_argument("instance", nargs="*", help="instance id(s)")
    p.add_argument("--repo", choices=instances.PROJECTS, help="every instance for one project")
    p.add_argument("--all", action="store_true")
    p.add_argument("--patch", help="validate this patch instead of the upstream fix")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--verbose", "-v", action="store_true",
                   help="stream build and test output instead of capturing it")
    p.add_argument("--determinism", type=int, default=0, metavar="N",
                   help="Mynewt instances: run the before state N times in all and "
                        "require the same judgement every time")
    args = p.parse_args()

    if args.all:
        targets = instances.discover_instances()
    elif args.repo:
        targets = instances.discover_instances(args.repo)
    elif args.instance:
        targets = args.instance
    else:
        p.error("give an instance id, or --repo, or --all")

    if args.patch and len(targets) != 1:
        p.error("--patch applies to exactly one instance")

    start = time.time()
    results = {t: validate(t, Path(args.patch) if args.patch else None, args.timeout,
                           args.verbose, args.determinism)
               for t in targets}

    print(f"\n{'=' * 70}\nSUMMARY  ({(time.time() - start) / 60:.1f} min)\n{'=' * 70}")
    for t, ok in results.items():
        print(f"  [{'VALID' if ok else 'FAIL '}] {t}")
    ok = sum(results.values())
    print(f"\n  {ok}/{len(results)} valid")
    if ok != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
