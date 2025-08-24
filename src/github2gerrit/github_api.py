# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
#
# GitHub API wrapper using PyGithub with retries/backoff.
# - Centralized construction of the client
# - Helpers for common PR operations used by github2gerrit
# - Deterministic, typed interfaces with strict typing
# - Basic exponential backoff with jitter for transient failures
#
# Notes:
# - This module intentionally limits its surface area to the needs of the
#   orchestration flow: PR discovery, metadata, comments, and closing PRs.
# - Rate limit handling is best-effort. For heavy usage, consider honoring
#   the reset timestamp exposed by the API. Here we implement a capped
#   exponential backoff with jitter for simplicity.

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from importlib import import_module
from typing import Any
from typing import Protocol
from typing import TypeVar
from typing import cast

from .external_api import ApiType
from .external_api import external_api_call


# Error message constants to comply with TRY003
_MSG_PYGITHUB_REQUIRED = "PyGithub required"
_MSG_MISSING_GITHUB_TOKEN = "missing GITHUB_TOKEN"  # noqa: S105
_MSG_BAD_GITHUB_REPOSITORY = "bad GITHUB_REPOSITORY"


class GithubExceptionType(Exception):
    pass


class RateLimitExceededExceptionType(GithubExceptionType):
    pass


def _load_github_classes() -> tuple[Any | None, type[BaseException], type[BaseException]]:
    try:
        exc_mod = import_module("github.GithubException")
        ge = exc_mod.GithubException
        rle = exc_mod.RateLimitExceededException
    except Exception:
        ge = GithubExceptionType
        rle = RateLimitExceededExceptionType
    try:
        gh_mod = import_module("github")
        gh_cls = gh_mod.Github
    except Exception:
        gh_cls = None
    return gh_cls, ge, rle


_GITHUB_CLASS, _GITHUB_EXCEPTION, _RATE_LIMIT_EXC = _load_github_classes()
# Expose a public Github alias for tests and callers.
# If PyGithub is not available, provide a placeholder that raises.
if _GITHUB_CLASS is not None:
    Github = _GITHUB_CLASS
else:

    class Github:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(_MSG_PYGITHUB_REQUIRED)


class GhIssueComment(Protocol):
    body: str | None


class GhIssue(Protocol):
    def get_comments(self) -> Iterable[GhIssueComment]: ...
    def create_comment(self, body: str) -> None: ...


class GhPullRequest(Protocol):
    number: int
    title: str | None
    body: str | None

    def as_issue(self) -> GhIssue: ...
    def edit(self, *, state: str) -> None: ...


class GhRepository(Protocol):
    def get_pull(self, number: int) -> GhPullRequest: ...
    def get_pulls(self, state: str) -> Iterable[GhPullRequest]: ...


class GhClient(Protocol):
    def get_repo(self, full: str) -> GhRepository: ...


__all__ = [
    "Github",
    "GithubExceptionType",
    "RateLimitExceededExceptionType",
    "build_client",
    "close_pr",
    "create_pr_comment",
    "get_pr_title_body",
    "get_pull",
    "get_recent_change_ids_from_comments",
    "get_repo_from_env",
    "iter_open_pulls",
]

log = logging.getLogger("github2gerrit.github_api")

_T = TypeVar("_T")


def _getenv_str(name: str) -> str:
    val = os.getenv(name, "")
    return val.strip()


@external_api_call(ApiType.GITHUB, "build_client")
def build_client(token: str | None = None) -> GhClient:
    """Construct a PyGithub client from a token or environment.

    Order of precedence:
    - Provided 'token' argument
    - GITHUB_TOKEN environment variable

    Returns:
      Github client with sane defaults.
    """
    tok = token or _getenv_str("GITHUB_TOKEN")
    if not tok:
        raise ValueError(_MSG_MISSING_GITHUB_TOKEN)
    # per_page improves pagination; adjust as needed.
    base_url = _getenv_str("GITHUB_API_URL")
    if not base_url:
        server_url = _getenv_str("GITHUB_SERVER_URL")
        # Only synthesize API URL for non-github.com servers (GHE instances)
        if server_url and server_url.rstrip("/") != "https://github.com":
            base_url = server_url.rstrip("/") + "/api/v3"
    client_any: Any
    try:
        gh_mod = import_module("github")
        auth_factory = getattr(gh_mod, "Auth", None)
        if auth_factory is not None and hasattr(auth_factory, "Token"):
            auth_obj = auth_factory.Token(tok)
            if base_url:
                client_any = Github(auth=auth_obj, per_page=100, base_url=base_url)
            else:
                client_any = Github(auth=auth_obj, per_page=100)
        else:
            if base_url:
                client_any = Github(login_or_token=tok, per_page=100, base_url=base_url)
            else:
                client_any = Github(login_or_token=tok, per_page=100)
    except Exception:
        if base_url:
            client_any = Github(login_or_token=tok, per_page=100, base_url=base_url)
        else:
            client_any = Github(login_or_token=tok, per_page=100)
    return cast(GhClient, client_any)


