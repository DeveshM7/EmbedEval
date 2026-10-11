#!/usr/bin/env python3
"""
Twister-related facts about a Zephyr PR, read from the PR's own commits.

    python scripts/zephyr_env.py facts 65697          # print the facts for one PR
    python scripts/zephyr_env.py refresh-lookups      # re-download the two lookup tables

Everything a twister-based instance needs to know about its era is either
stated in the tree at the base commit or follows from two small lookup tables,
so nothing here is a judgement and nothing here needs a model:

    python_min      PYTHON_MINIMUM_REQUIRED in cmake/python.cmake
    sdk_min         the minimum the tree's CMake asks for
    ci              the CI config of that commit: image and SDK upstream used
    python          the CI image's Python, raised to python_min and to 3.9
    sdk             the CI's SDK if it has an ARM64 download, else the nearest
                    newer release that does
    boards          the candidate board names valid at the base commit
    twister         which era-specific twister options exist at that commit
    suites          the test suites the PR changes, with their scenarios

Read from the local full clone (candidates/zephyr.git), never from GitHub, so
it is fast and repeatable. PR head commits are not on any branch of the clone,
so they are fetched into it by sha when missing -- the same thing
generate_instance.py already does to build the test patch.

The two lookup tables live in templates/zephyr/lookup/ and are filled lazily:

    ci_images.json     CI image tag -> Ubuntu release, from the Dockerfiles in
                       github.com/zephyrproject-rtos/docker-image
    sdk_releases.json  SDK version -> ARM64 minimal download format, from the
                       sdk-ng GitHub releases
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CLONE = REPO_ROOT / "candidates" / "zephyr.git"
LOOKUP = REPO_ROOT / "templates" / "zephyr" / "lookup"
ENRICHED = REPO_ROOT / "candidates" / "enriched"

# Ubuntu release -> its default python3. The CI images use the distribution's
# Python, so this is what upstream CI ran.
UBUNTU_PYTHON = {"18.04": "3.6", "20.04": "3.8", "22.04": "3.10", "24.04": "3.12"}

# Oldest Python we build with. Every instance so far ran on 3.9 or newer; older
# interpreters with today's packages are untested on ARM64.
PYTHON_FLOOR = "3.9"

# The boards an instance may use, in order of preference within each kind.
# Names changed over the years (native_posix_64 -> native_sim_64 ->
# native_sim/native/64), so each kind lists every spelling it has had.
CANDIDATE_BOARDS = {
    "native64": ["native_sim/native/64", "native_sim_64",
                 "native_posix/native/64", "native_posix_64"],
    "qemu_x86": ["qemu_x86"],
}

SUITE_MARKERS = ("testcase.yaml", "tests.yaml", "sample.yaml")

# Scenario keys that decide where and how a scenario runs. Kept verbatim so the
# dry-run table and a reader can see why a pair was selected or filtered.
SCENARIO_KEYS = ("platform_allow", "platform_exclude", "arch_allow", "arch_exclude",
                 "depends_on", "filter", "extra_configs", "extra_args", "harness",
                 "build_only", "skip", "slow", "integration_platforms", "platform_key",
                 "platform_type", "min_ram", "min_flash", "toolchain_allow",
                 "toolchain_exclude", "timeout", "tags")


# ---------------------------------------------------------------- git


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(CLONE), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout


def has_commit(sha: str) -> bool:
    return subprocess.run(["git", "-C", str(CLONE), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def ensure_commits(*shas: str) -> None:
    """Fetch commits the clone does not have, by sha. PR heads are not on any
    branch, so a plain clone never contains them."""
    missing = [s for s in shas if s and not has_commit(s)]
    if missing:
        r = subprocess.run(["git", "-C", str(CLONE), "fetch", "-q", "origin", *missing],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"could not fetch {missing}: {r.stderr.strip()[:300]}")


def show(commit: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(CLONE), "show", f"{commit}:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def ls_tree(commit: str, path: str) -> list[str]:
    return git("ls-tree", "-r", "--name-only", commit, "--", path, check=False).split()


def commit_date(commit: str) -> str:
    return git("log", "-1", "--format=%cs", commit).strip()


# ---------------------------------------------------------------- versions


def vtuple(v: str) -> tuple:
    """'0.16.5-1' -> (0, 16, 5, 1); '3.10' -> (3, 10)."""
    return tuple(int(x) for x in re.findall(r"\d+", v))


def python_min(commit: str) -> str | None:
    for path in ("cmake/modules/python.cmake", "cmake/python.cmake"):
        text = show(commit, path)
        if text:
            m = re.search(r"set\(PYTHON_MINIMUM_REQUIRED\s+([0-9.]+)\)", text)
            if m:
                return m.group(1)
    return None


def sdk_min(commit: str) -> str | None:
    """The lowest SDK the tree accepts. Three spellings over the years:
    REQUIRED_SDK_VER (2021), TOOLCHAIN_ZEPHYR_MINIMUM_REQUIRED_VERSION (2022),
    find_package(Zephyr-sdk X) in FindHostTools.cmake (2023 on)."""
    probes = [
        ("cmake/modules/FindHostTools.cmake", r"find_package\(Zephyr-sdk\s+([0-9.]+)\)"),
        ("cmake/modules/verify-toolchain.cmake",
         r"set\(TOOLCHAIN_ZEPHYR_MINIMUM_REQUIRED_VERSION\s+([0-9.]+)\)"),
        ("cmake/verify-toolchain.cmake",
         r"set\(TOOLCHAIN_ZEPHYR_MINIMUM_REQUIRED_VERSION\s+([0-9.]+)\)"),
        ("cmake/toolchain/zephyr/host-tools.cmake", r"set\(REQUIRED_SDK_VER\s+([0-9.]+)\)"),
    ]
    for path, rx in probes:
        text = show(commit, path)
        m = re.search(rx, text or "")
        if m:
            return m.group(1)
    return None


def ci_config(commit: str) -> dict:
    """What upstream CI used at this commit: its container image and SDK.

    GitHub Actions since 2021-11-23 (.github/workflows/twister.yaml), Buildkite
    before that (.buildkite/pipeline.yml). From 2024-01-18 the SDK version is
    also in the SDK_VERSION file, which the workflow then reads.
    """
    out: dict = {"source": None, "image": None, "sdk": None}
    for path in (".github/workflows/twister.yaml", ".buildkite/pipeline.yml"):
        text = show(commit, path)
        if not text:
            continue
        out["source"] = path
        m = re.search(r"image:\s*\"?([\w./-]*ci(?:-repo-cache)?:v[0-9.]+)", text)
        out["image"] = m.group(1) if m else None
        m = re.search(r"zephyr-sdk-([0-9][0-9.]*[0-9](?:-\d+)?)", text)
        out["sdk"] = m.group(1) if m else None
        break
    sdk_file = show(commit, "SDK_VERSION")
    if sdk_file and sdk_file.strip():
        out["sdk"] = sdk_file.strip()
        out["sdk_source"] = "SDK_VERSION"
    elif out["sdk"]:
        out["sdk_source"] = out["source"]
    return out


def twister_features(commit: str) -> dict:
    """Era-specific twister options, read from that commit's twister.

    --json-report:     per-test JSON on request, 2021-01-25 to 2022-05-13. Later
                       twister writes twister.json by default and rejects it.
    --report-filtered: list filtered pairs in the JSON reports. Absent in 2021;
                       there, -vv logs the reasons instead.
    """
    files = ["scripts/twister", "scripts/pylib/twister/twisterlib/environment.py"]
    text = "".join(show(commit, f) or "" for f in files)
    return {"json_report": '"--json-report"' in text,
            "report_filtered": '"--report-filtered"' in text}


# ---------------------------------------------------------------- boards


def board_identifiers(commit: str) -> dict:
    """The candidate board names that exist at this commit.

    Read from the board's twister metadata: <board>.yaml with an `identifier:`
    line, or, from 2026-09, a single twister.yaml listing names under
    `variants:`.
    """
    found: set[str] = set()
    for path in ls_tree(commit, "boards"):
        name = path.rsplit("/", 1)[-1]
        if not name.endswith(".yaml"):
            continue
        if not (name.startswith(("native_", "qemu_x86")) or name == "twister.yaml"):
            continue
        if name == "twister.yaml" and "native" not in path and "qemu" not in path:
            continue
        try:
            doc = yaml.safe_load(show(commit, path) or "") or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("identifier"):
            found.add(str(doc["identifier"]))
        for v in (doc.get("variants") or {}):
            found.add(str(v))
    out = {}
    for kind, names in CANDIDATE_BOARDS.items():
        out[kind] = next((n for n in names if n in found), None)
    return out


# ---------------------------------------------------------------- suites


def changed_test_files(base: str, head: str) -> list[str]:
    return git("diff", "--name-only", f"{base}..{head}", "--", "tests/").split()


def suite_root(commit: str, file_path: str, cache: dict) -> tuple[str | None, str | None]:
    """Walk up from a test file to the directory holding the suite's yaml."""
    parts = file_path.split("/")
    for depth in range(len(parts) - 1, 1, -1):
        d = "/".join(parts[:depth])
        if d not in cache:
            names = {p.rsplit("/", 1)[-1] for p in
                     git("ls-tree", "--name-only", f"{commit}:{d}", check=False).split()}
            cache[d] = next((m for m in SUITE_MARKERS if m in names), None)
        if cache[d]:
            return d, cache[d]
    return None, None


