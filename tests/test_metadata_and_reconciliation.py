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

import pytest

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
    assert any(t.startswith("GitHub-PR: ") for t in trailers), "Missing GitHub-PR trailer"
    assert any(t.startswith("GitHub-Hash: ") for t in trailers), "Missing GitHub-Hash trailer"
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


def test_parse_single_mapping_block(tmp_path: Path) -> None:
    orch = Orchestrator(workspace=tmp_path)
    mapping = """
<!-- github2gerrit:change-id-map v1 -->
PR: https://github.example/acme/widget/pull/42
Mode: multi-commit
Topic: GH-widget-42
Change-Ids:
  Iabcdef1234567890
  I1111222233334444
GitHub-Hash: deadbeefcafebabe
<!-- end github2gerrit:change-id-map -->
"""
    result = orch._parse_previous_change_id_map([mapping])
    assert result == ["Iabcdef1234567890", "I1111222233334444"]


def test_parse_latest_mapping_block_wins(tmp_path: Path) -> None:
    orch = Orchestrator(workspace=tmp_path)
    old = """
<!-- github2gerrit:change-id-map v1 -->
PR: https://x/y/pull/1
Mode: multi-commit
Topic: GH-y-1
Change-Ids:
  Iold1111
<!-- end github2gerrit:change-id-map -->
"""
    new = """
Noise before
<!-- github2gerrit:change-id-map v1 -->
PR: https://x/y/pull/1
Mode: multi-commit
Topic: GH-y-1
Change-Ids:
  Inew2222
  Inew3333
GitHub-Hash: cafe0000cafe
<!-- end github2gerrit:change-id-map -->
Trailing noise
"""
    result = orch._parse_previous_change_id_map([old, new])
    assert result == ["Inew2222", "Inew3333"]


def test_parse_ignores_malformed_blocks(tmp_path: Path) -> None:
    orch = Orchestrator(workspace=tmp_path)
    malformed = """
<!-- github2gerrit:change-id-map v1 -->
PR: missing end tag and no change ids
"""
    result = orch._parse_previous_change_id_map([malformed])
    assert result == []


# ---------------------------------------------------------------------------
# Tests for reconciliation end-to-end (without real git)
# ---------------------------------------------------------------------------


def test_reconciliation_returns_previous_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Arrange prior comment with mapping
    mapping = """
Intro text
<!-- github2gerrit:change-id-map v1 -->
PR: https://github.example/acme/widget/pull/42
Mode: multi-commit
Topic: GH-widget-42
Change-Ids:
  Iabc1234567890
  Idef2222aaaa
GitHub-Hash: feedfeedfeedfeed
<!-- end github2gerrit:change-id-map -->
"""
    pull = _Pull(pr_number=42, comments=[_Comment(mapping)])
    repo = _Repo(pull)
    client = _Client(repo)
    orch = Orchestrator(workspace=tmp_path)
    gh = gh_ctx(pr_number=42)
    # Ensure environment satisfies get_repo_from_env
    monkeypatch.setenv("GITHUB_REPOSITORY", gh.repository)

    # Patch GitHub API helpers used inside reconciliation
    # Patch underlying github_api module symbols (imported inside helper)
    monkeypatch.setattr("github2gerrit.github_api.build_client", lambda: client)
    monkeypatch.setattr("github2gerrit.github_api.get_repo_from_env", lambda _c: repo)
    monkeypatch.setattr("github2gerrit.github_api.get_pull", lambda _r, n: pull)

    reuse = orch._attempt_change_id_reconciliation(gh)
    assert reuse == ["Iabc1234567890", "Idef2222aaaa"]


def test_reconciliation_no_pr_number(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pull = _Pull(pr_number=99, comments=[])
    repo = _Repo(pull)
    client = _Client(repo)
    orch = Orchestrator(workspace=tmp_path)
    gh = gh_ctx(pr_number=None)

    monkeypatch.setattr("github2gerrit.core.build_client", lambda: client)
    monkeypatch.setattr("github2gerrit.core.get_repo_from_env", lambda _c: repo)
    monkeypatch.setattr("github2gerrit.core.get_pull", lambda _r, n: pull)

    assert orch._attempt_change_id_reconciliation(gh) == []


def test_reconciliation_latest_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    old_map = """
<!-- github2gerrit:change-id-map v1 -->
PR: x
Mode: multi-commit
Topic: T
Change-Ids:
  IOld1111a
<!-- end github2gerrit:change-id-map -->
"""
    new_map = """
<!-- github2gerrit:change-id-map v1 -->
PR: x
Mode: multi-commit
Topic: T
Change-Ids:
  INew2222b
  INew3333c
<!-- end github2gerrit:change-id-map -->
"""
    pull = _Pull(pr_number=7, comments=[_Comment(old_map), _Comment(new_map)])
    repo = _Repo(pull)
    client = _Client(repo)
    orch = Orchestrator(workspace=tmp_path)
    gh = gh_ctx(pr_number=7)
    monkeypatch.setenv("GITHUB_REPOSITORY", gh.repository)

    monkeypatch.setattr("github2gerrit.github_api.build_client", lambda: client)
    monkeypatch.setattr("github2gerrit.github_api.get_repo_from_env", lambda _c: repo)
    monkeypatch.setattr("github2gerrit.github_api.get_pull", lambda _r, n: pull)

    reuse = orch._attempt_change_id_reconciliation(gh)
    assert reuse == ["INew2222b", "INew3333c"]


# ---------------------------------------------------------------------------
# Defensive test: unchanged when no mapping comment exists
# ---------------------------------------------------------------------------


def test_reconciliation_no_mapping_comments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pull = _Pull(pr_number=55, comments=[_Comment("Just a regular comment")])
    repo = _Repo(pull)
    client = _Client(repo)
    orch = Orchestrator(workspace=tmp_path)
    gh = gh_ctx(pr_number=55)

    monkeypatch.setattr("github2gerrit.github_api.build_client", lambda: client)
    monkeypatch.setattr("github2gerrit.github_api.get_repo_from_env", lambda _c: repo)
    monkeypatch.setattr("github2gerrit.github_api.get_pull", lambda _r, n: pull)

    assert orch._attempt_change_id_reconciliation(gh) == []
