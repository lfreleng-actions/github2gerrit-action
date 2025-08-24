# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for extracted reconciliation module (Phase 1).

Goals:
- Cover early strategy=none short-circuit
- Exercise topic-based reuse path (validated by PR URL + GitHub-Hash)
- Exercise comment-based fallback + extension of mapping
- Exercise JSON summary emission (log_reconcile_json=True)
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from github2gerrit.gerrit_query import GerritChange
from github2gerrit.orchestrator import perform_reconciliation
from github2gerrit.orchestrator import reconciliation as recon_mod
from github2gerrit.reconcile_matcher import LocalCommit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_commit(idx: int, sha: str, subject: str) -> LocalCommit:
    return LocalCommit(
        index=idx,
        sha=sha,
        subject=subject,
        files=[f"file_{idx}.py"],
        commit_message=subject,
        existing_change_id=None,
    )


def _gerrit_change(
    change_id: str, subject: str, pr_url: str, gh_hash: str
) -> GerritChange:
    return GerritChange(
        change_id=change_id,
        number="1001",
        subject=subject,
        status="NEW",
        current_revision="deadbeef",
        files=["file_0.py"],
        commit_message=(
            f"{subject}\n\nReference: {pr_url}\nGitHub-Hash: {gh_hash}\n"
        ),
        topic="GH-org-repo-5",
    )


class _DummyGH:
    server_url = "https://github.com"
    repository = "org/repo"
    repository_owner = "org"
    pr_number = 5


class _DummyGerrit:
    host = "gerrit.example.org"
    port = 29418


def _inputs(
    *,
    reuse_strategy: str,
    allow_orphan_changes: bool = False,
    similarity_subject: float = 0.7,
    log_reconcile_json: bool = False,
):
    return SimpleNamespace(
        reuse_strategy=reuse_strategy,
        allow_orphan_changes=allow_orphan_changes,
        similarity_subject=similarity_subject,
        log_reconcile_json=log_reconcile_json,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reconciliation_strategy_none_returns_empty():
    gh = _DummyGH()
    gerrit = _DummyGerrit()
    inputs = _inputs(reuse_strategy="none")
    local_commits = [_local_commit(0, "a1", "feat: one")]
    result = perform_reconciliation(
        inputs=inputs,
        gh=gh,
        gerrit=gerrit,
        local_commits=local_commits,
    )
    assert result == [], "Strategy 'none' must suppress reconciliation"


def test_reconciliation_topic_reuse_path(monkeypatch: pytest.MonkeyPatch):
    gh = _DummyGH()
    gerrit = _DummyGerrit()
    inputs = _inputs(reuse_strategy="topic")
    pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"
    gh_hash = "abc123hash"

    # Provide an existing Gerrit change referencing PR + hash
    change = _gerrit_change(
        "I1111111111111111111111111111111111111111",
        "feat: initial",
        pr_url,
        gh_hash,
    )

    # Monkeypatch topic query to return the change
    monkeypatch.setattr(
        recon_mod, "query_changes_by_topic", lambda *a, **k: [change]
    )

    local_commits = [_local_commit(0, "sha0", "feat: initial")]
    result = perform_reconciliation(
        inputs=inputs,
        gh=gh,
        gerrit=gerrit,
        local_commits=local_commits,
        expected_github_hash=gh_hash,
    )
    assert result == [change.change_id], "Expected reuse of existing Change-Id"


def test_reconciliation_comment_fallback_extension(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    When topic path yields no changes and comment mapping exists with
    fewer entries than local commits, new IDs must extend mapping.
    """
    gh = _DummyGH()
    gerrit = _DummyGerrit()
    inputs = _inputs(reuse_strategy="topic+comment")

    # Empty topic result
    monkeypatch.setattr(recon_mod, "query_changes_by_topic", lambda *a, **k: [])

    # Simulate recovered mapping with two IDs
    base_ids = [
        "Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "Ibbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    monkeypatch.setattr(
        recon_mod,
        "_attempt_comment_based_reuse",
        lambda **k: base_ids,
    )

    local_commits = [
        _local_commit(0, "sha0", "feat: A"),
        _local_commit(1, "sha1", "feat: B"),
        _local_commit(2, "sha2", "feat: C"),
    ]

    result = perform_reconciliation(
        inputs=inputs,
        gh=gh,
        gerrit=gerrit,
        local_commits=local_commits,
    )

    assert len(result) == 3
    assert result[0:2] == base_ids
    assert result[2].startswith("I")
    assert result[2] not in base_ids


def test_reconciliation_json_summary_emitted(
    caplog, monkeypatch: pytest.MonkeyPatch
):
    """
    Enabling log_reconcile_json should emit a RECONCILE_SUMMARY log line.
    """
    caplog.set_level(logging.INFO)
    gh = _DummyGH()
    gerrit = _DummyGerrit()
    inputs = _inputs(reuse_strategy="topic", log_reconcile_json=True)
    pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"
    gh_hash = "def456hash"

    change = _gerrit_change(
        "I2222222222222222222222222222222222222222",
        "feat: second",
        pr_url,
        gh_hash,
    )
    monkeypatch.setattr(
        recon_mod, "query_changes_by_topic", lambda *a, **k: [change]
    )

    local_commits = [_local_commit(0, "shaX", "feat: second")]
    result = perform_reconciliation(
        inputs=inputs,
        gh=gh,
        gerrit=gerrit,
        local_commits=local_commits,
        expected_github_hash=gh_hash,
    )

    assert result == [change.change_id]
    summary_lines = [
        rec.message
        for rec in caplog.records
        if "RECONCILE_SUMMARY" in rec.message
    ]
    assert summary_lines, "Expected a RECONCILE_SUMMARY log line"
    # Basic shape check (compact JSON separators)
    assert '"reused": 1' in summary_lines[-1]
    assert '"new": 0' in summary_lines[-1]
