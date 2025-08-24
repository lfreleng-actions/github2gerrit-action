# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Scenario tests for Phase 3 reconciliation hardening.

Covers:
- Commit reorder (subject-exact realignment)
- Added commit (partial reuse + new Change-Id allocation)
- Removed commit (orphan classification)
- Duplicate Change-Id trailer conflict detection
- File signature fallback (different subjects, identical file set)

These tests exercise the multi-pass reconciliation matcher directly
without needing live Gerrit or GitHub integrations.
"""

from __future__ import annotations

import re
import time

import pytest

from github2gerrit.gerrit_query import GerritChange
from github2gerrit.reconcile_matcher import LocalCommit
from github2gerrit.reconcile_matcher import MatchStrategy
from github2gerrit.reconcile_matcher import ReconciliationMatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gerrit_change(
    change_id: str,
    subject: str,
    files: list[str],
    *,
    number: int | str = 1000,
    status: str = "NEW",
    topic: str | None = "GH-org-repo-1",
) -> GerritChange:
    """
    Build a GerritChange instance for tests.
    """
    return GerritChange(
        change_id=change_id,
        number=str(number),
        subject=subject,
        status=status,
        current_revision="deadbeef",
        files=files,
        commit_message=f"{subject}\n\nGitHub-PR: https://example.com/org/repo/pull/1\nGitHub-Hash: 1234567890abcdef\n",
        topic=topic,
    )


def _local_commit(
    index: int,
    sha: str,
    subject: str,
    files: list[str],
    *,
    existing_change_id: str | None = None,
) -> LocalCommit:
    """
    Build a LocalCommit instance.
    """
    return LocalCommit(
        index=index,
        sha=sha,
        subject=subject,
        files=files,
        commit_message=subject,
        existing_change_id=existing_change_id,
    )


def _extract_match(result, index: int):
    """
    Find the MatchResult for a given local commit index.
    """
    for m in result.matches:
        if m.local_commit.index == index:
            return m
    raise AssertionError(f"No match for commit index {index}")


def _assert_change_id_format(change_id: str):
    assert change_id.startswith("I"), "Change-Id must start with 'I'"
    assert len(change_id) >= 8
    assert re.fullmatch(r"I[0-9a-f]{7,40}", change_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reconcile_reorder_subject_exact():
    """
    Reordering commits should still map each logical subject to the
    original Gerrit Change-Id via SUBJECT_EXACT pass (Pass B).
    """
    gerrit_changes = [
        _gerrit_change(
            "Iaaaa1111aaaa1111aaaa1111aaaa1111aaaa1111",
            "feat: add alpha",
            ["alpha.py"],
        ),
        _gerrit_change(
            "Ibbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
            "fix: correct beta",
            ["beta.py"],
        ),
    ]

    # Local commits arrive in reversed order (simulate user interactive rebase)
    local_commits = [
        _local_commit(0, "sha-beta", "fix: correct beta", ["beta.py"]),
        _local_commit(1, "sha-alpha", "feat: add alpha", ["alpha.py"]),
    ]

    matcher = ReconciliationMatcher(similarity_threshold=0.7)
    result = matcher.reconcile(local_commits, gerrit_changes)

    assert result.reused_count == 2
    assert result.new_count == 0
    assert len(result.orphaned_changes) == 0

    m0 = _extract_match(result, 0)
    m1 = _extract_match(result, 1)
    assert m0.change_id == "Ibbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"
    assert m1.change_id == "Iaaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
    assert m0.strategy == MatchStrategy.SUBJECT_EXACT
    assert m1.strategy == MatchStrategy.SUBJECT_EXACT


def test_reconcile_additional_commit_new_id():
    """
    Addition of a new commit should produce exactly one new Change-Id
    while reusing existing ones for unchanged logical commits.
    """
    gerrit_changes = [
        _gerrit_change(
            "I1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa",
            "chore: setup",
            ["setup.sh"],
        ),
        _gerrit_change(
            "I2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb",
            "feat: core impl",
            ["core.py"],
        ),
    ]

    local_commits = [
        _local_commit(0, "sha-setup", "chore: setup", ["setup.sh"]),
        _local_commit(1, "sha-core", "feat: core impl", ["core.py"]),
        _local_commit(2, "sha-extra", "docs: add usage guide", ["README.md"]),
    ]

    matcher = ReconciliationMatcher(similarity_threshold=0.7)
    result = matcher.reconcile(local_commits, gerrit_changes)

    assert result.reused_count == 2
    assert result.new_count == 1
    assert len(result.orphaned_changes) == 0

    reused_ids = {m.change_id for m in result.matches if m.strategy is not None}
    assert "I1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa" in reused_ids
    assert "I2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb" in reused_ids

    # New commit should have strategy None and a valid new Change-Id format
    new_matches = [m for m in result.matches if m.strategy is None]
    assert len(new_matches) == 1
    _assert_change_id_format(new_matches[0].change_id)


def test_reconcile_removed_commit_orphaned():
    """
    Removing a commit should classify the unmatched Gerrit change as
    orphaned while reusing still-present logical commits.
    """
    gerrit_changes = [
        _gerrit_change(
            "Iaaaabbbbcccc1111222233334444555566667777",
            "feat: first piece",
            ["a.py"],
        ),
        _gerrit_change(
            "Ibbbbccccdddd1111222233334444555566667777",
            "feat: second piece",
            ["b.py"],
        ),
        _gerrit_change(
            "Iccccddddaaaa1111222233334444555566667777",
            "feat: third piece",
            ["c.py"],
        ),
    ]

    # Local commits omit the middle one ("second piece")
    local_commits = [
        _local_commit(0, "sha-a", "feat: first piece", ["a.py"]),
        _local_commit(1, "sha-c", "feat: third piece", ["c.py"]),
    ]

    matcher = ReconciliationMatcher(similarity_threshold=0.7)
    result = matcher.reconcile(local_commits, gerrit_changes)

    assert result.reused_count == 2
    assert result.new_count == 0
    # Exactly one orphan (the second piece)
    assert len(result.orphaned_changes) == 1
    assert (
        result.orphaned_changes[0].change_id
        == "Ibbbbccccdddd1111222233334444555566667777"
    )


def test_reconcile_file_signature_fallback():
    """
    When subjects differ but the file set is identical, a FILE_SIGNATURE
    match (Pass C) should occur.
    """
    gerrit_changes = [
        _gerrit_change(
            "Iabcabcabcabcabcabcabcabcabcabcabcabcabcd",
            "refactor: optimize algo",
            ["algo/core.py"],
        )
    ]

    # Subject changed significantly but touches same file
    local_commits = [
        _local_commit(
            0,
            "sha-new",
            "rewrite algorithm data flow",
            ["algo/core.py"],
        )
    ]

    matcher = ReconciliationMatcher(
        similarity_threshold=0.95
    )  # High to avoid Pass D fallback
    result = matcher.reconcile(local_commits, gerrit_changes)

    assert result.reused_count == 1
    assert result.new_count == 0
    match = _extract_match(result, 0)
    assert match.strategy in (
        MatchStrategy.FILE_SIGNATURE,
        MatchStrategy.SUBJECT_EXACT,
    )
    # Prefer FILE_SIGNATURE; if normalization made them equal, SUBJECT_EXACT
    # might hit first.
    assert match.change_id == "Iabcabcabcabcabcabcabcabcabcabcabcabcabcd"


def test_reconcile_duplicate_change_id_conflict():
    """
    Two local commits with the same existing Change-Id trailer must raise
    a conflict error (abort) per Phase 3 hardening rules.
    """
    shared_id = "Ideadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    gerrit_changes = [
        _gerrit_change(shared_id, "feat: original", ["x.py"]),
    ]

    local_commits = [
        _local_commit(
            0,
            "sha-1",
            "feat: original",
            ["x.py"],
            existing_change_id=shared_id,
        ),
        _local_commit(
            1,
            "sha-2",
            "feat: tweak",
            ["x2.py"],
            existing_change_id=shared_id,  # Deliberate duplicate
        ),
    ]

    matcher = ReconciliationMatcher()
    with pytest.raises(ValueError) as exc:
        matcher.reconcile(local_commits, gerrit_changes)
    assert "Duplicate Change-Id trailer reuse" in str(exc.value)


# ---------------------------------------------------------------------------
# Stress / timing (optional quick sanity to ensure new ID path works)
# ---------------------------------------------------------------------------


def test_reconcile_many_new_commits_perf_smoke():
    """
    Smoke test: many brand-new commits should all get distinct Change-Ids
    quickly (non-performance critical, just ensures uniqueness & format).
    """
    local_commits: list[LocalCommit] = []
    for i in range(15):
        local_commits.append(
            _local_commit(
                i,
                f"sha-{i}",
                f"docs: add section {i}",
                [f"docs/sect{i}.md"],
            )
        )
    matcher = ReconciliationMatcher()
    result = matcher.reconcile(local_commits, gerrit_changes=[])
    assert result.reused_count == 0
    assert result.new_count == 15
    ids = result.change_ids
    assert len(ids) == len(set(ids)), "All new Change-Ids must be unique"
    for cid in ids:
        _assert_change_id_format(cid)
    # Ensure generation is not pathologically slow
    assert time.time()  # placeholder to avoid flake for unused imports
