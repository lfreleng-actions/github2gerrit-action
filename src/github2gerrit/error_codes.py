# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Centralized error codes and exit codes for github2gerrit.

This module defines standard exit codes and error messages to provide
consistent, user-friendly error reporting across the CLI and action.
"""

from __future__ import annotations

import logging
import re
from enum import IntEnum
from typing import NoReturn

import typer

from .rich_display import safe_console_print


log = logging.getLogger("github2gerrit.error_codes")


class ExitCode(IntEnum):
    """Standard exit codes for github2gerrit operations."""

    SUCCESS = 0
    """Operation completed successfully."""

    GENERAL_ERROR = 1
    """General operational failure."""

    CONFIGURATION_ERROR = 2
    """Configuration validation failed or missing required parameters."""

    DUPLICATE_ERROR = 3
    """Duplicate change detected (when duplicates not allowed)."""

    GITHUB_API_ERROR = 4
    """GitHub API access failed due to permissions or authentication issues."""

    GERRIT_CONNECTION_ERROR = 5
    """Failed to connect to or authenticate with Gerrit server."""

    NETWORK_ERROR = 6
    """Network connectivity issues."""

    REPOSITORY_ERROR = 7
    """Git repository access or operation failed."""

    PR_STATE_ERROR = 8
    """Pull request is in invalid state for processing."""

    VALIDATION_ERROR = 9
    """Input validation failed."""

    GERRIT_CHANGE_ALREADY_FINAL = 10
    """Gerrit change is already in a final state (MERGED/ABANDONED)."""


# Error message templates
ERROR_MESSAGES = {
    ExitCode.GITHUB_API_ERROR: (
        "❌ GitHub API access failed; ensure GITHUB_TOKEN has required "
        "permissions"
    ),
    ExitCode.GERRIT_CONNECTION_ERROR: (
        "❌ Gerrit connection failed; check SSH keys and server configuration"
    ),
    ExitCode.CONFIGURATION_ERROR: (
        "❌ Configuration validation failed; check required parameters"
    ),
    ExitCode.DUPLICATE_ERROR: (
        "❌ Duplicate change detected; use --allow-duplicates to override"
    ),
    ExitCode.NETWORK_ERROR: (
        "❌ Network connectivity failed; check internet connection"
    ),
    ExitCode.REPOSITORY_ERROR: (
        "❌ Git repository access failed; check repository permissions"
    ),
    ExitCode.PR_STATE_ERROR: (
        "❌ Pull request cannot be processed in current state"
    ),
    ExitCode.VALIDATION_ERROR: (
        "❌ Input validation failed; check parameter values"
    ),
    ExitCode.GERRIT_CHANGE_ALREADY_FINAL: (
        "❌ Gerrit change is already in a final state; use --force to override"
    ),
    ExitCode.GENERAL_ERROR: "❌ Operation failed; check logs for details",
}


class GitHub2GerritError(Exception):
    """Base exception class for GitHub2Gerrit errors with exit codes."""

    def __init__(
        self,
        exit_code: ExitCode,
        message: str | None = None,
        details: str | None = None,
        original_exception: Exception | None = None,
    ):
        self.exit_code = exit_code
        self.message = message or ERROR_MESSAGES.get(
            exit_code, ERROR_MESSAGES[ExitCode.GENERAL_ERROR]
        )
        self.details = details
        self.original_exception = original_exception

        # Call parent constructor with the error message
        super().__init__(self.message)

    def display_and_exit(self) -> NoReturn:
        """Display the error message and exit with the appropriate code."""
        # Log the error with details
        # Don't show raw exception for known error types - just the message
        exception_type = (
            type(self.original_exception).__name__
            if self.original_exception
            else None
        )

        # Skip showing exception details for known orchestrator errors
        if self.original_exception and exception_type != "OrchestratorError":
            log.error(
                "Exit code %d: %s (Exception: %s)",
                self.exit_code,
                self.message,
                self.original_exception,
            )
            if self.details:
                log.error("Additional details: %s", self.details)
        else:
            log.error("Exit code %d: %s", self.exit_code, self.message)
            if self.details:
                log.error("Details: %s", self.details)

        # Display user-friendly error message
        safe_console_print(self.message, style="red", err=True)

        if self.details:
            safe_console_print(
                f"Details: {self.details}", style="dim red", err=True
            )

        raise typer.Exit(int(self.exit_code))


def exit_with_error(
    exit_code: ExitCode,
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with standardized error message and code.

    Args:
        exit_code: Standard exit code from ExitCode enum
        message: Override default error message (optional)
        details: Additional error details (optional)
        exception: Original exception for logging (optional)
    """
    error = GitHub2GerritError(exit_code, message, details, exception)
    error.display_and_exit()


