# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for:
- Digest integration in mapping comment (serialize + parse round trip)
- Idempotent Gerrit back-reference logic (skip existing backref)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import RepoNames
from github2gerrit.mapping_comment import parse_mapping_comments
from github2gerrit.mapping_comment import serialize_mapping_comment
from github2gerrit.models import GitHubContext


# ---------------------------------------------------------------------------
# Digest in mapping comment
# ---------------------------------------------------------------------------


def test_mapping_comment_includes_and_parses_digest():
    pr_url = "https://github.com/org/repo/pull/5"
    digest = "abc123def456"
    body = serialize_mapping_comment(
        pr_url=pr_url,
        mode="multi-commit",
        topic="GH-org-repo-5",
        change_ids=[
            "I1111111111111111111111111111111111111111",
            "I2222222222222222222222222222222222222222",
        ],
        github_hash="deadbeefcafebabe",
        digest=digest,
    )
    # Ensure the digest line is present
    assert f"Digest: {digest}" in body
    # Emulate posting this body as a comment
    parsed = parse_mapping_comments([body])
    assert parsed is not None
    assert parsed.digest == digest
    assert parsed.github_hash == "deadbeefcafebabe"
    assert parsed.change_ids[0].startswith("I1111")
    assert parsed.change_ids[1].startswith("I2222")


# ---------------------------------------------------------------------------
# Idempotent back-reference comment logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force_duplicate", [False, True])
def test_idempotent_backref_skip_and_force(
    monkeypatch, tmp_path, force_duplicate
):
    """
    When an existing Gerrit change message already contains the GHPR marker,
    the back-reference for that commit should be skipped unless forced.
    """
    # Prepare orchestrator with minimal required attributes
    orch = Orchestrator(workspace=str(tmp_path))
    orch.workspace = str(tmp_path)

    # Fake GitHub context
    gh = GitHubContext(
        event_name="pull_request_target",
        event_action="synchronize",
        event_path="",
        repository="org/repo",
        repository_owner="org",
        server_url="https://github.com",
        run_id="9999",
        base_ref="main",
        head_ref="feature",
        pr_number=42,
        sha="deadbeef",
    )

    # Minimal Gerrit + repo metadata
    gerrit = GerritInfo(host="gerrit.example.org", port=29418, project="repo")
    repo_names = RepoNames(
        project_gerrit="repo",
        project_github="repo",
    )

    existing_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    new_sha = "feedfacefeedfacefeedfacefeedfacefeedface"

    pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"

    # Stub REST client to emulate existing back-reference for existing_sha
    class _StubRestClient:
        def __init__(self, *a, **k):
            pass

        def get(self, path: str):
            if existing_sha in path:
                return [
                    {
                        "messages": [
                            {
                                "message": f"Previous note\nGHPR: {pr_url} | "
                                "Action-Run: https://github.com/org/repo/"
                                "actions/runs/1111"
                            }
                        ]
                    }
                ]
            if new_sha in path:
                return [{"messages": [{"message": "Some other unrelated msg"}]}]
            return []

    monkeypatch.setenv(
        "G2G_FORCE_BACKREF_DUPLICATE",
        "true" if force_duplicate else "false",
    )

    # Patch GerritRestClient used inside _add_backref_comment_in_gerrit
    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.GerritRestClient",
        _StubRestClient,
        raising=True,
    )

    # Capture run_cmd invocations
    calls: list[str] = []

    def _fake_run_cmd(cmd, cwd=None, env=None):
        # Record the commit SHA argument (last element of ssh command)
        joined = " ".join(cmd)
        calls.append(joined)
        # Return simple namespace with stdout/returncode for compatibility
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(
        "github2gerrit.core.run_cmd", _fake_run_cmd, raising=True
    )

    # Minimal ssh env
    monkeypatch.setattr(
        orch,
        "_ssh_env",
        dict,
        raising=False,
    )

    # Invoke back-reference logic
    orch._add_backref_comment_in_gerrit(  # type: ignore[attr-defined]
        gerrit=gerrit,
        repo=repo_names,
        branch="main",
        commit_shas=[existing_sha, new_sha],
        gh=gh,
    )

    if force_duplicate:
        # Both commits should produce an SSH command
        assert any(existing_sha in c for c in calls), (
            "Expected existing_sha call"
        )
        assert any(new_sha in c for c in calls), "Expected new_sha call"
    else:
        # Only the new SHA should trigger a command
        assert not any(existing_sha in c for c in calls), (
            "Existing backref should skip"
        )
        assert any(new_sha in c for c in calls), "Expected new_sha call"
