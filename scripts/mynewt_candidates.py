#!/usr/bin/env python3
"""Discover and enrich Apache Mynewt unit-test PR candidates.

    python scripts/mynewt_candidates.py selftest
    python scripts/mynewt_candidates.py fetch
    python scripts/mynewt_candidates.py fetch --pr 3299 251
    python scripts/mynewt_candidates.py filter
    python scripts/mynewt_candidates.py enrich
    python scripts/mynewt_candidates.py enrich --pr 3299 --refresh

`filter_candidates.py <command> --repo mynewt` runs the same commands.

A candidate is a merged PR that changes a file inside a package whose pkg.yml,
at that PR's revision, says `pkg.type: unittest` (or inside the shared `lib`
package of testcases next to such packages, e.g. sys/log/full/selftest/util). That is what `newt test`
selects, so it covers `selftest/` (since March 2019), the older `test/` and
`test-*`, nested packages and any other directory a unit-test package lives in.
Merged, not a revert (a heuristic), and a complete file list (recovered from
git when GitHub truncates it) are the only other requirements.

No linked issue and no production change is required. Issue and JIRA
references are recorded as evidence; a PR with no change outside its unit-test
packages is kept and flagged `test_only` (the fix may be an earlier PR).

    fetch   cheap, wide    -- one GraphQL record per PR: text, labels, commits,
                              closing issues and the changed-file list.
    filter  free, instant  -- classify from the cache and the local clone;
                              writes candidates.jsonl and FUNNEL.md.
    enrich  costly, narrow -- the record the triage reads: per-file diffs,
                              discussion, linked issues, and every file of each
                              changed unit-test package at base and at head.

Code is read from a local clone (candidates/mynewt.git, or
EMBEDEVAL_MYNEWT_CLONE), not the API. It must hold refs/pull/*/head so the
PRs' own commits exist; `fetch` brings them in:

    git clone --bare https://github.com/apache/mynewt-core.git candidates/mynewt.git

Requires the `gh` CLI, authenticated. Read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)

REPO = "apache/mynewt-core"
OWNER, NAME = REPO.split("/")
ROOT = Path(__file__).resolve().parent.parent
DATA = layout.candidates() / "mynewt"
CACHE = DATA / "cache"
CANDIDATES = DATA / "candidates.jsonl"
FUNNEL = DATA / "FUNNEL.md"
ENRICHED = DATA / "enriched"
CLONE = Path(os.environ.get("EMBEDEVAL_MYNEWT_CLONE") or layout.candidates() / "mynewt.git")

# Hand-validated instances that must survive, and PRs that must not.
KNOWN_GOOD = {2809: "selftest, issue", 3299: "selftest, issue", 3680: "selftest, issue",
              251: "old test/ layout, JIRA-era regression",
              2157: "selftest, no linked issue", 1702: "triage decides",
              3381: "triage decides", 3509: "native regression, no linked issue"}
KNOWN_FILTERED = {3708: "no unit-test package", 3707: "no unit-test package",
                  2511: "demo apps", 821: "demo apps"}

# The `selftest` rename of the older test/ packages (7f0fd8933, 2019-03-12).
SELFTEST_RENAME = "2019-03-12"
# Before this, hw/bsp/native built 32-bit only (x86-64 support landed
# 2023-07; the toolchain default switched 2024-04-09): builds need -m32.
M32_UNTIL = "2024-04-09"

FILE_CEILING = 3000
BATCH = 10

PR_FIELDS = """
  number title body state merged mergedAt closedAt
  baseRefName baseRefOid headRefName headRefOid
  mergeCommit { oid }
  labels(first: 50) { nodes { name } }
  closingIssuesReferences(first: 50) { nodes { number repository { nameWithOwner } } }
  commits(first: 100) {
    totalCount
    nodes { commit { oid messageHeadline messageBody } }
  }
  files(first: 100) {
    pageInfo { hasNextPage endCursor }
    nodes { path additions deletions changeType }
  }
