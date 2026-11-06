#!/usr/bin/env bash
# Build and execute one native Mynewt selftest package. The result parser
# rejects missing tests, unexpected tests, and crashed binaries.
set -euo pipefail

exec python3 - "$@" <<'PY'
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def sync_repository(name, package_path):
    package = Path("/testbed") / package_path
    if not package.exists():
        return
    block = re.search(
        rf"(?m)^repository\.{re.escape(name)}:[ \t]*\n((?:[ \t]+[^\n]*(?:\n|$))*)",
        package.read_text(),
    )
    if not block:
        return
    fields = dict(re.findall(r"(?m)^\s+(\w+):\s*([^\s#]+)", block[1]))
    fields = {key: value.strip("\"'") for key, value in fields.items()}
    if fields.get("type") != "github" or not fields.get("vers", "").endswith("-commit"):
        raise ValueError(f"unsupported repository.{name} dependency specification")
    # Newt's -commit suffix names a Git revision directly, not a Newt release.
    revision = fields["vers"][:-len("-commit")]
    url = f"https://github.com/{fields['user']}/{fields['repo']}.git"
    checkout_repository(name, url, revision)


def checkout_repository(name, url, revision):
    repo = Path("/project/repos") / name
    repo.mkdir(parents=True, exist_ok=True)
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=120)
    if not (repo / ".git").exists():
        git("init", "-q")
    ref = "refs/embedeval/" + revision
    found = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", ref],
                           capture_output=True).returncode == 0
    if not found:
        git("fetch", "-q", "--depth", "1", url, revision)
        git("update-ref", ref, "FETCH_HEAD")
    git("checkout", "-q", "--detach", "--force", ref)
    print(f"dependency {name} resolved commit:", flush=True)
    git("rev-parse", "HEAD")


def sync_dependencies(meta):
    sync_repository("mbedtls", "crypto/mbedtls/pkg.yml")
    sync_repository("tinycbor", "encoding/tinycbor/pkg.yml")
    sync_repository("coremqtt", "net/mqtt/coremqtt/pkg.yml")
    revision = meta.get("mcumgr_commit")
    if revision:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("mcumgr fixture must use an exact commit")
        name = "apache-mynewt-mcumgr"
        checkout_repository(name, "https://github.com/apache/mynewt-mcumgr.git", revision)
        project = Path("/project/project.yml")
        text = project.read_text()
        block = (f"repository.{name}:\n    type: github\n    vers: {revision}-commit\n"
                 "    user: apache\n    repo: mynewt-mcumgr\n")
        if f"repository.{name}:" in text:
            if block not in text:
                raise ValueError("project has a different mcumgr fixture")
        else:
            text = text.replace("project.repositories:\n", f"project.repositories:\n    - {name}\n", 1)
            project.write_text(text + block)


def classify(output, returncode, expected):
    passed = set(re.findall(r"\[pass\] (\S+)", output))
    failed = set(re.findall(r"\[FAIL\] (\S+)", output))
    if returncode < 0:
        # Old tests (before 2019) use plain assert(): a failing case kills the
        # whole binary with SIGABRT and prints no [FAIL] line. The cases that
        # never reported are the ones that did not pass; a validator holding
        # the exact inventory still rejects a crash that fails the wrong set.
        failed |= set(expected) - passed - failed
        returncode = 1
    result = {
        "passed": sorted(passed - failed),
        "failed": sorted(failed),
        "missing": sorted(set(expected) - passed - failed),
        "unexpected": sorted((passed | failed) - set(expected)),
    }
    if result["missing"] or result["unexpected"] or returncode not in (0, 1):
        return 3, result
    if failed:
        return 1, result
    return (0 if returncode == 0 else 3), result


