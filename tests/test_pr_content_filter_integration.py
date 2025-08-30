# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Integration tests for PR content filtering in the core workflow."""

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

from github2gerrit.core import Orchestrator
from github2gerrit.models import GitHubContext
from github2gerrit.models import Inputs


class TestPRContentFilteringIntegration:
    """Test PR content filtering integration with the core workflow."""

    def _create_inputs(self, use_pr_as_commit: bool = True) -> Inputs:
        """Create test inputs with sensible defaults."""
        return Inputs(
            submit_single_commits=False,
            use_pr_as_commit=use_pr_as_commit,
            fetch_depth=10,
            gerrit_known_hosts="example.org ssh-rsa AAAAB3Nza...",
            gerrit_ssh_privkey_g2g="-----BEGIN KEY-----\nabc\n-----END KEY-----",
            gerrit_ssh_user_g2g="gerrit-bot",
            gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
            organization="test-org",
            reviewers_email="",
            preserve_github_prs=False,
            dry_run=False,
            normalise_commit=True,
            gerrit_server="gerrit.example.org",
            gerrit_server_port="29418",
            gerrit_project="test/project",
            issue_id="",
            allow_duplicates=False,
            ci_testing=False,
            duplicates_filter="open",
        )

    def _create_github_context(self, pr_number: int = 123) -> GitHubContext:
        """Create test GitHub context."""
        return GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="test-org/test-repo",
            repository_owner="test-org",
            server_url="https://github.com",
            run_id="123456789",
            sha="abc123def456",
            base_ref="main",
            head_ref="feature-branch",
            pr_number=pr_number,
        )

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    @patch("github2gerrit.core.git_show")
    @patch("github2gerrit.core.git_commit_amend")
    @patch("github2gerrit.core.run_cmd")
    def test_dependabot_pr_body_filtering_applied(
        self,
        mock_run_cmd: MagicMock,
        mock_git_commit_amend: MagicMock,
        mock_git_show: MagicMock,
        mock_get_pull: MagicMock,
        mock_get_repo: MagicMock,
        mock_build_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that Dependabot PR bodies are filtered during processing."""
        # Setup mocks
        mock_client = Mock()
        mock_repo = Mock()
        mock_pr = Mock()

        # Mock PR data - realistic Dependabot content
        mock_pr.title = "Bump package from 1.0 to 2.0"
        mock_pr.body = """
Bumps [package-name](https://github.com/owner/repo) from 1.0 to 2.0.

<details>
<summary>Release notes</summary>

## What's Changed
- Bug fix #123
- New feature added

</details>

<details>
<summary>Commits</summary>
- abc123: Fix issue
- def456: Add feature
</details>

![compatibility](https://camo.githubusercontent.com/example/compatibility_score)

Dependabot will resolve any conflicts with this PR as long as you don't alter it yourself.

### Dependabot commands and options
- `@dependabot rebase` will rebase this PR
"""

        # Mock author as dependabot
        mock_author = Mock()
        mock_author.login = "dependabot[bot]"
        mock_pr.user = mock_author

        mock_build_client.return_value = mock_client
        mock_get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # Mock current commit message
        mock_git_show.return_value = "Original commit message\n\nSigned-off-by: Test Bot <test@example.org>"

        # Mock git commands
        mock_run_cmd.return_value = Mock(stdout="Test Bot <test@example.org>")

        # Create orchestrator and run
        orch = Orchestrator(workspace=tmp_path)
        inputs = self._create_inputs(use_pr_as_commit=True)
        gh = self._create_github_context(pr_number=123)

        # Execute the method that should apply filtering
        orch._apply_pr_title_body_if_requested(inputs, gh)

        # Verify git_commit_amend was called
        assert mock_git_commit_amend.called

        # Get the commit message that was used
        call_args = mock_git_commit_amend.call_args
        commit_message = call_args[1]["message"]  # keyword argument

        # Verify filtering was applied
        assert "## Release notes" in commit_message  # Expanded details
        assert "## Commits" in commit_message  # Expanded details
        assert "Bug fix #123" in commit_message  # Content preserved
        assert "abc123: Fix issue" in commit_message  # Content preserved

        # Verify unwanted content was removed
        assert "github.com" not in commit_message  # Links removed
        assert "camo.githubusercontent.com" not in commit_message  # Image removed
        assert "Dependabot will resolve" not in commit_message  # Commands removed
        assert "@dependabot rebase" not in commit_message  # Commands removed
        assert "<details>" not in commit_message  # HTML removed
        assert "<summary>" not in commit_message  # HTML removed

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    @patch("github2gerrit.core.git_show")
    @patch("github2gerrit.core.git_commit_amend")
    @patch("github2gerrit.core.run_cmd")
    def test_regular_pr_body_not_filtered(
        self,
        mock_run_cmd: MagicMock,
        mock_git_commit_amend: MagicMock,
        mock_git_show: MagicMock,
        mock_get_pull: MagicMock,
        mock_get_repo: MagicMock,
        mock_build_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that regular (non-Dependabot) PR bodies are not filtered."""
        # Setup mocks
        mock_client = Mock()
        mock_repo = Mock()
        mock_pr = Mock()

        # Mock regular PR data
        mock_pr.title = "Fix authentication bug"
        mock_pr.body = (
            "This fixes issue #123 by updating the auth logic.\n\n"
            "See https://github.com/owner/repo/issues/123 for details."
        )

        # Mock regular user
        mock_author = Mock()
        mock_author.login = "developer123"
        mock_pr.user = mock_author

        mock_build_client.return_value = mock_client
        mock_get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # Mock current commit message
        mock_git_show.return_value = "Original commit message\n\nSigned-off-by: Test Bot <test@example.org>"

        # Mock git commands
        mock_run_cmd.return_value = Mock(stdout="Test Bot <test@example.org>")

        # Create orchestrator and run
        orch = Orchestrator(workspace=tmp_path)
        inputs = self._create_inputs(use_pr_as_commit=True)
        gh = self._create_github_context(pr_number=456)

        # Execute the method
        orch._apply_pr_title_body_if_requested(inputs, gh)

        # Verify git_commit_amend was called
        assert mock_git_commit_amend.called

        # Get the commit message that was used
        call_args = mock_git_commit_amend.call_args
        commit_message = call_args[1]["message"]

        # Verify regular content was preserved unchanged
        assert "This fixes issue #123" in commit_message
        assert "https://github.com/owner/repo/issues/123" in commit_message  # Links preserved

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    def test_use_pr_as_commit_disabled_skips_filtering(
        self,
        mock_get_pull: MagicMock,
        mock_get_repo: MagicMock,
        mock_build_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that filtering is skipped when USE_PR_AS_COMMIT is disabled."""
        # Create orchestrator
        orch = Orchestrator(workspace=tmp_path)
        inputs = self._create_inputs(use_pr_as_commit=False)  # Disabled
        gh = self._create_github_context(pr_number=789)

        # Execute the method
        orch._apply_pr_title_body_if_requested(inputs, gh)

        # Verify GitHub API was not called (since PR processing was skipped)
        mock_build_client.assert_not_called()
        mock_get_repo.assert_not_called()
        mock_get_pull.assert_not_called()

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    @patch("github2gerrit.core.git_show")
    @patch("github2gerrit.core.git_commit_amend")
    @patch("github2gerrit.core.run_cmd")
    def test_empty_pr_body_handled_gracefully(
        self,
        mock_run_cmd: MagicMock,
        mock_git_commit_amend: MagicMock,
        mock_git_show: MagicMock,
        mock_get_pull: MagicMock,
        mock_get_repo: MagicMock,
        mock_build_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that empty PR bodies are handled gracefully."""
        # Setup mocks
        mock_client = Mock()
        mock_repo = Mock()
        mock_pr = Mock()

        # Mock PR with empty body
        mock_pr.title = "Fix typo"
        mock_pr.body = ""  # Empty body

        mock_author = Mock()
        mock_author.login = "developer123"
        mock_pr.user = mock_author

        mock_build_client.return_value = mock_client
        mock_get_repo.return_value = mock_repo
        mock_get_pull.return_value = mock_pr

        # Mock current commit message
        mock_git_show.return_value = "Original commit message\n\nSigned-off-by: Test Bot <test@example.org>"

        # Mock git commands
        mock_run_cmd.return_value = Mock(stdout="Test Bot <test@example.org>")

        # Create orchestrator and run
        orch = Orchestrator(workspace=tmp_path)
        inputs = self._create_inputs(use_pr_as_commit=True)
        gh = self._create_github_context(pr_number=999)

        # Should not raise an exception
        orch._apply_pr_title_body_if_requested(inputs, gh)

        # Verify it still processed (git_commit_amend called)
        assert mock_git_commit_amend.called
