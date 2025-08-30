# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from collections.abc import Callable

import pytest

from github2gerrit import gerrit_urls as urls_mod
from github2gerrit.gerrit_urls import GerritUrlBuilder
from github2gerrit.gerrit_urls import create_gerrit_url_builder


class _FakeResp:
    def __init__(self, code: int, headers: dict[str, str] | None = None) -> None:
        self._code = code
        self.status = code
        self.headers = headers or {}

    def getcode(self) -> int:
        return self._code


class _FakeOpener:
    def __init__(self, decide: Callable[[str], _FakeResp | BaseException]) -> None:
        self._decide = decide
        self.addheaders: list[tuple[str, str]] = []

    def open(self, url: str, timeout: float | None = None) -> _FakeResp:
        result = self._decide(url)
        if isinstance(result, BaseException):
            raise result
        return result


def _clear_builder_cache() -> None:
    # Ensure base-path discovery cache does not bleed across tests
    urls_mod._BASE_PATH_CACHE.clear()  # pyright: ignore[reportPrivateUsage]


def test_builder_env_base_path_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GERRIT_HTTP_BASE_PATH", "r")
    b = GerritUrlBuilder("gerrit.example.org")
    assert b.base_path == "r"
    assert b.has_base_path is True
    assert b.web_url("c/project/+/1").startswith("https://gerrit.example.org/r/")
    assert b.hook_url("commit-msg") == "https://gerrit.example.org/r/tools/hooks/commit-msg"


def test_api_and_web_url_joining_with_and_without_leading_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GERRIT_HTTP_BASE_PATH", "")
    b = GerritUrlBuilder("gerrit.example.org")

    # api_url should normalize endpoint whether or not it starts with '/'
    assert b.api_url("changes/") == "https://gerrit.example.org/changes/"
    assert b.api_url("/changes/") == "https://gerrit.example.org/changes/"

    # web_url should strip leading slashes to avoid double slashes
    assert b.web_url("c/project/+/42") == "https://gerrit.example.org/c/project/+/42"
    assert b.web_url("/c/project/+/42") == "https://gerrit.example.org/c/project/+/42"


def test_get_web_base_path_and_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    # With explicit base path
    monkeypatch.setenv("GERRIT_HTTP_BASE_PATH", "r")
    b = GerritUrlBuilder("gerrit.example.org")
    assert b.get_web_base_path() == "/r/"
    assert b.get_api_url_candidates("changes/") == ["https://gerrit.example.org/r/changes/"]
    assert b.get_hook_url_candidates("commit-msg") == ["https://gerrit.example.org/r/tools/hooks/commit-msg"]

    # Override forces no base path
    assert b.get_web_base_path("") == "/"
    assert b.api_url("changes/", base_path_override="") == "https://gerrit.example.org/changes/"

    # Without base path
    monkeypatch.setenv("GERRIT_HTTP_BASE_PATH", "")
    b2 = GerritUrlBuilder("gerrit.example.org")
    assert b2.get_web_base_path() == "/"
    assert b2.get_api_url_candidates("accounts/self") == ["https://gerrit.example.org/accounts/self"]
    assert b2.get_hook_url_candidates("commit-msg") == ["https://gerrit.example.org/tools/hooks/commit-msg"]


def test_hook_and_change_url_with_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GERRIT_HTTP_BASE_PATH", "r")
    b = GerritUrlBuilder("gerrit.example.org")
    # Override with custom base path
    assert (
        b.hook_url("commit-msg", base_path_override="custom")
        == "https://gerrit.example.org/custom/tools/hooks/commit-msg"
    )
    # Override to empty removes base path
    assert b.hook_url("commit-msg", base_path_override="") == "https://gerrit.example.org/tools/hooks/commit-msg"

    # change_url keeps project as-is (no encoding) and applies override
    assert (
        b.change_url("releng/builder", 123, base_path_override="")
        == "https://gerrit.example.org/c/releng/builder/+/123"
    )
    assert (
        b.change_url("releng/builder", 123, base_path_override="r")
        == "https://gerrit.example.org/r/c/releng/builder/+/123"
    )


def test_repr_contains_host_and_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GERRIT_HTTP_BASE_PATH", "r")
    b = GerritUrlBuilder("gerrit.example.org")
    rep = repr(b)
    assert "gerrit.example.org" in rep
    assert "base_path='r'" in rep


