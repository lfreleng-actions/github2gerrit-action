# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Tests for the gerrit_pr_closer module.

Tests the functionality of closing GitHub PRs when Gerrit changes are merged.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from github2gerrit.gerrit_pr_closer import _build_closure_comment
from github2gerrit.gerrit_pr_closer import _env_bool
from github2gerrit.gerrit_pr_closer import (
    close_github_pr_for_merged_gerrit_change,
)
from github2gerrit.gerrit_pr_closer import extract_pr_info_for_display
from github2gerrit.gerrit_pr_closer import extract_pr_url_from_commit
from github2gerrit.gerrit_pr_closer import parse_pr_url
from github2gerrit.gerrit_pr_closer import process_recent_commits_for_pr_closure


class TestEnvBool:
    """Tests for _env_bool helper function."""

    def test_returns_true_for_true_string(self, monkeypatch):
        """Test returns True for 'true' string."""
        monkeypatch.setenv("TEST_VAR", "true")
        assert _env_bool("TEST_VAR", False) is True

    def test_returns_true_for_one(self, monkeypatch):
        """Test returns True for '1' string."""
        monkeypatch.setenv("TEST_VAR", "1")
        assert _env_bool("TEST_VAR", False) is True

    def test_returns_true_for_yes(self, monkeypatch):
        """Test returns True for 'yes' string."""
        monkeypatch.setenv("TEST_VAR", "yes")
        assert _env_bool("TEST_VAR", False) is True

    def test_returns_true_for_on(self, monkeypatch):
        """Test returns True for 'on' string."""
        monkeypatch.setenv("TEST_VAR", "on")
        assert _env_bool("TEST_VAR", False) is True

    def test_returns_false_for_false_string(self, monkeypatch):
        """Test returns False for 'false' string."""
        monkeypatch.setenv("TEST_VAR", "false")
        assert _env_bool("TEST_VAR", True) is False

    def test_returns_default_when_not_set(self, monkeypatch):
        """Test returns default value when env var not set."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert _env_bool("TEST_VAR", True) is True
        assert _env_bool("TEST_VAR", False) is False

    def test_returns_default_for_empty_string(self, monkeypatch):
        """Test returns default value for empty string."""
        monkeypatch.setenv("TEST_VAR", "")
        assert _env_bool("TEST_VAR", True) is True
        assert _env_bool("TEST_VAR", False) is False

    def test_case_insensitive(self, monkeypatch):
        """Test is case insensitive."""
        monkeypatch.setenv("TEST_VAR", "TRUE")
        assert _env_bool("TEST_VAR", False) is True
        monkeypatch.setenv("TEST_VAR", "Yes")
        assert _env_bool("TEST_VAR", False) is True


class TestExtractPrUrlFromCommit:
    """Tests for extract_pr_url_from_commit function."""

    def test_extracts_pr_url_from_commit_with_trailer(self):
        """Test extracting PR URL from a commit with GitHub-PR trailer."""
        commit_message = """Fix critical bug

This commit fixes a critical bug in the parser.

Change-Id: I1234567890abcdef1234567890abcdef12345678
GitHub-PR: https://github.com/owner/repo/pull/123
GitHub-Hash: abc123
Signed-off-by: Developer <dev@example.com>"""

        with patch("github2gerrit.gerrit_pr_closer.git_show") as mock_git_show:
            mock_git_show.return_value = commit_message

            result = extract_pr_url_from_commit("abc123def456")

            assert result == "https://github.com/owner/repo/pull/123"
            mock_git_show.assert_called_once_with("abc123def456", fmt="%B")

    def test_returns_none_when_no_trailer(self):
        """Test returns None when commit has no GitHub-PR trailer."""
        commit_message = """Regular commit

