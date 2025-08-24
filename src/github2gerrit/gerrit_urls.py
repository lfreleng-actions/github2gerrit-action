# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Centralized Gerrit URL construction utilities.

This module provides a unified way to construct Gerrit URLs, ensuring
consistent handling of GERRIT_HTTP_BASE_PATH and eliminating the need
for manual URL construction throughout the codebase.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import urljoin


log = logging.getLogger(__name__)

_BASE_PATH_CACHE: dict[str, str] = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_301(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return fp

    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return fp

    def http_error_303(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return fp

    def http_error_307(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return fp

    def http_error_308(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return fp


def _discover_base_path_for_host(host: str, timeout: float = 5.0) -> str:
    """
    Discover Gerrit HTTP base path for the given host by probing redirects.

    Strategy:
    - Probe '/dashboard/self' and '/' without following redirects.
    - If redirected, infer base path from the first non-endpoint path segment.
    - If no redirect and 200 OK at '/dashboard/self', assume no base path.
    - Cache discovery results per host for the process lifetime.
    """
    try:
        if not host:
            return ""
        cached = _BASE_PATH_CACHE.get(host)
        if cached is not None:
            return cached

        opener = urllib.request.build_opener(_NoRedirect)
        opener.addheaders = [("User-Agent", "github2gerrit/urls-discovery")]
        probes = ["/dashboard/self", "/"]
        known_endpoints = {
            "changes",
            "accounts",
            "dashboard",
            "c",
            "q",
            "admin",
            "login",
            "settings",
            "plugins",
            "Documentation",
        }

        for scheme in ("https", "http"):
            for probe in probes:
                url = f"{scheme}://{host}{probe}"
                parsed_url = urllib.parse.urlparse(url)
                if parsed_url.scheme not in ("https", "http"):
                    log.debug("Skipping non-HTTP(S) probe URL: %s", url)
                    continue
                try:
                    resp = opener.open(url, timeout=timeout)
                    code = getattr(resp, "getcode", lambda: None)() or getattr(resp, "status", 0)
                    # If we reached the page without redirects
                    if code == 200:
                        _BASE_PATH_CACHE[host] = ""
                        log.info("Gerrit base path: ''")
                        return ""
                    # Handle 3xx responses when redirects are disabled (no-redirect opener)
                    if code in (301, 302, 303, 307, 308):
                        headers = getattr(resp, "headers", {}) or {}
                        loc = headers.get("Location") or headers.get("location") or ""
                        if loc:
                            # Normalize to absolute path
                            parsed = urllib.parse.urlparse(loc)
                            path = (
                                parsed.path
                                if parsed.scheme or parsed.netloc
                                else urllib.parse.urlparse(f"https://{host}{loc}").path
                            )
                            # Determine candidate base path
                            segs = [s for s in path.split("/") if s]
                            base = ""
                            if segs:
                                first = segs[0]
                                if first not in known_endpoints:
                                    base = first
                            _BASE_PATH_CACHE[host] = base
                            log.info("Gerrit base path: '%s'", base)
                            return base
                    # If we get any other non-redirect response, try next probe
                    continue
                except urllib.error.HTTPError as e:
                    # HTTPError doubles as the response; capture Location for redirects
                    code = e.code
                    loc = e.headers.get("Location") or e.headers.get("location") or ""
                    if code in (301, 302, 303, 307, 308) and loc:
                        # Normalize to absolute path
                        parsed = urllib.parse.urlparse(loc)
                        path = (
                            parsed.path
                            if parsed.scheme or parsed.netloc
                            else urllib.parse.urlparse(f"https://{host}{loc}").path
                        )
                        # Determine candidate base path
                        segs = [s for s in path.split("/") if s]
                        base = ""
                        if segs:
                            first = segs[0]
                            if first not in known_endpoints:
                                base = first
                        _BASE_PATH_CACHE[host] = base
                        log.info("Gerrit base path: '%s'", base)
                        return base
                    # Non-redirect error; try next probe
                    continue
                except Exception as exc:
                    log.debug("Gerrit base path probe failed for %s%s: %s", host, probe, exc)
                    continue

    except Exception as exc:
        log.debug("Gerrit base path discovery error for %s: %s", host, exc)
        return ""
    # Default if nothing conclusive after exhausting all probes
    _BASE_PATH_CACHE[host] = ""
    log.info("Gerrit base path: ''")
    return ""


class GerritUrlBuilder:
    """
    Centralized builder for Gerrit URLs with consistent base path handling.

    This class encapsulates all Gerrit URL construction logic, ensuring that
    GERRIT_HTTP_BASE_PATH is properly handled in all contexts. It provides
    methods for building different types of URLs (API, web, hooks) and handles
    the common fallback patterns used throughout the application.
    """

    def __init__(self, host: str, base_path: str | None = None):
        """
        Initialize the URL builder for a specific Gerrit host.

        Args:
            host: Gerrit hostname (without protocol)
            base_path: Optional base path override. If None, reads from
                      GERRIT_HTTP_BASE_PATH environment variable or discovers dynamically.
        """
        self.host = host.strip()

        # Normalize base path - remove leading/trailing slashes and whitespace
        if base_path is not None:
            self._base_path = base_path.strip().strip("/")
        else:
            env_bp = os.getenv("GERRIT_HTTP_BASE_PATH", "").strip().strip("/")
            if env_bp:
                self._base_path = env_bp
            else:
                discovered = _discover_base_path_for_host(self.host)
                self._base_path = discovered.strip().strip("/")

        log.debug(
            "GerritUrlBuilder initialized for host=%s, base_path='%s'",
            self.host,
            self._base_path,
        )

    @property
    def base_path(self) -> str:
        """Get the normalized base path."""
        return self._base_path

    @property
    def has_base_path(self) -> bool:
        """Check if a base path is configured."""
        return bool(self._base_path)

    def _build_base_url(self, base_path_override: str | None = None) -> str:
        """
        Build the base URL with optional base path override.

        Args:
            base_path_override: Optional base path to use instead of the instance default

        Returns:
            Base URL with trailing slash
        """
        path = base_path_override if base_path_override is not None else self._base_path
        if path:
            return f"https://{self.host}/{path}/"
        else:
            return f"https://{self.host}/"

    def api_url(self, endpoint: str = "", base_path_override: str | None = None) -> str:
        """
        Build a Gerrit REST API URL.

        Args:
            endpoint: API endpoint path (e.g., "/changes/", "/accounts/self")
            base_path_override: Optional base path override for fallback scenarios

        Returns:
            Complete API URL
        """
        base_url = self._build_base_url(base_path_override)
        # Ensure endpoint starts with / for proper URL joining
        if endpoint and not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return urljoin(base_url, endpoint.lstrip("/"))

    def web_url(self, path: str = "", base_path_override: str | None = None) -> str:
        """
        Build a Gerrit web UI URL.

        Args:
            path: Web path (e.g., "c/project/+/123", "dashboard")
            base_path_override: Optional base path override for fallback scenarios

        Returns:
            Complete web URL
        """
        base_url = self._build_base_url(base_path_override)
        if path:
            # Remove leading slash if present to avoid double slashes
            path = path.lstrip("/")
            return urljoin(base_url, path)
        return base_url.rstrip("/")

    def change_url(
        self,
        project: str,
        change_number: int,
        base_path_override: str | None = None,
    ) -> str:
        """
        Build a URL for a specific Gerrit change.

        Args:
            project: Gerrit project name
            change_number: Gerrit change number
            base_path_override: Optional base path override for fallback scenarios

        Returns:
            Complete change URL
        """
        # Don't URL-encode project names - Gerrit expects them as-is (backward compatibility)
        path = f"c/{project}/+/{change_number}"
        return self.web_url(path, base_path_override)

    def hook_url(self, hook_name: str, base_path_override: str | None = None) -> str:
        """
        Build a URL for downloading Gerrit hooks.

        Args:
            hook_name: Name of the hook (e.g., "commit-msg")
            base_path_override: Optional base path override for fallback scenarios

        Returns:
            Complete hook download URL
        """
        path = f"tools/hooks/{hook_name}"
        return self.web_url(path, base_path_override)

    def get_api_url_candidates(self, endpoint: str = "") -> list[str]:
        """
        Get the single API URL based on discovered/configured base path.

        This method avoids hard-coded fallbacks by relying on dynamic detection
        of Gerrit's HTTP base path (or explicit configuration).

        Args:
            endpoint: API endpoint path

        Returns:
            A single API URL to use
        """
        return [self.api_url(endpoint)]

    def get_hook_url_candidates(self, hook_name: str) -> list[str]:
        """
        Get the single hook URL based on discovered/configured base path.

        This method avoids hard-coded fallbacks by relying on dynamic detection
        of Gerrit's HTTP base path (or explicit configuration).

        Args:
            hook_name: Name of the hook to download

        Returns:
            A single hook URL to use
        """
        return [self.hook_url(hook_name)]

    def get_web_base_path(self, base_path_override: str | None = None) -> str:
        """
        Get the web base path for URL construction.

        This is useful when you need just the path component for manual URL building.

        Args:
            base_path_override: Optional base path override

        Returns:
            Web base path with leading and trailing slashes (e.g., "/r/", "/")
        """
        path = base_path_override if base_path_override is not None else self._base_path
        if path:
            return f"/{path}/"
        else:
            return "/"

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"GerritUrlBuilder(host='{self.host}', base_path='{self._base_path}')"


def create_gerrit_url_builder(host: str, base_path: str | None = None) -> GerritUrlBuilder:
    """
    Factory function to create a GerritUrlBuilder instance.

    This is the preferred way to create URL builders throughout the application.

    Args:
        host: Gerrit hostname
        base_path: Optional base path override

    Returns:
        Configured GerritUrlBuilder instance
    """
    return GerritUrlBuilder(host, base_path)