"""

JIRA = re.compile(r"\bMYNEWT-\d+\b", re.IGNORECASE)
ISSUE_URL = re.compile(r"https://github\.com/apache/mynewt-core/issues/(\d+)\b", re.IGNORECASE)
HASH_REF = re.compile(r"(?<![\w/&])#(\d+)\b")
CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
NON_PRODUCTION_PREFIXES = ("docs/", ".github/")
NON_PRODUCTION_SUFFIXES = (".md", ".rst", ".txt")


# ---------------------------------------------------------------- github


def gh(path: str, retries: int = 4):
    for attempt in range(retries):
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        error = proc.stderr.lower()
        if "404" in error or "not found" in error:
            return None
        if "rate limit" in error or "submitted too quickly" in error:
            time.sleep(60 * (attempt + 1))
        elif any(s in error for s in ("http 5", "unexpected eof", "connection reset", "timed out")):
            time.sleep(attempt + 1)
        else:
            raise RuntimeError(f"gh api {path}: {proc.stderr.strip()[:300]}")
    raise RuntimeError(f"gh api {path}: failed after {retries} attempts")


def gh_paged(path: str, cap: int = 3000) -> list:
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while len(out) < cap:
        batch = gh(f"{path}{sep}per_page=100&page={page}")
        if not batch:
            break
        out += batch
        if len(batch) < 100:
            break
        page += 1
    return out


def gql(query: str, variables: dict | None = None, retries: int = 5) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in (variables or {}).items():
        cmd += ["-F", f"{key}={value}"]
    for attempt in range(retries):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            if data.get("errors") and not data.get("data"):
                raise RuntimeError(f"graphql: {data['errors']}")
            return data
        error = proc.stderr.lower()
        if "rate limit" in error or "submitted too quickly" in error or "abuse" in error:
            time.sleep(60 * (attempt + 1))
        elif any(s in error for s in ("502", "503", "504", "timeout", "timed out", "eof", "reset")):
            time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"gh graphql: {proc.stderr.strip()[:300]}")
    raise RuntimeError(f"gh graphql failed after {retries} attempts")


def rate_guard() -> None:
    proc = subprocess.run(["gh", "api", "rate_limit"], capture_output=True, text=True)
    if proc.returncode != 0:
        return
    graphql = json.loads(proc.stdout)["resources"]["graphql"]
    if graphql["remaining"] < 200:
        time.sleep(max(graphql["reset"] - int(time.time()), 0) + 5)


def list_prs() -> list[dict]:
    """Every PR number with its state."""
    query = """query($owner: String!, $name: String!, $after: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
          pageInfo { hasNextPage endCursor }
          nodes { number state merged }
        }
      }
    }"""
    rows, after = [], ""
    while True:
        variables = {"owner": OWNER, "name": NAME}
        if after:
            variables["after"] = after
        page = gql(query, variables)["data"]["repository"]["pullRequests"]
        rows += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return rows
        after = page["pageInfo"]["endCursor"]


def more_files(number: int, after: str) -> list[dict]:
    query = """query($owner: String!, $name: String!, $number: Int!, $after: String) {
      repository(owner: $owner, name: $name) { pullRequest(number: $number) {
        files(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes { path additions deletions changeType }
        } } } }"""
    out = []
    while after:
        page = gql(query, {"owner": OWNER, "name": NAME, "number": number, "after": after}
                   )["data"]["repository"]["pullRequest"]["files"]
        out += page["nodes"]
        after = page["pageInfo"]["endCursor"] if page["pageInfo"]["hasNextPage"] else ""
    return out


def to_record(node: dict) -> dict:
    files = node["files"]["nodes"]
    if node["files"]["pageInfo"]["hasNextPage"]:
        files = files + more_files(node["number"], node["files"]["pageInfo"]["endCursor"])
    return {
        "number": node["number"],
        "title": node["title"],
        "body": node["body"] or "",
        "state": node["state"],
        "merged": node["merged"],
        "merged_at": node["mergedAt"],
        "closed_at": node["closedAt"],
        "base_ref": node["baseRefName"],
        "base_sha": node["baseRefOid"],
        "head_ref": node["headRefName"],
        "head_sha": node["headRefOid"],
        "merge_commit_sha": (node["mergeCommit"] or {}).get("oid"),
        "labels": [l["name"] for l in node["labels"]["nodes"]],
        "closing_issues": [
            n["number"] for n in node["closingIssuesReferences"]["nodes"]
            if (n.get("repository") or {}).get("nameWithOwner") == REPO],
        "n_commits": node["commits"]["totalCount"],
        "commits": [{"sha": c["commit"]["oid"], "headline": c["commit"]["messageHeadline"],
                     "body": c["commit"]["messageBody"]} for c in node["commits"]["nodes"]],
        # GraphQL has no previous path for a rename; filled in from git.
        "files": [{"filename": f["path"], "additions": f["additions"],
                   "deletions": f["deletions"], "status": f["changeType"].lower()}
                  for f in files],
        # Only true at GitHub's hard ceiling, where the list is really incomplete.
        "files_truncated": len(files) >= FILE_CEILING,
    }


def fetch_batch(numbers: list[int]) -> None:
    body = "\n".join(f"p{n}: pullRequest(number: {n}) {{ {PR_FIELDS} }}" for n in numbers)
    data = gql(f'query {{ repository(owner: "{OWNER}", name: "{NAME}") {{ {body} }} }}'
               )["data"]["repository"]
    CACHE.mkdir(parents=True, exist_ok=True)
    for n in numbers:
        if data.get(f"p{n}"):
            (CACHE / f"{n}.json").write_text(json.dumps(to_record(data[f"p{n}"])))


def fetch_pr(number: int) -> dict | None:
    """One cached PR record, fetched on first use."""
    path = CACHE / f"{number}.json"
    if not path.exists():
        fetch_batch([number])
    return json.loads(path.read_text()) if path.exists() else None


# ---------------------------------------------------------------- git


class Git:
    """The local clone, read through one long-lived `git cat-file --batch`."""

    def __init__(self) -> None:
        if not CLONE.exists():
            sys.exit(f"no clone at {CLONE}; run: git clone --bare "
                     f"https://github.com/{REPO}.git {CLONE}")
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(["git", "-C", str(CLONE), "cat-file", "--batch"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, spec: str) -> bytes | None:
        """The object named by `<rev>:<path>`, or None if it does not exist."""
        with self.lock:
            self.proc.stdin.write(spec.encode() + b"\n")
            self.proc.stdin.flush()
            header = self.proc.stdout.readline().split()
            if len(header) < 3:          # "<spec> missing"
                return None
            size = int(header[2])
            data = self.proc.stdout.read(size + 1)[:-1]
        return data if header[1] == b"blob" else None

    def text(self, rev: str, path: str) -> str | None:
        data = self.read(f"{rev}:{path}")
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", "replace")

    def has(self, sha: str) -> bool:
        return git("cat-file", "-e", f"{sha}^{{commit}}", check=False) is not None


def git(*args: str, check: bool = True) -> str | None:
    r = subprocess.run(["git", "-C", str(CLONE), *args], capture_output=True, text=True)
    if r.returncode != 0:
        if check:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
        return None
    return r.stdout


_GIT: Git | None = None


def repo_git() -> Git:
    global _GIT
    if _GIT is None:
        _GIT = Git()
    return _GIT


def ensure_commits(*shas: str) -> None:
    """Fetch commits the clone lacks (a PR branch deleted after merge)."""
    missing = [s for s in shas if s and not repo_git().has(s)]
    if missing:
        git("fetch", "-q", "origin", *missing, check=False)


def base_commit(pr: dict) -> str | None:
    """The state before the PR: the first parent of its first commit."""
    if not pr["commits"]:
        return None
    out = git("rev-parse", "--verify", "-q", f"{pr['commits'][0]['sha']}^", check=False)
    return out.strip() if out else None


def commit_date(sha: str) -> str | None:
    out = git("show", "-s", "--format=%cI", sha, check=False)
    return out.strip() if out else None


# ---------------------------------------------------------------- packages


def pkg_type(text: str) -> str | None:
    m = re.search(r"^pkg\.type:\s*['\"]?([\w-]+)", text, re.MULTILINE)
    return m.group(1) if m else None


def pkg_deps(text: str) -> list[str]:
    """Package names under pkg.deps* (including conditional keys)."""
    deps, inside = [], False
    for line in text.splitlines():
        if re.match(r"^pkg\.deps\b[^:]*:", line):
            inside = True
            m = re.search(r"['\"]?(@[\w.-]+/[\w./-]+|[\w./-]+/[\w./-]+)['\"]?", line.split(":", 1)[1])
            if m:
                deps.append(m.group(1))
        elif re.match(r"^\S", line) and not line.startswith("#"):
            inside = False
        elif inside:
            m = re.match(r"\s*-\s*['\"]?([^'\"#\s]+)", line)
            if m:
                deps.append(m.group(1))
    return deps


_PKG_CACHE: dict[tuple[str, str], str | None] = {}


def pkg_yml(rev: str, directory: str) -> str | None:
    key = (rev, directory)
    if key not in _PKG_CACHE:
        _PKG_CACHE[key] = repo_git().text(rev, f"{directory}/pkg.yml" if directory else "pkg.yml")
    return _PKG_CACHE[key]


def unittest_root(path: str, rev: str) -> str | None:
    """The nearest enclosing package that is `pkg.type: unittest` at `rev`."""
    parts = path.split("/")[:-1]
    for depth in range(len(parts), 0, -1):
        directory = "/".join(parts[:depth])
        text = pkg_yml(rev, directory)
        if text is not None and pkg_type(text) == "unittest":
            return directory
    return None


TEST_DIR = re.compile(r"^(self)?test(-.*)?$")
_SIBLING_CACHE: dict[tuple[str, str], bool] = {}


def support_root(path: str, rev: str) -> str | None:
    """The package of a shared test-support library (`selftest/util`).

    Such a package is `pkg.type: lib`, but it sits in a selftest/test directory
    next to the unit-test packages that link it, and its testcases are what
    they run. Returns its directory, or None.
    """
    parts = path.split("/")[:-1]
    for i, part in enumerate(parts):
        if TEST_DIR.match(part):
            test_dir = "/".join(parts[:i + 1])
            key = (rev, test_dir)
            if key not in _SIBLING_CACHE:
                tree = git("ls-tree", "-d", "--name-only", rev, test_dir + "/", check=False) or ""
                _SIBLING_CACHE[key] = any(
                    (t := pkg_yml(rev, d)) is not None and pkg_type(t) == "unittest"
                    for d in tree.split())
            if not _SIBLING_CACHE[key]:
                return None
            for depth in range(len(parts), i, -1):
                directory = "/".join(parts[:depth])
                if pkg_yml(rev, directory) is not None:
                    return directory
            return None
    return None


def renamed_from(pr: dict, base: str, head: str) -> dict[str, str]:
    """{new path: old path} for the PR's renames; GraphQL does not say."""
    if not any(f["status"] == "renamed" for f in pr["files"]):
        return {}
    out = git("diff", "--name-status", "-M", base, head, check=False) or ""
    pairs = {}
    for line in out.splitlines():
        cols = line.split("\t")
        if cols[0].startswith("R") and len(cols) == 3:
            pairs[cols[2]] = cols[1]
    return pairs


