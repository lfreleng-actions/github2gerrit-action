# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Phase 2 tests for reconciliation plan construction and orphan policy logging.

Covers:
- Plan digest emission in RECONCILE_SUMMARY (topic reuse path)
- Orphan policy stub actions (comment / abandon / ignore)
- Comment-based mapping reuse extension still yields deterministic digest
- Reused vs new vs orphan counts reflected in summary JSON

These tests exercise the Phase 2 enhancements without depending on
network I/O (topic queries and comment parsing are monkeypatched).
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

# (removed unused List import)
import pytest

from github2gerrit.gerrit_query import GerritChange
from github2gerrit.orchestrator import reconciliation as recon_mod
from github2gerrit.orchestrator.reconciliation import perform_reconciliation
from github2gerrit.reconcile_matcher import LocalCommit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _GH:
    server_url = "https://github.com"
    repository = "org/repo"
    repository_owner = "org"
    pr_number = 42
    run_id = "99999"


class _Gerrit:
    host = "gerrit.example.org"
    port = 29418


def _lc(idx: int, sha: str, subject: str, files: list[str]) -> LocalCommit:
    return LocalCommit(
        index=idx,
        sha=sha,
        subject=subject,
        files=files,
        commit_message=subject,
        existing_change_id=None,
    )


def _gc(
    change_id: str, subject: str, files: list[str], pr_url: str, gh_hash: str
):
    return GerritChange(
        change_id=change_id,
        number="1000",
        subject=subject,
        status="NEW",
        current_revision="deadbeef",
        files=files,
        commit_message=(
            f"{subject}\n\nPR: {pr_url}\nGitHub-PR: {pr_url}\nGitHub-Hash: {gh_hash}\n"
        ),
        topic="GH-org-repo-42",
    )


def _inputs(
    *,
    reuse_strategy: str = "topic",
    log_reconcile_json: bool = True,
    orphan_policy: str = "comment",
    similarity_subject: float = 0.7,
):
    return SimpleNamespace(
        reuse_strategy=reuse_strategy,
        log_reconcile_json=log_reconcile_json,
        orphan_policy=orphan_policy,
        similarity_subject=similarity_subject,
    )


def _extract_summary(caplog) -> dict:
    """
    Find and parse the most recent RECONCILE_SUMMARY json line.
    """
    records = [
        r.message for r in caplog.records if "RECONCILE_SUMMARY" in r.message
    ]
    assert records, "Expected at least one RECONCILE_SUMMARY log line"
    last = records[-1]
    # Expect pattern: RECONCILE_SUMMARY json={...}
    m = re.search(r"RECONCILE_SUMMARY json=(\{.*\})", last)
    assert m, f"Could not parse summary JSON: {last}"
    return json.loads(m.group(1))


def _extract_orphan_actions(caplog) -> dict | None:
    lines = [r.message for r in caplog.records if "ORPHAN_ACTIONS" in r.message]
    if not lines:
        return None
    m = re.search(r"ORPHAN_ACTIONS json=(\{.*\})", lines[-1])
    if not m:
        return None
    return json.loads(m.group(1))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_topic_reuse_and_orphan_policy_comment(
    caplog, monkeypatch: pytest.MonkeyPatch
):
    """
    Topic path with two existing changes, one matched / one orphan.
    Orphan policy 'comment' should log commented array with orphan id.
    """
    caplog.set_level("INFO")
    gh = _GH()
    gerrit = _Gerrit()
    pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"
    gh_hash = "1234abcdhash"
    # Local commits -> only first Gerrit change reused.
    local_commits = [
        _lc(0, "sha-a", "feat: alpha", ["a.py"]),
    ]
    change_reused = _gc(
        "I1111111111111111111111111111111111111111",
        "feat: alpha",
        ["a.py"],
        pr_url,
        gh_hash,
    )
    change_orphan = _gc(
        "I2222222222222222222222222222222222222222",
        "feat: beta",
        ["b.py"],
        pr_url,
        gh_hash,
    )
    monkeypatch.setattr(
        recon_mod,
        "query_changes_by_topic",
        lambda *a, **k: [change_reused, change_orphan],
    )

    # Mock Gerrit REST client for comment operations
    class MockRestClient:
        def post(self, path, data=None):
            # Simulate successful REST operation
            return {"message": "comment added"}

    def mock_build_client_for_host(host, **kwargs):
        return MockRestClient()

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host",
        mock_build_client_for_host,
    )

    inputs = _inputs(orphan_policy="comment")
    result_ids = perform_reconciliation(
        inputs=inputs,
        gh=gh,
        gerrit=gerrit,
        local_commits=local_commits,
        expected_github_hash=gh_hash,
    )

    assert result_ids == [change_reused.change_id]
    summary = _extract_summary(caplog)
    assert summary["reused"] == 1
    assert summary["new"] == 0
    assert summary["orphaned"] == 1
    # digest field is no longer generated in reconciliation output
    orphan_actions = _extract_orphan_actions(caplog)
    assert orphan_actions is not None
    assert orphan_actions["commented"] == [change_orphan.change_id]
    assert orphan_actions["abandoned"] == []
    assert orphan_actions["ignored"] == []


