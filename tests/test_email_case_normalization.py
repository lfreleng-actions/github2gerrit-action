# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from github2gerrit.core import Orchestrator
from github2gerrit.gitutils import CommandError


class TestEmailCaseNormalization:
    """Test email case normalization functionality for Gerrit account errors."""

    def setup_method(self) -> None:
        """Set up test environment."""
        self.workspace = Path(tempfile.mkdtemp())
        self.orch = Orchestrator(workspace=self.workspace)

    def _create_command_error(self, stdout: str, stderr: str) -> CommandError:
        """Create a CommandError for testing."""
        return CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout=stdout,
            stderr=stderr,
        )

    def test_extract_account_not_found_emails_single(self) -> None:
        """Test extracting single email from 'Account not found' error."""
        error_output = """
To ssh://testuser@gerrit.example.org:29418/test-project
 ! [remote rejected] HEAD ->
 refs/for/master%topic=GH-test-project-37,r=Test.User@Example.COM,r=master \
(Account 'Test.User@Example.COM' not found
error: failed to push some refs to
'ssh://testuser@gerrit.example.org:29418/test-project'
"""

        exc = self._create_command_error("", error_output)
        emails = self.orch._extract_account_not_found_emails(exc)

        assert emails == ["Test.User@Example.COM"]

    def test_extract_account_not_found_emails_multiple(self) -> None:
        """Test extracting multiple emails from 'Account not found' error."""
        error_output = """
 ! [remote rejected] HEAD -> refs/for/master (Account 'John.Doe@Example.COM' not
 found, \
Account 'jane.SMITH@company.org' not found)
"""

        exc = self._create_command_error("", error_output)
        emails = self.orch._extract_account_not_found_emails(exc)

        expected = ["John.Doe@Example.COM", "jane.SMITH@company.org"]
        assert emails == expected

    def test_extract_account_not_found_emails_none(self) -> None:
        """Test no extraction when no 'Account not found' errors present."""
        error_output = "Some other error occurred"

        exc = self._create_command_error("", error_output)
        emails = self.orch._extract_account_not_found_emails(exc)

        assert emails == []

    def test_extract_account_not_found_emails_malformed(self) -> None:
        """Test that malformed emails are filtered out."""
        error_output = "Account 'not-an-email' not found"

        exc = self._create_command_error("", error_output)
        emails = self.orch._extract_account_not_found_emails(exc)

        assert emails == []  # Should filter out malformed email

    def test_normalize_reviewer_emails_basic(self) -> None:
        """Test basic email case normalization."""
        reviewers = "user@example.com,Test.User@Example.COM,another@test.org"
        failed_emails = ["Test.User@Example.COM"]

        result = self.orch._normalize_reviewer_emails(reviewers, failed_emails)
        expected = "user@example.com,test.user@example.com,another@test.org"

        assert result == expected

    def test_normalize_reviewer_emails_no_failed(self) -> None:
        """Test no normalization when no failed emails."""
        reviewers = "User@Example.COM,Another@Test.ORG"
        failed_emails: list[str] = []

        result = self.orch._normalize_reviewer_emails(reviewers, failed_emails)

        assert result == reviewers  # Should be unchanged

    def test_normalize_reviewer_emails_empty(self) -> None:
        """Test handling empty reviewers string."""
        result = self.orch._normalize_reviewer_emails("", ["test@example.com"])
        assert result == ""

    def test_normalize_reviewer_emails_multiple_failed(self) -> None:
        """Test normalizing multiple failed emails."""
        reviewers = (
            "Good@example.com,BAD1@test.COM,normal@site.org,BAD2@Company.NET"
        )
        failed_emails = ["BAD1@test.COM", "BAD2@Company.NET"]

        result = self.orch._normalize_reviewer_emails(reviewers, failed_emails)
        expected = (
            "Good@example.com,bad1@test.com,normal@site.org,bad2@company.net"
        )

        assert result == expected

    def test_update_config_with_normalized_emails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test updating configuration file with normalized emails."""
        config_path = tmp_path / "test_config.txt"

        # Create test configuration file
        config_content = """[default]
GERRIT_SERVER = "gerrit.example.org"
PRESERVE_GITHUB_PRS = "true"