def recovered_files(pr: dict, base: str, head: str) -> list[dict]:
    """The file list from git, for a PR past GitHub's 3000-file ceiling."""
    out = git("diff", "--numstat", "-M", base, head) or ""
    status = {l.split("\t")[-1]: l[0] for l in
              (git("diff", "--name-status", "-M", base, head) or "").splitlines()}
    names = {"A": "added", "D": "removed", "M": "modified", "R": "renamed"}
    files = []
    for line in out.splitlines():
        add, dele, name = line.split("\t", 2)
        files.append({"filename": name.split(" => ")[-1].strip("{}"),
                      "additions": int(add) if add.isdigit() else 0,
                      "deletions": int(dele) if dele.isdigit() else 0,
                      "status": names.get(status.get(name, "M"), "modified")})
    return files


# ---------------------------------------------------------------- analysis


def is_revert(pr: dict) -> bool:
    """Heuristic: a revert PR's title or body says so, or every commit does."""
    if pr["title"].lower().startswith("revert") or "this reverts commit" in pr["body"].lower():
        return True
    return bool(pr["commits"]) and all(
        c["headline"].lower().startswith("revert") for c in pr["commits"])


def is_production(path: str) -> bool:
    return not path.startswith(NON_PRODUCTION_PREFIXES) \
        and not path.endswith(NON_PRODUCTION_SUFFIXES)


