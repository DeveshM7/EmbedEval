from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import filter_candidates


def pr(*files: str, body: str = "Fixes #123") -> dict:
    return {
        "number": 42,
        "title": "Fix native behavior",
        "body": body,
        "merged_at": "2026-01-01T00:00:00Z",
        "merge_commit_sha": "deadbeef",
        "labels": [],
        "files": [
            {
                "filename": name,
                "status": "modified",
                "additions": 1,
                "deletions": 1,
            }
            for name in files
        ],
        "files_truncated": False,
    }


class MynewtCandidateFilterTests(unittest.TestCase):
    def test_keeps_issue_linked_source_change_with_native_selftest(self) -> None:
        candidate = pr(
            "kernel/os/src/os_msys.c",
            "kernel/os/selftest/src/testcases/os_msys_test_cases.c",
        )

        self.assertEqual(filter_candidates.classify(candidate, "mynewt"), (True, "ok"))

    def test_rejects_source_change_without_native_selftest(self) -> None:
        candidate = pr("kernel/os/src/os_msys.c")

        self.assertEqual(
            filter_candidates.classify(candidate, "mynewt"),
            (False, "touches no native selftest package"),
        )

    def test_defers_uncertain_issue_provenance_to_enrichment(self) -> None:
        candidate = pr(
            "kernel/os/src/os_msys.c",
            "kernel/os/selftest/src/testcases/os_msys_test_cases.c",
            body="Adds coverage but does not close an issue.",
        )

        self.assertEqual(filter_candidates.classify(candidate, "mynewt"), (True, "ok"))

    def test_extracts_standard_same_repo_closing_issue_forms(self) -> None:
        body = """
        Fix #11
        Fixed: #12
        closes apache/mynewt-core#13
        Resolved https://github.com/apache/mynewt-core/issues/14
        See https://github.com/apache/mynewt-core/issues/15 for context.
        Fixes other/project#99
        """

        self.assertEqual(
            filter_candidates.issue_numbers_from_body(
                body, "apache/mynewt-core"
            ),
            [11, 12, 13, 14, 15],
        )

    def test_combines_body_and_github_linked_issues_without_duplicates(self) -> None:
        with patch.object(
            filter_candidates,
            "gh_graphql",
            return_value={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "closingIssuesReferences": {
                                "nodes": [
                                    {
                                        "number": 12,
                                        "repository": {
                                            "nameWithOwner": "apache/mynewt-core"
                                        },
                                    },
                                    {
                                        "number": 15,
                                        "repository": {
                                            "nameWithOwner": "apache/mynewt-core"
                                        },
                                    },
                                    {
                                        "number": 15,
                                        "repository": {
                                            "nameWithOwner": "other/project"
                                        },
                                    },
                                    {
                                        "number": 77,
                                        "repository": {
                                            "nameWithOwner": "other/project"
                                        },
                                    },
                                ]
                            }
                        }
                    }
                }
            },
        ):
            numbers = filter_candidates.linked_issue_numbers(
                "apache/mynewt-core", 42, "Fixes #12"
            )

        self.assertEqual(numbers, [12, 15])

    def test_zephyr_filter_behavior_is_unchanged(self) -> None:
        candidate = pr("kernel/thread.c", "tests/kernel/thread/src/main.c", body="")

        self.assertEqual(filter_candidates.classify(candidate, "zephyr"), (True, "ok"))


if __name__ == "__main__":
    unittest.main()