def compile_diagnostic(output, source, api):
    if not source or not api:
        return None
    if re.search(r"internal compiler error", output, re.IGNORECASE):
        return None
    diagnostic = None
    linker_diagnostic = False
    missing_function = False
    bad_signature = False
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if "undefined reference to" in line:
            match = re.fullmatch(
                r"(?:/[^:]+/ld:\s*)?(.+?):\d+:\([^\n]+\): undefined reference to [`‘]([^'’]+)['’]",
                line,
            )
            if not match or not (
                (match[1] == source or match[1].endswith("/" + source))
                and match[2] == api
            ):
                return None
            diagnostic = diagnostic or line
            linker_diagnostic = True
            continue
        if linker_diagnostic and re.fullmatch(r"collect2: error: ld returned \d+ exit status", line):
            continue
        if not re.search(r"(?:fatal )?error:", line):
            continue
        match = re.match(r"(.+?):(\d+)(?::(\d+))?:\s*(?:fatal )?error:\s*(.*)", line)
        if not match:
            return None
        path, row, column, message = match.groups()
        if not (path == source or path.endswith("/" + source)):
            return None
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(api)}(?![A-Za-z0-9_])", message):
            if diagnostic is None:
                diagnostic = line
            if re.search(rf"implicit declaration of function ['‘]{re.escape(api)}['’]", message):
                missing_function = True
            if re.fullmatch(rf"too many arguments to function ['‘]{re.escape(api)}['’]", message):
                bad_signature = True
            continue
        # GCC reports later assignments from an undeclared function as int-to-
        # pointer conversions. Attribute only direct calls at this error's location.
        excerpt = re.match(rf"\s*{row}\s*\| (.*)$", lines[index + 1]) if index + 1 < len(lines) else None
        assignment = re.fullmatch(
            rf"\s*[A-Za-z_]\w*\s*(?P<operator>=)\s*{re.escape(api)}\s*\([^();]*\);\s*",
            excerpt[1],
        ) if excerpt else None
        if not (
            ((missing_function and re.fullmatch(
                r"assignment to ['‘][^'’]+\*['’] from ['‘]int['’] "
                r"makes pointer from integer without a cast \[-Werror=int-conversion\]",
                message,
            )) or (bad_signature and message == "void value not ignored as it ought to be"))
            and assignment
            and column
            and int(column) == assignment.start("operator") + 1
        ):
            return None
    return diagnostic


def source_excerpt(lines, index, row, column=None):
    """The text GCC quotes for ROW at lines[index], if it has the expected caret."""
    if index >= len(lines):
        return None
    quoted = re.fullmatch(r"\s*(\d+) \| (.*)", lines[index])
    if not quoted or quoted[1] != row:
        return None
    if column is not None:
        caret = lines[index + 1] if index + 1 < len(lines) else ""
        marker = re.fullmatch(r"\s*\| ( *)\^~*", caret)
        if not marker or len(marker[1]) != column - 1:
            return None
    return quoted[2]