def issue_refs(pr: dict) -> tuple[list[int], list[int]]:
    """(linked, mentioned): closing references and issue URLs, then every bare #N."""
    body = pr["body"]
    target = rf"(?:#|{re.escape(REPO)}#|https://github\.com/{re.escape(REPO)}/issues/)(\d+)\b"
    linked = list(pr["closing_issues"])
    linked += [int(m.group(1)) for m in
               re.finditer(rf"(?i)\b{CLOSING_KEYWORD}\s*:?\s+{target}", body)]
    linked += [int(m.group(1)) for m in ISSUE_URL.finditer(body)]
    linked = list(dict.fromkeys(linked))
    mentioned = linked + [int(m.group(1)) for m in HASH_REF.finditer(pr["title"] + "\n" + body)
                          if int(m.group(1)) != pr["number"]]
    return linked, list(dict.fromkeys(mentioned))


def jira_refs(pr: dict) -> list[str]:
    text = [pr["title"], pr["body"]] + [c["headline"] + "\n" + c["body"] for c in pr["commits"]]
    return list(dict.fromkeys(m.upper() for m in JIRA.findall("\n".join(text))))


# Packages that need real hardware or a radio. A static hint only: it reads
# the unit-test package's dependency closure, not the build.
HARDWARE_PACKAGE = re.compile(
    r"^(hw/mcu/(?!native)|hw/bsp/(?!native)|hw/radio|hw/drivers/(sensors|display|chg_ctrl|lcd)"
    r"|.*nimble/(controller|drivers))")