@external_api_call(ApiType.GITHUB, "get_repo_from_env")
def get_repo_from_env(client: GhClient) -> GhRepository:
    """Return the repository object based on GITHUB_REPOSITORY."""
    full = _getenv_str("GITHUB_REPOSITORY")
    log.debug("GITHUB_REPOSITORY environment variable: '%s'", full)
    if not full or "/" not in full:
        log.error("Invalid GITHUB_REPOSITORY: '%s' (expected format: 'owner/repo')", full)
        raise ValueError(_MSG_BAD_GITHUB_REPOSITORY)
    repo = client.get_repo(full)
    return repo


@external_api_call(ApiType.GITHUB, "get_pull")
def get_pull(repo: GhRepository, number: int) -> GhPullRequest:
    """Fetch a pull request by number."""
    pr = repo.get_pull(number)
    return pr


def iter_open_pulls(repo: GhRepository) -> Iterable[GhPullRequest]:
    """Yield open pull requests in this repository."""
    yield from repo.get_pulls(state="open")


def get_pr_title_body(pr: GhPullRequest) -> tuple[str, str]:
    """Return PR title and body, replacing None with empty strings."""
    title = getattr(pr, "title", "") or ""
    body = getattr(pr, "body", "") or ""
    return str(title), str(body)


_CHANGE_ID_RE: re.Pattern[str] = re.compile(r"Change-Id:\s*([A-Za-z0-9._-]+)")


@external_api_call(ApiType.GITHUB, "get_pr_comments")
def _get_issue(pr: GhPullRequest) -> GhIssue:
    """Return the issue object corresponding to a pull request."""
    issue = pr.as_issue()
    return issue


@external_api_call(ApiType.GITHUB, "get_issue_from_pr")
def get_recent_change_ids_from_comments(
    pr: GhPullRequest,
    *,
    max_comments: int = 50,
) -> list[str]:
    """Scan recent PR comments for Change-Id trailers.

    Args:
      pr: Pull request.
      max_comments: Max number of most recent comments to scan.

    Returns:
      List of Change-Id values in order of appearance (oldest to newest)
      within the scanned window. Duplicates are preserved.
    """
    issue = _get_issue(pr)
    comments: Iterable[GhIssueComment] = issue.get_comments()
    # Collect last 'max_comments' by buffering and slicing at the end.
    buf: list[GhIssueComment] = []
    for c in comments:
        buf.append(c)
        # No early stop; PaginatedList can be large, we'll truncate after.
    # Truncate to the most recent 'max_comments'
    recent = buf[-max_comments:] if max_comments > 0 else buf
    found: list[str] = []
    for c in recent:
        body = getattr(c, "body", "") or ""
        for m in _CHANGE_ID_RE.finditer(body):
            cid = m.group(1).strip()
            if cid:
                found.append(cid)
    return found


@external_api_call(ApiType.GITHUB, "create_pr_comment")
def create_pr_comment(pr: GhPullRequest, body: str) -> None:
    """Create a new comment on the pull request."""
    if not body.strip():
        return
    issue = _get_issue(pr)
    issue.create_comment(body)


@external_api_call(ApiType.GITHUB, "close_pr")
def close_pr(pr: GhPullRequest, *, comment: str | None = None) -> None:
    """Close a pull request, optionally posting a comment first."""
    if comment and comment.strip():
        try:
            create_pr_comment(pr, comment)
        except Exception as exc:
            log.warning("Failed to add close comment to PR #%s: %s", pr.number, exc)
    pr.edit(state="closed")