def exit_for_github_api_error(
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with GitHub API error code and message."""
    default_msg = (
        "❌ GitHub API query failed; provide a GITHUB_TOKEN with the "
        "required permissions"
    )
    error = GitHub2GerritError(
        ExitCode.GITHUB_API_ERROR,
        message=message or default_msg,
        details=details,
        original_exception=exception,
    )
    error.display_and_exit()


def exit_for_gerrit_connection_error(
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with Gerrit connection error code and message."""
    error = GitHub2GerritError(
        ExitCode.GERRIT_CONNECTION_ERROR,
        message=message,
        details=details,
        original_exception=exception,
    )
    error.display_and_exit()


def exit_for_configuration_error(
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with configuration error code and message."""
    error = GitHub2GerritError(
        ExitCode.CONFIGURATION_ERROR,
        message=message,
        details=details,
        original_exception=exception,
    )
    error.display_and_exit()


def exit_for_pr_state_error(
    pr_number: int,
    pr_state: str,
    details: str | None = None,
) -> NoReturn:
    """Exit with PR state error code and message."""
    message = (
        f"❌ Pull request #{pr_number} is {pr_state} and cannot be processed"
    )
    error = GitHub2GerritError(
        ExitCode.PR_STATE_ERROR,
        message=message,
        details=details,
    )
    error.display_and_exit()


def exit_for_pr_not_found(
    pr_number: int,
    repository: str,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with PR not found error (GitHub API error)."""
    message = (
        f"❌ Pull request #{pr_number} not found in repository {repository}"
    )
    error = GitHub2GerritError(
        ExitCode.GITHUB_API_ERROR,
        message=message,
        details=details,
        original_exception=exception,
    )
    error.display_and_exit()


def exit_for_duplicate_error(
    message: str | None = None,
    details: str | None = None,
    exception: Exception | None = None,
) -> NoReturn:
    """Exit with duplicate change error code and message."""
    error = GitHub2GerritError(
        ExitCode.DUPLICATE_ERROR,
        message=message,
        details=details,
        original_exception=exception,
    )
    error.display_and_exit()


def is_github_api_permission_error(exception: Exception) -> bool:
    """Check if exception indicates GitHub API permission/authentication issues.

    Uses structured error detection with fallback to pattern matching. This
    approach is more reliable than complex regex patterns and leverages
    structured information when available.

    Args:
        exception: Exception to check

    Returns:
        True if the exception indicates GitHub API permission issues
    """
    # First, check if it's a GitHub API exception with structured data
    if _is_github_exception_with_permission_error(exception):
        return True

    # Second, check for HTTP status codes in structured way
    if _has_permission_related_http_status(exception):
        return True

    # Fallback to simple pattern matching for known GitHub API messages
    return _matches_github_permission_patterns(exception)


def _is_github_exception_with_permission_error(exception: Exception) -> bool:
    """Check if exception is a GitHub API exception with permission error."""
    # Check if it's a PyGithub exception type
    exception_type = type(exception)
    if (
        hasattr(exception_type, "__module__")
        and exception_type.__module__
        and "github" in exception_type.__module__.lower()
    ):
        # Check for status code attribute (PyGithub exceptions have this)
        if hasattr(exception, "status") and exception.status in (401, 403, 404):
            return True
        # Check for data attribute with status
        if hasattr(exception, "data") and isinstance(exception.data, dict):
            status = exception.data.get("status")
            if status in (401, 403, 404):
                return True
    return False


def _has_permission_related_http_status(exception: Exception) -> bool:
    """Check if exception contains permission-related HTTP status codes."""
    # Look for status code attributes
    for attr in ["status_code", "status", "code", "response_code"]:
        if hasattr(exception, attr):
            status = getattr(exception, attr)
            if isinstance(status, int) and status in (401, 403, 404):
                return True

    # Check for requests.HTTPError or similar
    if hasattr(exception, "response"):
        response = exception.response
        if hasattr(response, "status_code"):
            status = response.status_code
            if isinstance(status, int) and status in (401, 403, 404):
                return True

    return False


def _matches_github_permission_patterns(exception: Exception) -> bool:
    """Check if exception message matches known GitHub permission patterns."""
    error_str = str(exception).lower()

    # High-confidence GitHub API error messages
    github_api_patterns = [
        "resource not accessible by integration",
        "bad credentials",
        "requires authentication",
    ]

    # Check high-confidence patterns first
    for pattern in github_api_patterns:
        if pattern in error_str:
            return True

    # Check for HTTP status indicators in context
    status_indicators = ["401", "403", "forbidden", "unauthorized"]
    context_indicators = ["api", "http", "github", "request", "response"]
    if any(indicator in error_str for indicator in status_indicators) and any(
        context in error_str for context in context_indicators
    ):
        return True

    # Handle 404/not found cases - be more permissive for backward compatibility
    # but still avoid obvious false positives
    if "404" in error_str or "not found" in error_str:
        # Exclude obvious file/script contexts
        excludes = ["file", "script", ".txt", ".html", ".js", ".css"]
        if any(exclude in error_str for exclude in excludes):
            return False
        # Allow GitHub contexts or simple cases like "404 not found"
        github_contexts = ["repository", "pull request", "github", "api"]
        simple_cases = ["404 not found", "not found"]
        if (
            any(context in error_str for context in github_contexts)
            or error_str.strip() in simple_cases
        ):
            return True

    return False


def is_gerrit_connection_error(exception: Exception) -> bool:
    """Check if exception indicates Gerrit connection/authentication issues.

    Uses regex patterns to match specific connection error patterns while
    avoiding false positives from generic terms.

    Args:
        exception: Exception to check

    Returns:
        True if the exception indicates Gerrit connection issues
    """
    error_str = str(exception).lower()

    # Define regex patterns for Gerrit connection errors
    gerrit_patterns = [
        r"connection refused",
        r"connection timed out",
        r"host key verification failed",
        r"permission denied \(publickey\)",
        r"authentication failed",
        r"connection reset",
        # SSH-related errors in context of git operations
        r"ssh.*(?:error|failed|refused)",
        # Gerrit-specific errors
        r"gerrit.*(?:error|failed|connection)",
    ]

    return any(re.search(pattern, error_str) for pattern in gerrit_patterns)


def is_network_error(exception: Exception) -> bool:
    """Check if exception indicates network connectivity issues.

    Uses regex patterns to identify network-specific errors and distinguish them
    from other types of connection issues.

    Args:
        exception: Exception to check

    Returns:
        True if the exception indicates network issues
    """
    error_str = str(exception).lower()

    # Define regex patterns for network errors
    network_patterns = [
        r"network is unreachable",
        r"name resolution failed",
        r"connection timeout",
        r"socket timeout",
        r"unable to connect",
        r"network error",
        # DNS-related errors
        r"dns.*(?:error|failed|timeout)",
        r"(?:name|host).*resolution.*failed",
    ]

    return any(re.search(pattern, error_str) for pattern in network_patterns)


def map_orchestrator_error_to_exit_code(
    error_msg: str, original_exception: Exception | None = None
) -> ExitCode:
    """Map OrchestratorError messages to appropriate exit codes.

    Uses regex patterns to categorize errors more precisely and avoid false
    positives from generic terms that might appear in multiple contexts.

    Args:
        error_msg: The error message from OrchestratorError
        original_exception: The original exception that caused the orchestrator
            error

    Returns:
        Appropriate ExitCode for the error type
    """
    error_lower = error_msg.lower()

    # Configuration-related error patterns
    config_patterns = [
        r"\bmissing\b",
        r"bad gerrit_server_port",
        r"invalid \.gitreview",
        r"failed to read \.gitreview",
        r"bad repository context",
        r"missing gerrit server",
        r"missing gerrit project",
        r"missing pr context",
        r"cannot configure git user identity",
    ]

    if any(re.search(pattern, error_lower) for pattern in config_patterns):
        return ExitCode.CONFIGURATION_ERROR

    # Network/connection errors
    if original_exception and is_network_error(original_exception):
        return ExitCode.NETWORK_ERROR

    # Gerrit connection error patterns
    gerrit_patterns = [
        r"failed to push",
        r"git-review",
        r"commit-msg hook",
        # SSH errors in context of git/gerrit operations
        r"ssh.*(?:gerrit|git)",
        r"gerrit.*(?:connection|error|failed)",
    ]

    if any(re.search(pattern, error_lower) for pattern in gerrit_patterns):
        return ExitCode.GERRIT_CONNECTION_ERROR

    # Repository/Git error patterns (more specific than just "git")
    repo_patterns = [
        r"git.*(?:repository|merge|commit|branch|clone|fetch)",
        r"repository.*(?:access|clone|fetch|error)",
        r"merge.*(?:conflict|failed|error)",
        r"commit.*(?:failed|error)",
        r"branch.*(?:not found|error|failed)",
    ]

    if any(re.search(pattern, error_lower) for pattern in repo_patterns):
        return ExitCode.REPOSITORY_ERROR

    # Default to general error
    return ExitCode.GENERAL_ERROR


def convert_orchestrator_error(
    orchestrator_error: Exception,
) -> GitHub2GerritError:
    """Convert OrchestratorError to GitHub2GerritError with appropriate exit
    code.

    Args:
        orchestrator_error: The OrchestratorError to convert

    Returns:
        GitHub2GerritError with mapped exit code and message
    """
    error_msg = str(orchestrator_error)
    original_exception = getattr(orchestrator_error, "__cause__", None)

    exit_code = map_orchestrator_error_to_exit_code(
        error_msg, original_exception
    )

    # Create user-friendly message based on exit code
    if exit_code == ExitCode.CONFIGURATION_ERROR:
        user_message = (
            "❌ Configuration validation failed; check required parameters"
        )
        details = f"Configuration issue: {error_msg}"
    elif exit_code == ExitCode.GERRIT_CONNECTION_ERROR:
        user_message = (
            "❌ Gerrit connection failed; check SSH keys and server "
            "configuration"
        )
        details = f"Gerrit connection issue: {error_msg}"
    elif exit_code == ExitCode.NETWORK_ERROR:
        user_message = (
            "❌ Network connectivity failed; check internet connection"
        )
        details = f"Network issue: {error_msg}"
    elif exit_code == ExitCode.REPOSITORY_ERROR:
        user_message = (
            "❌ Git repository access failed; check repository permissions"
        )
        details = f"Repository issue: {error_msg}"
    else:
        user_message = "❌ Operation failed; check logs for details"
        details = error_msg

    return GitHub2GerritError(
        exit_code=exit_code,
        message=user_message,
        details=details,
        original_exception=original_exception or orchestrator_error,
    )


def convert_duplicate_error(duplicate_error: Exception) -> GitHub2GerritError:
    """Convert DuplicateChangeError to GitHub2GerritError.

    Args:
        duplicate_error: The DuplicateChangeError to convert

    Returns:
        GitHub2GerritError with DUPLICATE_ERROR exit code
    """
    return GitHub2GerritError(
        exit_code=ExitCode.DUPLICATE_ERROR,
        message=(
            "❌ Duplicate change detected; use --allow-duplicates to override"
        ),
        details=str(duplicate_error),
        original_exception=duplicate_error,
    )


def convert_configuration_error(config_error: Exception) -> GitHub2GerritError:
    """Convert ConfigurationError to GitHub2GerritError.

    Args:
        config_error: The ConfigurationError to convert

    Returns:
        GitHub2GerritError with CONFIGURATION_ERROR exit code
    """
    return GitHub2GerritError(
        exit_code=ExitCode.CONFIGURATION_ERROR,
        message="❌ Configuration validation failed; check required parameters",
        details=str(config_error),
        original_exception=config_error,
    )