def needs_hw_hint(roots: list[str], rev: str) -> list[str]:
    """Hardware-only packages in the dependency closure of the unit-test packages."""
    seen, queue, hits = set(), list(roots), set()
    while queue:
        directory = queue.pop()
        if directory in seen:
            continue
        seen.add(directory)
        text = pkg_yml(rev, directory)
        if text is None:
            continue
        for dep in pkg_deps(text):
            if dep.startswith("@") and not dep.startswith("@apache-mynewt-core/"):
                # another repository (nimble, ...): judge by name only
                path = dep.split("/", 1)[1]
                if HARDWARE_PACKAGE.match(path):
                    hits.add(dep)
                continue
            path = re.sub(r"^@apache-mynewt-core/", "", dep)
            if HARDWARE_PACKAGE.match(path):
                hits.add(path)
            elif path not in seen:
                queue.append(path)
    return sorted(hits)


def analyse(pr: dict) -> dict:
    """Everything the candidate stage knows about a merged PR."""
    base, head = base_commit(pr), pr["head_sha"]
    ensure_commits(head, base)
    files = pr["files"]
    if pr["files_truncated"] and base:
        files = recovered_files(pr, base, head)
    old_name = renamed_from(pr, base, head) if base else {}
    roots, support, tests, source = {}, {}, [], []
    for f in files:
        name = f["filename"]
        root = None
        if f["status"] in ("removed", "deleted"):
            root = base and unittest_root(name, base)
        else:
            root = unittest_root(name, head)
            if not root and name in old_name and base:
                root = unittest_root(old_name[name], base)
        if root:
            roots[root] = None
            tests.append(name)
        elif (extra := support_root(name, head)):
            support[extra] = None
            tests.append(name)
        elif is_production(name):
            source.append(name)
    linked, mentioned = issue_refs(pr)
    base_date = commit_date(base) if base else None
    layouts = sorted({"test" if Path(r).name.startswith("test") else "selftest" for r in roots})
    return {
        "number": pr["number"],
        "title": pr["title"],
        "url": f"https://github.com/{REPO}/pull/{pr['number']}",
        "merged_at": pr["merged_at"],
        "base_commit": base,
        "head_commit": head,
        "merge_commit_sha": pr["merge_commit_sha"],
        "labels": pr["labels"],
        "n_files": len(files),
        "files_recovered_from_git": bool(pr["files_truncated"]),
        "unittest_pkgs": sorted(roots),
        "test_support_pkgs": sorted(set(support) - set(roots)),
        "test_files": tests,
        "source_files": source,
        "has_production_change": bool(source),
        "test_only": bool(tests) and not source,
        "github_issues": linked,
        "github_issue_mentions": mentioned,
        "jira": jira_refs(pr),
        "era": {
            "layout": layouts,                      # test/ before 2019-03, selftest/ after
            "base_date": base_date,
            "needs_m32": bool(base_date and base_date[:10] < M32_UNTIL),
        },
        "needs_hw": needs_hw_hint(sorted(roots), head),
    }


