# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Tests for the --force flag in CLI when processing Gerrit change URLs.

The force flag should:
- WITHOUT --force: Reject MERGED/ABANDONED Gerrit changes with an error
- WITH --force: Allow processing MERGED/ABANDONED Gerrit changes
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from github2gerrit.cli import _process_close_gerrit_change
from github2gerrit.error_codes import ExitCode
from github2gerrit.error_codes import GitHub2GerritError
from github2gerrit.models import GitHubContext
from github2gerrit.models import Inputs


@pytest.fixture
def mock_inputs():
    """Create mock Inputs object for testing."""
    return Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="gerrit.example.org ssh-rsa AAAA...",
        gerrit_ssh_privkey_g2g="-----BEGIN RSA PRIVATE KEY-----\n...",
        gerrit_ssh_user_g2g="bot",
        gerrit_ssh_user_g2g_email="bot@example.org",
        github_token="ghp_test",  # noqa: S106
        organization="test-org",
        reviewers_email="reviewer@example.org",
        preserve_github_prs=False,
        dry_run=False,
        normalise_commit=True,
        gerrit_server="gerrit.example.org",
        gerrit_server_port=29418,
        gerrit_project="test/project",
        issue_id="",
        issue_id_lookup_json="",
        allow_duplicates=False,
        ci_testing=False,
    )


@pytest.fixture
def mock_github_context():
    """Create mock GitHubContext for testing."""
    from pathlib import Path

    return GitHubContext(
        event_name="workflow_dispatch",
        event_action="",
        event_path=Path("/tmp/event.json"),  # noqa: S108
        repository="test-org/test-repo",
        repository_owner="test-org",
        server_url="https://github.com",
        run_id="123456",
        sha="abc123",
        base_ref="main",
        head_ref="feature-branch",
        pr_number=None,
    )


