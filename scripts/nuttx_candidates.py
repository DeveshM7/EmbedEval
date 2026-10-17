#!/usr/bin/env python3
"""Discover linked NuttX fix and ostest PR leads from either repository.

    python scripts/nuttx_candidates.py selftest
    python scripts/nuttx_candidates.py fetch --since 2024-01-01 --until 2024-03-31
    python scripts/nuttx_candidates.py pairs --since 2024-01-01 --until 2024-03-31
    python scripts/nuttx_candidates.py pairs --kernel-pr 11898
    python scripts/nuttx_candidates.py filter
    python scripts/nuttx_candidates.py enrich --pr 2329
    python scripts/nuttx_candidates.py enrich-apps --ostest-scan
    python scripts/nuttx_candidates.py enrich-apps --pr 3790 2327
    python scripts/nuttx_candidates.py enrich-kernel --pr 20112

Date-window pairing searches both repositories and fetches linked partners.
No pairwise comparison of all apps and kernel PRs is performed.
`enrich-apps` writes apps-only records for the per-apps-PR triage, which
finds kernel partners itself; `enrich-kernel` caches a kernel PR it proposes.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode


ROOT = Path(__file__).resolve().parent.parent
# EMBEDEVAL_NUTTX_DATA moves the data directory, e.g. to one per parallel
# run; EMBEDEVAL_NUTTX_DATA_RO names a shared, pre-warmed copy that cached()
# reads from but never writes.
DATA = Path(os.environ.get("EMBEDEVAL_NUTTX_DATA") or ROOT / "candidates" / "nuttx")
DATA_RO = Path(os.environ["EMBEDEVAL_NUTTX_DATA_RO"]) if os.environ.get("EMBEDEVAL_NUTTX_DATA_RO") \
    else None
CACHE = DATA / "apps" / "cache"
CANDIDATES = DATA / "candidates.jsonl"
PAIRS = DATA / "pairs.jsonl"
ENRICHED = DATA / "enriched"
# Every merged apps PR with its changed paths (GitHub GraphQL scan).
SCAN = DATA / "apps_merged_file_scan.json"
# Apps-only records for the per-apps-PR triage, and cached GitHub PRs,
# issues and commits it follows, under github/<repo name>/.
APPS_RECORDS = DATA / "apps_records"
GITHUB_CACHE = DATA / "github"
APPS = "apache/nuttx-apps"
KERNEL = "apache/nuttx"
KNOWN_PAIRS = {2327: 11889, 2329: 11898, 2458: 12802,
               2725: 14152, 1665: 8832, 1682: 8885}

KERNEL_LINK = re.compile(
    r"(?:https?://github\.com/)?apache/nuttx/(?:pull/|pulls/)?(\d+)\b"
    r"|#apache/nuttx/(\d+)\b"
    r"|apache/nuttx#(\d+)\b",
    re.IGNORECASE,
)
APPS_LINK = re.compile(
    r"(?:https?://github\.com/)?apache/nuttx-apps/(?:pull/|pulls/)?(\d+)\b"
    r"|#apache/nuttx-apps/(\d+)\b"
    r"|apache/nuttx-apps#(\d+)\b",
    re.IGNORECASE,
)


def kernel_pr_refs(body: str) -> list[int]:
    """Scoped kernel references; apache/nuttx#N still needs type resolution."""
    numbers = []
    for match in KERNEL_LINK.finditer(body or ""):
        # URLs to /issues/ are intentionally excluded by the pattern.
        numbers.append(int(next(group for group in match.groups() if group)))
    return list(dict.fromkeys(numbers))


def apps_pr_refs(body: str) -> list[int]:
    return list(dict.fromkeys(
        int(next(group for group in match.groups() if group))
        for match in APPS_LINK.finditer(body or "")
    ))


def resolve_kernel_prs(refs: list[int]) -> list[int]:
    """A scoped #N can be a kernel issue; only PRs are fix leads."""
    return [number for number in refs
            if "pull_request" in gh(f"repos/{KERNEL}/issues/{number}")]