def classify(pr: dict) -> tuple[bool, str]:
    """Apply the hard filters. Returns (kept, reason)."""
    if not pr.get("merged"):
        return False, "not merged"
    if is_revert(pr):
        return False, "revert"
    if pr["files_truncated"] and not base_commit(pr):
        return False, "file list incomplete and not recoverable from git"
    found = analyse(pr)
    if not found["unittest_pkgs"] and not found["test_support_pkgs"]:
        return False, "touches no pkg.type: unittest package or test-support library"
    return True, "ok"


# ---------------------------------------------------------------- enrichment


def fetch_suite(root: str, rev: str) -> dict:
    """Every file under a package at `rev`, keyed by its path inside the package."""
    out = {}
    listing = git("ls-tree", "-r", "-l", "-z", rev, "--", root + "/", check=False) or ""
    for line in listing.split("\0"):
        if not line:
            continue
        meta, name = line.split("\t", 1)
        _, kind, _, size = meta.split()
        if kind != "blob":
            continue
        content = repo_git().text(rev, name)
        out[name[len(root) + 1:]] = {"size": int(size) if size.isdigit() else None,
                                     "content": content}
    return out


def issue_record(number: int, via: str) -> dict | None:
    issue = gh(f"repos/{REPO}/issues/{number}")
    if not issue or "pull_request" in issue:
        return None
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue.get("body") or "",
        "state": issue["state"],
        "labels": [l["name"] for l in issue.get("labels", [])],
        "linked_by": via,
        "comments": [{"author": (c.get("user") or {}).get("login"), "body": c.get("body") or ""}
                     for c in gh_paged(f"repos/{REPO}/issues/{number}/comments")],
    }


def fetch_full(number: int, refresh: bool = False) -> dict | None:
    """The record the triage reads, from the cache, the clone and GitHub."""
    out = ENRICHED / f"{number}.json"
    if out.exists() and not refresh:
        return json.loads(out.read_text())
    pr = fetch_pr(number)
    if not pr or not pr["merged"]:
        return None
    summary = analyse(pr)
    base, head = summary["base_commit"], summary["head_commit"]

    # The diff, with `patch`: absent for binary or oversized files.
    files = gh_paged(f"repos/{REPO}/pulls/{number}/files", cap=FILE_CEILING)
    if not files or len(files) >= FILE_CEILING:
        files = [{**f, "patch": None} for f in
                 (recovered_files(pr, base, head) if base else pr["files"])]

    linked = []
    for num in summary["github_issues"]:
        if rec := issue_record(num, "closing reference"):
            linked.append(rec)
    mentions = [n for n in summary["github_issue_mentions"] if n not in summary["github_issues"]]
    for num in mentions:
        if rec := issue_record(num, "mentioned in the PR text"):
            linked.append(rec)

    suites, suites_base = {}, {}
    for root in summary["unittest_pkgs"] + summary["test_support_pkgs"]:
        suites[root] = {"config": "pkg.yml", "files": fetch_suite(root, head)}
        if base:
            before = fetch_suite(root, base)
            if before:
                suites_base[root] = {"config": "pkg.yml", "files": before}

    record = {
        "project": "mynewt",
        "number": number,
        "title": pr["title"],
        "body": pr["body"],
        "url": summary["url"],
        "state": pr["state"].lower(),
        "merged_at": pr["merged_at"],
        "merge_commit_sha": pr["merge_commit_sha"],
        "base_sha": pr["base_sha"],
        "labels": pr["labels"],
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "files": [{"filename": f["filename"], "status": f["status"],
                   "additions": f["additions"], "deletions": f["deletions"],
                   "patch": f.get("patch"),
                   **({"previous_filename": f["previous_filename"]}
                      if f.get("previous_filename") else {})} for f in files],
        "files_truncated": pr["files_truncated"],
        "commits": [{"sha": c["sha"],
                     "message": c["headline"] + ("\n\n" + c["body"] if c["body"] else "")}
                    for c in pr["commits"]],
        "base_commit": base,
        "head_commit": head,
        "comments": [{"author": (c.get("user") or {}).get("login"), "body": c.get("body") or ""}
                     for c in gh_paged(f"repos/{REPO}/issues/{number}/comments")],
        "review_comments": [
            {"author": (c.get("user") or {}).get("login"), "path": c.get("path"),
             "line": c.get("line"), "diff_hunk": c.get("diff_hunk"), "body": c.get("body") or ""}
            for c in gh_paged(f"repos/{REPO}/pulls/{number}/comments")],
        "reviews": [
            {"author": (r.get("user") or {}).get("login"), "state": r.get("state"),
             "body": r.get("body") or ""}
            for r in gh_paged(f"repos/{REPO}/pulls/{number}/reviews")
            if (r.get("body") or "").strip() or r.get("state") != "COMMENTED"],
        "linked_issues": linked,
        # The candidate stage's facts about the PR.
        "candidate": {k: summary[k] for k in (
            "unittest_pkgs", "test_support_pkgs", "test_files", "source_files", "has_production_change",
            "test_only", "github_issues", "github_issue_mentions", "jira", "era", "needs_hw")},
        # Whole changed unit-test packages: at the PR's last commit, and before it.
        "test_suites": suites,
        "test_suites_base": suites_base,
    }
    ENRICHED.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2))
    return record