Just a regular commit without any trailers.
"""

        with patch("github2gerrit.gerrit_pr_closer.git_show") as mock_git_show:
            mock_git_show.return_value = commit_message

            result = extract_pr_url_from_commit("xyz789")

            assert result is None

    def test_returns_last_trailer_when_multiple(self):
        """Test returns the last GitHub-PR trailer when multiple exist."""
        commit_message = """Commit with multiple trailers

Change-Id: I1234567890abcdef1234567890abcdef12345678
GitHub-PR: https://github.com/owner/repo/pull/111
GitHub-PR: https://github.com/owner/repo/pull/222
Signed-off-by: Developer <dev@example.com>"""

        with patch("github2gerrit.gerrit_pr_closer.git_show") as mock_git_show:
            mock_git_show.return_value = commit_message

            result = extract_pr_url_from_commit("commit123")

            assert result == "https://github.com/owner/repo/pull/222"

    def test_handles_git_show_error(self):
        """Test gracefully handles git show errors."""
        with patch("github2gerrit.gerrit_pr_closer.git_show") as mock_git_show:
            mock_git_show.side_effect = Exception("Git error")

            result = extract_pr_url_from_commit("badcommit")

            assert result is None


class TestParsePrUrl:
    """Tests for parse_pr_url function."""

    def test_parses_valid_pr_url(self):
        """Test parsing a valid GitHub PR URL."""
        url = "https://github.com/owner/repo/pull/123"

        result = parse_pr_url(url)

        assert result == ("owner", "repo", 123)

    def test_parses_http_url(self):
        """Test parsing HTTP (not HTTPS) URL."""
        url = "http://github.com/myorg/myrepo/pull/456"

        result = parse_pr_url(url)

        assert result == ("myorg", "myrepo", 456)

    def test_returns_none_for_invalid_url(self):
        """Test returns None for invalid URL format."""
        invalid_urls = [
            "not-a-url",
            "https://gitlab.com/owner/repo/pull/123",
            "https://github.com/owner/repo/issues/123",
            "https://github.com/owner/repo",
            "github.com/owner/repo/pull/123",
        ]

        for url in invalid_urls:
            result = parse_pr_url(url)
            assert result is None, f"Expected None for {url}"

    def test_handles_numeric_pr_numbers(self):
        """Test correctly parses PR number as integer."""
        url = "https://github.com/test/test/pull/99999"

        result = parse_pr_url(url)

        assert result == ("test", "test", 99999)
        assert isinstance(result[2], int)


class TestExtractPrInfoForDisplay:
    """Tests for extract_pr_info_for_display function."""

    def test_extracts_complete_pr_info(self):
        """Test extracting complete PR information."""
        mock_pr = MagicMock()
        mock_pr.title = "Test PR Title"
        mock_pr.user.login = "testuser"
        mock_pr.base.ref = "main"
        mock_pr.head.sha = "abc123def456"
        mock_pr.get_files.return_value = [MagicMock(), MagicMock()]  # 2 files

        result = extract_pr_info_for_display(
            mock_pr,
            owner="owner",
            repo="repo",
            pr_number=42,
        )

        assert result["Repository"] == "owner/repo"
        assert result["PR Number"] == 42
        assert result["Title"] == "Test PR Title"
        assert result["Author"] == "testuser"
        assert result["Base Branch"] == "main"
        assert result["SHA"] == "abc123def456"
        assert result["URL"] == "https://github.com/owner/repo/pull/42"
        assert result["Files Changed"] == 2

    def test_handles_missing_user(self):
        """Test handles PR with missing user information."""
        mock_pr = MagicMock()
        mock_pr.title = "Test PR"
        mock_pr.user = None
        mock_pr.base.ref = "main"
        mock_pr.head.sha = "abc123"

        result = extract_pr_info_for_display(mock_pr, "owner", "repo", 1)

        assert result["Author"] == "Unknown"

    def test_handles_files_error(self):
        """Test handles error when fetching file count."""
        mock_pr = MagicMock()
        mock_pr.title = "Test PR"
        mock_pr.user.login = "testuser"
        mock_pr.base.ref = "main"
        mock_pr.head.sha = "abc123"
        mock_pr.get_files.side_effect = Exception("API error")

        result = extract_pr_info_for_display(mock_pr, "owner", "repo", 1)

        assert result["Files Changed"] == "unknown"


class TestBuildClosureComment:
    """Tests for _build_closure_comment function."""

    def test_builds_comment_with_gerrit_url(self):
        """Test builds comment with Gerrit change URL."""
        gerrit_url = "https://gerrit.example.com/c/project/+/12345"

        result = _build_closure_comment(gerrit_url)

        assert "**Automated PR Closure**" in result
        assert "merged" in result.lower()
        assert gerrit_url in result
        assert "GitHub2Gerrit" in result

    def test_builds_comment_without_gerrit_url(self):
        """Test builds comment without Gerrit change URL."""
        result = _build_closure_comment(None)

        assert "**Automated PR Closure**" in result
        assert "merged" in result.lower()
        assert "https://" not in result  # No specific URL
        assert "GitHub2Gerrit" in result


class TestCloseGithubPrForMergedGerritChange:
    """Tests for close_github_pr_for_merged_gerrit_change function."""

    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_info_for_display")
    @patch("github2gerrit.gerrit_pr_closer.display_pr_info")
    def test_closes_pr_successfully(
        self,
        mock_display,
        mock_extract_info,
        mock_close,
        mock_get_pull,
        mock_build_client,
        mock_parse_url,
        mock_extract_url,
    ):
        """Test successfully closing a GitHub PR."""
        # Setup mocks
        mock_extract_url.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_url.return_value = ("owner", "repo", 123)

        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"

        mock_build_client.return_value = mock_client
        mock_client.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr
        mock_extract_info.return_value = {"PR Number": 123}

        # Execute
        result = close_github_pr_for_merged_gerrit_change("abc123")

        # Verify
        assert result is True
        mock_extract_url.assert_called_once_with("abc123")
        mock_parse_url.assert_called_once_with(
            "https://github.com/owner/repo/pull/123"
        )
        mock_client.get_repo.assert_called_once_with("owner/repo")
        mock_get_pull.assert_called_once_with(mock_repo, 123)
        mock_close.assert_called_once()

    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    def test_returns_false_when_no_pr_url(self, mock_extract_url):
        """Test returns False when commit has no PR URL."""
        mock_extract_url.return_value = None

        result = close_github_pr_for_merged_gerrit_change("commit123")

        assert result is False

    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    def test_returns_false_when_invalid_pr_url(
        self, mock_parse_url, mock_extract_url
    ):
        """Test returns False when PR URL is invalid."""
        mock_extract_url.return_value = "invalid-url"
        mock_parse_url.return_value = None

        result = close_github_pr_for_merged_gerrit_change("commit123")

        assert result is False

    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_info_for_display")
    @patch("github2gerrit.gerrit_pr_closer.display_pr_info")
    def test_returns_false_when_pr_already_closed(
        self,
        mock_display,
        mock_extract_info,
        mock_get_pull,
        mock_build_client,
        mock_parse_url,
        mock_extract_url,
    ):
        """Test returns False when PR already closed (non-fatal)."""
        mock_extract_url.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_url.return_value = ("owner", "repo", 123)

        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "closed"  # Already closed

        mock_build_client.return_value = mock_client
        mock_client.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        result = close_github_pr_for_merged_gerrit_change("abc123")

        assert result is False

    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    def test_returns_false_when_pr_not_found(
        self,
        mock_get_pull,
        mock_build_client,
        mock_parse_url,
        mock_extract_url,
    ):
        """Test returns False when PR not found (404) - non-fatal."""
        mock_extract_url.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_url.return_value = ("owner", "repo", 123)

        mock_client = MagicMock()
        mock_repo = MagicMock()

        mock_build_client.return_value = mock_client
        mock_client.get_repo.return_value = mock_repo

        # Simulate 404 error when fetching PR
        mock_get_pull.side_effect = Exception("404 Not Found")

        # Should return False without raising exception
        result = close_github_pr_for_merged_gerrit_change("abc123")

        assert result is False

    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    def test_returns_false_on_api_error(
        self,
        mock_get_pull,
        mock_build_client,
        mock_parse_url,
        mock_extract_url,
    ):
        """Test returns False on GitHub API error - non-fatal."""
        mock_extract_url.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_url.return_value = ("owner", "repo", 123)

        mock_client = MagicMock()
        mock_repo = MagicMock()

        mock_build_client.return_value = mock_client
        mock_client.get_repo.return_value = mock_repo

        # Simulate API error
        mock_get_pull.side_effect = Exception("API rate limit exceeded")

        # Should return False without raising exception
        result = close_github_pr_for_merged_gerrit_change("abc123")

        assert result is False

    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_info_for_display")
    @patch("github2gerrit.gerrit_pr_closer.display_pr_info")
    def test_dry_run_mode(
        self,
        mock_display,
        mock_extract_info,
        mock_get_pull,
        mock_build_client,
        mock_parse_url,
        mock_extract_url,
    ):
        """Test dry-run mode doesn't actually close PR."""
        mock_extract_url.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse_url.return_value = ("owner", "repo", 123)

        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"

        mock_build_client.return_value = mock_client
        mock_client.get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr
        mock_extract_info.return_value = {"PR Number": 123}

        with patch("github2gerrit.gerrit_pr_closer.close_pr") as mock_close:
            result = close_github_pr_for_merged_gerrit_change(
                "abc123", dry_run=True
            )

            assert result is True
            mock_close.assert_not_called()  # Should not close in dry-run


