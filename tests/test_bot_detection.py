# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for bot author detection module.

Validates the ``is_bot_author`` function that distinguishes between
automation bots and human developers, which is critical for the
NACR (Non-Author Code Review) author-swap logic.
"""

from __future__ import annotations

import pytest

from github2gerrit.bot_detection import KNOWN_BOT_LOGINS
from github2gerrit.bot_detection import is_bot_author


class TestKnownBotLogins:
    """Verify known bot logins are detected correctly."""

    @pytest.mark.parametrize(
        "login",
        sorted(KNOWN_BOT_LOGINS),
        ids=sorted(KNOWN_BOT_LOGINS),
    )
    def test_all_known_bots_detected(self, login: str) -> None:
        """Each entry in KNOWN_BOT_LOGINS must return True."""
        assert is_bot_author(login) is True

    @pytest.mark.parametrize(
        "login",
        [
            "Dependabot[bot]",
            "DEPENDABOT[BOT]",
            "Pre-Commit-Ci[Bot]",
            "RENOVATE[BOT]",
        ],
    )
    def test_known_bots_case_insensitive(self, login: str) -> None:
        """Known bot detection must be case-insensitive."""
        assert is_bot_author(login) is True


class TestBotSuffixConvention:
    """GitHub App bots use the ``[bot]`` suffix."""

    @pytest.mark.parametrize(
        "login",
        [
            "custom-app[bot]",
            "my-org-ci[bot]",
            "lfreleng[bot]",
            "some-NEW-tool[Bot]",
            "UNKNOWN[BOT]",
        ],
    )
    def test_unknown_bot_suffix_detected(self, login: str) -> None:
        """Any login ending with [bot] should be detected as bot."""
        assert is_bot_author(login) is True


class TestWordBoundaryHeuristic:
    """Heuristic catches custom bots with ``bot`` as a word boundary."""

    @pytest.mark.parametrize(
        "login",
        [
            "my-bot",
            "bot-runner",
            "ci-bot-42",
            "Bot",
            "BOT",
        ],
    )
    def test_word_boundary_bots_detected(self, login: str) -> None:
        """Logins with 'bot' at a word boundary should be detected."""
        assert is_bot_author(login) is True


class TestFalsePositiveProtection:
    """Ensure common human names are NOT misclassified as bots."""

    @pytest.mark.parametrize(
        "login",
        [
            "abbott",
            "robot",
            "roboto",
            "bottleneck",
            "saboteur",
            "turnbottom",
        ],
    )
    def test_human_names_not_flagged(self, login: str) -> None:
        """Words containing 'bot' as a substring must NOT match."""
        assert is_bot_author(login) is False


class TestHumanAuthors:
    """Regular human GitHub logins."""

    @pytest.mark.parametrize(
        "login",
        [
            "askb",
            "octocat",
            "johndoe",
            "alice-smith",
            "user_123",
            "rvarga",
        ],
    )
    def test_human_authors_not_flagged(self, login: str) -> None:
        """Normal human logins must return False."""
        assert is_bot_author(login) is False


class TestEdgeCases:
    """Edge cases: empty, None, whitespace."""

    def test_empty_string_returns_false(self) -> None:
        """Empty login → conservative human classification."""
        assert is_bot_author("") is False

    def test_none_returns_false(self) -> None:
        """None login → conservative human classification."""
        assert is_bot_author(None) is False

    def test_whitespace_only_returns_false(self) -> None:
        """Whitespace-only login → False."""
        assert is_bot_author("   ") is False

    def test_login_with_surrounding_whitespace(self) -> None:
        """Leading/trailing whitespace should be stripped."""
        assert is_bot_author("  dependabot[bot]  ") is True