# ---------------------------------------------------------------- commands


def funnel_text(prs: list[dict], rows: list[dict], steps: list[tuple[str, int]]) -> str:
    era = {"test/ layout (pre-2019-03)": 0, "selftest/ layout": 0, "both": 0}
    for r in rows:
        kinds = r["era"]["layout"]
        era["both" if len(kinds) > 1 else "test/ layout (pre-2019-03)" if kinds == ["test"]
            else "selftest/ layout"] += 1
    m32 = sum(r["era"]["needs_m32"] for r in rows)
    lines = ["# Mynewt candidate funnel", "",
             f"Generated by `scripts/mynewt_candidates.py filter` from {len(prs)} cached "
             f"PRs of {REPO}.", "",
             "| step | PRs left | dropped |", "|---|---:|---:|"]
    previous = None
    for name, count in steps:
        lines.append(f"| {name} | {count} | {'' if previous is None else previous - count} |")
        previous = count
    lines += ["", "## Candidates", "",
              f"- candidates: {len(rows)}",
              f"- with a production change: {sum(r['has_production_change'] for r in rows)}",
              f"- test_only (no change outside unit-test packages): "
              f"{sum(r['test_only'] for r in rows)}",
              f"- era: {', '.join(f'{k} {v}' for k, v in era.items())}",
              f"- base before {M32_UNTIL} (needs -m32): {m32}; later: {len(rows) - m32}",
              f"- needs_hw hint (hardware package in the dependency closure): "
              f"{sum(bool(r['needs_hw']) for r in rows)}",
              f"- with a linked GitHub issue: {sum(bool(r['github_issues']) for r in rows)}; "
              f"mentioning any #N: {sum(bool(r['github_issue_mentions']) for r in rows)}; "
              f"with a JIRA ref: {sum(bool(r['jira']) for r in rows)}",
              f"- unit-test packages only via a shared test-support library: "
              f"{sum(not r['unittest_pkgs'] for r in rows)}",
              f"- file list recovered from git: "
              f"{sum(r['files_recovered_from_git'] for r in rows)}", ""]
    return "\n".join(lines)


def cmd_filter() -> None:
    files = sorted(CACHE.glob("*.json"), key=lambda p: int(p.stem))
    if not files:
        sys.exit("No cached PRs. Run `fetch` first.")
    prs = [json.loads(f.read_text()) for f in files]
    merged = [p for p in prs if p["merged"]]
    steps = [("PRs", len(prs)), ("merged", len(merged))]
    pool = [p for p in merged if not is_revert(p)]
    steps.append(("not a revert", len(pool)))
    pool = [p for p in pool if not p["files_truncated"] or base_commit(p)]
    steps.append(("complete file list (recovered from git if truncated)", len(pool)))
    rows = [a for a in map(analyse, pool) if a["unittest_pkgs"] or a["test_support_pkgs"]]
    steps.append(("changes a pkg.type: unittest package or its test-support library", len(rows)))
    CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATES.write_text("".join(json.dumps(r) + "\n" for r in rows))
    text = funnel_text(prs, rows, steps)
    FUNNEL.write_text(text)
    print(text)
    print(f"  written to {layout.show(CANDIDATES)} and {layout.show(FUNNEL)}")


