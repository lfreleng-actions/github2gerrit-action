# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for the extensible @github2gerrit PR command system.

.. note::
   ``_TEST_TOKEN`` is used instead of inline string literals for the
   ``github_token`` parameter to satisfy the S106 (possible hardcoded
   password) linter rule.

Covers:
- Command parsing from PR comment bodies
- Case insensitivity
- Alias matching
- Deduplication (latest occurrence wins)
- Prefix matching (trailing punctuation tolerance)
- Unrecognised directive logging
- Registry management
- Convenience helpers (has_command, find_command, list_commands)
- Integration with the Orchestrator create-missing fallback
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.pr_commands import CMD_CREATE_MISSING
from github2gerrit.pr_commands import COMMAND_REGISTRY
from github2gerrit.pr_commands import CommandDefinition
from github2gerrit.pr_commands import CommandMatch
from github2gerrit.pr_commands import CommandParseResult
from github2gerrit.pr_commands import find_command
from github2gerrit.pr_commands import has_command
from github2gerrit.pr_commands import list_commands
from github2gerrit.pr_commands import parse_commands
from github2gerrit.pr_commands import register_command


# Dummy token value kept in a module-level constant so that passing it
# as a keyword argument does not trigger the S106 "possible hardcoded
# password" lint rule.
_TEST_TOKEN = "test-token-for-unit-tests"


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def _clean_registry():
    """Snapshot and restore COMMAND_REGISTRY around a test."""
    original = list(COMMAND_REGISTRY)
    yield
    COMMAND_REGISTRY.clear()
    COMMAND_REGISTRY.extend(original)


# ── CommandDefinition tests ─────────────────────────────────────────


class TestCommandDefinition:
    """Tests for the CommandDefinition dataclass."""

    def test_all_phrases_includes_name_and_aliases(self):
        defn = CommandDefinition(
            name="foo bar",
            aliases=("fb", "foobar"),
        )
        assert defn.all_phrases() == ("foo bar", "fb", "foobar")

    def test_all_phrases_no_aliases(self):
        defn = CommandDefinition(name="solo")
        assert defn.all_phrases() == ("solo",)

    def test_frozen(self):
        defn = CommandDefinition(name="x")
        with pytest.raises(AttributeError):
            defn.name = "y"  # type: ignore[misc]


# ── Built-in registry tests ────────────────────────────────────────


class TestBuiltInRegistry:
    """Ensure the built-in commands are registered on import."""

    def test_create_missing_registered(self):
        names = [c.name for c in COMMAND_REGISTRY]
        assert "create missing change" in names

    def test_cmd_create_missing_constant(self):
        assert CMD_CREATE_MISSING.name == "create missing change"
        assert "create-missing" in CMD_CREATE_MISSING.aliases
        assert "create missing" in CMD_CREATE_MISSING.aliases

    def test_list_commands_returns_non_hidden(self):
        visible = list_commands()
        assert any(c.name == "create missing change" for c in visible)


# ── register_command tests ──────────────────────────────────────────


class TestRegisterCommand:
    """Tests for dynamic command registration."""

    @pytest.mark.usefixtures("_clean_registry")
    def test_register_new_command(self):
        defn = CommandDefinition(
            name="test command",
            aliases=("tc",),
            description="A test command",
        )
        result = register_command(defn)
        assert result is defn
        assert defn in COMMAND_REGISTRY

    @pytest.mark.usefixtures("_clean_registry")
    def test_register_hidden_command(self):
        defn = CommandDefinition(
            name="secret",
            hidden=True,
        )
        register_command(defn)
        assert defn in COMMAND_REGISTRY
        visible = list_commands()
        assert defn not in visible


# ── parse_commands tests ────────────────────────────────────────────


