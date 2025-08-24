# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Central external API call framework for github2gerrit.

This module provides a unified framework for all external API calls with:
- Consistent retry logic with exponential backoff and jitter
- Uniform logging patterns across all API types
- Metrics collection for timing and success/failure tracking
- Configurable timeout and retry behavior per API type

The framework supports different API types:
- GitHub API calls
- Gerrit REST API calls
- SSH operations (keyscan, git operations)
- HTTP downloads (curl-based fetches)

Design principles:
- Non-intrusive: wraps existing implementations without breaking changes
- Configurable: different retry/timeout policies per API type
- Observable: consistent logging and metrics collection
- Resilient: handles transient failures with appropriate backoff
"""

from __future__ import annotations

import functools
import logging
import random
import socket
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any
from typing import NoReturn
from typing import TypeVar

from .utils import log_exception_conditionally


log = logging.getLogger("github2gerrit.external_api")

# Error message constants to comply with TRY003
_MSG_RUNTIME_NO_EXCEPTION = "External API call failed without exception"
_MSG_CURL_FAILED = "curl failed"
_MSG_CURL_NO_OUTPUT = "curl completed but output file was not created"
_MSG_CURL_TIMEOUT = "curl download timed out"
_MSG_CURL_DOWNLOAD_FAILED = "curl download failed"

# Complete error message templates
_MSG_CURL_FAILED_WITH_RC = "{}: (rc={}): {}"
_MSG_CURL_TIMEOUT_WITH_TIME = "{} after {}s"
_MSG_CURL_DOWNLOAD_FAILED_WITH_EXC = "{}: {}"

_T = TypeVar("_T")


class ApiType(Enum):
    """Types of external APIs supported by the framework."""

    GITHUB = "github"
    GERRIT_REST = "gerrit_rest"
    SSH = "ssh"
    HTTP_DOWNLOAD = "http_download"


@dataclass(frozen=True)
class RetryPolicy:
    """Configuration for retry behavior of external API calls."""

    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 6.0
    timeout: float = 10.0
    jitter_factor: float = 0.5


@dataclass
class ApiMetrics:
    """Metrics collected for external API calls."""

    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    total_duration: float = 0.0
    retry_attempts: int = 0
    timeout_errors: int = 0
    transient_errors: int = 0


@dataclass
class ApiCallContext:
    """Context information for an external API call."""

    api_type: ApiType
    operation: str
    target: str  # URL, hostname, etc.
    attempt: int = 1
    start_time: float = field(default_factory=time.time)
    policy: RetryPolicy = field(default_factory=RetryPolicy)


# Global metrics storage - in production this could be replaced with
# proper metrics collection system (Prometheus, etc.)
_METRICS: dict[ApiType, ApiMetrics] = {api_type: ApiMetrics() for api_type in ApiType}


def get_api_metrics(api_type: ApiType) -> ApiMetrics:
    """Get metrics for a specific API type."""
    return _METRICS[api_type]


def reset_api_metrics(api_type: ApiType | None = None) -> None:
    """Reset metrics for a specific API type or all types."""
    if api_type is not None:
        _METRICS[api_type] = ApiMetrics()
    else:
        for api_type in ApiType:
            _METRICS[api_type] = ApiMetrics()


def _calculate_backoff_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    jitter_factor: float,
) -> float:
    """Calculate exponential backoff delay with jitter."""
    delay = min(base_delay * (2 ** max(0, attempt - 1)), max_delay)
    jitter = random.uniform(0.0, delay * jitter_factor)  # noqa: S311
    return float(delay + jitter)


def _is_transient_error(exc: BaseException, api_type: ApiType) -> bool:
    """Determine if an exception represents a transient error."""
    # Common network/timeout errors
    if isinstance(
        exc,
        socket.timeout
        | TimeoutError
        | ConnectionResetError
        | ConnectionAbortedError
        | BrokenPipeError
        | ConnectionRefusedError,
    ):
        return True

    # HTTP-specific errors
    if isinstance(exc, urllib.error.HTTPError):
        status = getattr(exc, "code", None)
        # Retry on 5xx and 429 (rate limit)
        return (500 <= status <= 599) or (status == 429) if status else False

    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(
            reason,
            socket.timeout | TimeoutError | ConnectionResetError | ConnectionAbortedError,
        ):
            return True

    # GitHub API specific errors (if PyGithub is available)
    if api_type == ApiType.GITHUB:
        # Import GitHub exception types to check isinstance
        try:
            from .github_api import GithubExceptionType
            from .github_api import RateLimitExceededExceptionType
        except ImportError:
            GithubExceptionType = type(None)  # type: ignore[misc,assignment]
            RateLimitExceededExceptionType = type(None)  # type: ignore[misc,assignment]

        # Check by class name or isinstance for mock/test exceptions
        exc_name = exc.__class__.__name__
        if exc_name in ("RateLimitExceededException", "RateLimitExceededExceptionType") or isinstance(
            exc, RateLimitExceededExceptionType
        ):
            return True
        if exc_name in ("GithubException", "GithubExceptionType") or isinstance(exc, GithubExceptionType):
            status = getattr(exc, "status", None)
            if isinstance(status, int) and 500 <= status <= 599:
                return True
            # Check for rate limit in error data
            data = getattr(exc, "data", "")
            if isinstance(data, str | bytes):
                try:
                    text = data.decode("utf-8") if isinstance(data, bytes) else data
                    if "rate limit" in text.lower():
                        return True
                except Exception:
                    # Ignore decode errors when checking for rate limit text
                    log.debug("Failed to decode GitHub API error data for rate limit check")
                    return False

    # Gerrit REST specific errors - check for wrapped HTTP errors
    if api_type == ApiType.GERRIT_REST:
        # Handle GerritRestError that wraps HTTP errors
        if "HTTP 5" in str(exc) or "HTTP 429" in str(exc):
            return True
        # Also check for original HTTP errors that caused the GerritRestError
        if hasattr(exc, "__cause__") and isinstance(exc.__cause__, urllib.error.HTTPError):
            status = getattr(exc.__cause__, "code", None)
            return (500 <= status <= 599) or (status == 429) if status else False

    # SSH/Git command errors - check stderr for common transient messages
    if api_type == ApiType.SSH:
        msg = str(exc).lower()
        transient_patterns = [
            "connection timed out",
            "connection refused",
            "temporarily unavailable",
            "network is unreachable",
            "host key verification failed",  # May be transient during discovery
            "broken pipe",
            "connection reset",
        ]
        return any(pattern in msg for pattern in transient_patterns)

    # String-based detection for other error types
    msg = str(exc).lower()
    transient_substrings = [
        "timed out",
        "temporarily unavailable",
        "temporary failure",
        "connection reset",
        "connection aborted",
        "broken pipe",
        "connection refused",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "rate limit",
    ]
    return any(substring in msg for substring in transient_substrings)


def _update_metrics(
    api_type: ApiType,
    context: ApiCallContext,
    success: bool,
    exc: BaseException | None = None,
) -> None:
    """Update metrics for an API call."""
    metrics = _METRICS[api_type]
    metrics.total_calls += 1
    duration = time.time() - context.start_time
    metrics.total_duration += duration

    if success:
        metrics.successful_calls += 1
    else:
        metrics.failed_calls += 1

    if context.attempt > 1:
        metrics.retry_attempts += context.attempt - 1

    if exc:
        if isinstance(exc, socket.timeout | TimeoutError):
            metrics.timeout_errors += 1
        elif _is_transient_error(exc, api_type):
            metrics.transient_errors += 1


def external_api_call(
    api_type: ApiType,
    operation: str,
    *,
    policy: RetryPolicy | None = None,
    target: str = "",
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """
    Decorator for external API calls with unified retry/logging/metrics.

    Args:
        api_type: Type of external API being called
        operation: Description of the operation (e.g., "get_pull_request")
        policy: Custom retry policy, uses default if not provided
        target: Target identifier (URL, hostname, etc.) for logging

    Returns:
        Decorated function with retry/logging/metrics capabilities

    Example:
        @external_api_call(ApiType.GITHUB, "get_pull_request")
        def get_pull_request(repo, number):
            return repo.get_pull(number)
    """
    if policy is None:
        # Default policies per API type
        default_policies = {
            ApiType.GITHUB: RetryPolicy(max_attempts=5, timeout=10.0),
            ApiType.GERRIT_REST: RetryPolicy(max_attempts=5, timeout=8.0),
            ApiType.SSH: RetryPolicy(max_attempts=3, timeout=15.0),
            ApiType.HTTP_DOWNLOAD: RetryPolicy(max_attempts=3, timeout=30.0),
        }
        policy = default_policies.get(api_type, RetryPolicy())

    def decorator(func: Callable[..., _T]) -> Callable[..., _T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> _T:
            context = ApiCallContext(
                api_type=api_type,
                operation=operation,
                target=target,
                policy=policy,
            )

            last_exc: BaseException | None = None

            for attempt in range(1, policy.max_attempts + 1):
                context.attempt = attempt

                try:
                    log.debug(
                        "[%s] %s attempt %d/%d: %s %s",
                        api_type.value,
                        operation,
                        attempt,
                        policy.max_attempts,
                        target,
                        f"(timeout={policy.timeout}s)" if policy.timeout else "",
                    )

                    # Call the actual function
                    result = func(*args, **kwargs)
                except BaseException as exc:
                    last_exc = exc
                    duration = time.time() - context.start_time

                    # Determine if this error should be retried
                    is_transient = _is_transient_error(exc, api_type)
                    is_final_attempt = attempt == policy.max_attempts

                    if is_transient and not is_final_attempt:
                        # Retry case
                        delay = _calculate_backoff_delay(
                            attempt,
                            policy.base_delay,
                            policy.max_delay,
                            policy.jitter_factor,
                        )
                        log.warning(
                            "[%s] %s attempt %d/%d failed (%.2fs): %s; retrying in %.2fs",
                            api_type.value,
                            operation,
                            attempt,
                            policy.max_attempts,
                            duration,
                            exc,
                            delay,
                        )
                        time.sleep(delay)
                        continue
                    # Final failure - log and re-raise
                    reason = "final attempt" if is_final_attempt else "non-retryable"
                    log_exception_conditionally(
                        log,
                        f"[{api_type.value}] {operation} failed ({reason}) "
                        f"after {attempt} attempt(s) in {duration:.2f}s: {target}",
                    )
                    _update_metrics(api_type, context, success=False, exc=exc)
                    raise
                else:
                    # Success - log and update metrics
                    duration = time.time() - context.start_time
                    log.debug(
                        "[%s] %s succeeded in %.2fs: %s",
                        api_type.value,
                        operation,
                        duration,
                        target,
                    )
                    _update_metrics(api_type, context, success=True)
                    return result

            # Should not reach here, but handle it gracefully
            if last_exc:
                _update_metrics(api_type, context, success=False, exc=last_exc)
                raise last_exc

            # Helper function for raising runtime error
            def _raise_no_exception() -> NoReturn:
                raise RuntimeError(_MSG_RUNTIME_NO_EXCEPTION + f": {operation}")

            _raise_no_exception()

        return wrapper

    return decorator


def log_api_metrics_summary() -> None:
    """Log a summary of all API metrics."""
    log.info("=== External API Metrics Summary ===")
    for api_type in ApiType:
        metrics = _METRICS[api_type]
        if metrics.total_calls == 0:
            continue

        success_rate = (metrics.successful_calls / metrics.total_calls * 100) if metrics.total_calls > 0 else 0.0
        avg_duration = metrics.total_duration / metrics.total_calls if metrics.total_calls > 0 else 0.0

        log.info(
            "[%s] Calls: %d, Success: %.1f%%, Avg Duration: %.2fs, Retries: %d, Timeouts: %d, Transient Errors: %d",
            api_type.value,
            metrics.total_calls,
            success_rate,
            avg_duration,
            metrics.retry_attempts,
            metrics.timeout_errors,
            metrics.transient_errors,
        )


def curl_download(
    url: str,
    output_path: str,
    *,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    silent: bool = True,
    policy: RetryPolicy | None = None,
) -> tuple[int, str]:
    """
    Download a file using curl with centralized retry/logging/metrics.

    Args:
        url: URL to download from
        output_path: Local path to save the file
        timeout: Request timeout in seconds
        follow_redirects: Whether to follow HTTP redirects
        silent: Whether to suppress curl progress output
        policy: Custom retry policy

    Returns:
        Tuple of (return_code, http_status_code)

    Raises:
        RuntimeError: If curl command fails after retries
    """
    import subprocess
    from pathlib import Path

    if policy is None:
        policy = RetryPolicy(max_attempts=3, timeout=timeout)

    @external_api_call(ApiType.HTTP_DOWNLOAD, "curl_download", target=url, policy=policy)
    def _do_curl() -> tuple[int, str]:
        cmd = ["curl"]

        if follow_redirects:
            cmd.append("-fL")
        else:
            cmd.append("-f")

        if silent:
            cmd.append("-sS")

        # Write HTTP status code to stdout
        cmd.extend(["-w", "%{http_code}"])

        # Set timeout
        cmd.extend(["--max-time", str(int(timeout))])

        # Output file
        cmd.extend(["-o", output_path])

        # URL (last argument)
        cmd.append(url)

        # Helper functions for raising errors to comply with TRY301
        def _raise_curl_failed(returncode: int, error_msg: str) -> None:
            raise RuntimeError(_MSG_CURL_FAILED_WITH_RC.format(_MSG_CURL_FAILED, returncode, error_msg))

        def _raise_no_output() -> None:
            raise RuntimeError(_MSG_CURL_NO_OUTPUT)

        def _raise_timeout(timeout_val: float) -> None:
            raise TimeoutError(_MSG_CURL_TIMEOUT_WITH_TIME.format(_MSG_CURL_TIMEOUT, timeout_val))

        def _raise_download_failed(exc: Exception) -> None:
            raise RuntimeError(_MSG_CURL_DOWNLOAD_FAILED_WITH_EXC.format(_MSG_CURL_DOWNLOAD_FAILED, exc)) from exc

        # Initialize variables
        result = None
        http_status = "unknown"

        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 5,  # Give subprocess a bit more time than curl
                check=False,
            )

            # Extract HTTP status code from stdout
            http_status = result.stdout.strip() if result.stdout else "unknown"

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else _MSG_CURL_FAILED
                _raise_curl_failed(result.returncode, error_msg)

            # Verify file was created
            if not Path(output_path).exists():
                _raise_no_output()

        except subprocess.TimeoutExpired:
            _raise_timeout(timeout)
        except Exception as exc:
            _raise_download_failed(exc)

        return result.returncode if result else -1, http_status

    return _do_curl()