def cmd_fetch(args) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    if args.pr:
        numbers = args.pr
    else:
        rows = list_prs()
        numbers = [r["number"] for r in rows
                   if getattr(args, "refresh", False) or not (CACHE / f"{r['number']}.json").exists()]
    print(f"{len(numbers)} PRs to fetch")
    for i in range(0, len(numbers), BATCH):
        if (i // BATCH) % 20 == 0:
            rate_guard()
        chunk = numbers[i:i + BATCH]
        try:
            fetch_batch(chunk)
        except RuntimeError:
            for n in chunk:
                fetch_batch([n])
        print(f"  fetched through #{chunk[-1]}  ({min(i + BATCH, len(numbers))}/{len(numbers)})",
              flush=True)
    print("Fetching the PRs' own commits into the clone ...")
    git("fetch", "-q", "origin", "+refs/pull/*/head:refs/pull/*", check=False)


def cmd_enrich(args) -> None:
    if args.pr:
        numbers = args.pr
    else:
        if not CANDIDATES.exists():
            sys.exit("No candidates.jsonl. Run `filter` first.")
        numbers = [json.loads(l)["number"] for l in CANDIDATES.read_text().splitlines()]
        if args.limit:
            numbers = numbers[:args.limit]
    print(f"Enriching {len(numbers)} candidates, {args.parallel} at a time.\n")
    ok = 0
    with ThreadPoolExecutor(args.parallel) as pool:
        futures = {pool.submit(fetch_full, n, args.refresh): n for n in numbers}
        for i, future in enumerate(as_completed(futures), 1):
            n = futures[future]
            try:
                rec = future.result()
            except Exception as e:  # one PR's failure must not stop the rest
                print(f"  [{i}/{len(numbers)}] {n}  FAILED {type(e).__name__}: {e}", flush=True)
                continue
            if not rec:
                print(f"  [{i}/{len(numbers)}] {n}  FAILED (not merged or not found)", flush=True)
                continue
            ok += 1
            suites = rec["test_suites"]
            print(f"  [{i}/{len(numbers)}] {n}  {sum(1 for f in rec['files'] if f.get('patch'))}"
                  f"/{len(rec['files'])} files with diff, {len(rec['commits'])} commits, "
                  f"{len(rec['linked_issues'])} issues, {len(suites)} package(s)/"
                  f"{sum(len(s['files']) for s in suites.values())} files", flush=True)
    print(f"\n  {ok}/{len(numbers)} enriched  ->  {layout.show(ENRICHED)}")


def cmd_selftest() -> None:
    """Known-good PRs must be kept, known-filtered ones rejected."""
    failures = []
    print(f"Checking {len(KNOWN_GOOD)} known-good and {len(KNOWN_FILTERED)} known-filtered "
          f"Mynewt PRs.\n")
    for expected, table in ((True, KNOWN_GOOD), (False, KNOWN_FILTERED)):
        for n, why in table.items():
            pr = fetch_pr(n)
            kept, reason = classify(pr) if pr else (not expected, "could not fetch")
            print(f"  [{'KEEP  ' if kept else 'REJECT'}] {n}  {reason} "
                  f"(expected {'KEEP' if expected else 'REJECT'}: {why})")
            if kept != expected:
                failures.append((n, reason))
    print()
    if failures:
        sys.exit(f"  {len(failures)} PRs were classified wrongly: {failures}")
    print("  All as expected. Filters are safe.")


def dispatch(args) -> None:
    command = getattr(args, "command", None) or args.cmd
    if command == "selftest":
        cmd_selftest()
    elif command == "fetch":
        cmd_fetch(args)
    elif command == "filter":
        cmd_filter()
    else:
        cmd_enrich(args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest")
    fetch = sub.add_parser("fetch", help="cache every PR (or --pr N) and the PR commits")
    fetch.add_argument("--pr", type=int, nargs="+")
    fetch.add_argument("--refresh", action="store_true")
    sub.add_parser("filter", help="classify the cache; write candidates.jsonl and FUNNEL.md")
    enrich = sub.add_parser("enrich", help="full context for the candidates")
    enrich.add_argument("--pr", type=int, nargs="+")
    enrich.add_argument("--limit", type=int)
    enrich.add_argument("--parallel", type=int, default=4)
    enrich.add_argument("--refresh", action="store_true")
    dispatch(parser.parse_args())


if __name__ == "__main__":
    main()
