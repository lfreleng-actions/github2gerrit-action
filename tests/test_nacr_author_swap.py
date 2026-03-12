# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for NACR (Non-Author Code Review) author-swap logic.

Validates the ``_resolve_author_for_gerrit`` and ``_get_pr_author_login``
methods on ``Orchestrator``, as well as the Co-authored-by trailer
injection in ``_build_commit_message_with_trailers``.

The NACR fix ensures that when a *human* developer opens a GitHub PR,
G2G swaps the commit author to the bot identity and preserves the
original author via a ``Co-authored-by`` trailer.  For *bot* PRs the
original author is preserved unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.bot_detection import is_bot_author
from github2gerrit.core import Orchestrator


# ---------------------------------------------------------------------------
# Helpers — lightweight stand-ins for Inputs / GitHubContext
# ---------------------------------------------------------------------------


def _make_inputs(
    *,
    gerrit_ssh_user_g2g: str = "opendaylight.gh2gerrit",
    gerrit_ssh_user_g2g_email: str = "releng+odl-gh2gerrit@linuxfoundation.org",
    issue_id: str = "",
) -> SimpleNamespace:
    """Minimal ``Inputs``-like object for testing."""
    return SimpleNamespace(
        gerrit_ssh_user_g2g=gerrit_ssh_user_g2g,
        gerrit_ssh_user_g2g_email=gerrit_ssh_user_g2g_email,
        issue_id=issue_id,
    )


def _make_gh(pr_number: str | None = "42") -> SimpleNamespace:
    """Minimal ``GitHubContext``-like object for testing."""
    return SimpleNamespace(
        pr_number=pr_number,
        server_url="https://github.com",
        repository="opendaylight/mdsal",
        head_sha="abc1234",
    )


def _make_mock_self() -> MagicMock:
    """Create a MagicMock standing in for an Orchestrator instance.

    Pre-configures ``_build_pr_metadata_trailers`` to return an empty
    list so that ``_build_commit_message_with_trailers`` works without
    needing a real GitHub context.
    """
    mock = MagicMock(spec=Orchestrator)
    mock._build_pr_metadata_trailers.return_value = []
    return mock


# ---------------------------------------------------------------------------
# _resolve_author_for_gerrit
# ---------------------------------------------------------------------------


class TestResolveAuthorForGerrit:
    """Test author resolution for Gerrit submission."""

    def test_bot_pr_preserves_original_author(self) -> None:
        """Bot PRs keep original author; no Co-authored-by trailer."""
        mock_self = _make_mock_self()

        author, trailer = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author="dependabot[bot] <dependabot@github.com>",
            pr_author_login="dependabot[bot]",
            inputs=_make_inputs(),
        )

        assert author == "dependabot[bot] <dependabot@github.com>"
        assert trailer is None

    def test_human_pr_swaps_to_bot_author(self) -> None:
        """Human PRs use bot identity as author."""
        mock_self = _make_mock_self()

        author, _trailer = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author="Anil Belur <askb23@gmail.com>",
            pr_author_login="askb",
            inputs=_make_inputs(),
        )

        assert "opendaylight.gh2gerrit" in author
        assert "releng+odl-gh2gerrit@linuxfoundation.org" in author

    def test_human_pr_adds_co_authored_by_trailer(self) -> None:
        """Human PRs get a Co-authored-by trailer for attribution."""
        mock_self = _make_mock_self()

        _, trailer = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author="Robert Varga <rovarga@example.com>",
            pr_author_login="rvarga",
            inputs=_make_inputs(),
        )

        assert trailer is not None
        assert trailer == "Co-authored-by: Robert Varga <rovarga@example.com>"

    def test_empty_login_treated_as_human(self) -> None:
        """Empty login (API failure) → conservative: treat as human → swap."""
        mock_self = _make_mock_self()

        author, trailer = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author="Unknown Dev <dev@example.com>",
            pr_author_login="",
            inputs=_make_inputs(),
        )

        # Empty string → is_bot_author returns False → human path
        assert "opendaylight.gh2gerrit" in author
        assert trailer is not None
        assert "Unknown Dev" in trailer

    def test_custom_bot_identity(self) -> None:
        """Non-default bot identity is used when configured."""
        mock_self = _make_mock_self()

        author, _ = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author="Human <human@example.com>",
            pr_author_login="human-dev",
            inputs=_make_inputs(
                gerrit_ssh_user_g2g="myorg-bot",
                gerrit_ssh_user_g2g_email="bot@myorg.example",
            ),
        )

        assert author == "myorg-bot <bot@myorg.example>"

    @pytest.mark.parametrize(
        "bot_login",
        [
            "dependabot[bot]",
            "pre-commit-ci[bot]",
            "renovate[bot]",
            "github-actions[bot]",
        ],
    )
    def test_common_bots_all_preserved(self, bot_login: str) -> None:
        """All common bots keep their original author."""
        mock_self = _make_mock_self()
        original = f"{bot_login} <{bot_login}@github.com>"

        author, trailer = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author=original,
            pr_author_login=bot_login,
            inputs=_make_inputs(),
        )

        assert author == original
        assert trailer is None


# ---------------------------------------------------------------------------
# _get_pr_author_login
# ---------------------------------------------------------------------------


