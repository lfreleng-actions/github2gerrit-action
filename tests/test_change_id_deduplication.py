# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from github2gerrit.core import Orchestrator
from github2gerrit.gitutils import _parse_trailers
from github2gerrit.models import GitHubContext
from github2gerrit.models import Inputs


class _DummyResult:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _gh_ctx(
    *,
    repository: str = "acme/widget",
    pr_number: int = 42,
    server_url: str = "https://github.com",
    run_id: str = "12345",
) -> GitHubContext:
    return GitHubContext(
        event_name="pull_request_target",
        event_action="synchronize",
        event_path=None,
        repository=repository,
        repository_owner=repository.split("/")[0],
        server_url=server_url,
        run_id=run_id,
        sha="deadbeef",
        base_ref="master",
        head_ref="feature/branch",
        pr_number=pr_number,
    )


def _inputs(*, use_pr_as_commit: bool = False) -> Inputs:
    return Inputs(
        submit_single_commits=False,
        use_pr_as_commit=use_pr_as_commit,
        fetch_depth=50,
        gerrit_known_hosts="",
        gerrit_ssh_privkey_g2g="",
        gerrit_ssh_user_g2g="gerrit-bot",
        gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
        organization="acme",
        reviewers_email="",
        preserve_github_prs=True,
        dry_run=False,
        normalise_commit=True,
        gerrit_server="",
        gerrit_server_port="",
        gerrit_project="",
        issue_id="",
        allow_duplicates=False,
        ci_testing=False,
        duplicates_filter="open",
    )


def test_parse_trailers_only_footer() -> None:
    """Test that _parse_trailers only extracts trailers from the footer, not the body."""
    commit_message = """Fix critical bug in authentication

This commit addresses issue #123 by updating the authentication
logic. The previous Change-Id: Iwrongplace was invalid.

Some more details about the fix.

Signed-off-by: Developer One <dev1@example.org>
Change-Id: Icorrectplace1234567890123456789012345678
"""

    trailers = _parse_trailers(commit_message)

    # Should only find the footer Change-Id, not the one in the body
    assert "Change-Id" in trailers
    assert len(trailers["Change-Id"]) == 1
    assert trailers["Change-Id"][0] == "Icorrectplace1234567890123456789012345678"

    assert "Signed-off-by" in trailers
    assert trailers["Signed-off-by"][0] == "Developer One <dev1@example.org>"


def test_parse_trailers_no_false_positives() -> None:
    """Test that _parse_trailers doesn't extract non-trailer colons."""
    commit_message = """Update documentation

This update includes:
- Better examples
- Clearer instructions

The file src/config.yaml: contains configuration.
Note: this is important.

Signed-off-by: Developer Two <dev2@example.org>
"""

    trailers = _parse_trailers(commit_message)

    # Should only find Signed-off-by trailer
    assert "Signed-off-by" in trailers
    assert len(trailers) == 1
    # Should not extract "src/config.yaml" or "Note" as trailers
    assert "src/config.yaml" not in trailers
    assert "Note" not in trailers


def test_clean_commit_message_removes_body_change_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that _clean_commit_message_for_change_id removes Change-Id from body but preserves footer."""
    orch = Orchestrator(workspace=tmp_path)

    message_with_body_change_id = """Fix authentication bug

This addresses the Change-Id: Iwrongplace issue that was
causing problems with user login.

The solution involves updating the auth module.

Signed-off-by: Developer <dev@example.org>
Change-Id: Icorrectplace1234567890123456789012345678
"""

    cleaned = orch._clean_commit_message_for_change_id(message_with_body_change_id)

    # Should remove Change-Id from body
    assert "Change-Id: Iwrongplace" not in cleaned
    # Should preserve footer Change-Id
    assert "Change-Id: Icorrectplace1234567890123456789012345678" in cleaned
    # Should preserve Signed-off-by
    assert "Signed-off-by: Developer <dev@example.org>" in cleaned
    # Should preserve the rest of the body
    assert "Fix authentication bug" in cleaned
    assert "causing problems with user login" in cleaned


def test_ensure_change_id_cleans_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that _ensure_change_id_present cleans up body Change-IDs when footer ones exist."""
    orch = Orchestrator(workspace=tmp_path)

    # Mock git show to return message with Change-Id in both body and footer
    message_with_duplicates = """Update feature X

This references Change-Id: Ibodychangeid123 from previous work.

More details about the implementation.

Signed-off-by: Dev <dev@example.org>
Change-Id: Ifooterchangeid456789012345678901234567890
"""

    def mock_run_cmd(cmd: list[str], **kwargs: Any) -> _DummyResult:
        if cmd[:3] == ["git", "show", "-s"]:
            return _DummyResult(stdout=message_with_duplicates)
        return _DummyResult()

    monkeypatch.setattr("github2gerrit.core.run_cmd", mock_run_cmd)

    def mock_git_commit_amend(**kwargs: Any) -> None:
        # Capture the cleaned message
        if "message" in kwargs:
            cleaned_msg = kwargs["message"]
            # Verify body Change-Id was removed
            assert "Change-Id: Ibodychangeid123" not in cleaned_msg
            # Verify footer Change-Id was preserved
            assert "Change-Id: Ifooterchangeid456789012345678901234567890" in cleaned_msg

    monkeypatch.setattr("github2gerrit.core.git_commit_amend", mock_git_commit_amend)

    # Mock git_last_commit_trailers to return the footer Change-Id
    def mock_trailers(keys: list[str] | None = None, **kwargs: Any) -> dict[str, list[str]]:
        return {"Change-Id": ["Ifooterchangeid456789012345678901234567890"]}

    monkeypatch.setattr("github2gerrit.core.git_last_commit_trailers", mock_trailers)

    from github2gerrit.core import GerritInfo

    gerrit = GerritInfo(host="gerrit.example.org", port=29418, project="example/project")

    result = orch._ensure_change_id_present(gerrit, "Dev <dev@example.org>")

    # Should return the footer Change-Id
    assert result == ["Ifooterchangeid456789012345678901234567890"]


