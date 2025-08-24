# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
#
# Tests covering:
#  - Phase 1: PR metadata trailer construction (GitHub-PR, GitHub-Hash)
#  - Phase 2: Change-Id mapping comment format parsing
#  - Phase 3: Reconciliation of prior Change-Ids via mapping comment
#
# These tests exercise internal helpers in a constrained, side-effect-free
# manner (no real git operations). They rely on lightweight fakes for the
# GitHub API protocol objects used by Orchestrator reconciliation logic.

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from github2gerrit.core import Orchestrator
from github2gerrit.models import GitHubContext


# ---------------------------------------------------------------------------
# Helper factories / fakes
# ---------------------------------------------------------------------------


def gh_ctx(
    *,
    pr_number: int | None = 42,
    repository: str = "acme/widget",
    server_url: str = "https://github.example",
) -> GitHubContext:
    return GitHubContext(
        event_name="pull_request",
        event_action="synchronize",
        event_path=None,
        repository=repository,
        repository_owner=repository.split("/")[0],
        server_url=server_url,
        run_id="999",
        sha="deadbeefcafebabe",
        base_ref="main",
        head_ref="feature/xyz",
        pr_number=pr_number,
    )


@dataclass
class _Comment:
    body: str | None


class _Issue:
    def __init__(self, comments: Iterable[_Comment]) -> None:
        self._comments = list(comments)

    def get_comments(self) -> Iterable[_Comment]:
        return self._comments


class _Pull:
    def __init__(self, pr_number: int, comments: Iterable[_Comment]) -> None:
        self.number = pr_number
        self._issue = _Issue(comments)

    def as_issue(self) -> _Issue:
        return self._issue

    # For robustness (core may access title/body in other paths)
    title: str | None = "Sample Title"
    body: str | None = "Body"


class _Repo:
    def __init__(self, pull: _Pull) -> None:
        self._pull = pull

    def get_pull(self, number: int) -> _Pull:  # pragma: no cover - trivial
        assert number == self._pull.number
        return self._pull


class _Client:
    def __init__(self, repo: _Repo) -> None:
        self._repo = repo

    def get_repo(self, full: str) -> _Repo:  # pragma: no cover - trivial
        return self._repo


# ---------------------------------------------------------------------------
# Tests for metadata trailer generation
# ---------------------------------------------------------------------------


def test_metadata_trailers_include_pr_and_hash(tmp_path: Path) -> None:
    orch = Orchestrator(workspace=tmp_path)
    gh = gh_ctx(pr_number=77)
    trailers = orch._build_pr_metadata_trailers(gh)
    # Expect exactly two lines: GitHub-PR and GitHub-Hash
    assert any(t.startswith("GitHub-PR: ") for t in trailers), (
        "Missing GitHub-PR trailer"
    )
    assert any(t.startswith("GitHub-Hash: ") for t in trailers), (
        "Missing GitHub-Hash trailer"
    )
    # Determinism: repeated calls should yield identical set (order stable)
    trailers2 = orch._build_pr_metadata_trailers(gh)
    assert trailers == trailers2


def test_metadata_trailers_absent_when_no_pr_number(tmp_path: Path) -> None:
    orch = Orchestrator(workspace=tmp_path)
    gh = gh_ctx(pr_number=None)
    trailers = orch._build_pr_metadata_trailers(gh)
    assert trailers == []


def test_metadata_trailers_idempotent_append_simulation(tmp_path: Path) -> None:
    """
    Simulate the commit amend logic that only appends missing trailers.
    """
    orch = Orchestrator(workspace=tmp_path)
    gh = gh_ctx(pr_number=101)
    meta = orch._build_pr_metadata_trailers(gh)
    base_message = "Title line\n\nSome body text."
    # First append
    combined = base_message + "\n" + "\n".join(meta)
    # Simulate second pass (should not duplicate)
    needed = [m for m in meta if m not in combined]
    assert not needed, "Expected no additional trailers needed on second pass"


# ---------------------------------------------------------------------------
# Tests for parsing previously published Change-Id mapping comments
# ---------------------------------------------------------------------------
# NOTE: Legacy parsing tests removed - functionality now handled by
# the modern reconciliation system in orchestrator/reconciliation.py
# Mapping comment parsing is tested in test_mapping_comment_*.py files