def scenarios(commit: str, root: str, marker: str) -> dict:
    """The suite's scenarios with the keys that decide where they run."""
    try:
        doc = yaml.safe_load(show(commit, f"{root}/{marker}") or "") or {}
    except yaml.YAMLError as e:
        return {"_error": f"unparsable {marker}: {e}"}
    keep = lambda d: {k: v for k, v in (d or {}).items() if k in SCENARIO_KEYS}
    return {"common": keep(doc.get("common")),
            "scenarios": {name: keep(body) for name, body in (doc.get("tests") or {}).items()}}


def changed_suites(base: str, head: str) -> dict:
    """Every suite the PR's test changes touch, read at the head commit (a new
    suite exists only there). Deleted test files are skipped: their suite may
    not exist at head."""
    cache, suites = {}, {}
    for f in changed_test_files(base, head):
        root, marker = suite_root(head, f, cache)
        if root and root not in suites:
            suites[root] = {"config": marker, **scenarios(head, root, marker)}
    return suites


# ---------------------------------------------------------------- lookups


def _load(name: str) -> dict:
    p = LOOKUP / name
    return json.loads(p.read_text()) if p.exists() else {}


def _save(name: str, data: dict) -> None:
    LOOKUP.mkdir(parents=True, exist_ok=True)
    (LOOKUP / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _http(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read().decode()
    except (urllib.error.URLError, TimeoutError):
        return None


def _image_tags() -> list[str]:
    r = subprocess.run(["git", "ls-remote", "--tags",
                        "https://github.com/zephyrproject-rtos/docker-image.git"],
                       capture_output=True, text=True, timeout=120)
    tags = {l.split("refs/tags/")[1] for l in r.stdout.splitlines()
            if "refs/tags/" in l and "^{}" not in l}
    return sorted((t for t in tags if re.fullmatch(r"v\d+(\.\d+)*", t)), key=vtuple)


def ci_image_ubuntu(image: str) -> dict:
    """CI image (e.g. ghcr.io/.../ci-repo-cache:v0.26.13.20240601) -> its
    Ubuntu release and therefore its Python.

    Tags carry a date suffix for the repo-cache flavour; that is dropped. Some
    tags used by old CI configs were never pushed to the docker-image repo
    (ci:v0.11.15), so the nearest older tag that exists stands in, and the
    entry records which one it was.
    """
    tag = "v" + ".".join(re.findall(r"\d+", image.split(":")[-1])[:3])
    table = _load("ci_images.json")
    if tag in table:
        return table[tag]
    resolved, ubuntu = None, None
    candidates = [tag] + [t for t in reversed(_image_tags()) if vtuple(t) < vtuple(tag)]
    for t in candidates[:15]:
        for f in ("Dockerfile.base", "Dockerfile"):
            text = _http(f"https://raw.githubusercontent.com/zephyrproject-rtos/docker-image/{t}/{f}")
            m = re.search(r"(?m)^FROM ubuntu:(\d+\.\d+)", text or "")
            if m:
                resolved, ubuntu = t, m.group(1)
                break
        if ubuntu:
            break
    entry = {"ubuntu": ubuntu, "python": UBUNTU_PYTHON.get(ubuntu or ""),
             "resolved_tag": resolved, "exact": resolved == tag}
    table[tag] = entry
    _save("ci_images.json", table)
    return entry


def sdk_releases(refresh: bool = False) -> dict:
    """SDK version -> {published, minimal}: the format of its linux-aarch64
    minimal bundle (tar.xz, tar.gz), or null when ARM64 has none."""
    table = _load("sdk_releases.json")
    if table and not refresh:
        return table
    r = subprocess.run(["gh", "api", "--paginate",
                        "repos/zephyrproject-rtos/sdk-ng/releases?per_page=100"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"gh api sdk-ng releases: {r.stderr.strip()[:200]}")
    # --paginate concatenates JSON arrays; split them back apart.
    releases = json.loads("[" + re.sub(r"\]\s*\[", "],[", r.stdout.strip()) + "]")
    table = {}
    for rel in (x for page in releases for x in page):
        if rel.get("prerelease") or rel.get("draft"):
            continue
        ver = rel["tag_name"].lstrip("v")
        names = {a["name"] for a in rel.get("assets", [])}
        fmt = next((ext for ext in ("tar.xz", "tar.gz")
                    if f"zephyr-sdk-{ver}_linux-aarch64_minimal.{ext}" in names), None)
        table[ver] = {"published": rel.get("published_at", "")[:10], "minimal": fmt}
    _save("sdk_releases.json", table)
    return table


# ---------------------------------------------------------------- choices


def choose_python(ci_python: str | None, minimum: str | None) -> str:
    """CI's Python, never below the tree's minimum or our floor."""
    options = [v for v in (ci_python, minimum, PYTHON_FLOOR) if v]
    return max(options, key=vtuple)


def choose_sdk(ci_sdk: str | None, minimum: str | None) -> tuple[str, str]:
    """The SDK CI used if ARM64 has a minimal bundle for it, else the nearest
    newer release that does. Returns (version, archive format)."""
    table = sdk_releases()
    target = ci_sdk or minimum or "0.16.8"
    usable = sorted((v for v, e in table.items() if e.get("minimal")), key=vtuple)
    for v in usable:
        if vtuple(v) >= vtuple(target) and (not minimum or vtuple(v) >= vtuple(minimum)):
            return v, table[v]["minimal"]
    raise RuntimeError(f"no ARM64 SDK at or above {target}")


# ---------------------------------------------------------------- facts


def twister_facts(base: str, head: str) -> dict:
    """Everything the twister pipeline derives from a PR's commits."""
    ensure_commits(base, head)
    ci = ci_config(base)
    image = ci_image_ubuntu(ci["image"]) if ci.get("image") else {}
    py_min = python_min(base)
    sdk_minimum = sdk_min(base)
    sdk, sdk_fmt = choose_sdk(ci.get("sdk"), sdk_minimum)
    return {
        "base_commit": base,
        "head_commit": head,
        "base_date": commit_date(base),
        "python_min": py_min,
        "sdk_min": sdk_minimum,
        "ci": {**ci, "ubuntu": image.get("ubuntu"), "python": image.get("python"),
               "image_resolved_tag": image.get("resolved_tag")},
        "python": choose_python(image.get("python"), py_min),
        "sdk": sdk,
        "sdk_archive": sdk_fmt,
        "boards": board_identifiers(base),
        "twister": twister_features(base),
        "suites": changed_suites(base, head),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("facts", help="print the twister facts for one enriched PR")
    f.add_argument("pr", type=int)
    sub.add_parser("refresh-lookups", help="re-download the SDK release table")
    args = p.parse_args()

    if args.cmd == "refresh-lookups":
        t = sdk_releases(refresh=True)
        print(f"{len(t)} SDK releases, {sum(1 for e in t.values() if e['minimal'])} with ARM64 minimal")
        return
    rec = json.loads((ENRICHED / f"{args.pr}.json").read_text())
    print(json.dumps(twister_facts(rec["base_commit"], rec["head_commit"]), indent=2))


if __name__ == "__main__":
    main()