def test_discover_base_path_returns_empty_on_200_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear env and cache to exercise discovery
    monkeypatch.delenv("GERRIT_HTTP_BASE_PATH", raising=False)
    _clear_builder_cache()

    # For '/dashboard/self', return 200 OK (no base path)
    def decide(url: str) -> _FakeResp | BaseException:
        if url.endswith("/dashboard/self"):
            return _FakeResp(200, {})
        # Not hit due to early return but safe default
        return _FakeResp(404, {})

    fake_opener = _FakeOpener(decide)
    monkeypatch.setattr(
        "github2gerrit.gerrit_urls.urllib.request.build_opener", lambda *_a, **_k: fake_opener, raising=True
    )

    b = create_gerrit_url_builder("gerrit.example.org")
    # No base path discovered
    assert b.base_path == ""
    assert b.web_url("c/project/+/1") == "https://gerrit.example.org/c/project/+/1"


def test_discover_base_path_3xx_location_relative_and_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GERRIT_HTTP_BASE_PATH", raising=False)
    _clear_builder_cache()

    # Simulate 302 to a relative '/r/dashboard/self'
    # and ensure discovery extracts 'r' as base path.
    def decide(url: str) -> _FakeResp | BaseException:
        if url.endswith("/dashboard/self"):
            return _FakeResp(302, {"Location": "/r/dashboard/self"})
        # Not used: but keep consistent shape
        return _FakeResp(404, {})

    fake_opener = _FakeOpener(decide)
    monkeypatch.setattr(
        "github2gerrit.gerrit_urls.urllib.request.build_opener", lambda *_a, **_k: fake_opener, raising=True
    )
    b1 = create_gerrit_url_builder("gerrit.example.org")
    assert b1.base_path == "r"
    assert b1.web_url("dashboard").startswith("https://gerrit.example.org/r/")

    # Now simulate absolute URL in Location header
    _clear_builder_cache()

    def decide_abs(url: str) -> _FakeResp | BaseException:
        if url.endswith("/dashboard/self"):
            return _FakeResp(302, {"Location": "https://gerrit.example.org/r/some/page"})
        return _FakeResp(404, {})

    fake_opener2 = _FakeOpener(decide_abs)
    monkeypatch.setattr(
        "github2gerrit.gerrit_urls.urllib.request.build_opener", lambda *_a, **_k: fake_opener2, raising=True
    )
    b2 = create_gerrit_url_builder("gerrit.example.org")
    assert b2.base_path == "r"
    assert b2.api_url("changes/") == "https://gerrit.example.org/r/changes/"


def test_discover_base_path_known_endpoint_does_not_become_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GERRIT_HTTP_BASE_PATH", raising=False)
    _clear_builder_cache()

    # If redirected to '/changes/1', the first segment 'changes' is known
    # and should not be treated as a base path -> expect empty base path.
    def decide(url: str) -> _FakeResp | BaseException:
        if url.endswith("/dashboard/self"):
            return _FakeResp(302, {"Location": "/changes/1"})
        return _FakeResp(404, {})

    fake_opener = _FakeOpener(decide)
    monkeypatch.setattr(
        "github2gerrit.gerrit_urls.urllib.request.build_opener", lambda *_a, **_k: fake_opener, raising=True
    )

    b = create_gerrit_url_builder("gerrit.example.org")
    assert b.base_path == ""
    assert b.web_url("changes/1").startswith("https://gerrit.example.org/")


def test_discover_base_path_network_error_fallback_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GERRIT_HTTP_BASE_PATH", raising=False)
    _clear_builder_cache()

    # Simulate raising an exception (non-HTTPError) during open to trigger
    # fallback behavior. The discovery should continue and eventually return ''.
    def decide(_url: str) -> _FakeResp | BaseException:
        return ValueError("network glitch")

    fake_opener = _FakeOpener(decide)
    monkeypatch.setattr(
        "github2gerrit.gerrit_urls.urllib.request.build_opener", lambda *_a, **_k: fake_opener, raising=True
    )

    b = create_gerrit_url_builder("gerrit.example.org")
    assert b.base_path == ""
    assert b.web_url("x").startswith("https://gerrit.example.org/")


def test_empty_host_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicitly set env so builder does not attempt discovery
    monkeypatch.setenv("GERRIT_HTTP_BASE_PATH", "")
    _clear_builder_cache()

    # Creating a builder with empty host would be a misuse, but
    # we can still directly call the discovery helper by simulating its effect:
    # Since the builder requires a host, we just ensure default builder with
    # a real host and empty base path functions sanely.
    b = GerritUrlBuilder("gerrit.example.org", base_path="")
    assert b.base_path == ""
    assert b.web_url("") == "https://gerrit.example.org"
    assert b.web_url() == "https://gerrit.example.org"