def attached_note_diagnostic(lines, index, expectations, notes):
    """Attribute an error through the GCC note attached to its location.

    Both shapes are checked against rows attested at generation from the test
    patch and the production diff: an initializer whose function was changed by
    the test patch, and a production macro expanded at a changed test line."""
    error = re.fullmatch(r"(.+?):(\d+):(\d+): error: (.*)", lines[index])
    if not error or not notes or index + 3 >= len(lines):
        return None
    path, row, column, message = error[1], error[2], int(error[3]), error[4]
    def at(file, wanted):
        return file == wanted or file.endswith("/" + wanted)
    for source, entries in notes.items():
        if source not in expectations:
            continue
        if at(path, source):
            entry = entries.get(row)
            note = re.fullmatch(
                rf"{re.escape(path)}:{row}:{column}: note: \(near initialization for ['‘]([A-Za-z_]\w*)\.([A-Za-z_]\w*)['’]\)",
                lines[index + 3])
            if (note and entry and entry["kind"] == "initializer"
                    and entry["api"] in expectations[source]
                    and (note[1], note[2]) == (entry["object"], entry["api"])
                    and re.fullmatch(r"initialization of ['‘][^’']+['’] from incompatible pointer type "
                                     r"['‘][^’']+['’] \[-Werror=incompatible-pointer-types\]", message)
                    and source_excerpt(lines, index + 1, row, column) == entry["text"]
                    and re.match(rf"{re.escape(entry['value'])}(?![A-Za-z0-9_])", entry["text"][column - 1:])):
                return {"source": source, "api": entry["api"], "kind": "initializer_note", "line": row,
                        "diagnostic": lines[index] + "\n" + lines[index + 3]}
            continue
        # A macro error is located at its production definition; the next note
        # names the changed test line that expanded it.
        note = re.fullmatch(r"(.+?):(\d+):(\d+): note: in expansion of macro ['‘]([A-Za-z_]\w*)['’]", lines[index + 2])
        entry = entries.get(note[2]) if note and at(note[1], source) else None
        if not entry or entry["kind"] != "macro" or entry["api"] != note[4] or entry["api"] not in expectations[source]:
            continue
        definition = entry["definition"]
        column_in_use = int(note[3])
        if (at(path, definition["path"]) and row == str(definition["line"])
                and re.fullmatch(r".* \[-Werror=overflow\]", message)
                and source_excerpt(lines, index + 1, row) == definition["text"]
                and source_excerpt(lines, index + 3, note[2], column_in_use) == entry["text"]
                and re.match(rf"{re.escape(entry['api'])}(?![A-Za-z0-9_])", entry["text"][column_in_use - 1:])
                and not re.search(r"[A-Za-z0-9_]$", entry["text"][:column_in_use - 1])):
            return {"source": source, "api": entry["api"], "kind": "macro_note", "line": note[2],
                    "diagnostic": lines[index] + "\n" + lines[index + 2]}
    return None


def preprocessor_diagnostic(lines, index, expectations, notes, output):
    """Attribute a #if use of a macro whose header is reached only through the missing header.

    The row is attested at generation. The dependency scan must also list the
    missing header by its bare name, which GCC -MG does when it cannot find it."""
    error = re.fullmatch(r"(.+?):(\d+):(\d+): error: missing binary operator before token \"\(\"", lines[index])
    if not error:
        return None
    path, row, column = error[1], error[2], int(error[3])
    for source, entries in (notes or {}).items():
        entry = entries.get(row)
        if (source not in expectations or not entry or entry.get("kind") != "preprocessor"
                or entry.get("api") not in expectations[source]
                or not (path == source or path.endswith("/" + source))):
            continue
        use = re.match(rf"\s*#\s*(?:if|elif)\s+({re.escape(entry['macro'])})\(", entry["text"])
        excerpt = source_excerpt(lines, index + 1, row, column)
        missing = re.compile(rf"\s*{re.escape(entry['api'])}(?: \\)?")
        if (use and column == use.end(1) + 1 and excerpt == entry["text"]
                and (Path("/testbed") / source).read_text().splitlines()[int(row) - 1] == entry["text"]
                and any(missing.fullmatch(line) for line in output.splitlines())):
            return {"source": source, "api": entry["api"], "kind": "preprocessor_macro", "line": row,
                    "diagnostic": lines[index] + "\n" + lines[index + 1]}
    return None


