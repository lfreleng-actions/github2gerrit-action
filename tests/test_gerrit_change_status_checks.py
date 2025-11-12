# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Tests for Gerrit change status verification and force flag behavior.

Verifies that:
1. Gerrit change status can be extracted from URLs
2. Status checking works correctly (MERGED, ABANDONED, NEW, UNKNOWN)
3. Force flag overrides status checks appropriately
4. PR closure behavior respects or ignores status based on context
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from github2gerrit.gerrit_pr_closer import check_gerrit_change_status
from github2gerrit.gerrit_pr_closer import (
    close_github_pr_for_merged_gerrit_change,
)
from github2gerrit.gerrit_pr_closer import extract_change_number_from_url


class TestExtractChangeNumberFromUrl:
    """Tests for extracting change numbers from Gerrit URLs."""

    def test_standard_gerrit_url(self):
        """Test extraction from standard Gerrit URL format."""
        url = "https://gerrit.example.org/c/project/+/12345"
        result = extract_change_number_from_url(url)
        assert result == ("gerrit.example.org", "12345")

    def test_gerrit_url_with_subpath(self):
        """Test extraction from Gerrit URL with subpath (e.g., /infra/)."""
        url = "https://gerrit.linuxfoundation.org/infra/c/releng/lftools/+/123"
        result = extract_change_number_from_url(url)
        assert result == ("gerrit.linuxfoundation.org", "123")

    def test_gerrit_url_with_nested_project(self):
        """Test extraction from URL with deeply nested project path."""
        url = "https://gerrit.example.org/c/some/nested/project/+/99999"
        result = extract_change_number_from_url(url)
        assert result == ("gerrit.example.org", "99999")

    def test_invalid_url_returns_none(self):
        """Test that invalid URLs return None."""
        url = "https://github.com/owner/repo/pull/123"
        result = extract_change_number_from_url(url)
        assert result is None

    def test_malformed_url_returns_none(self):
        """Test that malformed Gerrit URLs return None."""
        url = "https://gerrit.example.org/invalid/format"
        result = extract_change_number_from_url(url)
        assert result is None


class TestCheckGerritChangeStatus:
    """Tests for checking Gerrit change status via REST API."""

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    def test_merged_change_status(self, mock_build_client):
        """Test detection of MERGED change status."""
        mock_client = MagicMock()
        mock_client.get.return_value = {"status": "MERGED"}
        mock_build_client.return_value = mock_client

        url = "https://gerrit.example.org/c/project/+/12345"
        status = check_gerrit_change_status(url)

        assert status == "MERGED"
        mock_client.get.assert_called_once_with("/changes/12345")

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    def test_abandoned_change_status(self, mock_build_client):
        """Test detection of ABANDONED change status."""
        mock_client = MagicMock()
        mock_client.get.return_value = {"status": "ABANDONED"}
        mock_build_client.return_value = mock_client

        url = "https://gerrit.example.org/c/project/+/67890"
        status = check_gerrit_change_status(url)

        assert status == "ABANDONED"

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    def test_new_change_status(self, mock_build_client):
        """Test detection of NEW (open) change status."""
        mock_client = MagicMock()
        mock_client.get.return_value = {"status": "NEW"}
        mock_build_client.return_value = mock_client

        url = "https://gerrit.example.org/c/project/+/11111"
        status = check_gerrit_change_status(url)

        assert status == "NEW"

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    def test_api_failure_returns_unknown(self, mock_build_client):
        """Test that API failures return UNKNOWN status."""
        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("API error")
        mock_build_client.return_value = mock_client

        url = "https://gerrit.example.org/c/project/+/12345"
        status = check_gerrit_change_status(url)

        assert status == "UNKNOWN"

    def test_invalid_url_returns_unknown(self):
        """Test that invalid URLs return UNKNOWN status."""
        url = "https://invalid-url"
        status = check_gerrit_change_status(url)

        assert status == "UNKNOWN"