class TestProcessRecentCommitsForPrClosure:
    """Tests for process_recent_commits_for_pr_closure function."""

    @patch(
        "github2gerrit.gerrit_pr_closer.close_github_pr_for_merged_gerrit_change"
    )
    def test_processes_multiple_commits(self, mock_close):
        """Test processing multiple commits."""
        mock_close.side_effect = [True, False, True]  # Close 2 out of 3

        commits = ["commit1", "commit2", "commit3"]
        result = process_recent_commits_for_pr_closure(commits)

        assert result == 2
        assert mock_close.call_count == 3

    @patch(
        "github2gerrit.gerrit_pr_closer.close_github_pr_for_merged_gerrit_change"
    )
    def test_returns_zero_for_empty_list(self, mock_close):
        """Test returns 0 when no commits provided."""
        result = process_recent_commits_for_pr_closure([])

        assert result == 0
        mock_close.assert_not_called()

    @patch(
        "github2gerrit.gerrit_pr_closer.close_github_pr_for_merged_gerrit_change"
    )
    def test_continues_on_error(self, mock_close):
        """Test continues processing commits even when one fails."""
        # Since the function is now non-fatal, it returns False on errors
        mock_close.side_effect = [
            True,
            False,  # Returns False on error (non-fatal)
            True,
        ]

        commits = ["commit1", "commit2", "commit3"]
        result = process_recent_commits_for_pr_closure(commits)

        assert result == 2  # Should have closed 2
        assert mock_close.call_count == 3  # Should have tried all 3

    @patch(
        "github2gerrit.gerrit_pr_closer.close_github_pr_for_merged_gerrit_change"
    )
    def test_dry_run_mode_propagates(self, mock_close):
        """Test dry-run mode propagates to individual close calls."""
        mock_close.return_value = True

        commits = ["commit1", "commit2"]
        process_recent_commits_for_pr_closure(commits, dry_run=True)

        # Verify dry_run=True passed to each call
        for call_args in mock_close.call_args_list:
            assert call_args[1]["dry_run"] is True

    @patch(
        "github2gerrit.gerrit_pr_closer.close_github_pr_for_merged_gerrit_change"
    )
    def test_no_exceptions_on_failures(self, mock_close):
        """Test that failures don't raise exceptions (non-fatal behavior)."""
        # Simulate various failure scenarios by returning False
        mock_close.side_effect = [False, False, False]

        commits = ["commit1", "commit2", "commit3"]

        # Should not raise any exceptions
        result = process_recent_commits_for_pr_closure(commits)

        # All failed, so closed_count should be 0
        assert result == 0
        assert mock_close.call_count == 3