class TestParseCommands:
    """Tests for the main parse_commands function."""

    def test_empty_comments(self):
        result = parse_commands([])
        assert not result.has_matches
        assert result.matches == []
        assert result.unrecognised == []

    def test_no_mentions(self):
        comments = [
            "This is a normal comment.",
            "Another comment without any mentions.",
        ]
        result = parse_commands(comments)
        assert not result.has_matches

    def test_canonical_command(self):
        comments = ["@github2gerrit create missing change"]
        result = parse_commands(comments)
        assert result.has_matches
        assert len(result.matches) == 1
        assert result.matches[0].command_name == "create missing change"

    def test_case_insensitive(self):
        comments = ["@github2gerrit CREATE MISSING CHANGE"]
        result = parse_commands(comments)
        assert result.has_matches
        assert result.matches[0].command_name == "create missing change"

    def test_mixed_case(self):
        comments = ["@github2gerrit Create Missing Change"]
        result = parse_commands(comments)
        assert result.has_matches
        assert result.matches[0].command_name == "create missing change"

    def test_alias_create_missing(self):
        comments = ["@github2gerrit create-missing"]
        result = parse_commands(comments)
        assert result.has_matches
        assert result.matches[0].command_name == "create missing change"

    def test_alias_create_missing_short(self):
        comments = ["@github2gerrit create missing"]
        result = parse_commands(comments)
        assert result.has_matches
        assert result.matches[0].command_name == "create missing change"

    def test_command_in_multiline_comment(self):
        comments = [
            "Hey team, I noticed the workflow is stuck.\n"
            "\n"
            "@github2gerrit create missing change\n"
            "\n"
            "Let me know if this fixes it.",
        ]
        result = parse_commands(comments)
        assert result.has_matches
        assert result.matches[0].command_name == "create missing change"

    def test_command_with_trailing_punctuation(self):
        comments = ["@github2gerrit create missing change."]
        result = parse_commands(comments)
        assert result.has_matches
        assert result.matches[0].command_name == "create missing change"

    def test_command_with_trailing_text(self):
        comments = ["@github2gerrit create missing change please"]
        result = parse_commands(comments)
        # Should match via prefix matching
        assert result.has_matches
        assert result.matches[0].command_name == "create missing change"

    def test_deduplication_latest_wins(self):
        comments = [
            "@github2gerrit create missing change",
            "Some other comment",
            "@github2gerrit create missing change",
        ]
        result = parse_commands(comments)
        assert len(result.matches) == 1
        assert result.matches[0].command_name == "create missing change"
        # Latest occurrence (comment index 2) wins
        assert result.matches[0].comment_index == 2

    def test_unrecognised_directive(self):
        comments = ["@github2gerrit do something weird"]
        result = parse_commands(comments)
        assert not result.has_matches
        assert len(result.unrecognised) == 1
        assert "do something weird" in result.unrecognised[0]

    def test_mixed_recognised_and_unrecognised(self):
        comments = [
            "@github2gerrit create missing change",
            "@github2gerrit frobnicate the widget",
        ]
        result = parse_commands(comments)
        assert len(result.matches) == 1
        assert len(result.unrecognised) == 1

    def test_multiple_commands_in_single_comment(self):
        comments = [
            "@github2gerrit create missing change\n"
            "@github2gerrit create-missing"
        ]
        result = parse_commands(comments)
        # Both resolve to the same canonical name, so deduplicated to 1
        assert len(result.matches) == 1
        assert result.matches[0].command_name == "create missing change"

    def test_mention_prefix_at_start_of_line(self):
        comments = ["@github2gerrit create missing change"]
        result = parse_commands(comments)
        assert result.has_matches

    def test_mention_prefix_after_whitespace(self):
        comments = ["  @github2gerrit create missing change"]
        result = parse_commands(comments)
        assert result.has_matches

    def test_embedded_in_sentence_not_matched(self):
        # The mention needs to be preceded by whitespace or start-of-line
        comments = ["please-do-not@github2gerrit create missing change"]
        result = parse_commands(comments)
        assert not result.has_matches

    def test_empty_string_comments_skipped(self):
        comments = ["", "", "@github2gerrit create missing change", ""]
        result = parse_commands(comments)
        assert result.has_matches
        assert result.matches[0].comment_index == 2

    def test_none_body_handling(self):
        # The comment bodies list should contain strings; empty strings
        # represent None bodies from the GitHub API
        comments = ["", "@github2gerrit create missing change"]
        result = parse_commands(comments)
        assert result.has_matches

    def test_extra_whitespace_in_command(self):
        comments = ["@github2gerrit   create   missing   change"]
        result = parse_commands(comments)
        assert result.has_matches
        assert result.matches[0].command_name == "create missing change"

    def test_raw_text_preserved(self):
        comments = ["@github2gerrit Create Missing Change"]
        result = parse_commands(comments)
        assert result.matches[0].raw_text == "Create Missing Change"


