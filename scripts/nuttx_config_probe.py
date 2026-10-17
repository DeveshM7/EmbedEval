#!/usr/bin/env python3
"""Inspect a configured NuttX tree; run inside the NuttX image by nuttx_config.py.

    python3 nuttx_config_probe.py <nuttx tree> <request.json>

The tree holds the kernel with apps/ inside it, configured for sim:nsh. The
request names the overrides that were asked for, settings to report, and the
changed ostest sources with their entry functions. Prints JSON: each
override's outcome (kept, dropped with its unmet dependencies, or unknown in
this tree), the reported settings, and whether each changed test is built by
the ostest Makefile and called from ostest_main.c under this configuration.
Needs only the standard library, kconfiglib, make and gcc.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def read_config(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        match = re.match(r"(CONFIG_\w+)=(.*)", line)
        if match:
            values[match[1]] = match[2]
    return values


def wanted(value: str) -> str | None:
    """The .config value an override asks for; None for n (not set)."""
    return None if value == "n" else value


def explain(top: Path, names: list[str]) -> dict[str, dict]:
    """Why each symbol is not at its requested value: its dependencies and
    which of their symbols are off, from kconfiglib."""
    import kconfiglib

    os.environ.update({"srctree": str(top), "APPSDIR": str(top / "apps"),
                       "APPSBINDIR": str(top / "apps"), "BINDIR": str(top),
                       "EXTERNALDIR": "dummy"})
    cwd = os.getcwd()
    os.chdir(top)
    try:
        kconf = kconfiglib.Kconfig("Kconfig", warn=False)
        kconf.load_config(".config")
    finally:
        os.chdir(cwd)
    result = {}
    for name in names:
        sym = kconf.syms.get(name[len("CONFIG_"):])
        if sym is None or not sym.nodes:
            result[name] = {"status": "unknown",
                            "why": "no such symbol in this tree's Kconfig"}
            continue

        def show(item):
            if isinstance(item, kconfiglib.Symbol) and not item.is_constant:
                return f"{item.name}={item.str_value or 'n'}"
            return kconfiglib.standard_sc_expr_str(item)
        unmet = sorted({f"{item.name}={item.str_value or 'n'}"
                        for item in kconfiglib.expr_items(sym.direct_dep)
                        if isinstance(item, kconfiglib.Symbol) and not item.is_constant
                        and item.str_value in ("", "n")})
        result[name] = {"status": "dropped", "type": kconfiglib.TYPE_TO_STR[sym.type],
                        "depends_on": kconfiglib.expr_str(sym.direct_dep, show),
                        "unmet": unmet, "choice": sym.choice is not None}
    return result


def built_sources(ostest: Path, config: Path) -> set[str]:
    """CSRCS and MAINSRC of the ostest Makefile under this .config."""
    body = [line for line in (ostest / "Makefile").read_text().splitlines()
            if not re.match(r"\s*-?include\b", line)]
    with tempfile.TemporaryDirectory() as tmp:
        wrapper = Path(tmp) / "probe.mk"
        wrapper.write_text(f"include {config}\n" + "\n".join(body)
                           + "\n.DEFAULT_GOAL := probe\nprobe:\n\t@echo $(CSRCS) $(MAINSRC)\n")
        out = subprocess.run(["make", "-s", "-f", str(wrapper)], cwd=ostest,
                             capture_output=True, text=True)
    return set(out.stdout.split()) if out.returncode == 0 else set()


def config_header(values: dict[str, str]) -> str:
    """The macros tools/mkconfig derives from .config, as far as #if needs them."""
    lines = []
    for name, value in values.items():
        if value in ("y", "m"):
            lines.append(f"#define {name} 1")
        elif value != "n":
            lines.append(f"#define {name} {value}")
    return "\n".join(lines) + "\n"


def logical_lines(text: str):
    """(line number, line) with backslash continuations joined."""
    lines, index = text.splitlines(), 0
    while index < len(lines):
        line, number = lines[index], index + 1
        while line.rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            line = line.rstrip()[:-1] + " " + lines[index].strip()
        index += 1
        yield number, line


