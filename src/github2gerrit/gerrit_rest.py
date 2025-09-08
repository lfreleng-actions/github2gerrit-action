# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Gerrit REST helper with retry, timeout, and transient error detection.

This module provides a thin, typed wrapper for issuing Gerrit REST calls
with:
- Bounded retries using exponential backoff with jitter
- Request timeouts
- Transient error classification (HTTP 5xx/429 and common network errors)
- Centralized URL handling via GerritUrlBuilder

It prefers pygerrit2 when available and falls back to urllib otherwise.

Usage:
    from github2gerrit.gerrit_rest import build_client_for_host

    client = build_client_for_host("gerrit.example.org", timeout=8.0)
    items = client.get("/changes/?q=project:foo limit:1&n=1&o=CURRENT_REVISION")

Design notes:
- The surface area is intentionally small and focused on JSON calls.
- Authentication (HTTP basic auth) is supported when username/password are
  provided.
- Base URLs should be created via the URL builder to respect base paths.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from typing import Final
from urllib.parse import urljoin

from .external_api import ApiType
from .external_api import RetryPolicy
from .external_api import external_api_call
from .gerrit_urls import create_gerrit_url_builder
from .utils import log_exception_conditionally


log = logging.getLogger("github2gerrit.gerrit_rest")

# Optional pygerrit2 import
try:  # pragma: no cover - exercised indirectly by tests that monkeypatch
    from pygerrit2 import GerritRestAPI as _PygerritRestApi  # type: ignore[import-not-found, unused-ignore]
    from pygerrit2 import HTTPBasicAuth as _PygerritHttpAuth  # type: ignore[import-not-found, unused-ignore]
except Exception:  # pragma: no cover - absence path
    _PygerritRestApi = None
    _PygerritHttpAuth = None


_MSG_PYGERRIT2_REQUIRED_AUTH: Final[str] = "pygerrit2 is required for HTTP authentication"

_TRANSIENT_ERR_SUBSTRINGS: Final[tuple[str, ...]] = (
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
)


# Removed individual retry logic functions - now using centralized framework


class GerritRestError(RuntimeError):
    """Raised for non-retryable REST errors or exhausted retries."""


@dataclass(frozen=True)
class _Auth:
    user: str
    password: str


def _mask_secret(s: str) -> str:
    if not s:
        return s
    if len(s) <= 4:
        return "****"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


