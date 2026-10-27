# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.cli import _check_automation_only
from github2gerrit.models import GitHubContext


class TestAutomationOnly:
    """Test suite for automation_only feature."""

    def test_automation_only_disabled_allows_all_prs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When automation_only is disabled, all PRs should be allowed."""
        monkeypatch.setenv("AUTOMATION_ONLY", "false")

        mock_pr = MagicMock()
        mock_pr.user.login = "regular-user"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    def test_dependabot_pr_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dependabot PRs should be allowed when automation_only is enabled."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "dependabot[bot]"
        mock_pr.user.type = "Bot"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    def test_precommit_ci_pr_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-commit.ci PRs should be allowed when automation_only is enabled."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "pre-commit-ci[bot]"
        mock_pr.user.type = "Bot"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    def test_copilot_pr_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Copilot PRs should be allowed when automation_only is enabled."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "Copilot"
        mock_pr.user.type = "Bot"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    @patch("github2gerrit.github_api.close_pr")
    def test_regular_user_pr_rejected(
        self, mock_close_pr: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regular user PRs should be rejected when automation_only is enabled."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "regular-user"
        mock_pr.user.type = "User"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should raise GitHub2GerritError after closing PR
        with pytest.raises(SystemExit):
            _check_automation_only(mock_pr, gh)

        # Verify PR was closed with correct comment
        mock_close_pr.assert_called_once()
        call_args = mock_close_pr.call_args
        assert call_args[0][0] == mock_pr
        comment = call_args[1]["comment"]
        assert "GitHub mirror does not accept pull requests" in comment
        assert "Gerrit server" in comment

    @patch("github2gerrit.github_api.close_pr")
    def test_missing_author_rejects_pr_without_closing(
        self, mock_close_pr: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable author fails closed, but the PR is left open.

        The gate is what keeps a mirror from relaying arbitrary pull
        requests to Gerrit, so it cannot wave one through on a missing
        answer. The tool cannot say whose the pull request is, though,
        so it does not close it either.
        """
        monkeypatch.setenv("AUTOMATION_ONLY", "true")
        monkeypatch.delenv("G2G_SWEEP_LEG", raising=False)

        mock_pr = MagicMock()
        mock_pr.user = None

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        with pytest.raises(SystemExit) as exc:
            _check_automation_only(mock_pr, gh)
        assert exc.value.code == 1
        mock_close_pr.assert_not_called()

    @patch("github2gerrit.github_api.close_pr")
    def test_missing_author_is_a_skip_for_a_sweep_leg(
        self, mock_close_pr: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sweep leg records the unreadable author as a skip."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")
        monkeypatch.setenv("G2G_SWEEP_LEG", "true")

        mock_pr = MagicMock()
        mock_pr.user = None

        gh = GitHubContext(
            event_name="workflow_dispatch",
            event_action="",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        with pytest.raises(SystemExit) as exc:
            _check_automation_only(mock_pr, gh)
        assert exc.value.code == 0
        mock_close_pr.assert_not_called()

    @pytest.mark.parametrize(
        "login",
        ["dependabot[bot]", "pre-commit-ci[bot]", "Copilot"],
    )
    @patch("github2gerrit.github_api.close_pr")
    def test_known_login_on_a_user_account_rejected(
        self,
        mock_close_pr: Any,
        login: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A known login carried by a non-Bot account does not pass.

        Each recognised tool is a GitHub App, so its pull requests come
        from a Bot account. Matching the login alone would rest on
        GitHub keeping those names reserved.
        """
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = login
        mock_pr.user.type = "User"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        with pytest.raises(SystemExit):
            _check_automation_only(mock_pr, gh)
        mock_close_pr.assert_called_once()

    @pytest.mark.parametrize("login", ["dependabot", "pre-commit-ci"])
    @patch("github2gerrit.github_api.close_pr")
    def test_bare_tool_names_rejected(
        self,
        mock_close_pr: Any,
        login: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bare names are organisations, which never author PRs.

        They used to be accepted alongside the ``[bot]`` logins, which
        only widened the gate.
        """
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = login
        mock_pr.user.type = "Organization"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        with pytest.raises(SystemExit):
            _check_automation_only(mock_pr, gh)
        mock_close_pr.assert_called_once()

    @patch("github2gerrit.github_api.close_pr")
    def test_close_pr_comment_format(
        self, mock_close_pr: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the exact format of the close PR comment."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "some-user"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        # Should raise SystemExit after closing PR
        with pytest.raises(SystemExit):
            _check_automation_only(mock_pr, gh)

        # Verify exact comment format
        call_args = mock_close_pr.call_args
        comment = call_args[1]["comment"]
        expected_lines = [
            "This GitHub mirror does not accept pull requests.",
            "Please submit changes to the project's Gerrit server.",
        ]
        for line in expected_lines:
            assert line in comment
