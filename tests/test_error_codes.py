# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for the error_codes module."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import typer

from github2gerrit.error_codes import ERROR_MESSAGES
from github2gerrit.error_codes import ExitCode
from github2gerrit.error_codes import exit_for_configuration_error
from github2gerrit.error_codes import exit_for_duplicate_error
from github2gerrit.error_codes import exit_for_gerrit_connection_error
from github2gerrit.error_codes import exit_for_github_api_error
from github2gerrit.error_codes import exit_for_pr_not_found
from github2gerrit.error_codes import exit_for_pr_state_error
from github2gerrit.error_codes import exit_with_error
from github2gerrit.error_codes import is_gerrit_connection_error
from github2gerrit.error_codes import is_github_api_permission_error
from github2gerrit.error_codes import is_network_error


class TestExitCode:
    """Test the ExitCode enum."""

    def test_exit_codes_are_integers(self):
        """Test that exit codes are valid integers."""
        assert ExitCode.SUCCESS == 0
        assert ExitCode.GENERAL_ERROR == 1
        assert ExitCode.CONFIGURATION_ERROR == 2
        assert ExitCode.DUPLICATE_ERROR == 3
        assert ExitCode.GITHUB_API_ERROR == 4
        assert ExitCode.GERRIT_CONNECTION_ERROR == 5
        assert ExitCode.NETWORK_ERROR == 6
        assert ExitCode.REPOSITORY_ERROR == 7
        assert ExitCode.PR_STATE_ERROR == 8
        assert ExitCode.VALIDATION_ERROR == 9

    def test_all_exit_codes_have_messages(self):
        """Test that all exit codes have corresponding error messages."""
        for exit_code in ExitCode:
            if exit_code != ExitCode.SUCCESS:
                assert exit_code in ERROR_MESSAGES
                assert ERROR_MESSAGES[exit_code].startswith("❌")


class TestExitWithError:
    """Test the exit_with_error function."""

    def test_exit_with_error_uses_default_message(self):
        """Test exit_with_error uses default message for exit code."""
        with pytest.raises(typer.Exit) as exc_info:
            exit_with_error(ExitCode.GITHUB_API_ERROR)

        assert exc_info.value.exit_code == 4

    def test_exit_with_error_uses_custom_message(self):
        """Test exit_with_error uses custom message when provided."""
        custom_msg = "Custom error message"

        with pytest.raises(typer.Exit) as exc_info:
            exit_with_error(ExitCode.CONFIGURATION_ERROR, message=custom_msg)

        assert exc_info.value.exit_code == 2

    @patch("github2gerrit.error_codes.log")
    def test_exit_with_error_logs_exception(self, mock_log):
        """Test exit_with_error logs exception details."""
        test_exception = ValueError("Test error")

        with pytest.raises(typer.Exit):
            exit_with_error(
                ExitCode.GENERAL_ERROR,
                message="Test message",
                details="Test details",
                exception=test_exception,
            )

        mock_log.error.assert_called()


class TestSpecificExitFunctions:
    """Test specific exit functions for different error types."""

    def test_exit_for_github_api_error(self):
        """Test GitHub API error exit function."""
        with pytest.raises(typer.Exit) as exc_info:
            exit_for_github_api_error()

        assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR

    def test_exit_for_gerrit_connection_error(self):
        """Test Gerrit connection error exit function."""
        with pytest.raises(typer.Exit) as exc_info:
            exit_for_gerrit_connection_error()

        assert exc_info.value.exit_code == ExitCode.GERRIT_CONNECTION_ERROR

    def test_exit_for_configuration_error(self):
        """Test configuration error exit function."""
        with pytest.raises(typer.Exit) as exc_info:
            exit_for_configuration_error()

        assert exc_info.value.exit_code == ExitCode.CONFIGURATION_ERROR

    def test_exit_for_pr_state_error(self):
        """Test PR state error exit function."""
        with pytest.raises(typer.Exit) as exc_info:
            exit_for_pr_state_error(123, "closed")

        assert exc_info.value.exit_code == ExitCode.PR_STATE_ERROR

    def test_exit_for_pr_not_found(self):
        """Test PR not found error exit function."""
        with pytest.raises(typer.Exit) as exc_info:
            exit_for_pr_not_found(123, "owner/repo")

        assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR

    def test_exit_for_duplicate_error(self):
        """Test duplicate error exit function."""
        with pytest.raises(typer.Exit) as exc_info:
            exit_for_duplicate_error()

        assert exc_info.value.exit_code == ExitCode.DUPLICATE_ERROR


