# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.cli import _check_automation_only
from github2gerrit.cli import _gate_single_pr
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

    @patch("github2gerrit.github_api.close_pr")
    def test_an_unresolved_pull_request_is_rejected(
        self, mock_close_pr: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No pull request object fails closed, as a missing author does.

        The approval gate already refuses a pull request it could not
        resolve; this gate matches it rather than treating the absence
        as a pass. Nothing is closed, since there is nothing to close.
        """
        monkeypatch.setenv("AUTOMATION_ONLY", "true")
        monkeypatch.delenv("G2G_SWEEP_LEG", raising=False)

        gh = GitHubContext(
            event_name="pull_request_target",
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
            _check_automation_only(None, gh)
        assert exc.value.code == 1
        mock_close_pr.assert_not_called()

    def test_an_unresolved_pull_request_passes_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the gate off there is nothing to fail closed."""
        monkeypatch.setenv("AUTOMATION_ONLY", "false")

        gh = GitHubContext(
            event_name="pull_request_target",
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

        _check_automation_only(None, gh)

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


class TestTheGateRunsWithoutAToken:
    """The single-PR path cannot skip the gate for want of a token.

    The display step applies the gate once it holds the pull request,
    but its no-token branch returns before that. The gate has to be
    applied on that branch too, else a direct run or a caller that
    passes no ``GITHUB_TOKEN`` would fetch and push with the gate never
    consulted.
    """

    def _gate(self, displayed: Any, resolved: Any) -> tuple[MagicMock, Any]:
        gh = GitHubContext(
            event_name="pull_request_target",
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
        with (
            patch(
                "github2gerrit.cli._extract_and_display_pr_info",
                return_value=displayed,
            ),
            patch(
                "github2gerrit.cli._resolve_pr_for_gate",
                return_value=resolved,
            ),
            patch("github2gerrit.cli._check_automation_only") as gate,
            patch("github2gerrit.cli._recover_pr_metadata", return_value=gh),
            patch(
                "github2gerrit.cli._check_fork_approval",
                return_value=(True, ""),
            ),
        ):
            result = _gate_single_pr(gh, MagicMock(), MagicMock())
        return gate, result

    def test_the_fallback_applies_the_gate_to_what_it_resolved(
        self,
    ) -> None:
        resolved = MagicMock()
        gate, result = self._gate(displayed=None, resolved=resolved)
        gate.assert_called_once()
        assert gate.call_args[0][0] is resolved
        assert result[1] is resolved

    def test_the_fallback_applies_the_gate_to_nothing_at_all(self) -> None:
        # Resolving fails without a token; the gate then fails closed.
        gate, _result = self._gate(displayed=None, resolved=None)
        gate.assert_called_once()
        assert gate.call_args[0][0] is None

    def test_a_displayed_pull_request_is_not_gated_twice(self) -> None:
        # The display step applied the gate before returning; a second
        # pass here would be another API round for the same answer.
        displayed = MagicMock()
        gate, result = self._gate(displayed=displayed, resolved=None)
        gate.assert_not_called()
        assert result[1] is displayed
