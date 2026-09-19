#!/usr/bin/env python3
"""
Find candidate NuttX PR pairs (kernel PR + apps PR) suitable for EmbedEval.

A good candidate is a pair where:
  - The apps PR adds a NEW test file (or modifies a test) in testing/ostest/
  - The apps PR body references the companion kernel PR
  - Without the kernel fix, the test either fails to compile or asserts at runtime
  - The kernel fix is in a testable area (libs/libc, sched, include) and not
    exclusively in arch-specific or hardware-driver code
  - The failure is deterministic on the sim:nsh target (no real hardware needed)

Fail type classification:
  compile_error  — test includes a new nuttx/ header or uses a macro/symbol
                   that the kernel PR introduces (instant build failure)
  runtime_assert — test calls ASSERT() on a function with known-good inputs;
                   the function is broken at the base commit (runtime crash)

Usage:
    python3 scripts/find_nuttx_candidates.py [options]

Options:
    --limit N         Max apps PRs to fetch (default: 200)
    --min-score N     Only output candidates scoring >= N (default: 3)
    --output FILE     Write JSON results to FILE (default: candidates.json)
    --token TOKEN     GitHub API token (overrides .env)
    --since YYYY-MM-DD Only consider PRs merged after this date

Outputs:
    candidates.json   Ranked list of candidate PR pairs with scores + metadata
    Printed summary   Top candidates with scores and reasoning
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import requests as _requests
    _SESSION = _requests.Session()
    def _get(url, token, params=None):
        resp = _SESSION.get(url, headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }, params=params, timeout=30)
        if resp.status_code == 404:
            return None
        if resp.status_code == 403:
            reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(1, reset - int(time.time()))
            print(f"  Rate limited. Waiting {wait}s...", file=sys.stderr)
            time.sleep(min(wait, 60))
            resp = _SESSION.get(url, headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            }, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
except ImportError:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from urllib.parse import urlencode
    def _get(url, token, params=None):
        if params:
            url = url + "?" + urlencode({k: v for k, v in (params or {}).items()})
        req = Request(url, headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 404:
                return None
            raise

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent

# Directories in the kernel that the fix must touch for the PR to be testable
# on sim. If the kernel PR ONLY touches excluded directories, it's filtered out.
GOOD_KERNEL_DIRS = {
    "libs/libc/",
    "sched/",
    "include/nuttx/",
    "include/",
    "mm/",
    "fs/",
    "crypto/",
    "net/",
}

# If the kernel PR touches ONLY these, it's too arch/board-specific for sim
EXCLUDED_KERNEL_DIRS = {
    "arch/",
    "boards/",
    "drivers/",
    "Documentation/",
    ".github/",
}

# ostest test file patterns — a new .c file here is the strongest signal
OSTEST_NEW_FILE_PATTERN = re.compile(r"^testing/ostest/[^/]+\.c$")
OSTEST_MODIFIED_PATTERN = re.compile(r"^testing/ostest/")

# Patterns that indicate a compile-error fail type in a test file's content
COMPILE_ERROR_INDICATORS = [
    re.compile(r'#include\s+<nuttx/'),   # includes a new nuttx/ kernel header
    re.compile(r'nxevent|nxmutex|nxsem|nxcond'),  # uses new nx* kernel API
    re.compile(r'CONFIG_\w+'),            # uses a new Kconfig symbol
]

# Patterns that indicate a runtime-assert fail type
RUNTIME_ASSERT_INDICATORS = [
    re.compile(r'\bASSERT\s*\('),
    re.compile(r'\bassert\s*\('),
    re.compile(r'\bzassert_'),
]

# Signal for a likely hardware-only test (skip these)
HARDWARE_SKIP_PATTERNS = [
    re.compile(r'hrtimer', re.IGNORECASE),
    re.compile(r'spi|i2c|uart|gpio|adc|pwm', re.IGNORECASE),
    re.compile(r'qemu|board', re.IGNORECASE),
]

# ── GitHub API helpers ────────────────────────────────────────────────────────

def load_token():
    """Load GitHub token from .env or environment."""
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("GITHUB_API="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_API")


def gh_get(url, token, params=None):
    """Make a GitHub API GET request, return parsed JSON. Handles rate limits."""
    return _get(url, token, params)


def gh_search_issues(query, token, per_page=100, max_results=500):
    """Paginate through GitHub issue search results."""
    results = []
    page = 1
    while len(results) < max_results:
        data = gh_get(
            "https://api.github.com/search/issues",
            token,
            params={"q": query, "per_page": per_page, "page": page, "sort": "created", "order": "desc"},
        )
        if not data or not data.get("items"):
            break
        results.extend(data["items"])
        if len(data["items"]) < per_page:
            break
        page += 1
        time.sleep(0.5)  # respect secondary rate limits
    return results


def get_pr_files(repo, pr_number, token):
    """Return list of (filename, status, additions, deletions) for a PR."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
    data = gh_get(url, token, params={"per_page": 100})
    if not data:
        return []
    return [(f["filename"], f["status"], f.get("additions", 0), f.get("deletions", 0), f.get("patch", ""))
            for f in data]


