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

Requests go through python-gerrit-api, on a Requests session each client
owns. Authenticated requests use Gerrit's ``/a/`` prefix, beneath which
alone the server honours credentials. The client's own credentials, as
the centralized resolver found them, are the only ones it sends:
Requests' implicit netrc lookup is off, on redirects too. Requests drops
those credentials on a redirect that leaves the host or downgrades HTTPS
to HTTP, encodes request URLs, verifies TLS against
``REQUESTS_CA_BUNDLE`` or ``CURL_CA_BUNDLE`` when one is set, and honours
``HTTP_PROXY``, ``HTTPS_PROXY``, ``ALL_PROXY`` and ``NO_PROXY``. The
session re-reads the proxy settings for each redirect, and a
``NO_PROXY`` network such as ``fd00::/8`` also covers IPv6 addresses,
which Requests matches for IPv4 alone.

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

import ipaddress
import json
import logging
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Final

import requests

# python-gerrit-api, declared in pyproject.toml, installs as `gerrit`.
# aislop-ignore-next-line hallucinated-import
from gerrit import GerritClient
from requests.adapters import HTTPAdapter
from requests.auth import AuthBase

from .external_api import ApiType
from .external_api import RetryPolicy
from .external_api import external_api_call
from .gerrit_urls import create_gerrit_url_builder
from .netrc import GerritCredentials
from .netrc import resolve_gerrit_credentials
from .utils import log_exception_conditionally
from .utils import log_warning_once


log = logging.getLogger("github2gerrit.gerrit_rest")

# HTTP status codes that indicate an authentication/authorization problem
# rather than a transient fault or a bug. These are expected when no
# Gerrit REST credentials are available (or they are insufficient for the
# requested operation), and are surfaced as concise, default-visible
# warnings rather than error-level tracebacks.
_AUTH_ERROR_STATUSES: Final[tuple[int, ...]] = (401, 403)

# Shared dedup key so the "no Gerrit REST credentials" situation is
# surfaced at most once per run, regardless of how many auth-gated
# operations are affected.
_CREDENTIALS_UNAVAILABLE_KEY: Final[str] = "gerrit_rest_credentials_unavailable"


def _is_auth_status(status: int | None) -> bool:
    """Return True if the HTTP status indicates an auth/authorization error."""
    return status in _AUTH_ERROR_STATUSES


