"""Build and validate the exported instances without an EmbedEval checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent


def run(args, timeout=1200):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def checked(args):
    result = run(args)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def parse_tests(output, project):
    tests = {}
    if project == "zephyr":
        return {name: status for status, name in re.findall(r"^\s*(PASS|FAIL|SKIP) - (\w+)", output, re.MULTILINE)}
    for status, name in re.findall(r"^(PASS|FAIL): (\w+)$", output, re.MULTILINE):
        tests[name] = status
    for name, status in re.findall(r"^(\w+) (PASS|FAIL)\s*$", output, re.MULTILINE):
        tests[name] = status
    current = None
    for line in output.splitlines():
        match = re.match(r"^(test_\w+) \(", line)
        if match:
            current = match.group(1)
            tests[current] = "PASS"
        if current and re.search(r"\.\.\. (FAIL|ERROR)", line):
            tests[current] = "FAIL"
        match = re.match(r"^(FAIL|ERROR): (test_\w+) \(", line)
        if match:
            tests[match.group(2)] = "FAIL"
    return tests


def build(args, dry_run):
    print(subprocess.list2cmdline([str(a) for a in args]), flush=True)
    if not dry_run:
        subprocess.run(args, check=True)


def validate(case):
    directory = ROOT / case["directory"]
    output = ROOT.parent / "runs" / case["id"]
    output.mkdir(parents=True, exist_ok=True)
    cid = checked(["docker", "run", "-d", "--rm", case["image"], "sleep", "infinity"]).strip()
    result = {"instance": case["id"], "phases": {}, "accepted": False}
    known = set(case["expected_failures"] + case["expected_passes"])
    expected_failures = set(case["expected_failures"])

    def dexec(cmd):
        return checked(["docker", "exec", cid, "bash", "-c", f"cd /testbed && {cmd}"])

    try:
        result["image_id"] = checked(["docker", "image", "inspect", case["image"], "--format", "{{.Id}}"]).strip()
        checked(["docker", "cp", str(directory / "repair.diff"), f"{cid}:/tmp/repair.diff"])
        for phase, patch_command in [
            ("correct", "git apply /tmp/repair.diff"),
            ("synthetic", "git apply -R /tmp/repair.diff"),
            ("restored", "git apply /tmp/repair.diff"),
        ]:
            dexec(patch_command)
            compiled = run(["docker", "exec", cid, "bash", "-c", f"cd /testbed && {case['build_command']}"])
            (output / f"{phase}-build.log").write_text(compiled.stdout + compiled.stderr, encoding="utf-8")
            if compiled.returncode:
                raise RuntimeError(f"{case['id']}: {phase} compilation failed")
            executed = run(["docker", "exec", cid, "bash", "-c", f"cd /testbed && {case['run_command']}"], timeout=600)
            log = executed.stdout + executed.stderr
            (output / f"{phase}-tests.log").write_text(log, encoding="utf-8")
            tests = parse_tests(log, case["project"])
            source = dexec(f"cat {case['production_source']}")
            test_root = "apps/testing/ostest" if case["project"] == "nuttx" and not case.get("component_bundle") else "tests"
            test_material = dexec(
                f"find {test_root} -type f ! -path '{case['production_source']}' ! -name '*.o' ! -name '*.a' ! -name '*.d' "
                "! -name '*.pyc' ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum; "
                "sha256sum /usr/local/bin/run_tests"
            )
            if case["project"] == "nuttx" and not case.get("component_bundle"):
                test_material += dexec("cat /usr/local/bin/run_nuttx.py")
            result["phases"][phase] = {"exit": executed.returncode, "tests": tests,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "test_and_runner_sha256": hashlib.sha256(test_material.encode()).hexdigest()}
            failed = {name for name, status in tests.items() if status != "PASS"}
            print(f"{case['id']} {phase}: {len(tests) - len(failed)} pass, {len(failed)} fail; exit={executed.returncode}", flush=True)
            expected_exit = 1 if phase == "synthetic" else 0
            expected = expected_failures if phase == "synthetic" else set()
            if set(tests) != known or failed != expected or executed.returncode != expected_exit:
                raise RuntimeError(f"{case['id']}: {phase} did not produce the expected per-test outcomes")
        phases = result["phases"]
        if len({p["test_and_runner_sha256"] for p in phases.values()}) != 1:
            raise RuntimeError("Tests or runner changed across phases")
        if phases["correct"]["source_sha256"] != phases["restored"]["source_sha256"]:
            raise RuntimeError("Source restoration mismatch")
        if phases["correct"]["source_sha256"] == phases["synthetic"]["source_sha256"]:
            raise RuntimeError("Synthetic mutation did not change the production source")
        result["accepted"] = True
    finally:
        (output / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        checked(["docker", "rm", "-f", cid])


def main():
    cases = json.loads((ROOT / "manifest.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    select = parser.add_mutually_exclusive_group()
    select.add_argument("--all", action="store_true")
    select.add_argument("--instance", choices=[c["id"] for c in cases])
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--test", action="store_true", help="Run buggy image directly; its intended failure exits 1")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.list or not (args.all or args.instance):
        for case in cases:
            print(f"{case['id']} ({case['method']}): {case['parent_pr']}")
        if not (args.all or args.instance):
            return
    selected = cases if args.all else [c for c in cases if c["id"] == args.instance]
    built = set()
    for case in selected:
        if args.build:
            project = "contiki-ng" if case.get("component_bundle") else case["project"]
            if project not in built:
                build(["docker", "build", "-f", str(ROOT / "docker" / "bases" / f"{project}.Dockerfile"),
                       "-t", f"embedeval-{project}-base:latest", str(ROOT / "docker" / "bases")], args.dry_run)
                built.add(project)
            directory = ROOT / case["directory"]
            if case.get("parent_image"):
                build(["docker", "build", "--build-arg", f"BASE_COMMIT={case['base_commit']}",
                       "-t", case["parent_image"], str(directory / "parent")], args.dry_run)
            if case.get("correct_image"):
                build(["docker", "build", "-f", str(directory / "build" / "Correct.Dockerfile"),
                       "-t", case["correct_image"], str(directory / "build")], args.dry_run)
            build(["docker", "build", "-t", case["image"], str(directory / "build")], args.dry_run)
        if args.validate:
            if args.dry_run:
                print(f"Validate {case['id']}: repair -> correct PASS; undo repair -> expected regressions FAIL; repair -> restored PASS")
            else:
                validate(case)
        if args.test:
            cmd = ["docker", "run", "--rm", case["image"], "bash", "-c", f"cd /testbed && {case['run_command']}"]
            print(subprocess.list2cmdline(cmd), flush=True)
            if not args.dry_run:
                raise SystemExit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
