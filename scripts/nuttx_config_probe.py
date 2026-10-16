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


def call_sites(main: Path, entries: list[str], header: str) -> list[dict]:
    """Each call of an entry function in ostest_main.c, the conditionals
    around it, and whether the preprocessor keeps it under this config."""
    calls, out, stack = [], [], []
    lines = main.read_text().splitlines()
    index = 0
    while index < len(lines):
        line, number = lines[index], index + 1
        while line.rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            line = line.rstrip()[:-1] + " " + lines[index].strip()
        index += 1
        directive = re.match(r"\s*#\s*(\w+)", line)
        if directive:
            word, text = directive[1], " ".join(line.split())
            if word in ("if", "ifdef", "ifndef", "elif", "else", "endif", "define", "undef"):
                out.append(text)
            if word in ("if", "ifdef", "ifndef"):
                stack.append(text)
            elif word in ("elif", "else") and stack:
                stack[-1] = f"{stack[-1]} ... {text}"
            elif word == "endif" and stack:
                stack.pop()
            continue
        for entry in entries:
            if re.search(rf"\b{re.escape(entry)}\s*\(", line):
                out.append(f"EMBEDEVAL_CALL_{len(calls)}")
                calls.append({"function": entry, "line": number, "conditions": list(stack)})
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.h").write_text(header)
        (Path(tmp) / "skeleton.c").write_text("\n".join(out) + "\n")
        result = subprocess.run(["gcc", "-E", "-P", "-nostdinc", "-include", "config.h",
                                 "skeleton.c"], cwd=tmp, capture_output=True, text=True)
    kept = set(re.findall(r"EMBEDEVAL_CALL_(\d+)", result.stdout))
    for number, call in enumerate(calls):
        call["compiled"] = str(number) in kept
    return calls


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
    tests = []
    for test in request.get("tests", []):
        name = Path(test["path"]).name
        calls = call_sites(ostest / "ostest_main.c", test["entries"], header) \
            if test["entries"] else []
        tests.append({"path": test["path"], "built": name in built, "calls": calls,
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