class TestAbandonedChangeHandling:
    """Tests for handling abandoned Gerrit changes."""

    def test_build_abandoned_comment_with_url(self):
        """Test building abandoned comment with Gerrit URL."""
        from github2gerrit.gerrit_pr_closer import _build_abandoned_comment

        url = "https://gerrit.example.org/c/project/+/12345"
        comment = _build_abandoned_comment(url)

        assert "Automated PR Closure" in comment
        assert "⛔️" in comment
        assert "abandoned" in comment
        assert "rejected" in comment
        assert url in comment
        assert "NOT part of the main codebase" in comment

    def test_build_abandoned_comment_without_url(self):
        """Test building abandoned comment without Gerrit URL."""
        from github2gerrit.gerrit_pr_closer import _build_abandoned_comment

        comment = _build_abandoned_comment(None)

        assert "Automated PR Closure" in comment
        assert "⛔️" in comment
        assert "abandoned" in comment
        assert "rejected" in comment
        assert "NOT part of the main codebase" in comment

    def test_build_abandoned_notification_comment_with_url(self):
        """Test building abandoned notification comment with Gerrit URL."""
        from github2gerrit.gerrit_pr_closer import (
            _build_abandoned_notification_comment,
        )

        url = "https://gerrit.example.org/c/project/+/12345"
        comment = _build_abandoned_notification_comment(url)

        assert "Gerrit Change Abandoned" in comment
        assert "🏳️" in comment
        assert "abandoned" in comment
        assert url in comment
        assert "remains open" in comment
        assert "CLOSE_MERGED_PRS" in comment
        assert "disabled" in comment

    def test_build_abandoned_notification_comment_without_url(self):
        """Test building abandoned notification comment without Gerrit URL."""
        from github2gerrit.gerrit_pr_closer import (
            _build_abandoned_notification_comment,
        )

        comment = _build_abandoned_notification_comment(None)

        assert "Gerrit Change Abandoned" in comment
        assert "🏳️" in comment
        assert "abandoned" in comment
        assert "remains open" in comment

    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    def test_close_pr_when_abandoned_and_close_merged_prs_true(
        self,
        mock_close_pr,
        mock_build_client,
        mock_parse,
        mock_extract,
        mock_check_status,
    ):
        """Test PR is closed when change is abandoned and close_merged_prs=True."""
        # Setup mocks
        mock_check_status.return_value = "ABANDONED"
        mock_extract.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse.return_value = ("owner", "repo", 123)

        # Mock GitHub API
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_pr.number = 123
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo
        mock_build_client.return_value = mock_client

        # Call function with close_merged_prs=True (default)
        result = close_github_pr_for_merged_gerrit_change(
            "abc123",
            gerrit_change_url="https://gerrit.example.org/c/project/+/12345",
            close_merged_prs=True,
        )

        assert result is True
        mock_close_pr.assert_called_once()
        # Verify the comment contains abandoned message
        call_args = mock_close_pr.call_args
        comment = call_args[1]["comment"]
        assert "abandoned" in comment.lower()
        assert "rejected" in comment.lower()
        assert "⛔️" in comment

    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.create_pr_comment")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    def test_comment_only_when_abandoned_and_close_merged_prs_false(
        self,
        mock_close_pr,
        mock_create_comment,
        mock_build_client,
        mock_parse,
        mock_extract,
        mock_check_status,
    ):
        """Test only comment added when change is abandoned and close_merged_prs=False."""
        # Setup mocks
        mock_check_status.return_value = "ABANDONED"
        mock_extract.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse.return_value = ("owner", "repo", 123)

        # Mock GitHub API
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_pr.number = 123
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo
        mock_build_client.return_value = mock_client

        # Call function with close_merged_prs=False
        result = close_github_pr_for_merged_gerrit_change(
            "abc123",
            gerrit_change_url="https://gerrit.example.org/c/project/+/12345",
            close_merged_prs=False,
        )

        assert result is True
        # PR should NOT be closed
        mock_close_pr.assert_not_called()
        # Comment should be added
        mock_create_comment.assert_called_once()
        # Verify the comment contains abandoned notification
        call_args = mock_create_comment.call_args
        comment = call_args[0][1]
        assert "abandoned" in comment.lower()
        assert "remains open" in comment.lower()
        assert "🏳️" in comment

    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    @patch("github2gerrit.gerrit_pr_closer.parse_pr_url")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.close_pr")
    def test_close_pr_when_merged_and_close_merged_prs_true(
        self,
        mock_close_pr,
        mock_build_client,
        mock_parse,
        mock_extract,
        mock_check_status,
    ):
        """Test PR is closed when change is merged and close_merged_prs=True."""
        # Setup mocks
        mock_check_status.return_value = "MERGED"
        mock_extract.return_value = "https://github.com/owner/repo/pull/123"
        mock_parse.return_value = ("owner", "repo", 123)

        # Mock GitHub API
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_pr.number = 123
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_repo
        mock_build_client.return_value = mock_client

        # Call function with close_merged_prs=True
        result = close_github_pr_for_merged_gerrit_change(
            "abc123",
            gerrit_change_url="https://gerrit.example.org/c/project/+/12345",
            close_merged_prs=True,
        )

        assert result is True
        mock_close_pr.assert_called_once()
        # Verify the comment contains merged message (not abandoned)
        call_args = mock_close_pr.call_args
        comment = call_args[1]["comment"]
        assert "merged" in comment.lower()
        assert "abandoned" not in comment.lower()

    @patch("github2gerrit.gerrit_pr_closer.check_gerrit_change_status")
    @patch("github2gerrit.gerrit_pr_closer.extract_pr_url_from_commit")
    def test_no_action_when_merged_and_close_merged_prs_false(
        self,
        mock_extract,
        mock_check_status,
    ):
        """Test no action taken when change is merged and close_merged_prs=False."""
        # Setup mocks
        mock_check_status.return_value = "MERGED"
        mock_extract.return_value = "https://github.com/owner/repo/pull/123"

        # Call function with close_merged_prs=False
        result = close_github_pr_for_merged_gerrit_change(
            "abc123",
            gerrit_change_url="https://gerrit.example.org/c/project/+/12345",
            close_merged_prs=False,
        )

        # Should return False (no action taken)
        assert result is False


