# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from pathlib import Path

import pytest

from github2gerrit.core import Orchestrator
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
        event_action="opened",
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


def _inputs(*, use_pr_as_commit: bool = True) -> Inputs:
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
        reuse_strategy="topic+comment",
        similarity_subject=0.7,
        similarity_files=True,
        allow_orphan_changes=False,
        persist_single_mapping_comment=True,
        log_reconcile_json=True,
    )


def test_apply_pr_title_body_preserves_change_id_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When applying PR title/body to the commit, the Change-Id trailer must
    remain in the message footer before the GitHub-PR trailer.
    """
    # Arrange
    orch = Orchestrator(workspace=tmp_path)
    gh = _gh_ctx(pr_number=99)
    inputs = _inputs(use_pr_as_commit=True)

    # Current commit (before applying PR title/body) contains trailers
    existing_change_id = "Ideadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    current_commit_message = (
        "Subject from previous step\n\n"
        "Some body content\n\n"
        "Signed-off-by: Dev One <dev1@example.org>\n"
        f"Change-Id: {existing_change_id}\n"
    )

    # Stub GitHub API helpers used by _apply_pr_title_body_if_requested
    monkeypatch.setattr("github2gerrit.core.build_client", lambda: object())
    monkeypatch.setattr(
        "github2gerrit.core.get_repo_from_env", lambda client: object()
    )
    monkeypatch.setattr(
        "github2gerrit.core.get_pull", lambda repo, pr: object()
    )
    monkeypatch.setattr(
        "github2gerrit.core.get_pr_title_body",
        lambda pr_obj: (
            "New concise title",
            "Descriptive body line 1\n\nBody line 2",
        ),
    )

    # Stub git helpers used by _apply_pr_title_body_if_requested
    monkeypatch.setattr(
        "github2gerrit.core.git_show",
        lambda rev, fmt=None, cwd=None: current_commit_message,
    )

    # run_cmd is used to get the author line
    monkeypatch.setattr(
        "github2gerrit.core.run_cmd",
        lambda cmd, **kwargs: _DummyResult(stdout="Dev One <dev1@example.org>"),
    )

    amended_messages: list[str] = []

    def _capture_amend(
        *,
        cwd: Path | None = None,
        no_edit: bool = False,
        signoff: bool = True,
        author: str | None = None,
        message: str | None = None,
        message_file: Path | None = None,
    ) -> None:
        # Capture the message that would be written by git commit --amend
        assert message is not None, "Expected in-memory message to be provided"
        amended_messages.append(message)

    monkeypatch.setattr("github2gerrit.core.git_commit_amend", _capture_amend)

    # Act
    orch._apply_pr_title_body_if_requested(inputs, gh)

    # Assert
    assert amended_messages, (
        "Expected at least one amend call capturing a message"
    )

    # Use the first amend call which should have the PR title/body and preserved
    # trailers
    new_msg = amended_messages[0]
    lines = new_msg.strip().splitlines()

    # Ensure Change-Id is present and appears before GitHub-PR
    assert any(ln.startswith("Change-Id: ") for ln in lines), (
        "Change-Id trailer missing after amend"
    )
    assert any(ln.startswith("GitHub-PR: ") for ln in lines), (
        "GitHub-PR trailer missing after amend"
    )
    assert any(ln.startswith("GitHub-Hash: ") for ln in lines), (
        "GitHub-Hash trailer missing after amend"
    )
    assert lines[-1].startswith("GitHub-Hash: "), (
        "GitHub-Hash must be the last trailer line in the footer"
    )

    # Find Change-Id line and verify it contains the original value
    change_id_line = next(
        (ln for ln in lines if ln.startswith("Change-Id: ")), ""
    )
    assert existing_change_id in change_id_line, (
        "Original Change-Id value should be preserved"
    )

    # Ensure proper trailer ordering: Signed-off-by, Change-Id, GitHub-PR,
    # GitHub-Hash
    signed_idx = None
    change_idx = None
    github_pr_idx = None
    github_hash_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("Signed-off-by: "):
            signed_idx = i
        elif ln.startswith("Change-Id: "):
            change_idx = i
        elif ln.startswith("GitHub-PR: "):
            github_pr_idx = i
        elif ln.startswith("GitHub-Hash: "):
            github_hash_idx = i

    assert signed_idx is not None, "Signed-off-by trailer should be preserved"
    assert change_idx is not None, "Change-Id trailer should be present"
    assert github_pr_idx is not None, "GitHub-PR trailer should be present"
    assert github_hash_idx is not None, "GitHub-Hash trailer should be present"
    assert signed_idx < change_idx < github_pr_idx < github_hash_idx, (
        "Trailer order should be: Signed-off-by, Change-Id, GitHub-PR, GitHub-Hash"
    )

    # Verify GitHub-PR contains the correct URL
    github_pr_line = lines[github_pr_idx]
    assert "https://github.com/acme/widget/pull/99" in github_pr_line, (
        "GitHub-PR should contain correct PR URL"
    )

    # Ensure there's a blank line separating body and trailers
    # Find the first trailer index and verify the previous non-empty line is
    # blank-separated
    first_trailer_idx = min(
        i
        for i, ln in enumerate(lines)
        if ln.startswith(("Signed-off-by:", "Change-Id:"))
    )
    assert first_trailer_idx >= 2, (
        "Expected trailers to be separated by a blank line from body"
    )
    assert lines[first_trailer_idx - 1] == "", (
        "Expected a blank line before trailers"
    )