class TestGetPrAuthorLogin:
    """Test GitHub PR author login fetching."""

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    def test_returns_login_on_success(
        self, mock_get_pull, mock_get_repo, mock_build_client
    ) -> None:
        """Successfully fetch PR author login."""
        mock_pr = MagicMock()
        mock_pr.user.login = "askb"
        mock_get_pull.return_value = mock_pr

        mock_self = _make_mock_self()
        gh = _make_gh(pr_number="42")
        result = Orchestrator._get_pr_author_login(mock_self, gh)

        assert result == "askb"
        mock_get_pull.assert_called_once()

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    def test_returns_empty_on_api_failure(
        self, mock_get_pull, mock_get_repo, mock_build_client
    ) -> None:
        """API failure returns empty string (conservative fallback)."""
        mock_get_pull.side_effect = Exception("API error")

        mock_self = _make_mock_self()
        gh = _make_gh(pr_number="42")
        result = Orchestrator._get_pr_author_login(mock_self, gh)

        assert result == ""

    def test_returns_empty_when_no_pr_number(self) -> None:
        """No PR number → empty string."""
        mock_self = _make_mock_self()
        gh = _make_gh(pr_number=None)
        result = Orchestrator._get_pr_author_login(mock_self, gh)

        assert result == ""

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    def test_returns_empty_when_user_is_none(
        self, mock_get_pull, mock_get_repo, mock_build_client
    ) -> None:
        """PR with no user attribute → empty string."""
        mock_pr = MagicMock()
        mock_pr.user = None
        mock_get_pull.return_value = mock_pr

        mock_self = _make_mock_self()
        gh = _make_gh(pr_number="42")
        result = Orchestrator._get_pr_author_login(mock_self, gh)

        assert result == ""


# ---------------------------------------------------------------------------
# _build_commit_message_with_trailers — Co-authored-by injection
# ---------------------------------------------------------------------------


class TestCoAuthoredByTrailerInjection:
    """Test that extra_co_authored_by is correctly added to commit messages."""

    def test_extra_co_authored_by_added(self) -> None:
        """Co-authored-by trailer is appended when provided."""
        mock_self = _make_mock_self()
        inputs = _make_inputs()
        gh = _make_gh()

        msg = Orchestrator._build_commit_message_with_trailers(
            mock_self,
            base_message="fix: resolve build issue\n\nDetailed description.",
            inputs=inputs,
            gh=gh,
            extra_co_authored_by="Co-authored-by: Anil Belur <askb23@gmail.com>",
        )

        assert "Co-authored-by: Anil Belur <askb23@gmail.com>" in msg

    def test_no_co_authored_by_when_none(self) -> None:
        """No Co-authored-by added when parameter is None."""
        mock_self = _make_mock_self()
        inputs = _make_inputs()
        gh = _make_gh()

        msg = Orchestrator._build_commit_message_with_trailers(
            mock_self,
            base_message="feat: new feature",
            inputs=inputs,
            gh=gh,
            extra_co_authored_by=None,
        )

        assert "Co-authored-by:" not in msg

    def test_co_authored_by_dedup(self) -> None:
        """Duplicate Co-authored-by trailers are not repeated."""
        mock_self = _make_mock_self()
        inputs = _make_inputs()
        gh = _make_gh()

        base = "fix: thing\n\nCo-authored-by: Anil Belur <askb23@gmail.com>"

        msg = Orchestrator._build_commit_message_with_trailers(
            mock_self,
            base_message=base,
            inputs=inputs,
            gh=gh,
            extra_co_authored_by="Co-authored-by: Anil Belur <askb23@gmail.com>",
        )

        # Should appear exactly once
        assert msg.count("Co-authored-by: Anil Belur <askb23@gmail.com>") == 1

    def test_co_authored_by_with_existing_different_author(self) -> None:
        """New Co-authored-by alongside an existing different one."""
        mock_self = _make_mock_self()
        inputs = _make_inputs()
        gh = _make_gh()

        base = "fix: thing\n\nCo-authored-by: Robert Varga <rv@example.com>"

        msg = Orchestrator._build_commit_message_with_trailers(
            mock_self,
            base_message=base,
            inputs=inputs,
            gh=gh,
            extra_co_authored_by="Co-authored-by: Anil Belur <askb23@gmail.com>",
        )

        assert "Co-authored-by: Robert Varga <rv@example.com>" in msg
        assert "Co-authored-by: Anil Belur <askb23@gmail.com>" in msg


# ---------------------------------------------------------------------------
# Integration: bot detection + author resolution
# ---------------------------------------------------------------------------


class TestBotDetectionIntegration:
    """End-to-end: verify bot detection feeds correctly into author resolution."""

    def test_dependabot_full_flow(self) -> None:
        """Dependabot PR: is_bot_author=True → author preserved."""
        assert is_bot_author("dependabot[bot]") is True

        mock_self = _make_mock_self()
        author, trailer = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author="dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
            pr_author_login="dependabot[bot]",
            inputs=_make_inputs(),
        )

        assert "dependabot[bot]" in author
        assert trailer is None

    def test_human_developer_full_flow(self) -> None:
        """Human developer PR: is_bot_author=False → author swapped."""
        assert is_bot_author("rvarga") is False

        mock_self = _make_mock_self()
        author, trailer = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author="Robert Varga <rovarga@cisco.com>",
            pr_author_login="rvarga",
            inputs=_make_inputs(),
        )

        assert "opendaylight.gh2gerrit" in author
        assert trailer == "Co-authored-by: Robert Varga <rovarga@cisco.com>"

    def test_double_resolve_guard_skips_duplicate_swap(self) -> None:
        """When original_author already equals the bot, skip re-resolution."""
        mock_self = _make_mock_self()
        inputs = _make_inputs()
        bot_author = (
            f"{inputs.gerrit_ssh_user_g2g} <{inputs.gerrit_ssh_user_g2g_email}>"
        )

        author, trailer = Orchestrator._resolve_author_for_gerrit(
            mock_self,
            original_author=bot_author,
            pr_author_login="rvarga",  # human login
            inputs=inputs,
        )

        # Should return the bot author unchanged, no Co-authored-by
        assert author == bot_author
        assert trailer is None
