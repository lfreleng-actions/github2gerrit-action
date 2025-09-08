# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from github2gerrit import github_api as ghapi
from github2gerrit.external_api import ApiType
from github2gerrit.external_api import RetryPolicy
from github2gerrit.external_api import external_api_call


def _placeholder_non_test() -> None:
    # Placeholder to avoid duplicate test name; no-op
    pass


def _wrap_retry(
    attempts: int,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    # Use the external API framework instead of the old retry decorator
    policy = RetryPolicy(max_attempts=attempts)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return external_api_call(ApiType.GITHUB, "test_function", policy=policy)(func)

    return decorator


def test_retry_on_rate_limit_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(float(s)))

    attempts = {"n": 0}

    @_wrap_retry(attempts=3)
    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ghapi.RateLimitExceededExceptionType()
        return "ok"

    assert flaky() == "ok"
    # Should have retried at least once
    assert len(sleeps) >= 1
    # Exactly two calls: one fail + one success
    assert attempts["n"] == 2


def test_retry_on_5xx_github_exception_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(float(s)))

    class Dummy5xx(ghapi.GithubExceptionType):
        def __init__(self) -> None:
            super().__init__("server error")
            self.status = 503
            self.data = ""

    calls = {"n": 0}

    @_wrap_retry(attempts=3)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise Dummy5xx()
        return "ok"

    assert flaky() == "ok"
    assert len(sleeps) >= 1
    assert calls["n"] == 2


def test_retry_on_403_with_rate_limit_text_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(float(s)))

    class Dummy403(ghapi.GithubExceptionType):
        def __init__(self, data: Any) -> None:
            super().__init__("forbidden")
            self.status = 403
            self.data = data

    calls = {"n": 0}

    @_wrap_retry(attempts=3)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            # Both str and bytes are handled by the retry logic
            raise Dummy403("Rate limit exceeded")
        return "ok"

    assert flaky() == "ok"
    assert len(sleeps) >= 1
    assert calls["n"] == 2


def test_non_retryable_exception_bubbles_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure we do not sleep on non-retryable errors
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(float(s)))

    class Dummy400(ghapi.GithubExceptionType):
        def __init__(self) -> None:
            super().__init__("bad request")
            self.status = 400
            self.data = ""

    @_wrap_retry(attempts=3)
    def bad() -> str:
        raise Dummy400()

    with pytest.raises(Dummy400):
        bad()
    assert slept == []


def test_retry_exhaustion_raises_last_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(float(s)))

    @_wrap_retry(attempts=2)
    def always_rate_limited() -> str:
        raise ghapi.RateLimitExceededExceptionType()

    with pytest.raises(ghapi.RateLimitExceededExceptionType):
        always_rate_limited()
    # With attempts=2, we sleep once
    assert len(sleeps) == 1


def test_build_client_raises_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure no token present
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError):
        ghapi.build_client()


def test_build_client_raises_when_pygithub_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provide a token to pass the token gate
    monkeypatch.setenv("GITHUB_TOKEN", "token-xyz")

    class NoGithub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("PyGithub is required to access the GitHub API")

    # Force the alias used by build_client to raise on construction
    monkeypatch.setattr(ghapi, "Github", NoGithub, raising=True)

    with pytest.raises(RuntimeError) as ei:
        ghapi.build_client()
    assert "PyGithub is required to access the GitHub API" in str(ei.value)


def test_transient_error_detection_with_external_api_framework() -> None:
    """Test that external API framework detects transient errors correctly."""
    from github2gerrit.external_api import _is_transient_error

    # Test with rate limit exception
    rate_limit_exc = ghapi.RateLimitExceededExceptionType()
    assert _is_transient_error(rate_limit_exc, ApiType.GITHUB) is True

    # Test with GitHub exception having 5xx status
    github_exc = ghapi.GithubExceptionType()
    github_exc.status = 503  # type: ignore[attr-defined]
    assert _is_transient_error(github_exc, ApiType.GITHUB) is True

    # Test with GitHub exception having 403 status with rate limit in data
    github_exc_403 = ghapi.GithubExceptionType()
    github_exc_403.status = 403  # type: ignore[attr-defined]
    github_exc_403.data = "API rate limit exceeded"  # type: ignore[attr-defined]
    assert _is_transient_error(github_exc_403, ApiType.GITHUB) is True

    # Test with non-retryable exception
    other_exc = ValueError("some other error")
    assert _is_transient_error(other_exc, ApiType.GITHUB) is False

    # Test with GitHub exception having non-5xx status
    github_exc_400 = ghapi.GithubExceptionType()
    github_exc_400.status = 400  # type: ignore[attr-defined]
    assert _is_transient_error(github_exc_400, ApiType.GITHUB) is False