def gh(path: str, retries: int = 3):
    for attempt in range(retries):
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        error = proc.stderr.lower()
        if "rate limit" in error or "submitted too quickly" in error:
            time.sleep(20 * (attempt + 1))
            continue
        if "unexpected eof" in error or "connection reset" in error or "http 5" in error \
                or "timed out" in error:
            time.sleep(attempt + 1)
            continue
        raise RuntimeError(f"gh api {path}: {proc.stderr.strip()[:300]}")
    raise RuntimeError(f"gh api {path}: failed after {retries} attempts: {proc.stderr.strip()[:300]}")


def closing_issues(number: int) -> list[int]:
    query = """query($number: Int!) {
      repository(owner: "apache", name: "nuttx") {
        pullRequest(number: $number) {
          closingIssuesReferences(first: 100) {
            nodes { number repository { nameWithOwner } }
          }
        }
      }
    }"""
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"number={number}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"closing issue query for #{number}: {proc.stderr.strip()[:300]}")
    nodes = json.loads(proc.stdout)["data"]["repository"]["pullRequest"][
        "closingIssuesReferences"]["nodes"]
    return [node["number"] for node in nodes
            if node.get("repository", {}).get("nameWithOwner") == KERNEL]


def paged(path: str, cap: int = 3000) -> list:
    result = []
    for page in range(1, (cap + 99) // 100 + 1):
        separator = "&" if "?" in path else "?"
        batch = gh(f"{path}{separator}per_page=100&page={page}")
        result.extend(batch)
        if len(batch) < 100:
            break
    return result


def search_merged(repo: str, since: str, until: str, term: str = "") -> list[int]:
    query = f"repo:{repo} is:pr is:merged merged:{since}..{until} {term}".strip()
    numbers = []
    for page in range(1, 11):
        data = gh("search/issues?" + urlencode({"q": query, "per_page": 100,
                                                "page": page}))
        if data.get("incomplete_results"):
            raise ValueError("GitHub search was incomplete; retry this date window")
        if data.get("total_count", 0) > 1000:
            raise ValueError("GitHub search exceeds 1000 results; narrow the date window")
        batch = data.get("items") or []
        numbers.extend(item["number"] for item in batch)
        if len(batch) < 100:
            break
    return numbers


def fetch_apps(number: int) -> dict:
    cached = CACHE / f"{number}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    pr = gh(f"repos/{APPS}/pulls/{number}")
    files = paged(f"repos/{APPS}/pulls/{number}/files")
    record = {
        "number": number, "title": pr["title"], "body": pr.get("body") or "",
        "merged_at": pr.get("merged_at"), "merge_commit_sha": pr.get("merge_commit_sha"),
        "labels": [label["name"] for label in pr.get("labels", [])],
        "base_sha": pr["base"]["sha"], "head_sha": pr["head"]["sha"],
        "files": [{"filename": f["filename"], "status": f["status"],
                   "previous_filename": f.get("previous_filename")}
                  for f in files],
        "files_truncated": len(files) >= 3000,
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(record, indent=2) + "\n")
    return record


def classify_apps(pr: dict) -> tuple[bool, str]:
    if not pr.get("merged_at"):
        return False, "not merged"
    if pr["title"].lower().startswith("revert"):
        return False, "revert"
    if pr.get("files_truncated"):
        return False, "file list incomplete"
    names = [f["filename"] for f in pr["files"]]
    names += [f["previous_filename"] for f in pr["files"]
              if f.get("previous_filename")]
    if not any(name.startswith("testing/") for name in names):
        return False, "touches no testing/ path"
    return True, "ok"


def summarise(pr: dict) -> dict:
    files = [f["filename"] for f in pr["files"]]
    return {
        "project": "nuttx", "apps_pr": pr["number"],
        "apps_url": f"https://github.com/{APPS}/pull/{pr['number']}",
        "title": pr["title"], "merged_at": pr["merged_at"],
        "test_files": [name for name in files if name.startswith("testing/")],
        "kernel_refs_unverified": kernel_pr_refs(pr["body"]),
    }


def full_pr(repo: str, number: int) -> dict:
    pr = gh(f"repos/{repo}/pulls/{number}")
    files = paged(f"repos/{repo}/pulls/{number}/files")
    commits = paged(f"repos/{repo}/pulls/{number}/commits", cap=1000)
    first_parents = (commits[0].get("parents") or []) if commits else []
    return {
        "repo": repo, "number": number, "url": pr["html_url"],
        "title": pr["title"], "body": pr.get("body") or "",
        "created_at": pr.get("created_at"), "merged_at": pr.get("merged_at"),
        "labels": [label["name"] for label in pr.get("labels", [])],
        "additions": pr.get("additions"), "deletions": pr.get("deletions"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "base_commit": first_parents[0]["sha"] if first_parents else None,
        "head_commit": commits[-1]["sha"] if commits else None,
        "commits": [{"sha": c["sha"],
                     "message": (c.get("commit") or {}).get("message", "")}
                    for c in commits],
        "files": [{"filename": f["filename"], "status": f["status"],
                   "previous_filename": f.get("previous_filename"),
                   "additions": f.get("additions"),
                   "deletions": f.get("deletions"), "patch": f.get("patch")}
                  for f in files],
        "files_truncated": len(files) >= 3000,
        "comments": paged(f"repos/{repo}/issues/{number}/comments", cap=1000),
        "review_comments": paged(f"repos/{repo}/pulls/{number}/comments", cap=1000),
        "reviews": paged(f"repos/{repo}/pulls/{number}/reviews", cap=1000),
    }


def inverse_kernel_refs(apps_number: int) -> list[int]:
    """Search kernel PRs for links back to one apps PR."""
    numbers = []
    for ref in (f"nuttx-apps/pull/{apps_number}",
                f"apache/nuttx-apps#{apps_number}",
                f"#apache/nuttx-apps/{apps_number}"):
        query = f'repo:{KERNEL} is:pr is:merged "{ref}"'
        data = gh("search/issues?" + urlencode({"q": query, "per_page": 100}))
        if data.get("incomplete_results"):
            raise ValueError(f"GitHub link search for apps #{apps_number} was incomplete")
        if data.get("total_count", 0) > 100:
            raise ValueError(f"GitHub link search for apps #{apps_number} exceeds 100 results")
        numbers.extend(item["number"] for item in data.get("items", [])
                       if "pull_request" in item)
    return list(dict.fromkeys(numbers))


def ostest_paths(apps: dict) -> list[str]:
    if not apps.get("merged_at") or apps.get("files_truncated") or \
            apps["title"].lower().startswith("revert"):
        return []
    return sorted({file["filename"] for file in apps["files"]
                   if file["filename"].startswith("testing/ostest/")
                   and file["status"] != "removed"})


def pair_row(apps: dict, kernel: dict, paths: list[str], sources: set[str]) -> dict:
    return {"case": f"apps-{apps['number']}-kernel-{kernel['number']}",
            "apps_pr": apps["number"], "kernel_pr": kernel["number"],
            "apps_url": f"https://github.com/{APPS}/pull/{apps['number']}",
            "kernel_url": kernel["html_url"], "ostest_paths": paths,
            "link_sources": sorted(sources)}


def linked_ostest_pairs(apps: dict) -> list[dict]:
    """Find explicit cross-repo PR links for one merged apps PR with ostest changes."""
    paths = ostest_paths(apps)
    if not paths:
        return []

    refs: dict[int, set[str]] = {}
    for number in kernel_pr_refs(apps["body"]):
        refs.setdefault(number, set()).add("apps_body")
    for source, path in (
            ("apps_comment", f"repos/{APPS}/issues/{apps['number']}/comments"),
            ("apps_review_comment", f"repos/{APPS}/pulls/{apps['number']}/comments")):
        for comment in paged(path, cap=1000):
            for number in kernel_pr_refs(comment.get("body") or ""):
                refs.setdefault(number, set()).add(source)

    inverse = set(inverse_kernel_refs(apps["number"]))
    rows = []
    for number in sorted(refs.keys() | inverse):
        try:
            kernel = gh(f"repos/{KERNEL}/pulls/{number}")
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                continue  # A scoped number may be a kernel issue, not a PR.
            raise
        if not kernel.get("merged_at"):
            continue
        sources = refs.get(number, set()).copy()
        if number in inverse:
            if apps["number"] in apps_pr_refs(kernel.get("body") or ""):
                sources.add("kernel_body")
            for source, path in (
                    ("kernel_comment", f"repos/{KERNEL}/issues/{number}/comments"),
                    ("kernel_review_comment", f"repos/{KERNEL}/pulls/{number}/comments")):
                for comment in paged(path, cap=1000):
                    if apps["number"] in apps_pr_refs(comment.get("body") or ""):
                        sources.add(source)
        if not sources:
            continue  # GitHub search found text, but no exact cross-repo PR link.
        rows.append(pair_row(apps, kernel, paths, sources))
    return rows


def linked_apps_from_kernel(number: int) -> list[dict]:
    """Start with one kernel PR and fetch apps PRs linked in its discussion."""
    kernel = gh(f"repos/{KERNEL}/pulls/{number}")
    if not kernel.get("merged_at"):
        return []
    refs: dict[int, set[str]] = {}
    for apps_number in apps_pr_refs(kernel.get("body") or ""):
        refs.setdefault(apps_number, set()).add("kernel_body")
    for source, path in (
            ("kernel_comment", f"repos/{KERNEL}/issues/{number}/comments"),
            ("kernel_review_comment", f"repos/{KERNEL}/pulls/{number}/comments")):
        for comment in paged(path, cap=1000):
            for apps_number in apps_pr_refs(comment.get("body") or ""):
                refs.setdefault(apps_number, set()).add(source)
    for event in paged(f"repos/{KERNEL}/issues/{number}/timeline", cap=1000):
        if event.get("event") != "cross-referenced":
            continue
        issue = (event.get("source") or {}).get("issue") or {}
        if issue.get("repository_url") != f"https://api.github.com/repos/{APPS}" or \
                "pull_request" not in issue:
            continue
        apps_number = issue.get("number")
        if apps_number:
            refs.setdefault(apps_number, set()).add("kernel_timeline")
    rows = []
    for apps_number, sources in sorted(refs.items()):
        try:
            apps = fetch_apps(apps_number)
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                continue  # A scoped number may be an apps issue, not a PR.
            raise
        paths = ostest_paths(apps)
        if paths:
            rows.append(pair_row(apps, kernel, paths, sources))
    return rows


def issue_refs(body: str) -> list[int]:
    pattern = re.compile(
        r"https?://github\.com/apache/nuttx/issues/(\d+)\b"
        r"|\b(?:fixes|closes|resolves|issue)\s+#(\d+)\b",
        re.IGNORECASE,
    )
    return list(dict.fromkeys(
        int(next(group for group in match.groups() if group))
        for match in pattern.finditer(body or "")
    ))


def source_at(repo: str, path: str, ref: str) -> str | None:
    try:
        blob = gh(f"repos/{repo}/contents/{quote(path)}?ref={ref}")
    except RuntimeError as error:
        if "HTTP 404" in str(error):
            return None
        raise
    if not isinstance(blob, dict) or blob.get("encoding") != "base64":
        return None
    if blob.get("size", 0) > 100_000:
        return None
    try:
        return base64.b64decode(blob["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def enrich_kernel(kernel: dict, sources: bool = True) -> dict:
    """Add linked issues and, with sources, pre-fix file contents."""
    ref = kernel["number"]
    issue_numbers = list(dict.fromkeys([
        *issue_refs(kernel["body"]), *closing_issues(ref)
    ]))
    issues = []
    for issue_number in issue_numbers:
        issue = gh(f"repos/{KERNEL}/issues/{issue_number}")
        if "pull_request" not in issue:
            issues.append({"number": issue_number, "title": issue["title"],
                           "body": issue.get("body") or "", "url": issue["html_url"],
                           "state": issue.get("state"),
                           "labels": [label["name"] for label in issue.get("labels", [])],
                           "comments": paged(f"repos/{KERNEL}/issues/{issue_number}/comments",
                                             cap=1000)})
    kernel["linked_issues"] = issues
    if not sources:
        return kernel
    kernel["base_sources"] = {}
    for file in kernel["files"]:
        if file["status"] == "added" or file["filename"].startswith("Documentation/"):
            continue
        old_path = file.get("previous_filename") or file["filename"]
        kernel["base_sources"][old_path] = source_at(KERNEL, old_path, kernel["base_commit"])
    kernel["sim_nsh_defconfig"] = source_at(
        KERNEL, "boards/sim/sim/sim/configs/nsh/defconfig", kernel["base_commit"]
    )
    return kernel


def changed_test_contents(apps: dict) -> dict:
    # Triage can read other files at the pinned apps commit if needed.
    contents = {}
    for file in apps["files"]:
        path = file["filename"]
        if not path.startswith("testing/") or file["status"] == "removed":
            continue
        contents[path] = source_at(APPS, path, apps["head_commit"])
    if any(path.startswith("testing/ostest/") for path in contents):
        for path in ("testing/ostest/Makefile", "testing/ostest/CMakeLists.txt",
                     "testing/ostest/ostest.h", "testing/ostest/ostest_main.c"):
            if path not in contents:
                contents[path] = source_at(APPS, path, apps["head_commit"])
    return contents


def enrich(number: int, refresh: bool = False, kernel_number: int | None = None) -> dict:
    target = ENRICHED / (f"apps-{number}-kernel-{kernel_number}.json"
                         if kernel_number is not None else f"{number}.json")
    if target.exists() and not refresh:
        return json.loads(target.read_text())
    cached_path = ENRICHED / f"{number}.json"
    cached = (json.loads(cached_path.read_text())
              if kernel_number is not None and cached_path.exists() and not refresh else None)
    apps = cached["apps"] if cached else full_pr(APPS, number)
    if kernel_number is not None:
        refs = [kernel_number]
        pairing_source = "explicit_pair"
    else:
        refs = kernel_pr_refs(apps["body"])
        for comment in apps["comments"]:
            refs.extend(kernel_pr_refs(comment.get("body") or ""))
        refs = resolve_kernel_prs(list(dict.fromkeys(refs)))
        pairing_source = "apps_link"
        if not refs:
            refs = inverse_kernel_refs(number)
            pairing_source = "kernel_search" if refs else "unresolved"
    kernels = []
    for ref in refs:
        reused = next((k for k in cached["kernel_candidates"] if k["number"] == ref), None) if cached else None
        kernels.append(reused if reused else enrich_kernel(full_pr(KERNEL, ref)))
    test_contents = cached["test_contents"] if cached else changed_test_contents(apps)
    record = {"project": "nuttx", "apps": apps, "kernel_candidates": kernels,
              "test_contents": test_contents,
              "pairing_source": pairing_source}
    ENRICHED.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2) + "\n")
    return record


def ostest_apps_prs(scan: Path = SCAN) -> list[int]:
    """Merged apps PRs that change any path under testing/ostest/."""
    prs = json.loads(scan.read_text())["prs"]
    return sorted(int(number) for number, pr in prs.items()
                  if any(node["path"].startswith("testing/ostest/")
                         for node in pr["files"]["nodes"]))


def cached(path: Path, fetch, refresh: bool = False):
    """JSON at path, or fetch() written there; the write is atomic so
    parallel triage cases can share the cache. A copy under DATA_RO is read
    when path is missing."""
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    if DATA_RO and not refresh:
        with contextlib.suppress(ValueError):
            shared = DATA_RO / path.relative_to(DATA)
            if shared.exists():
                return json.loads(shared.read_text())
    value = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)
    return value


def timeline_refs(repo: str, number: int) -> list[dict]:
    """Cross-references to a PR or issue, and commits that mention it."""
    events = []
    for event in paged(f"repos/{repo}/issues/{number}/timeline", cap=1000):
        if event.get("event") == "cross-referenced":
            issue = (event.get("source") or {}).get("issue") or {}
            events.append({
                "event": "cross-referenced", "created_at": event.get("created_at"),
                "repo": (issue.get("repository_url") or "").rsplit("/repos/", 1)[-1],
                "number": issue.get("number"), "title": issue.get("title"),
                "is_pr": "pull_request" in issue, "state": issue.get("state"),
                "merged_at": (issue.get("pull_request") or {}).get("merged_at"),
                "url": issue.get("html_url")})
        elif event.get("event") == "referenced" and event.get("commit_id"):
            events.append({"event": "referenced", "created_at": event.get("created_at"),
                           "commit_id": event["commit_id"],
                           "commit_url": event.get("commit_url")})
    return events


def pr_record(repo: str, number: int, refresh: bool = False) -> dict:
    """A full PR with its timeline, cached; kernel PRs also get linked
    issues (not pre-fix sources: tools read those from the clone)."""
    def fetch():
        pr = {**full_pr(repo, number), "timeline": timeline_refs(repo, number)}
        return enrich_kernel(pr, sources=False) if repo == KERNEL else pr
    return cached(GITHUB_CACHE / repo.split("/")[1] / f"pr-{number}.json", fetch, refresh)


def kernel_record(number: int) -> dict:
    """A kernel PR found by search rather than by link, enriched on demand."""
    return pr_record(KERNEL, number)


def issue_or_pr(repo: str, number: int) -> dict:
    """A PR record, or an issue with comments and timeline; "kind" says which."""
    folder = GITHUB_CACHE / repo.split("/")[1]
    path = folder / f"issue-{number}.json"
    if path.exists():
        return {"kind": "issue", **json.loads(path.read_text())}
    if not (folder / f"pr-{number}.json").exists():
        issue = gh(f"repos/{repo}/issues/{number}")   # GitHub serves PRs here too
        if "pull_request" not in issue:
            record = {"repo": repo, "number": number, "url": issue["html_url"],
                      "title": issue["title"], "body": issue.get("body") or "",
                      "state": issue.get("state"), "created_at": issue.get("created_at"),
                      "labels": [label["name"] for label in issue.get("labels", [])],
                      "comments": paged(f"repos/{repo}/issues/{number}/comments", cap=1000),
                      "timeline": timeline_refs(repo, number)}
            return {"kind": "issue", **cached(path, lambda: record)}
    return {"kind": "pr", **pr_record(repo, number)}


def commit_record(repo: str, sha: str) -> dict:
    """A commit's message and files from GitHub, and the PRs it belongs to."""
    def fetch():
        commit = gh(f"repos/{repo}/commits/{sha}")
        return {"repo": repo, "sha": commit["sha"], "url": commit.get("html_url"),
                "message": (commit.get("commit") or {}).get("message", ""),
                "date": ((commit.get("commit") or {}).get("committer") or {}).get("date"),
                "parents": [parent["sha"] for parent in commit.get("parents", [])],
                "files": [{"filename": f["filename"], "status": f["status"],
                           "additions": f.get("additions"), "deletions": f.get("deletions"),
                           "patch": f.get("patch")} for f in commit.get("files", [])],
                "prs": [{"number": pr["number"], "title": pr["title"],
                         "merged_at": pr.get("merged_at"), "url": pr["html_url"]}
                        for pr in gh(f"repos/{repo}/commits/{sha}/pulls")]}
    return cached(GITHUB_CACHE / repo.split("/")[1] / f"commit-{sha}.json", fetch)


def apps_record(number: int, refresh: bool = False) -> dict:
    """One apps PR for the per-apps-PR triage: the full PR, its timeline and
    the changed test sources, with no kernel partner chosen."""
    def fetch():
        apps = pr_record(APPS, number, refresh)
        return {"project": "nuttx", "apps": apps,
                "test_contents": changed_test_contents(apps)}
    return cached(APPS_RECORDS / f"{number}.json", fetch, refresh)


def dispatch(args) -> None:
    command = getattr(args, "command", None) or args.cmd
    if command == "selftest":
        failed = []
        for apps_number, kernel_number in KNOWN_PAIRS.items():
            pr = fetch_apps(apps_number)
            kept, reason = classify_apps(pr)
            linked = kernel_number in kernel_pr_refs(pr["body"])
            print(f"{apps_number} -> {kernel_number}: kept={kept}, linked={linked}")
            if not kept or not linked:
                failed.append((apps_number, reason))
        if failed:
            sys.exit(f"known pairs missed: {failed}")
    elif command == "fetch":
        numbers = args.pr or search_merged(APPS, args.since, args.until)
        for index, number in enumerate(numbers, 1):
            fetch_apps(number)
            print(f"[{index}/{len(numbers)}] apps #{number}")
    elif command == "pairs":
        if bool(args.since) != bool(args.until):
            sys.exit("provide both --since and --until")
        apps_numbers = list(args.pr or [])
        kernel_numbers = list(args.kernel_pr or [])
        if args.since:
            apps_numbers.extend(search_merged(APPS, args.since, args.until))
            kernel_numbers.extend(search_merged(KERNEL, args.since, args.until,
                                                '"nuttx-apps"'))
        elif not apps_numbers and not kernel_numbers:
            apps_numbers = [int(file.stem) for file in sorted(CACHE.glob("*.json"))]
            if not apps_numbers:
                sys.exit("no cached apps PRs; use --since/--until or --kernel-pr")
        apps_numbers = list(dict.fromkeys(apps_numbers))
        kernel_numbers = list(dict.fromkeys(kernel_numbers))
        rows_by_pair = {}
        for number in apps_numbers:
            for row in linked_ostest_pairs(fetch_apps(number)):
                rows_by_pair[(row["apps_pr"], row["kernel_pr"])] = row
        for number in kernel_numbers:
            for row in linked_apps_from_kernel(number):
                key = (row["apps_pr"], row["kernel_pr"])
                if key in rows_by_pair:
                    current = rows_by_pair[key]
                    current["link_sources"] = sorted(set(current["link_sources"])
                                                     | set(row["link_sources"]))
                else:
                    rows_by_pair[key] = row
        rows = [rows_by_pair[key] for key in sorted(rows_by_pair)]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(json.dumps(row) + "\n" for row in rows))
        print(f"{len(rows)} pair leads from {len(apps_numbers)} apps PRs and "
              f"{len(kernel_numbers)} kernel PRs: {args.out}")
    elif command == "filter":
        files = sorted(CACHE.glob("*.json"))
        if not files:
            sys.exit("no cached apps PRs; run fetch first")
        kept = []
        for file in files:
            pr = json.loads(file.read_text())
            if classify_apps(pr)[0]:
                kept.append(summarise(pr))
        CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
        CANDIDATES.write_text("".join(json.dumps(r) + "\n" for r in kept))
        print(f"{len(files)} cached apps PRs -> {len(kept)} test leads: {CANDIDATES}")
    elif command == "enrich-apps":
        numbers = list(dict.fromkeys((args.pr or [])
                                     + (ostest_apps_prs(args.scan) if args.ostest_scan else [])))
        if not numbers:
            sys.exit("give --pr and/or --ostest-scan")
        failed = []
        for index, number in enumerate(numbers, 1):
            try:
                record = apps_record(number, args.refresh)
            except RuntimeError as error:   # one bad PR must not stop a long batch
                failed.append(number)
                print(f"[{index}/{len(numbers)}] apps #{number}: {error}", flush=True)
                continue
            print(f"[{index}/{len(numbers)}] apps #{number}: "
                  f"{len(record['apps']['files'])} files, "
                  f"{len(record['apps']['timeline'])} timeline refs", flush=True)
        print(f"{len(numbers) - len(failed)} records in {APPS_RECORDS}"
              + (f"; failed: {failed}" if failed else ""))
        if failed:
            sys.exit(1)
    elif command == "enrich-kernel":
        for number in args.pr:
            record = kernel_record(number)
            print(f"kernel #{number}: {record['title']} (merged {record['merged_at']})")
    else:
        if args.pr:
            numbers = args.pr
        else:
            if not CANDIDATES.exists():
                sys.exit("no candidates.jsonl; run filter first")
            numbers = [json.loads(line)["apps_pr"] for line in CANDIDATES.read_text().splitlines()]
            if args.limit:
                numbers = numbers[:args.limit]
        for number in numbers:
            record = enrich(number, getattr(args, "refresh", False))
            print(f"apps #{number}: {len(record['kernel_candidates'])} kernel lead(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--pr", type=int, nargs="+")
    fetch.add_argument("--since", default="2024-01-01")
    fetch.add_argument("--until", default="2024-12-31")
    pairs = sub.add_parser("pairs", help="linked merged PR pairs with ostest changes")
    pairs.add_argument("--pr", type=int, nargs="+")
    pairs.add_argument("--kernel-pr", type=int, nargs="+")
    pairs.add_argument("--since")
    pairs.add_argument("--until")
    pairs.add_argument("--out", type=Path, default=PAIRS)
    sub.add_parser("filter")
    en = sub.add_parser("enrich")
    en.add_argument("--pr", type=int, nargs="+")
    en.add_argument("--limit", type=int)
    en.add_argument("--refresh", action="store_true")
    apps = sub.add_parser("enrich-apps", help="apps-only records for per-apps-PR triage")
    apps.add_argument("--pr", type=int, nargs="+")
    apps.add_argument("--ostest-scan", action="store_true",
                      help="every merged apps PR in --scan that changes testing/ostest/")
    apps.add_argument("--scan", type=Path, default=SCAN)
    apps.add_argument("--refresh", action="store_true")
    kernel = sub.add_parser("enrich-kernel", help="cache kernel PRs a triage proposed")
    kernel.add_argument("--pr", type=int, nargs="+", required=True)
    dispatch(parser.parse_args())


if __name__ == "__main__":
    main()