# ── CommandParseResult tests ────────────────────────────────────────


class TestCommandParseResult:
    """Tests for the CommandParseResult helper methods."""

    def test_has_matches_empty(self):
        r = CommandParseResult()
        assert not r.has_matches

    def test_has_matches_with_match(self):
        r = CommandParseResult(
            matches=[CommandMatch(command_name="foo", raw_text="foo")]
        )
        assert r.has_matches

    def test_has_method(self):
        r = CommandParseResult(
            matches=[
                CommandMatch(command_name="create missing change", raw_text="x")
            ]
        )
        assert r.has("create missing change")
        assert r.has("CREATE MISSING CHANGE")
        assert not r.has("nonexistent")

    def test_has_strips_whitespace(self):
        r = CommandParseResult(
            matches=[CommandMatch(command_name="foo bar", raw_text="x")]
        )
        assert r.has("  foo bar  ")


# ── has_command convenience tests ───────────────────────────────────


class TestHasCommand:
    """Tests for the has_command convenience function."""

    def test_found(self):
        comments = ["@github2gerrit create missing change"]
        assert has_command(comments, "create missing change") is True

    def test_not_found(self):
        comments = ["Just a regular comment"]
        assert has_command(comments, "create missing change") is False

    def test_empty_comments(self):
        assert has_command([], "create missing change") is False


# ── find_command convenience tests ──────────────────────────────────


class TestFindCommand:
    """Tests for the find_command convenience function."""

    def test_found_returns_match(self):
        comments = ["@github2gerrit create missing change"]
        match = find_command(comments, "create missing change")
        assert match is not None
        assert match.command_name == "create missing change"

    def test_not_found_returns_none(self):
        comments = ["Nothing here"]
        match = find_command(comments, "create missing change")
        assert match is None

    def test_case_insensitive_lookup(self):
        comments = ["@github2gerrit create missing change"]
        match = find_command(comments, "CREATE MISSING CHANGE")
        assert match is not None


# ── Custom command registration tests ───────────────────────────────


class TestCustomCommandRegistration:
    """Tests for registering and parsing custom commands."""

    @pytest.mark.usefixtures("_clean_registry")
    def test_custom_command_parsed(self):
        register_command(
            CommandDefinition(
                name="force resubmit",
                aliases=("resubmit",),
                description="Force a resubmission",
            )
        )
        comments = ["@github2gerrit force resubmit"]
        result = parse_commands(comments)
        assert result.has("force resubmit")

    @pytest.mark.usefixtures("_clean_registry")
    def test_custom_alias_parsed(self):
        register_command(
            CommandDefinition(
                name="force resubmit",
                aliases=("resubmit",),
            )
        )
        comments = ["@github2gerrit resubmit"]
        result = parse_commands(comments)
        assert result.has("force resubmit")

    @pytest.mark.usefixtures("_clean_registry")
    def test_multiple_different_commands(self):
        register_command(CommandDefinition(name="alpha", aliases=("a",)))
        register_command(CommandDefinition(name="beta", aliases=("b",)))
        comments = [
            "@github2gerrit alpha",
            "@github2gerrit beta",
        ]
        result = parse_commands(comments)
        assert len(result.matches) == 2
        names = {m.command_name for m in result.matches}
        assert names == {"alpha", "beta"}


# ── _should_create_missing Orchestrator integration tests ───────────