@pytest.mark.parametrize(
    "policy,expected_field",
    [
        ("abandon", "abandoned"),
        ("ignore", "ignored"),
    ],
)
def test_orphan_policy_variants(policy, expected_field, caplog, monkeypatch):
    """
    Validate that the orphan action bucket changes with policy.
    """
    caplog.set_level("INFO")
    gh = _GH()
    # (removed unused variable; _Gerrit() passed inline)
    pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"
    gh_hash = "abcd5678hash"
    local_commits = [_lc(0, "sha-x", "feat: one", ["x.py"])]
    reused_change = _gc(
        "Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "feat: one",
        ["x.py"],
        pr_url,
        gh_hash,
    )
    orphan_change = _gc(
        "Ibbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "feat: two",
        ["y.py"],
        pr_url,
        gh_hash,
    )
    monkeypatch.setattr(
        recon_mod,
        "query_changes_by_topic",
        lambda *a, **k: [reused_change, orphan_change],
    )

    # Mock Gerrit REST client for all policies (abandon/comment operations)
    class MockRestClient:
        def post(self, path, data=None):
            # Simulate successful REST operation
            return {"message": "operation completed"}

    def mock_build_client_for_host(host, **kwargs):
        return MockRestClient()

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host",
        mock_build_client_for_host,
    )

    inputs = _inputs(orphan_policy=policy)
    _ = perform_reconciliation(
        inputs=inputs,
        gh=gh,
        gerrit=_Gerrit(),
        local_commits=local_commits,
        expected_github_hash=gh_hash,
    )
    orphan_actions = _extract_orphan_actions(caplog)
    if policy == "ignore":
        # For 'ignore' policy no orphan action log is emitted (no actions)
        assert orphan_actions is None
    else:
        assert orphan_actions is not None
        assert orphan_actions[expected_field] == [orphan_change.change_id]


def test_comment_based_reuse_extension_digest(caplog, monkeypatch):
    """
    Comment fallback path: prior mapping has one id; second commit
    appended with new Change-Id; digest reflects both.
    """
    caplog.set_level("INFO")
    gh = _GH()
    gerrit = _Gerrit()
    existing_ids = ["I1234567890abcdef1234567890abcdef12345678"]
    # Force empty topic results.
    monkeypatch.setattr(recon_mod, "query_changes_by_topic", lambda *a, **k: [])
    # Force mapping reuse.
    monkeypatch.setattr(
        recon_mod,
        "_attempt_comment_based_reuse",
        lambda **k: existing_ids,
    )

    # Mock Gerrit REST client (no orphan actions expected, but needed for
    # consistency)
    class MockRestClient:
        def post(self, path, data=None):
            return {"message": "operation completed"}

    def mock_build_client_for_host(host, **kwargs):
        return MockRestClient()

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host",
        mock_build_client_for_host,
    )

    local_commits = [
        _lc(0, "sha-1", "feat: existing", ["one.py"]),
        _lc(1, "sha-2", "feat: new", ["two.py"]),
    ]
    inputs = _inputs(reuse_strategy="topic+comment")
    ordered = perform_reconciliation(
        inputs=inputs,
        gh=gh,
        gerrit=gerrit,
        local_commits=local_commits,
    )
    assert len(ordered) == 2
    assert ordered[0] == existing_ids[0]
    summary = _extract_summary(caplog)
    assert summary["reused"] == 1
    assert summary["new"] == 1
    assert summary["orphaned"] == 0
    # digest field is no longer generated in reconciliation output