class TestErrorDetectionFunctions:
    """Test error detection helper functions."""

    def test_is_github_api_permission_error_detects_403(self):
        """Test detection of 403 Forbidden errors."""
        exc = Exception("403 Forbidden: Resource not accessible by integration")
        assert is_github_api_permission_error(exc) is True

    def test_is_github_api_permission_error_detects_401(self):
        """Test detection of 401 Unauthorized errors."""
        exc = Exception("401 Unauthorized: Bad credentials")
        assert is_github_api_permission_error(exc) is True

    def test_is_github_api_permission_error_detects_404(self):
        """Test detection of 404 Not Found errors."""
        exc = Exception("404 Not Found")
        assert is_github_api_permission_error(exc) is True

    def test_is_github_api_permission_error_ignores_other_errors(self):
        """Test that other errors are not detected as permission errors."""
        exc = Exception("500 Internal Server Error")
        assert is_github_api_permission_error(exc) is False

    def test_is_gerrit_connection_error_detects_ssh_errors(self):
        """Test detection of SSH connection errors."""
        exc = Exception(
            "ssh: connect to host gerrit.example.com port 29418: Connection refused"
        )
        assert is_gerrit_connection_error(exc) is True

    def test_is_gerrit_connection_error_detects_auth_errors(self):
        """Test detection of SSH authentication errors."""
        exc = Exception("Permission denied (publickey)")
        assert is_gerrit_connection_error(exc) is True

    def test_is_gerrit_connection_error_ignores_other_errors(self):
        """Test that other errors are not detected as Gerrit connection errors."""
        exc = Exception("Invalid configuration parameter")
        assert is_gerrit_connection_error(exc) is False

    def test_is_network_error_detects_dns_errors(self):
        """Test detection of DNS resolution errors."""
        exc = Exception(
            "Name resolution failed: No address associated with hostname"
        )
        assert is_network_error(exc) is True

    def test_is_network_error_detects_timeout_errors(self):
        """Test detection of connection timeout errors."""
        exc = Exception("Connection timeout occurred")
        assert is_network_error(exc) is True

    def test_is_network_error_ignores_other_errors(self):
        """Test that other errors are not detected as network errors."""
        exc = Exception("Invalid parameter value")
        assert is_network_error(exc) is False


class TestErrorMessageContent:
    """Test that error messages contain helpful information."""

    def test_github_api_error_message_mentions_token(self):
        """Test GitHub API error message mentions GITHUB_TOKEN."""
        msg = ERROR_MESSAGES[ExitCode.GITHUB_API_ERROR]
        assert "GITHUB_TOKEN" in msg
        assert "permissions" in msg

    def test_gerrit_error_message_mentions_ssh(self):
        """Test Gerrit error message mentions SSH and configuration."""
        msg = ERROR_MESSAGES[ExitCode.GERRIT_CONNECTION_ERROR]
        assert "SSH" in msg or "ssh" in msg
        assert "configuration" in msg

    def test_configuration_error_message_mentions_parameters(self):
        """Test configuration error message mentions parameters."""
        msg = ERROR_MESSAGES[ExitCode.CONFIGURATION_ERROR]
        assert "parameters" in msg or "configuration" in msg

    def test_duplicate_error_message_mentions_duplicates(self):
        """Test duplicate error message mentions duplicates."""
        msg = ERROR_MESSAGES[ExitCode.DUPLICATE_ERROR]
        assert "duplicate" in msg.lower()
        assert "allow-duplicates" in msg.lower()

    def test_all_error_messages_start_with_emoji(self):
        """Test that all error messages start with ❌ emoji."""
        for exit_code, message in ERROR_MESSAGES.items():
            assert message.startswith("❌"), (
                f"Message for {exit_code} should start with ❌"
            )


class TestRealWorldErrorScenarios:
    """Test error handling for realistic error scenarios."""

    def test_github_api_token_missing_scenario(self):
        """Test scenario where GITHUB_TOKEN is missing."""
        exc = Exception("missing GITHUB_TOKEN")
        assert (
            is_github_api_permission_error(exc) is False
        )  # This specific case handled in github_api.py

    def test_github_api_cross_repo_permission_scenario(self):
        """Test scenario for cross-repository permission issues."""
        exc = Exception("Resource not accessible by integration")
        assert is_github_api_permission_error(exc) is True

    def test_gerrit_ssh_key_not_added_scenario(self):
        """Test scenario where SSH key not added to Gerrit."""
        exc = Exception("Permission denied (publickey)")
        assert is_gerrit_connection_error(exc) is True

    def test_network_connectivity_scenario(self):
        """Test scenario for network connectivity issues."""
        exc = Exception("Network is unreachable")
        assert is_network_error(exc) is True

    @patch("github2gerrit.error_codes.safe_console_print")
    def test_error_display_formatting(self, mock_console_print):
        """Test that errors are displayed with proper formatting."""
        with pytest.raises(typer.Exit):
            exit_with_error(
                ExitCode.GITHUB_API_ERROR,
                details="Additional context information",
            )

        # Should call safe_console_print for the main error message
        assert mock_console_print.call_count >= 1

        # First call should be the main error message with red style
        first_call = mock_console_print.call_args_list[0]
        assert first_call[1]["style"] == "red"
        assert first_call[1]["err"] is True