def conditionals(text: str, entries: list[str] = ()) -> dict:
    """The preprocessor structure of a C source: each call of an entry
    function with the conditionals around it, and each #if group whose
    conditions name a CONFIG_ option, with its branches' line ranges. Also a
    skeleton (conditionals plus markers) for evaluate()."""
    calls, groups, open_groups, stack, out = [], [], [], [], []
    last = 0
    for number, line in logical_lines(text):
        last = number
        directive = re.match(r"\s*#\s*(\w+)", line)
        if directive:
            word, text_ = directive[1], " ".join(line.split())
            if word in ("if", "ifdef", "ifndef", "elif", "else", "endif", "define", "undef"):
                out.append(text_)
            if word in ("if", "ifdef", "ifndef"):
                stack.append(text_)
                open_groups.append({"branches": [{"directive": text_, "start": number + 1}],
                                    "outer": list(stack[:-1])})
            elif word in ("elif", "else") and open_groups:
                stack[-1] = f"{stack[-1]} ... {text_}"
                group = open_groups[-1]
                group["branches"][-1]["end"] = number - 1
                group["branches"].append({"directive": text_, "start": number + 1})
            elif word == "endif" and open_groups:
                stack.pop()
                group = open_groups.pop()
                group["branches"][-1]["end"] = number - 1
                if any("CONFIG_" in b["directive"] for b in group["branches"]):
                    groups.append(group)
            if word in ("if", "ifdef", "ifndef", "elif", "else"):
                group = open_groups[-1] if open_groups else None
                if group:   # a marker at the start of the branch just opened
                    index = (len(groups) + len(open_groups) - 1, len(group["branches"]) - 1)
                    group["branches"][-1]["marker"] = f"EMBEDEVAL_BRANCH_{id(group)}_{index[1]}"
                    out.append(group["branches"][-1]["marker"])
            continue
        for entry in entries:
            if re.search(rf"\b{re.escape(entry)}\s*\(", line):
                calls.append({"function": entry, "line": number, "conditions": list(stack),
                              "marker": f"EMBEDEVAL_CALL_{len(calls)}"})
                out.append(calls[-1]["marker"])
    for group in open_groups:   # unbalanced source: close at the end
        group["branches"][-1].setdefault("end", last)
    groups.sort(key=lambda g: g["branches"][0]["start"])
    return {"calls": calls, "groups": groups[:30], "skeleton": "\n".join(out) + "\n"}


