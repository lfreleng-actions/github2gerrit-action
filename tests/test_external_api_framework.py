# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import urllib.error
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from github2gerrit.external_api import ApiType
from github2gerrit.external_api import RetryPolicy
from github2gerrit.external_api import curl_download
from github2gerrit.external_api import external_api_call
from github2gerrit.external_api import get_api_metrics
from github2gerrit.external_api import log_api_metrics_summary
from github2gerrit.external_api import reset_api_metrics


def test_retry_policy_creation() -> None:
    """Test RetryPolicy dataclass creation with defaults."""
    policy = RetryPolicy()
    assert policy.max_attempts == 5
    assert policy.base_delay == 0.5
    assert policy.max_delay == 6.0
    assert policy.timeout == 10.0
    assert policy.jitter_factor == 0.5

    custom_policy = RetryPolicy(max_attempts=3, timeout=5.0)
    assert custom_policy.max_attempts == 3
    assert custom_policy.timeout == 5.0
    assert custom_policy.base_delay == 0.5  # Default


def test_api_metrics_tracking() -> None:
    """Test API metrics collection and reset functionality."""
    # Reset all metrics first
    reset_api_metrics()

    metrics = get_api_metrics(ApiType.GITHUB)
    assert metrics.total_calls == 0
    assert metrics.successful_calls == 0
    assert metrics.failed_calls == 0

    # Test successful call tracking
    @external_api_call(ApiType.GITHUB, "test_operation")
    def successful_operation() -> str:
        return "success"

    result = successful_operation()
    assert result == "success"

    metrics = get_api_metrics(ApiType.GITHUB)
    assert metrics.total_calls == 1
    assert metrics.successful_calls == 1
    assert metrics.failed_calls == 0

    # Reset specific API type
    reset_api_metrics(ApiType.GITHUB)
    metrics = get_api_metrics(ApiType.GITHUB)
    assert metrics.total_calls == 0


def test_external_api_call_with_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test external API call decorator with retry behavior."""
    # Mock sleep to avoid delays in tests
    monkeypatch.setattr("time.sleep", lambda s: None)

    attempts = {"count": 0}

    @external_api_call(ApiType.GERRIT_REST, "test_retry_operation", policy=RetryPolicy(max_attempts=3, base_delay=0.1))
    def flaky_operation() -> str:
        attempts["count"] += 1
        if attempts["count"] < 2:
            # Raise a transient error (HTTP 503)
            raise urllib.error.HTTPError(
                url="https://example.com/api",
                code=503,
                msg="Service Unavailable",
                hdrs={},  # type: ignore[arg-type]
                fp=StringIO(""),  # type: ignore[arg-type]
            )
        return "success"

    reset_api_metrics(ApiType.GERRIT_REST)
    result = flaky_operation()
    assert result == "success"
    assert attempts["count"] == 2  # Failed once, succeeded on retry

    metrics = get_api_metrics(ApiType.GERRIT_REST)
    assert metrics.total_calls == 1
    assert metrics.successful_calls == 1
    assert metrics.retry_attempts == 1  # One retry


def test_external_api_call_non_retryable_error() -> None:
    """Test external API call with non-retryable error."""

    @external_api_call(ApiType.SSH, "test_non_retryable", policy=RetryPolicy(max_attempts=3))
    def failing_operation() -> str:
        raise ValueError("Non-retryable error")

    reset_api_metrics(ApiType.SSH)

    with pytest.raises(ValueError, match="Non-retryable error"):
        failing_operation()

    metrics = get_api_metrics(ApiType.SSH)
    assert metrics.total_calls == 1
    assert metrics.successful_calls == 0
    assert metrics.failed_calls == 1
    assert metrics.retry_attempts == 0  # No retries for non-transient errors


def test_external_api_call_retry_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test external API call when retries are exhausted."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    attempts = {"count": 0}

    @external_api_call(ApiType.HTTP_DOWNLOAD, "test_exhaustion", policy=RetryPolicy(max_attempts=2))
    def always_failing_operation() -> str:
        attempts["count"] += 1
        raise ConnectionResetError("Connection reset")

    reset_api_metrics(ApiType.HTTP_DOWNLOAD)

    with pytest.raises(ConnectionResetError):
        always_failing_operation()

    assert attempts["count"] == 2  # Max attempts reached

    metrics = get_api_metrics(ApiType.HTTP_DOWNLOAD)
    assert metrics.total_calls == 1
    assert metrics.failed_calls == 1
    assert metrics.retry_attempts == 1


def test_curl_download_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test successful curl download."""
    output_file = tmp_path / "downloaded_file.txt"

    # Mock subprocess.run to simulate successful curl
    class MockCompletedProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = "200"
            self.stderr = ""

    def mock_subprocess_run(*args: Any, **kwargs: Any) -> MockCompletedProcess:
        # Find the -o flag and get the output path from curl command
        if len(args) > 0 and isinstance(args[0], list):
            cmd = args[0]
            try:
                o_index = cmd.index("-o")
                if o_index + 1 < len(cmd):
                    output_path = Path(cmd[o_index + 1])
                    output_path.write_text("downloaded content")
            except (ValueError, IndexError):
                pass
        return MockCompletedProcess()

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    reset_api_metrics(ApiType.HTTP_DOWNLOAD)
    return_code, http_status = curl_download(url="https://example.com/file.txt", output_path=str(output_file))

    assert return_code == 0
    assert http_status == "200"
    assert output_file.exists()

    metrics = get_api_metrics(ApiType.HTTP_DOWNLOAD)
    assert metrics.total_calls == 1
    assert metrics.successful_calls == 1


def test_curl_download_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test curl download failure."""
    output_file = tmp_path / "failed_download.txt"

    # Mock subprocess.run to simulate curl failure
    class MockFailedProcess:
        def __init__(self) -> None:
            self.returncode = 22  # Curl error code for HTTP error
            self.stdout = "404"
            self.stderr = "Not Found"

    def mock_subprocess_run(*args: Any, **kwargs: Any) -> MockFailedProcess:
        return MockFailedProcess()

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)
    monkeypatch.setattr("time.sleep", lambda s: None)  # Avoid delays

    reset_api_metrics(ApiType.HTTP_DOWNLOAD)

    with pytest.raises(RuntimeError, match="curl failed"):
        curl_download(
            url="https://example.com/nonexistent.txt",
            output_path=str(output_file),
            policy=RetryPolicy(max_attempts=1),  # Don't retry for this test
        )

    metrics = get_api_metrics(ApiType.HTTP_DOWNLOAD)
    assert metrics.total_calls == 1
    assert metrics.failed_calls == 1


def test_log_api_metrics_summary(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Test API metrics summary logging."""
    import logging

    # Set up some metrics
    reset_api_metrics()

    @external_api_call(ApiType.GITHUB, "test_metrics_logging")
    def test_operation() -> str:
        return "done"

    # Generate some activity
    test_operation()
    test_operation()

    # Clear previous logs and test summary
    caplog.clear()

    # Set logging level to ensure we capture INFO messages
    caplog.set_level(logging.INFO, logger="github2gerrit.external_api")

    log_api_metrics_summary()

    # Check that metrics were logged
    assert "External API Metrics Summary" in caplog.text
    assert "github" in caplog.text
    assert "Calls: 2" in caplog.text


def test_api_type_enum_values() -> None:
    """Test ApiType enum has expected values."""
    assert ApiType.GITHUB.value == "github"
    assert ApiType.GERRIT_REST.value == "gerrit_rest"
    assert ApiType.SSH.value == "ssh"
    assert ApiType.HTTP_DOWNLOAD.value == "http_download"