class TestShouldCreateMissing:
    """Tests for the Orchestrator._should_create_missing method."""

    def _make_inputs(self, create_missing: bool = False, **kwargs: Any):
        """Create a minimal Inputs object for testing."""
        from github2gerrit.models import Inputs

        defaults = {
            "submit_single_commits": False,
            "use_pr_as_commit": False,
            "fetch_depth": 10,
            "gerrit_known_hosts": "",
            "gerrit_ssh_privkey_g2g": "",
            "gerrit_ssh_user_g2g": "",
            "gerrit_ssh_user_g2g_email": "",
            "github_token": _TEST_TOKEN,
            "organization": "TestOrg",
            "reviewers_email": "",
            "preserve_github_prs": True,
            "dry_run": False,
            "normalise_commit": True,
            "gerrit_server": "gerrit.example.com",
            "gerrit_server_port": 29418,
            "gerrit_project": "test-project",
            "issue_id": "",
            "issue_id_lookup_json": "",
            "allow_duplicates": True,
            "ci_testing": False,
            "create_missing": create_missing,
        }
        defaults.update(kwargs)
        return Inputs(**defaults)

    def _make_gh(self, pr_number: int = 42):
        """Create a minimal GitHubContext for testing."""
        from github2gerrit.models import GitHubContext

        return GitHubContext(
            event_name="pull_request",
            event_action="synchronize",
            event_path=None,
            repository="TestOrg/test-repo",
            repository_owner="TestOrg",
            server_url="https://github.com",
            run_id="12345",
            sha="abc123",
            base_ref="main",
            head_ref="dependabot/npm/lodash-4.17.21",
            pr_number=pr_number,
        )

    def test_cli_flag_returns_true(self, tmp_path):
        """--create-missing flag bypasses comment check."""
        from github2gerrit.core import Orchestrator

        orch = Orchestrator(workspace=tmp_path)
        inputs = self._make_inputs(create_missing=True)
        gh = self._make_gh()

        result = orch._should_create_missing(inputs, gh)
        assert result is True

    def test_no_flag_no_comment_returns_false(self, tmp_path):
        """Without flag and without matching comment, returns False."""
        from github2gerrit.core import Orchestrator

        orch = Orchestrator(workspace=tmp_path)
        inputs = self._make_inputs(create_missing=False)
        gh = self._make_gh()

        # Mock GitHub API to return no matching comments
        mock_comment = MagicMock()
        mock_comment.body = "Just a regular comment"

        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = [mock_comment]

        mock_pr = MagicMock()
        mock_pr.as_issue.return_value = mock_issue

        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo

        with (
            patch("github2gerrit.core.build_client", return_value=mock_client),
            patch(
                "github2gerrit.core.get_repo_from_env",
                return_value=mock_repo,
            ),
            patch("github2gerrit.core.get_pull", return_value=mock_pr),
        ):
            result = orch._should_create_missing(inputs, gh)
            assert result is False

    def test_comment_directive_returns_true(self, tmp_path):
        """PR comment with @github2gerrit create missing change returns True."""
        from github2gerrit.core import Orchestrator

        orch = Orchestrator(workspace=tmp_path)
        inputs = self._make_inputs(create_missing=False)
        gh = self._make_gh()

        mock_comment_1 = MagicMock()
        mock_comment_1.body = "CI is stuck, let me try this:"

        mock_comment_2 = MagicMock()
        mock_comment_2.body = "@github2gerrit create missing change"

        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = [mock_comment_1, mock_comment_2]

        mock_pr = MagicMock()
        mock_pr.as_issue.return_value = mock_issue

        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo

        with (
            patch("github2gerrit.core.build_client", return_value=mock_client),
            patch(
                "github2gerrit.core.get_repo_from_env",
                return_value=mock_repo,
            ),
            patch("github2gerrit.core.get_pull", return_value=mock_pr),
        ):
            result = orch._should_create_missing(inputs, gh)
            assert result is True

    def test_no_pr_number_returns_false(self, tmp_path):
        """Without a PR number, cannot check comments."""
        from github2gerrit.core import Orchestrator

        orch = Orchestrator(workspace=tmp_path)
        inputs = self._make_inputs(create_missing=False)
        gh = self._make_gh(pr_number=None)

        result = orch._should_create_missing(inputs, gh)
        assert result is False

    def test_github_api_failure_returns_false(self, tmp_path):
        """GitHub API failure does not crash; returns False."""
        from github2gerrit.core import Orchestrator

        orch = Orchestrator(workspace=tmp_path)
        inputs = self._make_inputs(create_missing=False)
        gh = self._make_gh()

        with patch(
            "github2gerrit.core.build_client",
            side_effect=RuntimeError("API unavailable"),
        ):
            result = orch._should_create_missing(inputs, gh)
            assert result is False

    def test_alias_in_comment_returns_true(self, tmp_path):
        """PR comment with alias @github2gerrit create-missing returns True."""
        from github2gerrit.core import Orchestrator

        orch = Orchestrator(workspace=tmp_path)
        inputs = self._make_inputs(create_missing=False)
        gh = self._make_gh()

        mock_comment = MagicMock()
        mock_comment.body = "@github2gerrit create-missing"

        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = [mock_comment]

        mock_pr = MagicMock()
        mock_pr.as_issue.return_value = mock_issue

        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo

        with (
            patch("github2gerrit.core.build_client", return_value=mock_client),
            patch(
                "github2gerrit.core.get_repo_from_env",
                return_value=mock_repo,
            ),
            patch("github2gerrit.core.get_pull", return_value=mock_pr),
        ):
            result = orch._should_create_missing(inputs, gh)
            assert result is True