def compile_diagnostic_group(output, expectations, declarations=None, notes=None):
    """Account for every error using source/API pairs attested at generation."""
    diagnostics = []
    linked = False
    incomplete = set()
    if re.search(r"internal compiler error", output, re.IGNORECASE):
        return []
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if linked and re.fullmatch(r"collect2: error: ld returned \d+ exit status", line):
            continue
        if "undefined reference to" not in line and not re.search(r"(?:fatal )?error:", line):
            continue
        typed = any(text in line for text in (": error: storage size of ",
                    ": error: array type has incomplete element type ", "[-Werror=unused-variable]"))
        matched = None if typed else next(
            ({"source": source, "api": api, "diagnostic": line}
             for source, apis in expectations.items() for api in apis
             if compile_diagnostic(line, source, api)), None)
        if matched is None:
            error = re.match(r"(.+?):(\d+)(?::\d+)?:\s*(?:fatal )?error:\s*(.*)", line)
            for source, apis in expectations.items():
                declaration = (declarations or {}).get(source, {}).get(error[2]) if error else None
                if not declaration or not (error[1] == source or error[1].endswith("/" + source)):
                    continue
                api, variable = declaration["api"], declaration["variable"]
                if api not in apis or (Path("/testbed") / source).read_text().splitlines()[int(error[2]) - 1] != declaration["text"]:
                    continue
                key = source, error[2], variable
                if re.fullmatch(rf"storage size of ['‘]{re.escape(variable)}['’] isn['’]t known", error[3]):
                    kind = "incomplete_type"
                    incomplete.add(key)
                elif declaration.get("array") and re.fullmatch(
                        rf"array type has incomplete element type ['‘]struct {re.escape(api)}['’]", error[3]):
                    kind = "incomplete_array_type"
                    incomplete.add(key)
                elif key in incomplete and re.fullmatch(
                        rf"(?:unused variable ['‘]{re.escape(variable)}['’]|['‘]{re.escape(variable)}['’] defined but not used) \[-Werror=unused-variable\]", error[3]):
                    kind = "unused_after_incomplete_type"
                else:
                    continue
                matched = {"source": source, "api": api, "diagnostic": line, "kind": kind,
                           "line": error[2], "declaration": declaration["text"]}
                break
        if matched is None:
            matched = attached_note_diagnostic(lines, index, expectations, notes)
        if matched is None:
            matched = preprocessor_diagnostic(lines, index, expectations, notes, output)
        if matched is None:
            return []
        diagnostics.append(matched)
        linked |= "undefined reference to" in line
    return diagnostics


def observe_testutil(binary, runtime):
    # GDB reads real result-event globals. It never calls an inferior
    # function, changes program variables, or supplies an expected outcome.
    commands = ["set pagination off", "set confirm off", "set debuginfod enabled off",
                "set disable-randomization off", "set print thread-events off",
                "python import gdb",
                "handle SIGALRM nostop noprint pass", "handle SIGVTALRM nostop noprint pass",
                "handle SIGUSR1 nostop noprint pass",
                "handle SIGUSR2 nostop noprint pass"]
    suite = ("'tu_config.ts_suite_name' if gdb.lookup_global_symbol('tu_config') "
             "else 'ts_current_config->ts_suite_name'")
    for function, label in (("tu_case_write_pass_buf", "pass"), ("tu_case_fail", "FAIL")):
        printf = f'printf "[{label}] %s/%s\\n", '
        commands += [f"break {function}", "commands", "silent",
                     f"python gdb.execute({printf!r} + ({suite}) + ', tu_case_name')",
                     "continue", "end"]
    commands += ["run", "python",
                 "if gdb.selected_inferior().pid:",
                 "    gdb.execute('bt full')",
                 "    if gdb.lookup_global_symbol('tu_case_name'):",
                 "        gdb.execute('p tu_case_name')",
                 "    if gdb.lookup_global_symbol('g_current_task'):",
                 "        gdb.execute('p *g_current_task')",
                 "    gdb.execute('info registers')", "end"]
    script = runtime.parent / "mynewt-observe.gdb"
    script.write_text("\n".join(commands) + "\n")
    return ["gdb", "--batch", "--quiet", "--nx", "--return-child-result", "-x", str(script), str(binary)]


parser = argparse.ArgumentParser()
parser.add_argument("--baseline", action="store_true")
parser.add_argument("--install-deps", action="store_true")
parser.add_argument("--baseline-test-path", help="original unit-test package before relocation")
args = parser.parse_args()
if args.baseline_test_path and not args.baseline:
    parser.error("--baseline-test-path requires --baseline")
meta = json.loads(Path("/opt/benchmark/metadata.json").read_text())
if args.install_deps:
    sync_dependencies(meta)
    raise SystemExit(0)