class TestAbandonGerritChangeForClosedPr:
    """Tests for abandon_gerrit_change_for_closed_pr function."""

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer._abandon_gerrit_change")
    def test_abandons_gerrit_change_for_closed_pr(
        self,
        mock_abandon,
        mock_get_pull,
        mock_build_client,
        mock_build_gerrit_client,
    ):
        """Test successfully abandons Gerrit change when PR is closed."""
        from github2gerrit.gerrit_pr_closer import (
            abandon_gerrit_change_for_closed_pr,
        )

        # Setup Gerrit client mock
        mock_gerrit_client = MagicMock()
        mock_build_gerrit_client.return_value = mock_gerrit_client

        # Mock Gerrit query response with matching change
        mock_gerrit_client.get.return_value = [
            {
                "_number": "12345",
                "subject": "Test change",
                "current_revision": "rev1",
                "revisions": {
                    "rev1": {
                        "commit": {
                            "message": (
                                "Test commit\n\n"
                                "GitHub-PR: https://github.com/owner/repo/pull/42"
                            )
                        }
                    }
                },
            }
        ]

        # Setup GitHub client mock
        mock_repo = MagicMock()
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_pr = MagicMock()
        mock_pr.number = 42
        mock_get_pull.return_value = mock_pr

        # Mock PR comments
        mock_issue = MagicMock()
        mock_comment = MagicMock()
        mock_comment.body = "Closing this PR"
        mock_comment.user.login = "testuser"
        mock_issue.get_comments.return_value = [mock_comment]
        mock_pr.as_issue.return_value = mock_issue

        # Call function
        result = abandon_gerrit_change_for_closed_pr(
            pr_number=42,
            gerrit_server="gerrit.example.com",
            gerrit_project="test-project",
            repository="owner/repo",
            dry_run=False,
        )

        # Verify result
        assert result == "12345"
        mock_abandon.assert_called_once()
        call_args = mock_abandon.call_args
        assert call_args[0][1] == "12345"  # change_number
        assert "GitHub pull request #42 was closed" in call_args[0][2]
        assert "Closing this PR" in call_args[0][2]

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    def test_returns_false_when_no_matching_change(
        self,
        mock_build_gerrit_client,
    ):
        """Test returns False when no matching Gerrit change found."""
        from github2gerrit.gerrit_pr_closer import (
            abandon_gerrit_change_for_closed_pr,
        )

        # Setup Gerrit client mock
        mock_gerrit_client = MagicMock()
        mock_build_gerrit_client.return_value = mock_gerrit_client

        # Mock empty Gerrit query response
        mock_gerrit_client.get.return_value = []

        # Call function
        result = abandon_gerrit_change_for_closed_pr(
            pr_number=42,
            gerrit_server="gerrit.example.com",
            gerrit_project="test-project",
            repository="owner/repo",
            dry_run=False,
        )

        # Verify result
        assert result is None

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    def test_returns_false_when_no_pr_url_match(
        self,
        mock_build_gerrit_client,
    ):
        """Test returns False when Gerrit changes don't match PR URL."""
        from github2gerrit.gerrit_pr_closer import (
            abandon_gerrit_change_for_closed_pr,
        )

        # Setup Gerrit client mock
        mock_gerrit_client = MagicMock()
        mock_build_gerrit_client.return_value = mock_gerrit_client

        # Mock Gerrit query response with non-matching change
        mock_gerrit_client.get.return_value = [
            {
                "_number": "12345",
                "subject": "Test change",
                "current_revision": "rev1",
                "revisions": {
                    "rev1": {
                        "commit": {
                            "message": (
                                "Test commit\n\n"
                                "GitHub-PR: https://github.com/owner/repo/pull/99"
                            )
                        }
                    }
                },
            }
        ]

        # Call function looking for PR #42
        result = abandon_gerrit_change_for_closed_pr(
            pr_number=42,
            gerrit_server="gerrit.example.com",
            gerrit_project="test-project",
            repository="owner/repo",
            dry_run=False,
        )

        # Verify result
        assert result is None

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    def test_dry_run_does_not_abandon(
        self,
        mock_get_pull,
        mock_build_client,
        mock_build_gerrit_client,
    ):
        """Test dry-run mode does not actually abandon change."""
        from github2gerrit.gerrit_pr_closer import (
            abandon_gerrit_change_for_closed_pr,
        )

        # Setup Gerrit client mock
        mock_gerrit_client = MagicMock()
        mock_build_gerrit_client.return_value = mock_gerrit_client

        # Mock Gerrit query response with matching change
        mock_gerrit_client.get.return_value = [
            {
                "_number": "12345",
                "subject": "Test change",
                "current_revision": "rev1",
                "revisions": {
                    "rev1": {
                        "commit": {
                            "message": (
                                "Test commit\n\n"
                                "GitHub-PR: https://github.com/owner/repo/pull/42"
                            )
                        }
                    }
                },
            }
        ]

        # Setup GitHub client mock
        mock_repo = MagicMock()
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_pr = MagicMock()
        mock_pr.number = 42
        mock_get_pull.return_value = mock_pr
        mock_pr.as_issue.return_value.get_comments.return_value = []

        # Call function in dry-run mode
        result = abandon_gerrit_change_for_closed_pr(
            pr_number=42,
            gerrit_server="gerrit.example.com",
            gerrit_project="test-project",
            repository="owner/repo",
            dry_run=True,
        )

        # Verify result is change number but no POST was made
        assert result == "12345"
        mock_gerrit_client.post.assert_not_called()

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    def test_handles_gerrit_query_exception(
        self,
        mock_build_gerrit_client,
    ):
        """Test handles exception during Gerrit query gracefully."""
        from github2gerrit.gerrit_pr_closer import (
            abandon_gerrit_change_for_closed_pr,
        )

        # Setup Gerrit client mock to raise exception
        mock_gerrit_client = MagicMock()
        mock_build_gerrit_client.return_value = mock_gerrit_client
        mock_gerrit_client.get.side_effect = Exception("Connection error")

        # Call function
        result = abandon_gerrit_change_for_closed_pr(
            pr_number=42,
            gerrit_server="gerrit.example.com",
            gerrit_project="test-project",
            repository="owner/repo",
            dry_run=False,
        )

        # Verify result is None
        assert result is None

    @patch("github2gerrit.gerrit_pr_closer.build_client_for_host")
    @patch("github2gerrit.gerrit_pr_closer.build_client")
    @patch("github2gerrit.gerrit_pr_closer.get_pull")
    @patch("github2gerrit.gerrit_pr_closer._abandon_gerrit_change")
    def test_includes_closure_comments(
        self,
        mock_abandon,
        mock_get_pull,
        mock_build_client,
        mock_build_gerrit_client,
    ):
        """Test includes PR closure comments in abandon message."""
        from github2gerrit.gerrit_pr_closer import (
            abandon_gerrit_change_for_closed_pr,
        )

        # Setup Gerrit client mock
        mock_gerrit_client = MagicMock()
        mock_build_gerrit_client.return_value = mock_gerrit_client
        mock_gerrit_client.get.return_value = [
            {
                "_number": "12345",
                "subject": "Test change",
                "current_revision": "rev1",
                "revisions": {
                    "rev1": {
                        "commit": {
                            "message": (
                                "Test commit\n\n"
                                "GitHub-PR: https://github.com/owner/repo/pull/42"
                            )
                        }
                    }
                },
            }
        ]

        # Setup GitHub client mock with comments
        mock_repo = MagicMock()
        mock_build_client.return_value.get_repo.return_value = mock_repo
        mock_pr = MagicMock()
        mock_pr.number = 42
        mock_get_pull.return_value = mock_pr

        # Mock multiple PR comments
        mock_issue = MagicMock()
        mock_comment1 = MagicMock()
        mock_comment1.body = "First comment"
        mock_comment1.user.login = "user1"
        mock_comment2 = MagicMock()
        mock_comment2.body = "Second comment"
        mock_comment2.user.login = "user2"
        mock_comment3 = MagicMock()
        mock_comment3.body = "Closing this now"
        mock_comment3.user.login = "user3"
        mock_issue.get_comments.return_value = [
            mock_comment1,
            mock_comment2,
            mock_comment3,
        ]
        mock_pr.as_issue.return_value = mock_issue

        # Call function
        result = abandon_gerrit_change_for_closed_pr(
            pr_number=42,
            gerrit_server="gerrit.example.com",
            gerrit_project="test-project",
            repository="owner/repo",
            dry_run=False,
        )

        # Verify abandon was called with comments included
        assert result == "12345"
        mock_abandon.assert_called_once()
        abandon_message = mock_abandon.call_args[0][2]
        assert "Comment by user1" in abandon_message
        assert "Comment by user2" in abandon_message
        assert "Comment by user3" in abandon_message
        assert "Closing this now" in abandon_message