[testorg]
ISSUE_ID = "TEST-123"
REVIEWERS_EMAIL = "user@example.org,Test.User@Example.COM"
GERRIT_SSH_USER_G2G_EMAIL = "Test.User@Example.COM"
"""

        config_path.write_text(config_content, encoding="utf-8")

        # Set up environment
        monkeypatch.setenv("ORGANIZATION", "testorg")
        monkeypatch.setenv("G2G_CONFIG_PATH", str(config_path))

        # Test normalization
        original_emails = ["Test.User@Example.COM"]
        self.orch._update_config_with_normalized_emails(original_emails)

        # Read updated config
        updated_content = config_path.read_text(encoding="utf-8")

        # Check that email was normalized
        assert "test.user@example.com" in updated_content
        assert "Test.User@Example.COM" not in updated_content

        # Verify both occurrences were updated
        occurrences = updated_content.count("test.user@example.com")
        assert occurrences == 2

    def test_update_config_quoted_emails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test updating configuration file with various quote styles."""
        config_path = tmp_path / "test_config.txt"

        config_content = """[default]
GERRIT_SSH_USER_G2G_EMAIL = "Test.User@Example.COM"
REVIEWERS_EMAIL = 'Another.User@Site.ORG'
OTHER_EMAIL = Plain.Email@Domain.NET
"""

        config_path.write_text(config_content, encoding="utf-8")

        monkeypatch.setenv("ORGANIZATION", "default")
        monkeypatch.setenv("G2G_CONFIG_PATH", str(config_path))

        original_emails = [
            "Test.User@Example.COM",
            "Another.User@Site.ORG",
            "Plain.Email@Domain.NET",
        ]
        self.orch._update_config_with_normalized_emails(original_emails)

        updated_content = config_path.read_text(encoding="utf-8")

        # Check all styles were normalized
        assert '"test.user@example.com"' in updated_content
        assert "'another.user@site.org'" in updated_content
        assert "plain.email@domain.net" in updated_content

    def test_update_config_missing_organization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test graceful handling when organization is not found."""
        # Remove organization environment variables
        monkeypatch.delenv("ORGANIZATION", raising=False)
        monkeypatch.delenv("GITHUB_REPOSITORY_OWNER", raising=False)

        # Should not crash
        self.orch._update_config_with_normalized_emails(["test@example.com"])

    def test_update_config_missing_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test graceful handling when config file doesn't exist."""
        monkeypatch.setenv("ORGANIZATION", "test-org")
        monkeypatch.setenv("G2G_CONFIG_PATH", "/non/existent/path.txt")

        # Should not crash
        self.orch._update_config_with_normalized_emails(["test@example.com"])

    def test_update_config_no_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test when no email addresses are found in config to normalize."""
        config_path = tmp_path / "test_config.txt"

        config_content = """[default]
GERRIT_SERVER = "gerrit.example.org"
SOME_OTHER_SETTING = "value"
"""

        config_path.write_text(config_content, encoding="utf-8")
        original_content = config_content

        monkeypatch.setenv("ORGANIZATION", "default")
        monkeypatch.setenv("G2G_CONFIG_PATH", str(config_path))

        self.orch._update_config_with_normalized_emails(
            ["notfound@example.com"]
        )

        # Content should be unchanged
        updated_content = config_path.read_text(encoding="utf-8")
        assert updated_content == original_content

    def test_integration_email_patterns(self) -> None:
        """Test various email patterns in error messages."""
        test_cases = [
            # Standard case
            ("Account 'User@Example.COM' not found", ["User@Example.COM"]),
            # Multiple accounts
            (
                "Account 'a@b.com' not found, Account 'c@d.org' not found",
                ["a@b.com", "c@d.org"],
            ),
            # Mixed with other text
            (
                "Some error text Account 'test@site.net' not found more text",
                ["test@site.net"],
            ),
            # Case variations
            ("ACCOUNT 'Test@Domain.ORG' NOT FOUND", ["Test@Domain.ORG"]),
            # No matches
            ("Some other error message", []),
        ]

        for error_msg, expected in test_cases:
            exc = self._create_command_error("", error_msg)
            result = self.orch._extract_account_not_found_emails(exc)
            assert result == expected, f"Failed for: {error_msg}"