def _extract_http_status(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from an exception."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def warn_gerrit_credentials_unavailable() -> None:
    """Warn once per run that authenticated Gerrit REST is unavailable.

    Call this from auth-gated code paths before skipping an operation that
    requires Gerrit REST credentials. The warning is emitted at most once
    per process to avoid log spam while still making the degraded behavior
    visible at the default log level.
    """
    log_warning_once(
        log,
        _CREDENTIALS_UNAVAILABLE_KEY,
        "Gerrit REST credentials are not available; authenticated Gerrit "
        "REST operations are disabled. Fallback behavior may apply and "
        "could degrade performance. Provide GERRIT_HTTP_USER and "
        "GERRIT_HTTP_PASSWORD (or a .netrc entry) to enable authenticated "
        "Gerrit REST access.",
    )


def _in_no_proxy_network(host: str, no_proxy: str) -> bool:
    """Return True if *host* is an IP address inside a NO_PROXY network.

    Requests matches CIDR entries such as ``10.0.0.0/8`` for IPv4
    addresses only, so an IPv6 entry such as ``fd00::/8`` would never
    bypass the proxy without this check.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    for raw_entry in no_proxy.split(","):
        entry = raw_entry.strip()
        if "/" not in entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if address.version == network.version and address in network:
            return True
    return False


def _bypasses_proxy_by_network(url: str) -> bool:
    """Return True if a ``NO_PROXY`` network covers the host of *url*.

    Reads the variables as Requests does, the lowercase name first.
    """
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    host = urllib.parse.urlsplit(url).hostname or ""
    return _in_no_proxy_network(host, no_proxy)


class _NoCredentials(AuthBase):
    """Send a request as it is.

    Set on a request the client sends anonymously, so that Requests does
    not look its host up in a netrc file and authenticate it after all.
    """

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        return r


def _raise_for_status(
    response: requests.Response, *args: Any, **kwargs: Any
) -> None:
    """Raise HTTPError for a 4xx or 5xx response.

    Runs before python-gerrit-api translates the status into exceptions
    that drop it and the response along with it. A hook runs before
    Requests reads the body, so the body is read here and the connection
    released; otherwise each retry would open another connection while
    this one stayed checked out. A body that cannot be read leaves the
    HTTP error to report, and its connection is closed.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError:
        try:
            _ = response.content
        except Exception as exc:
            log.debug(
                "Could not read the body of HTTP %s: %s",
                response.status_code,
                exc,
            )
        finally:
            response.close()
        raise


class _ProxyVerifyingAdapter(HTTPAdapter):
    """Verify an https:// proxy that forwards a plain-HTTP request.

    Requests verifies TLS only for https URLs, so it would reach such a
    proxy over unverified TLS.
    """

    def cert_verify(self, conn: Any, url: str, verify: Any, cert: Any) -> None:
        if (
            verify
            and getattr(conn, "scheme", "") == "https"
            and not url.lower().startswith("https")
        ):
            # conn is the pool for the TLS proxy itself.
            url = "https://" + url.split("://", 1)[-1]
        super().cert_verify(conn, url, verify, cert)  # type: ignore[no-untyped-call]


class _GerritSession(requests.Session):
    """A Requests session that sends the client's credentials and no others.

    Requests would otherwise read a netrc file for any request without
    credentials, on every redirect as well, and keep using a proxy on a
    redirect to a host ``NO_PROXY`` covers. Environment CA bundles and
    proxies still apply, since ``trust_env`` stays on, and an https://
    proxy is verified whatever the scheme of the request it carries.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hooks["response"].append(_raise_for_status)
        for prefix in ("https://", "http://"):
            self.mount(prefix, _ProxyVerifyingAdapter())

    def prepare_request(
        self, request: requests.Request
    ) -> requests.PreparedRequest:
        if request.auth is None and self.auth is None:
            request.auth = _NoCredentials()
        return super().prepare_request(request)

    def rebuild_auth(
        self, prepared_request: requests.PreparedRequest, response: Any
    ) -> None:
        # As Requests does, without the netrc lookup that follows. The
        # stubs leave should_strip_auth and rebuild_proxies unannotated.
        headers = prepared_request.headers
        if "Authorization" in headers and self.should_strip_auth(  # type: ignore[no-untyped-call]
            response.request.url, prepared_request.url
        ):
            del headers["Authorization"]

    def rebuild_proxies(
        self, prepared_request: requests.PreparedRequest, proxies: Any
    ) -> dict[str, str]:
        # Requests keeps the proxies of the request that was redirected;
        # resolve them afresh for the new URL instead.
        new_proxies: dict[str, str] = super().rebuild_proxies(  # type: ignore[no-untyped-call]
            prepared_request, {}
        )
        if self.trust_env and _bypasses_proxy_by_network(
            str(prepared_request.url)
        ):
            prepared_request.headers.pop("Proxy-Authorization", None)
            return {}
        return new_proxies

    def merge_environment_settings(
        self,
        url: Any,
        proxies: Any,
        stream: Any,
        verify: Any,
        cert: Any,
    ) -> Any:
        settings = super().merge_environment_settings(
            url, proxies, stream, verify, cert
        )
        if self.trust_env and _bypasses_proxy_by_network(str(url)):
            settings["proxies"] = {}
        return settings


# Removed individual retry logic functions - now using centralized framework


class GerritRestError(RuntimeError):
    """Raised for non-retryable REST errors or exhausted retries.

    Args:
        status: HTTP status code associated with the failure, when known.
            Used by callers to distinguish authentication/authorization
            problems (401/403) from transient or unexpected errors.
    """

    def __init__(self, *args: object, status: int | None = None) -> None:
        super().__init__(*args)
        self.status = status

    @property
    def is_auth_error(self) -> bool:
        """Return True if this error stems from an auth failure (401/403)."""
        return _is_auth_status(self.status)


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

    Requests go through python-gerrit-api on a session the client owns
    and closes in ``close()``. With credentials, the client sends HTTP
    basic auth beneath Gerrit's ``/a/`` prefix, as the server treats a
    request outside it as anonymous.
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
        self._timeout: float = float(timeout)
        self._attempts: int = int(max_attempts)
        self._retry_policy = RetryPolicy(
            max_attempts=max_attempts,
            timeout=timeout,
        )
        self._auth: _Auth | None = None
        username: str | None = None
        password: str | None = None
        if auth and auth[0] and auth[1]:
            self._auth = _Auth(auth[0], auth[1])
            username, password = auth
            # python-gerrit-api appends /a itself once it has credentials,
            # so a base URL that already ends in it would get /a/a/.
            if base_url.endswith("/a/"):
                base_url = base_url[: -len("a/")]
        self._session = _GerritSession()
        # No retries of its own: external_api_call owns the retry policy,
        # and the timeout set here is the one every request uses.
        self._gerrit: Any = GerritClient(
            base_url=base_url,
            username=username,
            password=password,
            use_netrc=False,
            ssl_verify=True,
            # Annotated int upstream; Requests takes the float as given.
            timeout=self._timeout,  # pyright: ignore[reportArgumentType]
            session=self._session,
            auth_suffix="/a",
        )
        self._base_url: str = base_url
        if self._auth is not None:
            self._base_url = base_url + "a/"

        log.debug(
            "GerritRestClient(base_url=%s, timeout=%.1fs, attempts=%d, "
            "auth_user=%s)",
            self._base_url,
            self._timeout,
            self._attempts,
            self._auth.user if self._auth else "",
        )

    # Public API

    @property
    def is_authenticated(self) -> bool:
        """Return True if client has authentication credentials."""
        return self._auth is not None

    @property
    def host(self) -> str:
        """Return the Gerrit hostname derived from the base URL.

        Returns an empty string when the host cannot be determined.
        """
        from urllib.parse import urlparse

        try:
            return urlparse(self._base_url).hostname or ""
        except Exception:
            return ""

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

    def _request_json_with_retry(
        self, method: str, path: str, data: Any | None = None
    ) -> Any:
        """Perform a JSON request with retry using external API framework."""

        @external_api_call(
            ApiType.GERRIT_REST, f"{method.lower()}", policy=self._retry_policy
        )
        def _do_request() -> Any:
            return self._request_json(method, path, data)

        return _do_request()

    def _request_json(
        self, method: str, path: str, data: Any | None = None
    ) -> Any:
        """Perform a JSON request (retry logic handled by decorator)."""
        if not path:
            msg_required = "path is required"
            raise ValueError(msg_required)

        # python-gerrit-api joins the endpoint to the base URL as a string,
        # so a leading slash keeps any base path such as /r/.
        endpoint = "/" + path.removeprefix("/")
        url: str = self._gerrit.get_endpoint_url(endpoint)
        shown = requests.utils.requote_uri(url)

        try:
            scheme = urllib.parse.urlsplit(url).scheme
            if scheme not in ("http", "https"):
                msg_scheme = f"Unsupported URL scheme for Gerrit REST: {scheme}"
                raise GerritRestError(msg_scheme)

            headers = {"Accept": "application/json"}
            kwargs: dict[str, Any] = {}
            if data is not None:
                # python-gerrit-api would send a dict as form data and
                # drop an empty one, so the client sends the JSON itself.
                headers["Content-Type"] = "application/json; charset=UTF-8"
                kwargs["data"] = json.dumps(data).encode("utf-8")
            log.debug(
                "Gerrit REST %s %s (auth_user=%s)",
                method,
                shown,
                self._auth.user if self._auth else "",
            )

            send = getattr(self._gerrit.requester, method.lower())
            response = send(url, headers=headers, **kwargs)
            # Gerrit prepends ")]}'" to JSON responses to prevent JSON
            # hijacking; strip it and parse the rest strictly, as the
            # library would return a body without a JSON content type
            # as text.
            text = response.content.decode("utf-8", errors="replace")
            return _json_loads(_strip_xssi_guard(text))

        except requests.HTTPError as http_exc:
            status = _extract_http_status(http_exc)
            msg = f"Gerrit REST {method} {shown} failed with HTTP {status}"
            self._log_request_failure(msg, status)
            raise GerritRestError(msg, status=status) from http_exc

        except Exception as exc:
            status = _extract_http_status(exc)
            msg = f"Gerrit REST {method} {shown} failed: {exc}"
            self._log_request_failure(msg, status)
            raise GerritRestError(msg, status=status) from exc

    def close(self) -> None:
        """Close the client's HTTP session and its pooled connections."""
        self._session.close()

    def __enter__(self) -> GerritRestClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def _log_request_failure(msg: str, status: int | None) -> None:
        """Log a failed REST request at an appropriate level.

        Authentication/authorization failures (401/403) are expected when
        credentials are missing or insufficient; they are surfaced as a
        single concise warning per run rather than an error-level traceback,
        since they are neither transient faults nor bugs. All other failures
        retain the existing conditional error/traceback behavior.
        """
        if _is_auth_status(status):
            log_warning_once(
                log,
                f"gerrit_rest_auth_{status}",
                "Gerrit REST authentication failed (HTTP %s): the configured "
                "credentials are missing or insufficient for this operation. "
                "Fallback behavior may apply and could degrade performance. "
                "Details: %s",
                status,
                msg,
            )
        else:
            log_exception_conditionally(log, msg)

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
    base_path: str | None = None,
    http_user: str | None = None,
    http_password: str | None = None,
    use_netrc: bool = True,
    netrc_file: Path | None = None,
    credentials: GerritCredentials | None = None,
) -> GerritRestClient:
    """
    Build a GerritRestClient for a given host using the centralized URL builder.

    - Uses the given base path, else the auto-discovered or
      environment-provided one.
    - Reads HTTP auth from multiple sources in priority order:
      1. Pre-resolved GerritCredentials object (if provided)
      2. Explicit http_user/http_password arguments
      3. .netrc file (if use_netrc=True)
      4. Environment variables: GERRIT_HTTP_USER / GERRIT_HTTP_PASSWORD
         If user is not provided, falls back to GERRIT_SSH_USER_G2G per project
         norms.
      5. The netrc file NETRC names, or ~/.netrc past a local .netrc
         without the host (if use_netrc=True and netrc_file is None),
         where pygerrit2 used to look.

    Args:
      host: Gerrit hostname (no scheme)
      timeout: Request timeout in seconds.
      max_attempts: Max retry attempts for transient failures.
      base_path: HTTP base path override (e.g. ``"r"``).  Callers that
        run before the configuration file has been exported to the
        environment pass the file's value here; ``None`` defers to
        ``GERRIT_HTTP_BASE_PATH`` and discovery as before.
      http_user: Optional HTTP user (deprecated, use credentials).
      http_password: Optional HTTP password/token (deprecated, use credentials).
      use_netrc: Whether to try .netrc for credentials (default: True).
      netrc_file: Explicit path to a .netrc file (optional).
      credentials: Pre-resolved GerritCredentials object (preferred).

    Returns:
      Configured GerritRestClient.
    """
    builder = create_gerrit_url_builder(host, base_path)
    base_url = builder.api_url()

    # Use pre-resolved credentials if provided
    if credentials is not None and credentials.is_valid:
        log.debug(
            "Using pre-resolved credentials from %s",
            credentials.auth_method_display(),
        )
        auth: tuple[str, str] | None = (
            credentials.username,
            credentials.password,
        )
        return GerritRestClient(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
            max_attempts=max_attempts,
        )

    # Otherwise, resolve credentials using centralized function
    resolved = resolve_gerrit_credentials(
        host=host,
        explicit_username=http_user,
        explicit_password=http_password,
        use_netrc=use_netrc,
        netrc_file=netrc_file,
        env_username_var="GERRIT_HTTP_USER",
        env_password_var="GERRIT_HTTP_PASSWORD",  # noqa: S106
        fallback_env_username_var="GERRIT_SSH_USER_G2G",
        fallback_env_password_var=None,
    )

    auth = None
    if resolved is not None and resolved.is_valid:
        auth = (resolved.username, resolved.password)

    return GerritRestClient(
        base_url=base_url, auth=auth, timeout=timeout, max_attempts=max_attempts
    )


__all__ = [
    "GerritRestClient",
    "GerritRestError",
    "build_client_for_host",
    "warn_gerrit_credentials_unavailable",
]
