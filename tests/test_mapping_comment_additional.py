# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Additional coverage tests for mapping_comment utilities.

Focus:
- Validation errors in ChangeIdMapping
- serialize / parse round-trip variations
- Incomplete / malformed mapping blocks
- Mapping update replacement logic
- Digest stability and order sensitivity
- Consistency validation (PR URL, GitHub-Hash mismatches)
- find_mapping_comments indexing
"""

from __future__ import annotations

import re

import pytest

from github2gerrit.mapping_comment import ChangeIdMapping
from github2gerrit.mapping_comment import compute_mapping_digest
from github2gerrit.mapping_comment import find_mapping_comments
from github2gerrit.mapping_comment import parse_mapping_comments
from github2gerrit.mapping_comment import serialize_mapping_comment
from github2gerrit.mapping_comment import update_mapping_comment_body
from github2gerrit.mapping_comment import validate_mapping_consistency


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_ids(n: int) -> list[str]:
    return [f"I{hex(i)[2:]:0>39}"[:41] for i in range(1, n + 1)]


def _make_mapping_comment(
    *,
    pr_url: str = "https://github.com/org/repo/pull/7",
    mode: str = "multi-commit",
    topic: str = "GH-org-repo-7",
    change_ids: list[str] | None = None,
    gh_hash: str = "abcdef1234567890",
) -> str:
    change_ids = change_ids or _valid_ids(2)
    return serialize_mapping_comment(
        pr_url=pr_url,
        mode=mode,
        topic=topic,
        change_ids=change_ids,
        github_hash=gh_hash,
    )


# ---------------------------------------------------------------------------
# ChangeIdMapping validation
# ---------------------------------------------------------------------------


def test_change_id_mapping_invalid_mode():
    with pytest.raises(ValueError) as exc:
        ChangeIdMapping(
            pr_url="u",
            mode="invalid-mode",
            topic="t",
            change_ids=_valid_ids(1),
            github_hash="h",
        )
    assert "Invalid mode" in str(exc.value)


def test_change_id_mapping_no_change_ids():
    with pytest.raises(ValueError) as exc:
        ChangeIdMapping(
            pr_url="u",
            mode="squash",
            topic="t",
            change_ids=[],
            github_hash="h",
        )
    assert "At least one Change-ID" in str(exc.value)


def test_change_id_mapping_invalid_change_id_format():
    with pytest.raises(ValueError) as exc:
        ChangeIdMapping(
            pr_url="u",
            mode="squash",
            topic="t",
            change_ids=["badid123"],
            github_hash="h",
        )
    assert "Invalid Change-ID format" in str(exc.value)


def test_serialize_mapping_comment_requires_non_empty_ids():
    with pytest.raises(ValueError):
        serialize_mapping_comment(
            pr_url="u",
            mode="multi-commit",
            topic="t",
            change_ids=[],
            github_hash="h",
        )


# ---------------------------------------------------------------------------
# Parsing logic
# ---------------------------------------------------------------------------


def test_parse_mapping_comments_returns_latest_valid():
    first = _make_mapping_comment(change_ids=_valid_ids(1))
    second = _make_mapping_comment(change_ids=_valid_ids(2))
    comments = ["noise", first, "other", second]
    mapping = parse_mapping_comments(comments)
    assert mapping
    assert mapping.change_ids == _valid_ids(2)


def test_parse_mapping_comments_ignores_incomplete():
    # Missing Topic line
    incomplete = "\n".join(
        [
            "<!-- github2gerrit:change-id-map v1 -->",
            "PR: x",
            "Mode: multi-commit",
            "Change-Ids:",
            "  I1234567",
            "<!-- end github2gerrit:change-id-map -->",
        ]
    )
    mapping = parse_mapping_comments([incomplete])
    assert mapping is None


def test_parse_mapping_comments_handles_malformed_block():
    # End marker before start
    malformed = (
        "<!-- end github2gerrit:change-id-map -->\n<!--"
        "github2gerrit:change-id-map v1 -->"
    )
    assert parse_mapping_comments([malformed]) is None


def test_parse_mapping_comments_deduplicates_ids():
    dup_block = "\n".join(
        [
            "<!-- github2gerrit:change-id-map v1 -->",
            "PR: https://github.com/org/repo/pull/7",
            "Mode: multi-commit",
            "Topic: GH-org-repo-7",
            "Change-Ids:",
            "  I1111111111111111111111111111111111111111",
            "  I1111111111111111111111111111111111111111",
            "GitHub-Hash: hhhhhhhhhhhhhhhh",
            "<!-- end github2gerrit:change-id-map -->",
        ]
    )
    mapping = parse_mapping_comments([dup_block])
    assert mapping
    assert mapping.change_ids == ["I1111111111111111111111111111111111111111"]


# ---------------------------------------------------------------------------
# Mapping replacement
# ---------------------------------------------------------------------------


def test_update_mapping_comment_body_replaces_existing():
    original = _make_mapping_comment(change_ids=_valid_ids(1))
    new_mapping = ChangeIdMapping(
        pr_url="https://github.com/org/repo/pull/7",
        mode="multi-commit",
        topic="GH-org-repo-7",
        change_ids=_valid_ids(3),
        github_hash="zzzzzzzzzzzzzzzz",
    )
    updated = update_mapping_comment_body(original, new_mapping)
    assert updated.count("Change-Ids:") == 1
    for cid in _valid_ids(3):
        assert cid in updated
    # Original single ID should appear exactly once (no duplication)
    assert updated.count(_valid_ids(1)[0]) == 1


def test_update_mapping_comment_body_appends_when_missing():
    original = "Initial discussion comment."
    new_mapping = ChangeIdMapping(
        pr_url="https://github.com/org/repo/pull/7",
        mode="squash",
        topic="GH-org-repo-7",
        change_ids=_valid_ids(2),
        github_hash="1234abcd",
    )
    updated = update_mapping_comment_body(original, new_mapping)
    assert "Initial discussion comment." in updated
    assert "Change-Ids:" in updated
    assert updated.endswith("-->"), "Should include end marker"


# ---------------------------------------------------------------------------
# find_mapping_comments
# ---------------------------------------------------------------------------


def test_find_mapping_comments_indexes():
    c1 = "No mapping here"
    c2 = _make_mapping_comment()
    c3 = "Another note"
    c4 = _make_mapping_comment(change_ids=_valid_ids(1))
    indices = find_mapping_comments([c1, c2, c3, c4])
    assert indices == [1, 3]


# ---------------------------------------------------------------------------
# Digest computation
# ---------------------------------------------------------------------------


def test_compute_mapping_digest_deterministic_and_order_sensitive():
    ids_a = ["I1111111111111111111111111111111111111111", "I2222"]
    # Normalize second id to valid length for test
    ids_a[1] = "I2222222222222222222222222222222222222222"
    digest1 = compute_mapping_digest(ids_a)
    digest2 = compute_mapping_digest(ids_a)
    assert re.fullmatch(r"[0-9a-f]{12}", digest1)
    assert digest1 == digest2
    # Order change should alter digest
    digest3 = compute_mapping_digest(list(reversed(ids_a)))
    assert digest3 != digest1


# ---------------------------------------------------------------------------
# Consistency validation
# ---------------------------------------------------------------------------


def test_validate_mapping_consistency_mismatched_pr_url():
    mapping = ChangeIdMapping(
        pr_url="https://x/pr/1",
        mode="multi-commit",
        topic="t",
        change_ids=_valid_ids(1),
        github_hash="deadbeef",
    )
    ok = validate_mapping_consistency(
        mapping,
        expected_pr_url="https://y/pr/1",
        expected_github_hash="deadbeef",
    )
    assert ok is False


def test_validate_mapping_consistency_mismatched_hash():
    mapping = ChangeIdMapping(
        pr_url="https://x/pr/1",
        mode="squash",
        topic="t",
        change_ids=_valid_ids(1),
        github_hash="aaaa",
    )
    ok = validate_mapping_consistency(
        mapping,
        expected_pr_url="https://x/pr/1",
        expected_github_hash="bbbb",
    )
    assert ok is False


def test_validate_mapping_consistency_success():
    mapping = ChangeIdMapping(
        pr_url="https://x/pr/1",
        mode="squash",
        topic="t",
        change_ids=_valid_ids(1),
        github_hash="match",
    )
    ok = validate_mapping_consistency(
        mapping,
        expected_pr_url="https://x/pr/1",
        expected_github_hash="match",
    )
    assert ok is True


# ---------------------------------------------------------------------------
# Additional reconciliation coverage (Phase 1 extraction)
# ---------------------------------------------------------------------------


def test_reconciliation_all_new_ids_no_topic_no_comment(monkeypatch):
    """
    When strategy 'topic' yields no Gerrit changes and no comment fallback
    is enabled, all commits receive brand new Change-Ids.
    """
    from types import SimpleNamespace

    from github2gerrit.orchestrator import perform_reconciliation
    from github2gerrit.reconcile_matcher import LocalCommit

    class GH:
        server_url = "https://github.com"
        repository = "org/repo"
        repository_owner = "org"
        pr_number = 9

    class Gerrit:
        host = "gerrit.example.org"
        port = 29418

    # Force empty topic results
    from github2gerrit.orchestrator import reconciliation as rmod

    monkeypatch.setattr(rmod, "query_changes_by_topic", lambda *a, **k: [])

    inputs = SimpleNamespace(
        reuse_strategy="topic",
        allow_orphan_changes=False,
        similarity_subject=0.7,
        log_reconcile_json=False,
    )
    commits = [
        LocalCommit(
            index=i,
            sha=f"sha{i}",
            subject=f"feat: part {i}",
            files=[f"f{i}.py"],
            commit_message=f"feat: part {i}",
            existing_change_id=None,
        )
        for i in range(3)
    ]
    result = perform_reconciliation(
        inputs=inputs,
        gh=GH(),
        gerrit=Gerrit(),
        local_commits=commits,
    )
    assert len(result) == 3
    assert all(cid.startswith("I") for cid in result)
    assert len(set(result)) == 3


def test_reconciliation_topic_query_exception(monkeypatch):
    """
    Exception during topic query should be caught and produce new IDs
    (fallback path).
    """
    from types import SimpleNamespace

    from github2gerrit.orchestrator import perform_reconciliation
    from github2gerrit.orchestrator import reconciliation as rmod
    from github2gerrit.reconcile_matcher import LocalCommit

    class GH:
        server_url = "https://github.com"
        repository = "org/repo"
        repository_owner = "org"
        pr_number = 11

    class Gerrit:
        host = "gerrit.example.org"
        port = 29418

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rmod, "query_changes_by_topic", _boom)

    inputs = SimpleNamespace(
        reuse_strategy="topic",
        allow_orphan_changes=False,
        similarity_subject=0.7,
        log_reconcile_json=False,
    )
    commits = [
        LocalCommit(
            index=0,
            sha="sha0",
            subject="feat: unit",
            files=["x.py"],
            commit_message="feat: unit",
            existing_change_id=None,
        )
    ]
    result = perform_reconciliation(
        inputs=inputs,
        gh=GH(),
        gerrit=Gerrit(),
        local_commits=commits,
    )
    assert len(result) == 1
    assert result[0].startswith("I")