class GerritRestClient:
    """
    Simple JSON REST client for Gerrit with retry/timeout handling.

    - If pygerrit2 is available, use it directly (preferred).
    - Otherwise, use urllib with manual request construction.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: tuple[str, str] | None = None,
        timeout: float = 8.0,
        max_attempts: int = 5,
    ) -> None:
        # Normalize base URL to end with '/'
        base_url = base_url.rstrip("/") + "/"
        self._base_url: str = base_url
        self._timeout: float = float(timeout)
        self._attempts: int = int(max_attempts)
        self._retry_policy = RetryPolicy(
            max_attempts=max_attempts,
            timeout=timeout,
        )
        self._auth: _Auth | None = None
        if auth and auth[0] and auth[1]:
            self._auth = _Auth(auth[0], auth[1])

        # Build pygerrit client if library is present; otherwise None
        if _PygerritRestApi is not None:
            if self._auth is not None:
                if _PygerritHttpAuth is None:
                    raise GerritRestError(_MSG_PYGERRIT2_REQUIRED_AUTH)
                self._client: Any = _PygerritRestApi(
                    url=self._base_url, auth=_PygerritHttpAuth(self._auth.user, self._auth.password)
                )
            else:
                self._client = _PygerritRestApi(url=self._base_url)
        else:
            self._client = None

        log.debug(
            "GerritRestClient(base_url=%s, timeout=%.1fs, attempts=%d, auth_user=%s)",
            self._base_url,
            self._timeout,
            self._attempts,
            self._auth.user if self._auth else "",
        )

    # Public API

    def get(self, path: str) -> Any:
        """HTTP GET, returning parsed JSON."""
        return self._request_json_with_retry("GET", path)

    def post(self, path: str, data: Any | None = None) -> Any:
        """HTTP POST with JSON payload, returning parsed JSON."""
        return self._request_json_with_retry("POST", path, data=data)

    def put(self, path: str, data: Any | None = None) -> Any:
        """HTTP PUT with JSON payload, returning parsed JSON."""
        return self._request_json_with_retry("PUT", path, data=data)

    # Internal helpers

    def _request_json_with_retry(self, method: str, path: str, data: Any | None = None) -> Any:
        """Perform a JSON request with retry using external API framework."""

        @external_api_call(ApiType.GERRIT_REST, f"{method.lower()}", policy=self._retry_policy)
        def _do_request() -> Any:
            return self._request_json(method, path, data)

        return _do_request()

    def _request_json(self, method: str, path: str, data: Any | None = None) -> Any:
        """Perform a JSON request (retry logic handled by decorator)."""
        if not path:
            msg_required = "path is required"
            raise ValueError(msg_required)

        # Normalize absolute vs relative path
        rel = path[1:] if path.startswith("/") else path
        url = urljoin(self._base_url, rel)

        try:
            if self._client is not None and method == "GET" and data is None:
                # pygerrit2 path: only using GET to keep behavior consistent with current usage
                log.debug("Gerrit REST GET via pygerrit2: %s", url)
                # pygerrit2.get expects a relative path; keep 'path' argument as-is
                return self._client.get("/" + rel if not path.startswith("/") else path)

            # urllib path (or non-GET with pygerrit2 absent)
            headers = {"Accept": "application/json"}
            body_bytes: bytes | None = None
            if data is not None:
                headers["Content-Type"] = "application/json"
                body_bytes = json.dumps(data).encode("utf-8")

            if self._auth is not None:
                token = base64.b64encode(f"{self._auth.user}:{self._auth.password}".encode()).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
            scheme = urllib.parse.urlparse(url).scheme
            if scheme not in ("http", "https"):
                msg_scheme = f"Unsupported URL scheme for Gerrit REST: {scheme}"
                raise GerritRestError(msg_scheme)
            req = urllib.request.Request(url, data=body_bytes, method=method, headers=headers)
            log.debug("Gerrit REST %s %s (auth_user=%s)", method, url, self._auth.user if self._auth else "")

            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", None)
                content = resp.read()
                # Gerrit prepends ")]}'" in JSON responses to prevent JSON hijacking; strip if present
                text = content.decode("utf-8", errors="replace")
                text = _strip_xssi_guard(text)
                return _json_loads(text)

        except urllib.error.HTTPError as http_exc:
            status = getattr(http_exc, "code", None)
            msg = f"Gerrit REST {method} {url} failed with HTTP {status}"
            log_exception_conditionally(log, msg)
            raise GerritRestError(msg) from http_exc

        except Exception as exc:
            msg = f"Gerrit REST {method} {url} failed: {exc}"
            log_exception_conditionally(log, msg)
            raise GerritRestError(msg) from exc

    def __repr__(self) -> str:  # pragma: no cover - convenience
        masked = ""
        if self._auth is not None:
            masked = f"{self._auth.user}:{_mask_secret(self._auth.password)}@"
        return f"GerritRestClient(base_url='{self._base_url}', auth='{masked}')"


def _json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception as exc:
        msg_parse = f"Failed to parse JSON response: {exc}"
        raise GerritRestError(msg_parse) from exc


def _strip_xssi_guard(text: str) -> str:
    # Gerrit typically prefixes JSON with XSSI guard ")]}'"
    # Strip the guard and any trailing newline after it.
    if text.startswith(")]}'"):
        # Common patterns: ")]}'\n" or ")]}'\r\n"
        if text[4:6] == "\r\n":
            return text[6:]
        if text[4:5] == "\n":
            return text[5:]
        return text[4:]
    return text


# Removed _sleep function - using centralized retry framework


def build_client_for_host(
    host: str,
    *,
    timeout: float = 8.0,
    max_attempts: int = 5,
    http_user: str | None = None,
    http_password: str | None = None,
) -> GerritRestClient:
    """
    Build a GerritRestClient for a given host using the centralized URL builder.

    - Uses auto-discovered or environment-provided base path.
    - Reads HTTP auth from arguments or environment:
      GERRIT_HTTP_USER / GERRIT_HTTP_PASSWORD
      If user is not provided, falls back to GERRIT_SSH_USER_G2G per project norms.

    Args:
      host: Gerrit hostname (no scheme)
      timeout: Request timeout in seconds.
      max_attempts: Max retry attempts for transient failures.
      http_user: Optional HTTP user.
      http_password: Optional HTTP password/token.

    Returns:
      Configured GerritRestClient.
    """
    builder = create_gerrit_url_builder(host)
    base_url = builder.api_url()
    user = (
        (http_user or "").strip()
        or os.getenv("GERRIT_HTTP_USER", "").strip()
        or os.getenv("GERRIT_SSH_USER_G2G", "").strip()
    )
    passwd = (http_password or "").strip() or os.getenv("GERRIT_HTTP_PASSWORD", "").strip()
    auth: tuple[str, str] | None = (user, passwd) if user and passwd else None
    return GerritRestClient(base_url=base_url, auth=auth, timeout=timeout, max_attempts=max_attempts)


__all__ = [
    "GerritRestClient",
    "GerritRestError",
    "build_client_for_host",
]