mode = "runtime" if args.baseline else meta.get("failure_mode", "runtime")
expected = (
    meta["baseline_tests"]
    if args.baseline
    else meta["fail_to_pass"] + meta["pass_to_pass"]
)
result = {
    "failure_mode": mode,
    "build_ok": False,
    "compile_failure": False,
    "config_failure": False,
    "dependency_failure": False,
    "compile_test_source": meta.get("compile_test_source"),
    "missing_api": meta.get("missing_api"),
    "diagnostic": None,
    "compile_diagnostics": [],
    "observer": None,
    "passed": [],
    "failed": [],
    "missing": [],
    "unexpected": [],
    "error": None,
}
code = 3
phase = "dependency setup"
try:
    sync_dependencies(meta)
    phase = "build"
    # Fresh build and runtime directories prevent stale binaries or simulated flash.
    shutil.rmtree("/project/bin", ignore_errors=True)
    build = subprocess.run(
        ["newt", "test", "@apache-mynewt-core/" + (args.baseline_test_path or meta["test_path"])],
        cwd="/project",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=180,
    )
    output = build.stdout + build.stderr
    print(output, flush=True)
    # Newt 1.0 beta test executables have no .elf suffix. Replay only the
    # native program Newt reported, with the same one-program guard.
    binaries = []
    for name in re.findall(r"^Executing test: (\S+)$", output, re.MULTILINE):
        binary = Path(name).resolve()
        if Path("/project/bin") in binary.parents and binary.is_file():
            with binary.open("rb") as handle:
                if handle.read(4) == b"\x7fELF":
                    binaries.append(binary)
    if mode in ("config", "dependency") and build.returncode > 0 and "Executing test: " not in output:
        fingerprint = hashlib.sha256(output.strip().encode()).hexdigest()
        if fingerprint == meta.get(mode + "_output_sha256"):
            result[mode + "_failure"] = True
            result["diagnostic"] = output.strip()
            result[mode + "_output_sha256"] = fingerprint
            code = 1
        else:
            result["error"] = f"build failed without the source-attributed {mode} diagnostic"
    elif mode == "compile" and build.returncode > 0 and "Executing test: " not in output:
        if meta.get("compile_expectations"):
            result["compile_diagnostics"] = compile_diagnostic_group(
                output, meta["compile_expectations"], meta.get("compile_declarations"),
                meta.get("compile_notes"))
            diagnostic = "\n".join(item["diagnostic"] for item in result["compile_diagnostics"]) or None
        else:
            diagnostic = compile_diagnostic(
                output, meta.get("compile_test_source"), meta.get("missing_api")
            )
        if diagnostic is not None:
            result["compile_failure"] = True
            result["diagnostic"] = diagnostic
            code = 1
        else:
            result["error"] = "build failed without expected compile diagnostic"
    elif (
        "Executing test: " not in output
        or len(binaries) != 1
        or build.returncode not in (0, 1)
    ):
        result["error"] = "build did not reach native test execution"
    else:
        result["build_ok"] = True
        phase = "test execution"
        # Newt hides stdout for passing suites, so execute the ELF directly to
        # capture every individual testcase in both passing and failing runs.
        runtime = Path("/tmp/mynewt-runtime")
        shutil.rmtree(runtime, ignore_errors=True)
        runtime.mkdir()
        command = [str(binaries[0])]
        if meta.get("test_observer") == "testutil-gdb" and not args.baseline:
            command = observe_testutil(binaries[0], runtime)
            result["observer"] = "testutil-gdb"
        test = subprocess.run(
            command,
            cwd=runtime,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            timeout=90,
        )
        output = test.stdout + test.stderr
        print(output, flush=True)
        code, cases = classify(output, test.returncode, expected)
        result.update(cases)
        if code == 0 and build.returncode != 0:
            code = 3
            result["error"] = "Newt and direct execution disagree"
except subprocess.TimeoutExpired:
    code = 2
    result["error"] = "build or native test execution timed out"
except (subprocess.CalledProcessError, ValueError, KeyError) as exc:
    result["error"] = f"{phase} failed: {exc}"

result["exit_code"] = code
Path("/tmp/mynewt-result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result), flush=True)
raise SystemExit(code)
PY