# ── _post_create_missing_notice tests ───────────────────────────────


class TestPostCreateMissingNotice:
    """Tests for the PR notice posted during create-missing fallback."""

    def test_posts_comment(self, tmp_path):
        from github2gerrit.core import Orchestrator
        from github2gerrit.models import GitHubContext

        orch = Orchestrator(workspace=tmp_path)
        gh = GitHubContext(
            event_name="pull_request",
            event_action="synchronize",
            event_path=None,
            repository="Org/repo",
            repository_owner="Org",
            server_url="https://github.com",
            run_id="1",
            sha="abc",
            base_ref="main",
            head_ref="feature",
            pr_number=99,
        )

        mock_pr = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo

        with (
            patch("github2gerrit.core.build_client", return_value=mock_client),
            patch(
                "github2gerrit.core.get_repo_from_env",
                return_value=mock_repo,
            ),
            patch("github2gerrit.core.get_pull", return_value=mock_pr),
            patch("github2gerrit.core.create_pr_comment") as mock_comment,
        ):
            orch._post_create_missing_notice(gh)
            mock_comment.assert_called_once()
            call_body = mock_comment.call_args[0][1]
            assert "GitHub2Gerrit" in call_body
            assert "fallback" in call_body.lower()

    def test_skips_when_ci_testing(self, tmp_path, monkeypatch):
        from github2gerrit.core import Orchestrator
        from github2gerrit.models import GitHubContext

        monkeypatch.setenv("CI_TESTING", "true")
        orch = Orchestrator(workspace=tmp_path)
        gh = GitHubContext(
            event_name="pull_request",
            event_action="synchronize",
            event_path=None,
            repository="Org/repo",
            repository_owner="Org",
            server_url="https://github.com",
            run_id="1",
            sha="abc",
            base_ref="main",
            head_ref="feature",
            pr_number=99,
        )

        with patch("github2gerrit.core.build_client") as mock_build:
            orch._post_create_missing_notice(gh)
            mock_build.assert_not_called()

    def test_skips_when_no_pr_number(self, tmp_path):
        from github2gerrit.core import Orchestrator
        from github2gerrit.models import GitHubContext

        orch = Orchestrator(workspace=tmp_path)
        gh = GitHubContext(
            event_name="pull_request",
            event_action="synchronize",
            event_path=None,
            repository="Org/repo",
            repository_owner="Org",
            server_url="https://github.com",
            run_id="1",
            sha="abc",
            base_ref="main",
            head_ref="feature",
            pr_number=None,
        )

        with patch("github2gerrit.core.build_client") as mock_build:
            orch._post_create_missing_notice(gh)
            mock_build.assert_not_called()

    def test_handles_api_error_gracefully(self, tmp_path):
        from github2gerrit.core import Orchestrator
        from github2gerrit.models import GitHubContext

        orch = Orchestrator(workspace=tmp_path)
        gh = GitHubContext(
            event_name="pull_request",
            event_action="synchronize",
            event_path=None,
            repository="Org/repo",
            repository_owner="Org",
            server_url="https://github.com",
            run_id="1",
            sha="abc",
            base_ref="main",
            head_ref="feature",
            pr_number=99,
        )

        with patch(
            "github2gerrit.core.build_client",
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            orch._post_create_missing_notice(gh)


# ── Inputs model tests ──────────────────────────────────────────────


class TestInputsCreateMissing:
    """Tests for the create_missing field on the Inputs model."""

    def test_default_is_false(self):
        from github2gerrit.models import Inputs

        inputs = Inputs(
            submit_single_commits=False,
            use_pr_as_commit=False,
            fetch_depth=10,
            gerrit_known_hosts="",
            gerrit_ssh_privkey_g2g="",
            gerrit_ssh_user_g2g="",
            gerrit_ssh_user_g2g_email="",
            github_token=_TEST_TOKEN,
            organization="org",
            reviewers_email="",
            preserve_github_prs=True,
            dry_run=False,
            normalise_commit=True,
            gerrit_server="g.example.com",
            gerrit_server_port=29418,
            gerrit_project="proj",
            issue_id="",
            issue_id_lookup_json="",
            allow_duplicates=True,
            ci_testing=False,
        )
        assert inputs.create_missing is False

    def test_can_set_true(self):
        from github2gerrit.models import Inputs

        inputs = Inputs(
            submit_single_commits=False,
            use_pr_as_commit=False,
            fetch_depth=10,
            gerrit_known_hosts="",
            gerrit_ssh_privkey_g2g="",
            gerrit_ssh_user_g2g="",
            gerrit_ssh_user_g2g_email="",
            github_token=_TEST_TOKEN,
            organization="org",
            reviewers_email="",
            preserve_github_prs=True,
            dry_run=False,
            normalise_commit=True,
            gerrit_server="g.example.com",
            gerrit_server_port=29418,
            gerrit_project="proj",
            issue_id="",
            issue_id_lookup_json="",
            allow_duplicates=True,
            ci_testing=False,
            create_missing=True,
        )
        assert inputs.create_missing is True


# ── CLI environment variable integration tests ──────────────────────


class TestCreateMissingEnvVar:
    """Tests for G2G_CREATE_MISSING environment variable handling."""

    def test_build_inputs_from_env_default(self, monkeypatch):
        """Default value when G2G_CREATE_MISSING is not set."""
        monkeypatch.delenv("G2G_CREATE_MISSING", raising=False)

        from github2gerrit.cli import _build_inputs_from_env

        # Set minimal required env vars
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        inputs = _build_inputs_from_env()
        assert inputs.create_missing is False

    def test_build_inputs_from_env_true(self, monkeypatch):
        """G2G_CREATE_MISSING=true sets create_missing."""
        monkeypatch.setenv("G2G_CREATE_MISSING", "true")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")

        from github2gerrit.cli import _build_inputs_from_env

        inputs = _build_inputs_from_env()
        assert inputs.create_missing is True

    def test_build_inputs_from_env_false(self, monkeypatch):
        """G2G_CREATE_MISSING=false keeps create_missing off."""
        monkeypatch.setenv("G2G_CREATE_MISSING", "false")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")

        from github2gerrit.cli import _build_inputs_from_env

        inputs = _build_inputs_from_env()
        assert inputs.create_missing is False


# ── Edge case and resilience tests ──────────────────────────────────


class TestEdgeCases:
    """Edge cases for the command parsing system."""

    def test_mention_without_command(self):
        """Bare @github2gerrit with no following text produces no match."""
        comments = ["@github2gerrit"]
        result = parse_commands(comments)
        assert not result.has_matches
        assert not result.unrecognised

    def test_mention_with_only_whitespace(self):
        """@github2gerrit followed by only whitespace produces no match."""
        comments = ["@github2gerrit   \n"]
        result = parse_commands(comments)
        assert not result.has_matches

    def test_html_comment_body(self):
        """Commands inside HTML comments in the body still match."""
        comments = [
            "<!-- some html -->\n@github2gerrit create missing change\n<!-- end -->"
        ]
        result = parse_commands(comments)
        assert result.has_matches

    def test_code_block_not_special(self):
        """Commands in code blocks still match (we do text scanning)."""
        comments = ["```\n@github2gerrit create missing change\n```"]
        result = parse_commands(comments)
        # The regex does line-by-line matching; the mention inside
        # a code fence is still on its own line, so it matches.
        assert result.has_matches

    def test_very_long_comment_body(self):
        """Handling of very long comment bodies."""
        padding = "x" * 10000
        comments = [
            f"{padding}\n@github2gerrit create missing change\n{padding}"
        ]
        result = parse_commands(comments)
        assert result.has_matches

    def test_unicode_in_surrounding_text(self):
        """Unicode characters around the command don't break parsing."""
        comments = [
            "🔧 Let's fix this:\n@github2gerrit create missing change\n✅ Done"
        ]
        result = parse_commands(comments)
        assert result.has_matches

    def test_multiple_mentions_on_same_line(self):
        """Only the first mention on a line should match (regex behavior)."""
        comments = [
            "@github2gerrit create missing change @github2gerrit create-missing"
        ]
        result = parse_commands(comments)
        # Should get at least one match
        assert result.has_matches
        # Both resolve to same canonical name so deduplication applies
        assert len(result.matches) == 1

    def test_command_match_comment_index_tracking(self):
        """Verify comment_index tracks which comment contained the match."""
        comments = [
            "no command here",
            "also nothing",
            "@github2gerrit create missing change",
            "trailing comment",
        ]
        result = parse_commands(comments)
        assert result.matches[0].comment_index == 2


# ── Real-world regression tests ─────────────────────────────────────


class TestRealWorldRegression:
    """Regression tests based on real PR comments from production repos.

    These capture exact comment bodies observed in the wild so that
    parser changes never silently break known-good inputs.
    """

    def test_fdio_vpp_pr_3689_comment(self):
        """Exact comment posted on FDio/vpp PR #3689.

        See: https://github.com/FDio/vpp/pull/3689#issuecomment-3969230927
        The comment was added to unblock a stuck dependabot PR whose
        original ``opened`` event failed due to the shallow-clone bug
        fixed in v1.0.6.
        """
        # Exact body returned by the GitHub API for this comment
        real_comment_body = "@github2gerrit create missing change"

        result = parse_commands([real_comment_body])
        assert result.has_matches
        assert len(result.matches) == 1
        assert result.matches[0].command_name == "create missing change"
        assert result.matches[0].raw_text == "create missing change"
        assert result.matches[0].comment_index == 0
        assert not result.unrecognised

        # Also verify via convenience helpers
        assert has_command([real_comment_body], "create missing change")
        match = find_command([real_comment_body], "create missing change")
        assert match is not None
        assert match.command_name == "create missing change"

    def test_fdio_vpp_pr_3689_full_comment_sequence(self):
        """Simulate the full comment history that would exist on a
        stuck FDio/vpp PR: earlier bot comments followed by the
        human-issued create-missing directive.
        """
        comments = [
            # Dependabot's initial description (no command)
            "Bumps [some-dep](https://github.com/org/dep) from 1.0 to 2.0.",
            # Earlier failed github2gerrit mapping comment
            (
                "<!-- github2gerrit:change-id-map v1 -->\n"
                "PR: https://github.com/FDio/vpp/pull/3689\n"
                "<!-- end github2gerrit:change-id-map -->"
            ),
            # The human fix
            "@github2gerrit create missing change",
        ]
        result = parse_commands(comments)
        assert result.has_matches
        assert len(result.matches) == 1
        assert result.matches[0].command_name == "create missing change"
        assert result.matches[0].comment_index == 2