class TestForceFlag:
    """Tests for --force flag behavior with Gerrit change URLs."""

    @patch("github2gerrit.cli.extract_pr_url_from_gerrit_change")
    @patch("github2gerrit.cli.check_gerrit_change_status")
    def test_merged_change_without_force_raises_error(
        self,
        mock_check_status,
        mock_extract_pr,
        mock_inputs,
        mock_github_context,
    ):
        """Test that MERGED change without --force raises an error."""
        mock_check_status.return_value = "MERGED"
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        with pytest.raises(GitHub2GerritError) as exc_info:
            _process_close_gerrit_change(
                mock_inputs,
                mock_github_context,
                gerrit_url,
                force=False,
            )

        assert exc_info.value.exit_code == ExitCode.GERRIT_CHANGE_ALREADY_FINAL
        assert "already MERGED" in str(exc_info.value.message)
        assert "--force" in str(exc_info.value.message)

    @patch("github2gerrit.cli.extract_pr_url_from_gerrit_change")
    @patch("github2gerrit.cli.check_gerrit_change_status")
    def test_abandoned_change_without_force_raises_error(
        self,
        mock_check_status,
        mock_extract_pr,
        mock_inputs,
        mock_github_context,
    ):
        """Test that ABANDONED change without --force raises an error."""
        mock_check_status.return_value = "ABANDONED"
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        with pytest.raises(GitHub2GerritError) as exc_info:
            _process_close_gerrit_change(
                mock_inputs,
                mock_github_context,
                gerrit_url,
                force=False,
            )

        assert exc_info.value.exit_code == ExitCode.GERRIT_CHANGE_ALREADY_FINAL
        assert "already ABANDONED" in str(exc_info.value.message)
        assert "--force" in str(exc_info.value.message)

    @patch("github2gerrit.cli.close_pr_with_status")
    @patch("github2gerrit.cli.parse_pr_url")
    @patch("github2gerrit.cli.extract_pr_url_from_gerrit_change")
    @patch("github2gerrit.cli.check_gerrit_change_status")
    def test_merged_change_with_force_proceeds(
        self,
        mock_check_status,
        mock_extract_pr,
        mock_parse_pr,
        mock_close_pr,
        mock_inputs,
        mock_github_context,
    ):
        """Test that MERGED change with --force proceeds successfully."""
        mock_check_status.return_value = "MERGED"
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_pr.return_value = ("owner", "repo", 123)
        mock_close_pr.return_value = None

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should not raise an error
        _process_close_gerrit_change(
            mock_inputs,
            mock_github_context,
            gerrit_url,
            force=True,
        )

        # Verify PR closure was attempted
        mock_close_pr.assert_called_once()

    @patch("github2gerrit.cli.close_pr_with_status")
    @patch("github2gerrit.cli.parse_pr_url")
    @patch("github2gerrit.cli.extract_pr_url_from_gerrit_change")
    @patch("github2gerrit.cli.check_gerrit_change_status")
    def test_abandoned_change_with_force_proceeds(
        self,
        mock_check_status,
        mock_extract_pr,
        mock_parse_pr,
        mock_close_pr,
        mock_inputs,
        mock_github_context,
    ):
        """Test that ABANDONED change with --force proceeds successfully."""
        mock_check_status.return_value = "ABANDONED"
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_pr.return_value = ("owner", "repo", 123)
        mock_close_pr.return_value = None

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should not raise an error
        _process_close_gerrit_change(
            mock_inputs,
            mock_github_context,
            gerrit_url,
            force=True,
        )

        # Verify PR closure was attempted
        mock_close_pr.assert_called_once()

    @patch("github2gerrit.cli.close_pr_with_status")
    @patch("github2gerrit.cli.parse_pr_url")
    @patch("github2gerrit.cli.extract_pr_url_from_gerrit_change")
    @patch("github2gerrit.cli.check_gerrit_change_status")
    def test_new_change_proceeds_without_force(
        self,
        mock_check_status,
        mock_extract_pr,
        mock_parse_pr,
        mock_close_pr,
        mock_inputs,
        mock_github_context,
    ):
        """Test that NEW change proceeds without requiring --force."""
        mock_check_status.return_value = "NEW"
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_pr.return_value = ("owner", "repo", 123)
        mock_close_pr.return_value = None

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should not raise an error even without force
        _process_close_gerrit_change(
            mock_inputs,
            mock_github_context,
            gerrit_url,
            force=False,
        )

        # Verify PR closure was attempted
        mock_close_pr.assert_called_once()

    @patch("github2gerrit.cli.close_pr_with_status")
    @patch("github2gerrit.cli.parse_pr_url")
    @patch("github2gerrit.cli.extract_pr_url_from_gerrit_change")
    @patch("github2gerrit.cli.check_gerrit_change_status")
    def test_unknown_status_proceeds_without_force(
        self,
        mock_check_status,
        mock_extract_pr,
        mock_parse_pr,
        mock_close_pr,
        mock_inputs,
        mock_github_context,
    ):
        """Test that UNKNOWN status proceeds without requiring --force."""
        mock_check_status.return_value = "UNKNOWN"
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_pr.return_value = ("owner", "repo", 123)
        mock_close_pr.return_value = None

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should not raise an error even without force
        _process_close_gerrit_change(
            mock_inputs,
            mock_github_context,
            gerrit_url,
            force=False,
        )

        # Verify PR closure was attempted
        mock_close_pr.assert_called_once()

    @patch("github2gerrit.cli.extract_pr_url_from_gerrit_change")
    def test_merged_change_no_pr_url_returns_early(
        self,
        mock_extract_pr,
        mock_inputs,
        mock_github_context,
    ):
        """Test that function returns early when no PR URL is found (no GitHub origin)."""
        mock_extract_pr.return_value = None  # No PR URL found

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should not raise an error but should return early with no-op message
        _process_close_gerrit_change(
            mock_inputs,
            mock_github_context,
            gerrit_url,
            force=True,
        )

        # Should check for PR URL and return early
        mock_extract_pr.assert_called_once()