class TestForceFlag:
    """Tests for force flag behavior in PR closure."""

    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_info_for_display")
    @patch("github2gerrit.gerrit_pr_closer.create_pr_comment")
    def test_abandoned_change_without_close_merged_prs_adds_comment(
        self,
        mock_create_comment,
        mock_extract_info,
        mock_extract_pr,
        mock_check_status,
        mock_close_pr,
        mock_get_pull,
        mock_build_client,
    ):
        """Test that abandoned changes add comment but don't close PR when close_merged_prs=False."""
        # Setup mocks
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_check_status.return_value = "ABANDONED"

        # Mock GitHub API objects
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # Mock PR info extraction
        mock_extract_info.return_value = {
            "Repository": "owner/repo",
            "PR Number": 123,
            "Title": "Test PR",
            "Author": "bot",
            "Base Branch": "main",
            "SHA": "abc123",
            "URL": "https://github.com/owner/repo/pull/123",
        }

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should add comment but not close PR
        result = close_github_pr_for_merged_gerrit_change(
            "abc123de",
            gerrit_change_url=gerrit_url,
            force=False,
            close_merged_prs=False,
        )

        # Should not close PR, only add comment
        mock_close_pr.assert_not_called()
        mock_create_comment.assert_called_once()
        assert result is True

    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_info_for_display")
    def test_abandoned_change_with_force_closes_pr(
        self,
        mock_extract_info,
        mock_extract_pr,
        mock_check_status,
        mock_close_pr,
        mock_get_pull,
        mock_build_client,
    ):
        """Test that force flag allows closing PR for abandoned changes."""
        # Setup mocks
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_check_status.return_value = "ABANDONED"

        # Mock GitHub API objects
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # Mock PR info extraction
        mock_extract_info.return_value = {
            "Repository": "owner/repo",
            "PR Number": 123,
            "Title": "Test PR",
            "Author": "bot",
            "Base Branch": "main",
            "SHA": "abc123",
            "URL": "https://github.com/owner/repo/pull/123",
        }

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should succeed with force=True
        result = close_github_pr_for_merged_gerrit_change(
            "abc123de",
            gerrit_change_url=gerrit_url,
            force=True,
        )

        assert result is True
        # Should attempt to close PR
        mock_close_pr.assert_called_once()

    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_info_for_display")
    def test_merged_change_closes_pr_without_force(
        self,
        mock_extract_info,
        mock_extract_pr,
        mock_check_status,
        mock_close_pr,
        mock_get_pull,
        mock_build_client,
    ):
        """Test that merged changes close PRs without needing force flag."""
        # Setup mocks
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_check_status.return_value = "MERGED"

        # Mock GitHub API objects
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # Mock PR info extraction
        mock_extract_info.return_value = {
            "Repository": "owner/repo",
            "PR Number": 123,
            "Title": "Test PR",
            "Author": "bot",
            "Base Branch": "main",
            "SHA": "abc123",
            "URL": "https://github.com/owner/repo/pull/123",
        }

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should succeed without force flag
        result = close_github_pr_for_merged_gerrit_change(
            "abc123de",
            gerrit_change_url=gerrit_url,
            force=False,
        )

        assert result is True
        mock_check_status.assert_called_once_with(gerrit_url)
        mock_close_pr.assert_called_once()

    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    def test_no_gerrit_url_skips_status_check(
        self,
        mock_extract_pr,
        mock_close_pr,
        mock_get_pull,
        mock_build_client,
    ):
        """Test that status check is skipped when no Gerrit URL provided."""
        # Setup mocks
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"

        # Mock GitHub API objects
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # No gerrit_change_url provided
        with patch(
            "github2gerrit.gerrit_pr_closer.extract_pr_info_for_display"
        ) as mock_extract_info:
            mock_extract_info.return_value = {
                "Repository": "owner/repo",
                "PR Number": 123,
                "Title": "Test PR",
                "Author": "bot",
                "Base Branch": "main",
                "SHA": "abc123",
                "URL": "https://github.com/owner/repo/pull/123",
            }

            result = close_github_pr_for_merged_gerrit_change(
                "abc123de",
                gerrit_change_url=None,  # No URL provided
                force=False,
            )

        # Should succeed (status check skipped)
        assert result is True
        mock_close_pr.assert_called_once()


class TestContextAwareBehavior:
    """Tests for context-aware PR closure behavior."""

    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_info_for_display")
    def test_new_change_warns_but_proceeds(
        self,
        mock_extract_info,
        mock_extract_pr,
        mock_check_status,
        mock_close_pr,
        mock_get_pull,
        mock_build_client,
    ):
        """Test that NEW changes generate warning but still close PR."""
        # Setup mocks
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_check_status.return_value = "NEW"

        # Mock GitHub API objects
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # Mock PR info extraction
        mock_extract_info.return_value = {
            "Repository": "owner/repo",
            "PR Number": 123,
            "Title": "Test PR",
            "Author": "bot",
            "Base Branch": "main",
            "SHA": "abc123",
            "URL": "https://github.com/owner/repo/pull/123",
        }

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should proceed despite NEW status
        result = close_github_pr_for_merged_gerrit_change(
            "abc123de",
            gerrit_change_url=gerrit_url,
            force=False,
        )

        assert result is True
        mock_close_pr.assert_called_once()

    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_info_for_display")
    def test_unknown_status_warns_but_proceeds(
        self,
        mock_extract_info,
        mock_extract_pr,
        mock_check_status,
        mock_close_pr,
        mock_get_pull,
        mock_build_client,
    ):
        """Test that UNKNOWN status generates warning but still closes PR."""
        # Setup mocks
        mock_extract_pr.return_value = "https://github.com/owner/repo/pull/123"
        mock_check_status.return_value = "UNKNOWN"

        # Mock GitHub API objects
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # Mock PR info extraction
        mock_extract_info.return_value = {
            "Repository": "owner/repo",
            "PR Number": 123,
            "Title": "Test PR",
            "Author": "bot",
            "Base Branch": "main",
            "SHA": "abc123",
            "URL": "https://github.com/owner/repo/pull/123",
        }

        gerrit_url = "https://gerrit.example.org/c/project/+/12345"

        # Should proceed despite UNKNOWN status
        result = close_github_pr_for_merged_gerrit_change(
            "abc123de",
            gerrit_change_url=gerrit_url,
            force=False,
        )

        assert result is True
        mock_close_pr.assert_called_once()