def get_pr_details(repo, pr_number, token):
    """Return PR metadata dict."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    return gh_get(url, token)

# ── Extraction helpers ────────────────────────────────────────────────────────

def extract_kernel_pr(body):
    """
    Extract kernel PR number from an apps PR body.
    Returns int or None.
    """
    if not body:
        return None
    patterns = [
        r'https://github\.com/apache/nuttx/pull/(\d+)',
        r'apache/nuttx#(\d+)',
        r'nuttx#(\d+)',
        r'nuttx\s+PR\s+#?(\d+)',
        r'nuttx\s+PR\s+(\d+)',
        r'#(\d{4,5})',   # last-resort bare PR number (4-5 digits)
    ]
    for pat in patterns[:-1]:  # avoid bare number ambiguity for first pass
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            return int(m.group(1))
    # bare number only if no other match found
    m = re.search(patterns[-1], body)
    if m:
        return int(m.group(1))
    return None

# ── Scoring ──────────────────────────────────────────────────────────────────

def score_kernel_pr(kernel_files, kernel_details):
    """
    Score a kernel PR based on how suitable it is for EmbedEval.
    Returns (score: int, reasons: list[str]).
    """
    score = 0
    reasons = []
    filenames = [f[0] for f in kernel_files]
    n_files = len(filenames)
    additions = kernel_details.get("additions", 0)

    # ── File count ────────────────────────────────────────────────────────────
    if n_files == 1:
        score += 3
        reasons.append(f"+3 single-file fix ({filenames[0]})")
    elif n_files <= 5:
        score += 2
        reasons.append(f"+2 small fix ({n_files} files)")
    elif n_files <= 15:
        score += 1
        reasons.append(f"+1 medium fix ({n_files} files)")
    elif n_files <= 30:
        score += 0
        reasons.append(f"±0 large fix ({n_files} files)")
    else:
        score -= 2
        reasons.append(f"-2 very large fix ({n_files} files — harder for agent)")

    # ── Directory quality ─────────────────────────────────────────────────────
    dirs_touched = set()
    for fn in filenames:
        for d in list(GOOD_KERNEL_DIRS) + list(EXCLUDED_KERNEL_DIRS):
            if fn.startswith(d):
                dirs_touched.add(d)

    good_dirs = dirs_touched & GOOD_KERNEL_DIRS
    bad_dirs  = dirs_touched & EXCLUDED_KERNEL_DIRS

    if not filenames:
        score -= 5
        reasons.append("-5 no files found")
    elif good_dirs and not bad_dirs:
        score += 3
        reasons.append(f"+3 purely testable dirs: {sorted(good_dirs)}")
    elif good_dirs:
        score += 1
        reasons.append(f"+1 mixed dirs (good: {sorted(good_dirs)}, excluded: {sorted(bad_dirs)})")
    else:
        score -= 3
        reasons.append(f"-3 only excluded dirs: {sorted(bad_dirs)}")

    # ── libs/libc preference (easiest domain for LLM agents) ─────────────────
    if any(fn.startswith("libs/libc/") for fn in filenames):
        score += 2
        reasons.append("+2 fixes libs/libc/ (well-known domain)")

    # ── New header introduced (strong compile_error signal) ──────────────────
    new_headers = [fn for fn, st, *_ in kernel_files
                   if st == "added" and fn.startswith("include/") and fn.endswith(".h")]
    if new_headers:
        score += 2
        reasons.append(f"+2 introduces new header(s): {new_headers}")

    # ── New sched/ subsystem (like nxevent) ───────────────────────────────────
    new_sched = [fn for fn, st, *_ in kernel_files
                 if st == "added" and fn.startswith("sched/")]
    if new_sched:
        score += 1
        reasons.append(f"+1 adds new sched/ subsystem ({len(new_sched)} files)")

    # ── defconfig change (sim:nsh defconfig = good, arch-specific = neutral) ──
    defconfig_files = [fn for fn in filenames if "defconfig" in fn]
    sim_defconfig = [fn for fn in defconfig_files if "sim" in fn]
    if sim_defconfig:
        score += 1
        reasons.append(f"+1 updates sim defconfig: {sim_defconfig}")

    # ── Penalise if purely docs/CI/board ──────────────────────────────────────
    docs_only = all(
        fn.startswith("Documentation/") or fn.startswith(".github/") or fn.startswith("boards/")
        for fn in filenames
    )
    if docs_only:
        score -= 5
        reasons.append("-5 docs/CI/board changes only — not a code fix")

    return score, reasons


def classify_fail_type(apps_files_with_patches, kernel_files):
    """
    Guess whether the fail is compile_error or runtime_assert.
    Returns one of: 'compile_error', 'runtime_assert', 'unknown'
    """
    new_kernel_headers = {fn for fn, st, *_ in kernel_files
                          if st == "added" and fn.startswith("include/") and fn.endswith(".h")}

    for filename, status, adds, dels, patch in apps_files_with_patches:
        if not OSTEST_NEW_FILE_PATTERN.match(filename):
            continue
        if not patch:
            continue
        # Check for new nuttx/ includes in added lines
        added_lines = [l[1:] for l in patch.splitlines() if l.startswith("+")]
        added_text = "\n".join(added_lines)

        for p in COMPILE_ERROR_INDICATORS:
            if p.search(added_text):
                return "compile_error"

        # Check if added lines reference a new kernel header by basename
        for hdr in new_kernel_headers:
            basename = hdr.split("/")[-1]
            if basename in added_text:
                return "compile_error"

        for p in RUNTIME_ASSERT_INDICATORS:
            if p.search(added_text):
                return "runtime_assert"

    return "unknown"


def is_hardware_specific(apps_files, kernel_files):
    """Return True if the test likely requires real hardware (skip these)."""
    all_filenames = [f[0] for f in apps_files] + [f[0] for f in kernel_files]
    combined = " ".join(all_filenames)
    for pat in HARDWARE_SKIP_PATTERNS:
        if pat.search(combined):
            return True
    return False


def classify_agent_difficulty(kernel_files, kernel_details):
    """
    Rough difficulty estimate for an LLM agent.
    Returns 'easy', 'medium', or 'hard'.
    """
    n_files = len(kernel_files)
    additions = kernel_details.get("additions", 0)
    if n_files <= 2 and additions <= 30:
        return "easy"
    elif n_files <= 10 and additions <= 300:
        return "medium"
    else:
        return "hard"

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit",     type=int,   default=200,              help="Max apps PRs to fetch")
    parser.add_argument("--min-score", type=int,   default=3,                help="Minimum score to include")
    parser.add_argument("--output",    default="candidates.json",            help="Output JSON file")
    parser.add_argument("--token",     default=None,                         help="GitHub API token")
    parser.add_argument("--since",     default=None,                         help="Only PRs merged after YYYY-MM-DD")
    args = parser.parse_args()

    token = args.token or load_token()
    if not token:
        sys.exit("ERROR: No GitHub token. Set GITHUB_API in .env or pass --token.")

    # ── Already-used instances (skip these) ──────────────────────────────────
    already_used_kernel = {8885, 11889, 12802}
    already_used_apps   = {1682, 2327, 2458}

    # ── Step 1: Find apps PRs that reference a kernel PR and touch ostest ─────
    print("Searching for candidate apps PRs...")
    since_clause = f" merged:>{args.since}" if args.since else ""
    query = f'repo:apache/nuttx-apps is:pr is:merged "apache/nuttx/pull" ostest{since_clause}'
    apps_prs = gh_search_issues(query, token, max_results=args.limit)
    print(f"Found {len(apps_prs)} apps PRs matching search query.")

    candidates = []
    seen_kernel_prs = set(already_used_kernel)

    for i, apps_pr in enumerate(apps_prs):
        apps_num = apps_pr["number"]
        apps_title = apps_pr["title"]

        if apps_num in already_used_apps:
            continue

        body = apps_pr.get("body") or ""
        kernel_num = extract_kernel_pr(body)
        if not kernel_num:
            continue
        if kernel_num in seen_kernel_prs:
            continue

        print(f"  [{i+1}/{len(apps_prs)}] apps#{apps_num} -> nuttx#{kernel_num}: {apps_title[:55]}")

        # Fetch apps PR files
        apps_files = get_pr_files("apache/nuttx-apps", apps_num, token)
        time.sleep(0.2)

        # Must touch testing/ostest/ and add at least one new .c file
        ostest_new  = [f for f in apps_files if OSTEST_NEW_FILE_PATTERN.match(f[0]) and f[1] == "added"]
        ostest_any  = [f for f in apps_files if OSTEST_MODIFIED_PATTERN.match(f[0])]
        if not ostest_any:
            print(f"    SKIP: does not touch testing/ostest/")
            continue

        # Fetch kernel PR details + files
        kernel_details = get_pr_details("apache/nuttx", kernel_num, token)
        time.sleep(0.2)
        if not kernel_details:
            print(f"    SKIP: kernel PR nuttx#{kernel_num} not found")
            continue

        kernel_files = get_pr_files("apache/nuttx", kernel_num, token)
        time.sleep(0.2)

        # Skip hardware-specific tests
        if is_hardware_specific(apps_files, kernel_files):
            print(f"    SKIP: looks hardware-specific")
            continue

        # Score the kernel PR
        k_score, k_reasons = score_kernel_pr(kernel_files, kernel_details)

        # Bonus for new test file
        if ostest_new:
            k_score += 2
            k_reasons.insert(0, f"+2 adds new ostest test file(s): {[f[0] for f in ostest_new]}")

        if k_score < args.min_score:
            print(f"    SKIP: score {k_score} below threshold {args.min_score}")
            continue

        fail_type  = classify_fail_type(apps_files, kernel_files)
        difficulty = classify_agent_difficulty(kernel_files, kernel_details)

        candidate = {
            "kernel_pr":        kernel_num,
            "apps_pr":          apps_num,
            "kernel_title":     kernel_details.get("title", ""),
            "apps_title":       apps_title,
            "kernel_merged_at": kernel_details.get("merged_at", ""),
            "apps_merged_at":   apps_pr.get("created_at", ""),
            "kernel_files_changed": kernel_details.get("changed_files", 0),
            "kernel_additions": kernel_details.get("additions", 0),
            "kernel_deletions": kernel_details.get("deletions", 0),
            "kernel_files":     [f[0] for f in kernel_files],
            "apps_ostest_new":  [f[0] for f in ostest_new],
            "apps_ostest_any":  [f[0] for f in ostest_any],
            "fail_type":        fail_type,
            "difficulty":       difficulty,
            "score":            k_score,
            "score_reasons":    k_reasons,
            "kernel_url":       f"https://github.com/apache/nuttx/pull/{kernel_num}",
            "apps_url":         f"https://github.com/apache/nuttx-apps/pull/{apps_num}",
        }

        candidates.append(candidate)
        seen_kernel_prs.add(kernel_num)
        print(f"    score={k_score}  fail_type={fail_type}  difficulty={difficulty}  new_files={[f[0] for f in ostest_new]}")

    # ── Step 2: Sort and write output ─────────────────────────────────────────
    candidates.sort(key=lambda c: (-c["score"], c["kernel_files_changed"]))

    output_path = Path(args.output)
    output_path.write_text(json.dumps(candidates, indent=2))
    print(f"\nWrote {len(candidates)} candidates to {output_path}")

    # ── Step 3: Print summary ─────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"TOP CANDIDATES  (min-score={args.min_score}, total={len(candidates)})")
    print("=" * 72)

    for c in candidates[:20]:
        print(f"\nnuttx#{c['kernel_pr']} + apps#{c['apps_pr']}  "
              f"score={c['score']}  {c['fail_type']}  {c['difficulty']}")
        print(f"  Kernel : {c['kernel_title'][:65]}")
        print(f"  Apps   : {c['apps_title'][:65]}")
        print(f"  Merged : {c['kernel_merged_at'][:10]}")
        print(f"  Files  : {c['kernel_files_changed']} kernel  +{c['kernel_additions']}/-{c['kernel_deletions']} lines")
        print(f"  New tests: {c['apps_ostest_new']}")
        print(f"  URLs   : {c['kernel_url']}")
        print(f"           {c['apps_url']}")
        print(f"  Reasons: {'; '.join(c['score_reasons'][:4])}")

    print("\nDone.")


if __name__ == "__main__":
    main()