def test_squashed_commit_handles_reused_change_id_properly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that squashed commits properly handle reused Change-IDs without duplication."""
    orch = Orchestrator(workspace=tmp_path)
    inputs = _inputs()
    gh = _gh_ctx(pr_number=99)

    reused_change_id = "Ireused1234567890123456789012345678901234"

    # Mock the git operations
    def mock_run_cmd(cmd: list[str], **kwargs: Any) -> _DummyResult:
        if cmd[:3] == ["git", "fetch", "origin"]:
            return _DummyResult()
        if cmd[:2] == ["git", "rev-parse"]:
            return _DummyResult(stdout="abc123\n")
        if cmd[:2] == ["git", "checkout"]:
            return _DummyResult()
        if cmd[:3] == ["git", "merge", "--squash"]:
            return _DummyResult()
        if cmd[:3] == ["git", "log", "--format=%B"]:
            # Return commit history with Change-IDs in body
            return _DummyResult(
                stdout="""Feature: add cool feature

Some details about the feature.
Change-Id: Ioldchangeid123456789012345678901234567

More implementation details.

Signed-off-by: Dev One <dev1@example.org>
"""
            )
        if cmd[:3] == ["git", "show", "-s"] and "--pretty=format:%an <%ae>" in cmd:
            return _DummyResult(stdout="Lead Dev <lead@example.org>\n")
        if cmd[:3] == ["git", "show", "-s"] and "--pretty=format:%B" in cmd:
            # Return final commit message after creation
            return _DummyResult(
                stdout=f"""Feature: add cool feature

Some details about the feature.

Signed-off-by: Dev One <dev1@example.org>
Change-Id: {reused_change_id}
"""
            )
        return _DummyResult()

    monkeypatch.setattr("github2gerrit.core.run_cmd", mock_run_cmd)

    # Mock GitHub API for Change-ID reuse
    monkeypatch.setattr("github2gerrit.core.build_client", lambda: object())
    monkeypatch.setattr("github2gerrit.core.get_repo_from_env", lambda _: object())
    monkeypatch.setattr("github2gerrit.core.get_pull", lambda _, __: object())
    monkeypatch.setattr(
        "github2gerrit.core.get_recent_change_ids_from_comments",
        lambda _, max_comments=50: [reused_change_id],
    )

    # Mock git commit operations
    monkeypatch.setattr("github2gerrit.core.git_commit_new", lambda **kwargs: None)

    # Mock _ensure_change_id_present to return the reused Change-ID
    def mock_ensure_change_id(self: Any, gerrit_info: Any, author: str) -> list[str]:
        return [reused_change_id]

    monkeypatch.setattr("github2gerrit.core.Orchestrator._ensure_change_id_present", mock_ensure_change_id)

    from github2gerrit.core import GerritInfo

    gerrit = GerritInfo(host="gerrit.example.org", port=29418, project="example/project")

    result = orch._prepare_squashed_commit(inputs, gh, gerrit)

    # Should return only the reused Change-ID, no duplicates
    assert result.change_ids == [reused_change_id]
    assert len(result.change_ids) == 1


def test_parse_trailers_multiple_blank_lines() -> None:
    """Test that _parse_trailers handles multiple blank lines correctly."""
    commit_message = """Fix issue with parser

This is the body of the commit message.


There are multiple blank lines above.


Signed-off-by: Developer <dev@example.org>
Change-Id: I1234567890123456789012345678901234567890
Issue-ID: PROJ-123
"""

    trailers = _parse_trailers(commit_message)

    assert len(trailers) == 3
    assert "Signed-off-by" in trailers
    assert "Change-Id" in trailers
    assert "Issue-ID" in trailers
    assert trailers["Change-Id"][0] == "I1234567890123456789012345678901234567890"
    assert trailers["Issue-ID"][0] == "PROJ-123"


def test_parse_trailers_no_trailers() -> None:
    """Test that _parse_trailers returns empty dict when no trailers present."""
    commit_message = """Simple commit message

Just a body with no trailers at all.
No Signed-off-by or Change-Id here.
"""

    trailers = _parse_trailers(commit_message)

    assert trailers == {}


def test_parse_trailers_malformed_lines() -> None:
    """Test that _parse_trailers ignores malformed trailer-like lines."""
    commit_message = """Fix bug

Some content with colons: but not trailers.

Signed-off-by: Good Trailer <good@example.org>
Change-Id: I1234567890123456789012345678901234567890
"""

    trailers = _parse_trailers(commit_message)

    # Should only find properly formatted trailers
    assert len(trailers) == 2
    assert "Signed-off-by" in trailers
    assert "Change-Id" in trailers