def evaluate(found: dict, header: str) -> dict:
    """Mark each call and branch compiled or not under a config header."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.h").write_text(header)
        (Path(tmp) / "skeleton.c").write_text(found["skeleton"])
        result = subprocess.run(["gcc", "-E", "-P", "-nostdinc", "-include", "config.h",
                                 "skeleton.c"], cwd=tmp, capture_output=True, text=True)
    kept = set(re.findall(r"EMBEDEVAL_\w+", result.stdout))
    for call in found["calls"]:
        call["compiled"] = call.pop("marker") in kept
    for group in found["groups"]:
        for branch in group["branches"]:
            branch["compiled"] = branch.pop("marker", None) in kept
    del found["skeleton"]
    return found


def makefile_conditions(makefile: str, name: str) -> list[str]:
    """The ifeq/ifneq/ifdef/ifndef conditions around each line that adds
    name to the sources (one list per such line, joined with ' && ')."""
    stack, found = [], []
    for _, line in logical_lines(makefile):
        text = " ".join(line.split())
        word = text.split(" ", 1)[0] if text else ""
        if word in ("ifeq", "ifneq", "ifdef", "ifndef"):
            stack.append(text)
        elif word == "else" and stack:
            stack[-1] = f"not ({stack[-1]})"
        elif word == "endif" and stack:
            stack.pop()
        elif re.search(rf"(?<![\w/.-]){re.escape(name)}(?![\w.-])", text) and "SRCS" in text:
            found.append(" && ".join(stack))
    return found


def kconfig_depends(kconfig: str) -> dict[str, str]:
    """depends on / select lines of each config in a Kconfig file."""
    result, current = {}, None
    for _, line in logical_lines(kconfig):
        text = " ".join(line.split())
        match = re.match(r"(?:menu)?config (\w+)$", text)
        if match:
            current = f"CONFIG_{match[1]}"
        elif current and re.match(r"(depends on|select) ", text):
            result.setdefault(current, []).append(text)
        elif text.startswith(("endmenu", "endif", "menu ", "if ", "choice", "comment ")):
            current = None
    return {name: "; ".join(lines) for name, lines in result.items()}


def split_top(expr: str, op: str) -> list[str]:
    parts, depth, start, i = [], 0, 0, 0
    while i < len(expr):
        ch = expr[i]
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i])
            start = i = i + len(op)
            continue
        i += 1
    return [part.strip() for part in parts + [expr[start:]]]


def strip_parens(term: str) -> str:
    """term without parentheses that enclose all of it."""
    term = term.strip()
    while term.startswith("(") and term.endswith(")"):
        depth = 0
        for index, ch in enumerate(term):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and index < len(term) - 1:
                return term   # the first "(" closes before the end
        term = term[1:-1].strip()
    return term


def simple(term: str) -> tuple[str, str] | None:
    """(CONFIG_X, y or n) for defined(CONFIG_X), !defined(...), CONFIG_X."""
    term = strip_parens(term)
    match = re.fullmatch(r"(!?)\s*(?:defined\s*\(?\s*)?(CONFIG_\w+)\s*\)?", term)
    return (match[2], "n" if match[1] else "y") if match else None


def requirements(conditions: list[str]) -> dict:
    """Options that must hold, and one-of groups, from a conjunction of
    #if/#ifdef/#ifndef or Makefile conditions. Anything not of a simple form
    (comparisons, #else branches) is left in "other" for a person or the
    model to read; nothing is guessed."""
    required, one_of, other = {}, [], []
    for condition in conditions:
        text = condition.strip()
        if " ... " in text or text.startswith("not ("):
            other.append(text)
            continue
        make = re.fullmatch(r"(ifeq|ifneq) \(\$\((CONFIG_\w+)\),\s*(y|)\)", text)
        if make:
            positive = (make[1] == "ifeq") == (make[3] == "y")
            required[make[2]] = "y" if positive else "n"
            continue
        word, _, expr = text.partition(" ")
        if word in ("#ifdef", "#ifndef") and re.fullmatch(r"CONFIG_\w+", expr):
            required[expr] = "y" if word == "#ifdef" else "n"
            continue
        if word not in ("#if", "#elif"):
            other.append(text)
            continue
        for term in split_top(strip_parens(expr), "&&"):
            alternatives = split_top(strip_parens(term), "||")
            if len(alternatives) == 1 and simple(term):
                name, value = simple(term)
                required[name] = value
            elif len(alternatives) > 1 and all(simple(a) for a in alternatives):
                one_of.append(dict(simple(a) for a in alternatives))
            else:
                other.append(term)
    return {"required": required, "one_of": one_of, "other": other}


def main() -> None:
    top, request = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
    values = read_config(top / ".config")
    overrides = []
    for name, value in request.get("overrides", {}).items():
        final = values.get(name)
        overrides.append({"name": name, "requested": value, "final": final or "n",
                          "status": "kept" if final == wanted(value) else "dropped"})
    reasons = explain(top, [o["name"] for o in overrides if o["status"] == "dropped"])
    for override in overrides:
        override.update(reasons.get(override["name"], {}))
    ostest = top / "apps" / "testing" / "ostest"
    built = built_sources(ostest, top / ".config")
    header = config_header(values)
    main_source = (ostest / "ostest_main.c").read_text()
    tests = []
    for test in request.get("tests", []):
        name = Path(test["path"]).name
        calls = evaluate(conditionals(main_source, test["entries"]), header)["calls"] \
            if test["entries"] else []
        source = ostest / name
        groups = evaluate(conditionals(source.read_text()), header)["groups"] \
            if source.exists() else []
        tests.append({"path": test["path"], "built": name in built, "calls": calls,
                      "branches": groups,
                      "runs": name in built and (not test["entries"]
                                                 or any(c["compiled"] for c in calls))})
    print(json.dumps({
        "ostest": values.get("CONFIG_TESTING_OSTEST") == "y",
        "overrides": overrides,
        "settings": {name: values.get(name, "n") for name in request.get("report", [])},
        "makefile_ok": bool(built),
        "tests": tests}))


if __name__ == "__main__":
    main()
