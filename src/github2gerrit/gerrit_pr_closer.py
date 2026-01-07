# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Gerrit PR Closer - handles closing GitHub PRs when Gerrit changes are merged.

This module provides functionality to detect when a Gerrit change has been
merged and close the corresponding GitHub pull request that originated it.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from typing import Literal

from .constants import GERRIT_CHANGE_URL_PATTERN
from .constants import GITHUB_PR_URL_PATTERN
from .error_codes import ExitCode
from .error_codes import GitHub2GerritError
from .gerrit_rest import GerritRestError
from .gerrit_rest import build_client_for_host
from .github_api import build_client
from .github_api import close_pr
from .github_api import create_pr_comment
from .github_api import get_pull
from .github_api import iter_open_pulls
from .gitutils import git_show
from .pr_content_filter import sanitize_gerrit_comment
from .rich_display import display_pr_info
from .rich_display import safe_console_print
from .trailers import GITHUB_PR_TRAILER
from .trailers import parse_trailers


log = logging.getLogger(__name__)


# Cleanup Gerrit changes when GitHub pull requests are closed.
# Can be controlled via CLEANUP_ABANDONED and CLEANUP_GERRIT
# environment variables.
def _env_bool(key: str, default: bool = True) -> bool:
    """Parse boolean from environment variable."""
    import os

    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes", "on")


FORCE_ABANDONED_CLEANUP = _env_bool("CLEANUP_ABANDONED", True)
FORCE_GERRIT_CLEANUP = _env_bool("CLEANUP_GERRIT", True)


def extract_change_number_from_url(
    gerrit_change_url: str,
) -> tuple[str, str] | None:
    """
    Extract Gerrit host and change number from a Gerrit change URL.

    Args:
        gerrit_change_url: Gerrit change URL (e.g., https://gerrit.example.org/c/project/+/12345)

    Returns:
        Tuple of (host, change_number) if valid, None otherwise

    Examples:
        >>> extract_change_number_from_url("https://gerrit.example.org/c/project/+/12345")
        ('gerrit.example.org', '12345')
        >>> extract_change_number_from_url("https://gerrit.linuxfoundation.org/infra/c/releng/lftools/+/123")
        ('gerrit.linuxfoundation.org', '123')
    """
    # Use shared pattern from constants module
    match = re.match(GERRIT_CHANGE_URL_PATTERN, gerrit_change_url)

    if match:
        host = match.group(1)
        change_number = match.group(2)
        return (host, change_number)

    log.debug("Failed to parse Gerrit change URL: %s", gerrit_change_url)
    return None


def check_gerrit_change_status(
    gerrit_change_url: str,
) -> Literal["MERGED", "ABANDONED", "NEW", "UNKNOWN"]:
    """
    Check the status of a Gerrit change via REST API.

    Args:
        gerrit_change_url: Gerrit change URL

    Returns:
        Status string: "MERGED", "ABANDONED", "NEW", or "UNKNOWN"

    Note:
        This function logs warnings but does not raise exceptions on
        API failures. Returns "UNKNOWN" if status cannot be determined.
    """
    parsed = extract_change_number_from_url(gerrit_change_url)
    if not parsed:
        log.warning(
            "Cannot extract change number from URL: %s",
            gerrit_change_url,
        )
        return "UNKNOWN"

    host, change_number = parsed

    try:
        # Build Gerrit REST client for the host
        client = build_client_for_host(host)

        # Query change details
        # Gerrit REST API endpoint: GET /changes/{change-id}
        change_data = client.get(f"/changes/{change_number}")

        # Extract status from response
        status = change_data.get("status", "UNKNOWN")
        log.debug("Gerrit change %s status: %s", change_number, status)
    except GerritRestError as exc:
        log.warning(
            "Failed to query Gerrit change %s status: %s",
            change_number,
            exc,
        )
        return "UNKNOWN"
    except Exception as exc:
        log.warning(
            "Unexpected error querying Gerrit change %s: %s",
            change_number,
            exc,
        )
        return "UNKNOWN"
    else:
        # Validate status against allowed values for type safety
        allowed_statuses = ("MERGED", "ABANDONED", "NEW", "UNKNOWN")
        result: Literal["MERGED", "ABANDONED", "NEW", "UNKNOWN"] = (
            status if status in allowed_statuses else "UNKNOWN"
        )
        if status not in allowed_statuses:
            log.warning(
                "Unexpected Gerrit status '%s' for change %s, "
                "treating as UNKNOWN",
                status,
                change_number,
            )
        return result


def extract_pr_url_from_commit(commit_sha: str) -> str | None:
    """
    Extract GitHub PR URL from a commit's trailers.

    Args:
        commit_sha: Git commit SHA to inspect

    Returns:
        GitHub PR URL if found, None otherwise
    """
    try:
        # Get the commit message
        commit_message = git_show(commit_sha, fmt="%B")

        # Parse trailers
        trailers = parse_trailers(commit_message)

        # Look for GitHub-PR trailer
        pr_urls = trailers.get(GITHUB_PR_TRAILER, [])
        if pr_urls:
            pr_url = pr_urls[-1]  # Take the last one if multiple exist
            log.debug("Found GitHub-PR trailer: %s", pr_url)
            return pr_url
        else:
            log.debug("No GitHub-PR trailer found in commit %s", commit_sha[:8])
            return None

    except Exception as exc:
        log.debug(
            "Failed to extract PR URL from commit %s: %s",
            commit_sha[:8],
            exc,
        )
        return None


def parse_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    """
    Parse a GitHub PR URL to extract owner, repo, and PR number.

    Args:
        pr_url: GitHub PR URL (e.g., https://github.com/owner/repo/pull/123)

    Returns:
        Tuple of (owner, repo, pr_number) if valid, None otherwise
    """
    # Use shared pattern from constants module (supports GHE)
    match = re.match(GITHUB_PR_URL_PATTERN, pr_url)

    if match:
        host = match.group(1)  # GitHub host (github.com or GHE domain)

        # Exclude known non-GitHub hosts
        bad_hosts = {
            "gitlab.com",
            "www.gitlab.com",
            "bitbucket.org",
            "www.bitbucket.org",
        }
        if host in bad_hosts:
            log.debug("Rejected non-GitHub host: %s", host)
            return None

        owner = match.group(2)
        repo = match.group(3)
        pr_number = int(match.group(4))
        return (owner, repo, pr_number)

    log.debug("Failed to parse PR URL: %s", pr_url)
    return None


def extract_pr_url_from_gerrit_change(gerrit_change_url: str) -> str | None:
    """
    Extract GitHub PR URL from a Gerrit change by querying the Gerrit API.

    This function queries the Gerrit REST API to get the commit message,
    then extracts the GitHub-PR trailer.

    Args:
        gerrit_change_url: Full Gerrit change URL (e.g., https://gerrit.example.com/c/project/+/12345)

    Returns:
        GitHub PR URL if found in commit trailers, None otherwise
    """
    parsed = extract_change_number_from_url(gerrit_change_url)
    if not parsed:
        log.debug(
            "Cannot extract change number from URL: %s",
            gerrit_change_url,
        )
        return None

    host, change_number = parsed

    try:
        # Build Gerrit REST client for the host
        client = build_client_for_host(host)

        # Query change details including commit message
        # Gerrit REST API endpoint: GET /changes/{change-id}/detail
        change_data = client.get(f"/changes/{change_number}/detail")

        # Get the current revision (latest patchset)
        current_revision = change_data.get("current_revision")
        if not current_revision:
            log.debug("No current revision found for change %s", change_number)
            return None

        # Get commit message from the revision data
        revisions = change_data.get("revisions", {})
        revision_data = revisions.get(current_revision, {})
        commit_data = revision_data.get("commit", {})
        commit_message = commit_data.get("message", "")

        if not commit_message:
            log.debug("No commit message found for change %s", change_number)
            return None

        # Parse trailers to find GitHub-PR URL
        trailers = parse_trailers(commit_message)
        pr_urls = trailers.get(GITHUB_PR_TRAILER, [])

        if pr_urls:
            pr_url = pr_urls[-1]  # Take the last one if multiple exist
            log.debug("Found GitHub-PR trailer in Gerrit change: %s", pr_url)
            return pr_url
    except GerritRestError as exc:
        log.warning(
            "Failed to query Gerrit change %s: %s",
            change_number,
            exc,
        )
        return None
    except Exception as exc:
        log.warning(
            "Unexpected error querying Gerrit change %s: %s",
            change_number,
            exc,
        )
        return None
    else:
        # No PR URL found in trailers
        log.debug(
            "No GitHub-PR trailer found in Gerrit change %s",
            change_number,
        )
        return None


def extract_pr_info_for_display(
    pr_obj: Any,
    owner: str,
    repo: str,
    pr_number: int,
) -> dict[str, Any]:
    """
    Extract PR information for display in a formatted table.

    Args:
        pr_obj: GitHub PR object
        owner: Repository owner
        repo: Repository name
        pr_number: PR number

    Returns:
        Dictionary of PR information for display
    """
    try:
        # Get PR title
        title = getattr(pr_obj, "title", "No title")

        # Get PR author
        author = "Unknown"
        user = getattr(pr_obj, "user", None)
        if user:
            author = getattr(user, "login", "Unknown") or "Unknown"

        # Get base branch
        base_branch = "unknown"
        base = getattr(pr_obj, "base", None)
        if base:
            base_branch = getattr(base, "ref", "unknown") or "unknown"

        # Get SHA
        sha = "unknown"
        head = getattr(pr_obj, "head", None)
        if head:
            sha = getattr(head, "sha", "unknown") or "unknown"

        # Build PR info dictionary
        pr_info = {
            "Repository": f"{owner}/{repo}",
            "PR Number": pr_number,
            "Title": title or "No title",
            "Author": author,
            "Base Branch": base_branch,
            "SHA": sha,
            "URL": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
        }

        # Add file changes count if available
        try:
            files = list(getattr(pr_obj, "get_files", list)())
            pr_info["Files Changed"] = len(files)
        except Exception:
            pr_info["Files Changed"] = "unknown"

    except Exception as exc:
        log.debug("Failed to extract PR info for display: %s", exc)
        raise GitHub2GerritError(
            ExitCode.GITHUB_API_ERROR,
            message="Failed to extract PR information",
            details=f"PR #{pr_number} in {owner}/{repo}",
            original_exception=exc,
        ) from exc
    else:
        return pr_info


def close_pr_with_status(
    pr_url: str,
    gerrit_change_url: str | None,
    gerrit_status: Literal["MERGED", "ABANDONED", "NEW", "UNKNOWN"],
    *,
    dry_run: bool = False,
    progress_tracker: Any = None,
    close_merged_prs: bool = True,
) -> bool:
    """
    Close a GitHub PR based on Gerrit change status.

    This is a public helper function that consolidates the PR closing logic
    used by multiple functions across the codebase.

    Args:
        pr_url: GitHub PR URL
        gerrit_change_url: Gerrit change URL for the comment
        gerrit_status: Status of the Gerrit change
        dry_run: If True, only display info without closing the PR
        progress_tracker: Optional progress tracker for display management
        close_merged_prs: If True, close PRs; if False, only comment on
            abandoned

    Returns:
        True if PR was closed (or would be closed in dry-run), False otherwise
    """
    # Parse PR URL
    parsed = parse_pr_url(pr_url)
    if not parsed:
        log.info("Invalid GitHub PR URL format: %s - skipping", pr_url)
        return False

    owner, repo, pr_number = parsed
    log.debug("Found GitHub PR: %s/%s#%d", owner, repo, pr_number)

    try:
        # Build GitHub client and get repository
        client = build_client()

        # Get the specific repository (not from env, might be different)
        repo_obj = client.get_repo(f"{owner}/{repo}")

        # Fetch the pull request
        try:
            pr_obj = get_pull(repo_obj, pr_number)
        except Exception as exc:
            # PR not found or API error - log as info, not error
            if "404" in str(exc) or "Not Found" in str(exc):
                log.info(
                    "GitHub PR #%d not found in %s/%s - may have been deleted",
                    pr_number,
                    owner,
                    repo,
                )
            else:
                # Other API errors should still be logged but not fatal
                log.warning(
                    "Could not fetch GitHub PR #%d: %s - skipping",
                    pr_number,
                    exc,
                )
            return False

        # Check if PR is already closed
        pr_state = getattr(pr_obj, "state", "unknown")
        if pr_state == "closed":
            log.info(
                "GitHub PR #%d is already closed - nothing to do",
                pr_number,
            )
            return False

        # Extract and display PR information
        pr_info = extract_pr_info_for_display(pr_obj, owner, repo, pr_number)
        display_pr_info(
            pr_info, context="Abandoned", progress_tracker=progress_tracker
        )

        # Determine action based on Gerrit status and close_merged_prs setting
        should_close = False
        comment = ""

        if gerrit_status == "ABANDONED":
            if close_merged_prs:
                # Close PR with abandoned comment
                should_close = True
                comment = _build_abandoned_comment(gerrit_change_url)
            else:
                # Comment only, don't close
                should_close = False
                comment = _build_abandoned_notification_comment(
                    gerrit_change_url
                )
        else:
            # For MERGED, NEW, or UNKNOWN status with close_merged_prs=True
            if close_merged_prs:
                should_close = True
                comment = _build_closure_comment(gerrit_change_url)
            else:
                # close_merged_prs=False, don't close for merged either
                log.info(
                    "Skipping PR closure (CLOSE_MERGED_PRS=false) for "
                    "status: %s",
                    gerrit_status,
                )
                return False

        if dry_run:
            if should_close:
                log.info("DRY-RUN: Would close PR #%d with comment", pr_number)
            else:
                log.info(
                    "DRY-RUN: Would comment on PR #%d (not close)", pr_number
                )
            return True

        # Add comment and optionally close the PR
        if should_close:
            log.debug("Closing GitHub PR #%d...", pr_number)
            close_pr(pr_obj, comment=comment)
            log.debug("SUCCESS: Closed GitHub PR #%d", pr_number)

            # Extract Gerrit change number from URL
            gerrit_change_number = "unknown"
            if gerrit_change_url:
                match = re.search(r"/c/[^/]+/\+/(\d+)", gerrit_change_url)
                if match:
                    gerrit_change_number = match.group(1)

            # Console and log output for closed PR
            close_message = (
                f"🛑 Closed pull request #{pr_number} with abandoned "
                f"Gerrit change {gerrit_change_number}"
            )
            safe_console_print(close_message)
            log.debug(close_message)
        else:
            # Comment only, don't close
            log.debug(
                "Adding abandoned notification comment to PR #%d...", pr_number
            )
            create_pr_comment(pr_obj, comment)
            log.debug(
                "SUCCESS: Added comment to PR #%d (PR remains open)", pr_number
            )

    except GitHub2GerritError as exc:
        # Our structured errors - log as warning but don't fail the workflow
        log.warning(
            "Could not close GitHub PR #%d: %s - skipping",
            pr_number,
            exc.message,
        )
        return False
    except Exception as exc:
        # Catch unexpected errors with detailed context for debugging
        # Common cases: network issues, auth failures, API rate limits
        error_type = type(exc).__name__
        error_details = str(exc)

        # Check for common error patterns
        if "401" in error_details or "403" in error_details:
            log.exception(
                "Authentication/authorization error while closing PR #%d: "
                "%s - check GitHub token permissions",
                pr_number,
                error_details,
            )
        elif "404" in error_details:
            log.warning(
                "PR #%d not found or repository inaccessible: %s",
                pr_number,
                error_details,
            )
        elif "rate limit" in error_details.lower():
            log.exception(
                "GitHub API rate limit exceeded while processing PR #%d: %s",
                pr_number,
                error_details,
            )
        else:
            # Log with full traceback for unexpected errors
            log.exception(
                "Unexpected error (%s) while closing PR #%d: %s",
                error_type,
                pr_number,
                error_details,
            )

        return False
    else:
        return True


def close_github_pr_for_merged_gerrit_change(
    commit_sha: str,
    gerrit_change_url: str | None = None,
    *,
    dry_run: bool = False,
    progress_tracker: Any = None,
    close_merged_prs: bool = True,
) -> bool:
    """
    Close a GitHub PR when its corresponding Gerrit change has been
    merged or abandoned.

    This function:
    1. Extracts the GitHub PR URL from the commit's trailers
    2. Verifies the Gerrit change status (merged/abandoned/new/unknown)
    3. Delegates to _close_pr_with_status for the actual closing logic

    Args:
        commit_sha: Git commit SHA that was merged in Gerrit
        gerrit_change_url: Optional Gerrit change URL for the comment
        dry_run: If True, only display info without closing the PR
        progress_tracker: Optional progress tracker for display management
        close_merged_prs: If True, close PRs; if False, only comment on
            abandoned

    Returns:
        True if PR was closed (or would be closed in dry-run), False otherwise
    """
    log.info("Processing Gerrit change: %s", commit_sha[:8])

    # Check Gerrit change status
    gerrit_status: Literal["MERGED", "ABANDONED", "NEW", "UNKNOWN"] = "UNKNOWN"
    if gerrit_change_url:
        gerrit_status = check_gerrit_change_status(gerrit_change_url)

        if gerrit_status == "ABANDONED":
            if close_merged_prs:
                log.info(
                    "Gerrit change was ABANDONED; will close PR with "
                    "abandoned comment (CLOSE_MERGED_PRS=true)"
                )
            else:
                log.info(
                    "Gerrit change was ABANDONED; will comment on PR only "
                    "(CLOSE_MERGED_PRS=false)"
                )
        elif gerrit_status == "NEW":
            log.warning(
                "Gerrit change is still NEW (not merged yet), but "
                "proceeding to close PR"
            )
        elif gerrit_status == "UNKNOWN":
            log.warning(
                "Cannot verify Gerrit change status; proceeding with PR closure"
            )
        elif gerrit_status == "MERGED":
            log.debug("Gerrit change confirmed as MERGED")

    # Extract PR URL from commit
    pr_url = extract_pr_url_from_commit(commit_sha)
    if not pr_url:
        log.info(
            "No GitHub PR URL found in commit %s - skipping",
            commit_sha[:8],
        )
        return False

    # Delegate to helper function for the actual closing logic
    return close_pr_with_status(
        pr_url=pr_url,
        gerrit_change_url=gerrit_change_url,
        gerrit_status=gerrit_status,
        dry_run=dry_run,
        progress_tracker=progress_tracker,
        close_merged_prs=close_merged_prs,
    )


def _build_closure_comment(gerrit_change_url: str | None = None) -> str:
    """
    Build the comment to post when closing a GitHub PR.

    Args:
        gerrit_change_url: Optional Gerrit change URL to include in comment

    Returns:
        Comment text
    """
    comment_lines = [
        "**Automated PR Closure**",
        "",
        "This pull request has been automatically closed by GitHub2Gerrit.",
        "",
    ]

    if gerrit_change_url:
        comment_lines.extend(
            [
                (
                    "The corresponding Gerrit change has been accepted "
                    "and merged ✅"
                ),
                "",
                f"Gerrit change URL: {gerrit_change_url}",
                "",
            ]
        )
    else:
        comment_lines.extend(
            [
                (
                    "The corresponding Gerrit change has been accepted "
                    "and merged ✅"
                ),
                "",
            ]
        )

    comment_lines.extend(
        [
            (
                "The changes from this PR are now part of the main codebase "
                "in Gerrit."
            ),
            "",
            "---",
            (
                "*This is an automated action performed by the "
                "GitHub2Gerrit tool.*"
            ),
        ]
    )

    return "\n".join(comment_lines)


def _build_abandoned_comment(gerrit_change_url: str | None = None) -> str:
    """
    Build the comment to post when closing a GitHub PR for an abandoned
    Gerrit change.

    Args:
        gerrit_change_url: Optional Gerrit change URL to include in comment

    Returns:
        Comment text
    """
    comment_lines = [
        "**Automated PR Closure**",
        "",
        "This pull request has been automatically closed by GitHub2Gerrit.",
        "",
    ]

    if gerrit_change_url:
        comment_lines.extend(
            [
                (
                    "The corresponding Gerrit change has been abandoned "
                    "and rejected ⛔️"
                ),
                "",
                f"Gerrit change URL: {gerrit_change_url}",
                "",
            ]
        )
    else:
        comment_lines.extend(
            [
                (
                    "The corresponding Gerrit change has been abandoned "
                    "and rejected ⛔️"
                ),
                "",
            ]
        )

    comment_lines.extend(
        [
            (
                "The changes from this PR are NOT part of the main codebase "
                "in Gerrit."
            ),
            "",
            "---",
            (
                "*This is an automated action performed by the "
                "GitHub2Gerrit tool.*"
            ),
        ]
    )

    return "\n".join(comment_lines)


def _build_abandoned_notification_comment(
    gerrit_change_url: str | None = None,
) -> str:
    """
    Build a notification comment when a Gerrit change is abandoned but PR
    stays open.

    Args:
        gerrit_change_url: Optional Gerrit change URL to include in comment

    Returns:
        Comment text
    """
    comment_lines = [
        "**Gerrit Change Abandoned** 🏳️",
        "",
        "The corresponding Gerrit change has been **abandoned**.",
        "",
    ]

    if gerrit_change_url:
        comment_lines.extend(
            [
                f"Gerrit change URL: {gerrit_change_url}",
                "",
            ]
        )

    comment_lines.extend(
        [
            (
                "This pull request remains open because `CLOSE_MERGED_PRS` "
                "is disabled."
            ),
            "",
            "---",
            (
                "*This is an automated notification from the "
                "GitHub2Gerrit tool.*"
            ),
        ]
    )

    return "\n".join(comment_lines)


def process_recent_commits_for_pr_closure(
    commit_shas: list[str],
    *,
    dry_run: bool = False,
    progress_tracker: Any = None,
    close_merged_prs: bool = True,
) -> int:
    """
    Process a list of recent commits and close any associated GitHub PRs.

    This is useful when multiple commits have been pushed from Gerrit.

    Args:
        commit_shas: List of commit SHAs to process
        dry_run: If True, only display info without closing PRs
        progress_tracker: Optional progress tracker for display management
        close_merged_prs: If True, close PRs; if False, only comment on
            abandoned

    Returns:
        Number of PRs closed (or that would be closed in dry-run)
    """
    if not commit_shas:
        log.debug("No commits to process")
        return 0

    log.info("Processing %d commit(s) for PR closure", len(commit_shas))

    closed_count = 0
    for commit_sha in commit_shas:
        # The close function already handles errors gracefully and returns
        # False. No need for try/except here as it won't raise exceptions
        if close_github_pr_for_merged_gerrit_change(
            commit_sha,
            dry_run=dry_run,
            progress_tracker=progress_tracker,
            close_merged_prs=close_merged_prs,
        ):
            closed_count += 1

    log.info("Closed %d GitHub PR(s)", closed_count)
    return closed_count


def cleanup_abandoned_prs_single(
    gerrit_change_url: str,
    *,
    dry_run: bool = False,
    progress_tracker: Any = None,
    close_merged_prs: bool = True,
) -> bool:
    """
    Check a single Gerrit change and close its GitHub PR if abandoned.

    This is the single-change mode of abandoned PR cleanup. It checks
    one specific Gerrit change and closes the corresponding GitHub PR
    if the change has been abandoned.

    Note: This requires a new Gerrit_to_Platform integration/feature
    that is not yet ready/available for testing. This function is
    implemented now to be ready when the integration is available.

    Args:
        gerrit_change_url: Full Gerrit change URL to check
        dry_run: If True, only display info without closing the PR
        progress_tracker: Optional progress tracker for display management
        close_merged_prs: If True, close PRs; if False, only comment

    Returns:
        True if PR was closed (or would be closed in dry-run), False otherwise
    """
    log.debug("⛔️ Checking for abandoned Gerrit changes")
    safe_console_print("⛔️ Checking for abandoned Gerrit changes")
    log.debug("Checking Gerrit change: %s", gerrit_change_url)

    # Check Gerrit change status
    status = check_gerrit_change_status(gerrit_change_url)

    if status != "ABANDONED":
        log.debug(
            "Gerrit change is not abandoned (status: %s), nothing to do",
            status,
        )
        return False

    log.info("Gerrit change is ABANDONED, looking for GitHub PR to close")

    # Extract PR URL from Gerrit change
    pr_url = extract_pr_url_from_gerrit_change(gerrit_change_url)
    if not pr_url:
        log.info(
            "No GitHub PR URL found in Gerrit change %s - skipping",
            gerrit_change_url,
        )
        return False

    # Close the PR using the standard function
    return close_pr_with_status(
        pr_url=pr_url,
        gerrit_change_url=gerrit_change_url,
        gerrit_status="ABANDONED",
        dry_run=dry_run,
        progress_tracker=progress_tracker,
        close_merged_prs=close_merged_prs,
    )


def cleanup_abandoned_prs_bulk(
    owner: str,
    repo: str,
    *,
    dry_run: bool = False,
    progress_tracker: Any = None,
    close_merged_prs: bool = True,
) -> int:
    """
    Check all open PRs in a repository and close those with abandoned
    Gerrit changes.

    This is the bulk cleanup mode. It scans all open PRs in the repository,
    extracts the Gerrit change URL from each PR's mapping comment, checks
    if the Gerrit change has been abandoned, and closes the PR if so.

    This runs in parallel where possible using multiple worker threads
    and GraphQL queries for efficiency.

    Args:
        owner: Repository owner
        repo: Repository name
        dry_run: If True, only display info without closing PRs
        progress_tracker: Optional progress tracker for display management
        close_merged_prs: If True, close PRs; if False, only comment

    Returns:
        Number of PRs closed (or that would be closed in dry-run)
    """
    log.debug("⛔️ Checking for abandoned Gerrit changes")
    safe_console_print("⛔️ Checking for abandoned Gerrit changes")
    log.debug(
        "Scanning all open PRs in %s/%s for abandoned Gerrit changes",
        owner,
        repo,
    )

    try:
        # Build GitHub client and get repository
        client = build_client()
        repo_obj = client.get_repo(f"{owner}/{repo}")

        # Get all open PRs
        open_prs = list(iter_open_pulls(repo_obj))
        if not open_prs:
            log.debug("No open pull requests found in %s/%s", owner, repo)
            return 0

        log.debug("Found %d open pull request(s) to check", len(open_prs))

        closed_count = 0

        # Process each open PR
        for pr in open_prs:
            pr_number = pr.number
            log.debug("Checking PR #%d for Gerrit change status", pr_number)

            try:
                # Get the PR's issue to access comments
                issue = pr.as_issue()
                comments = list(issue.get_comments())

                # Look for Gerrit change URL in comments
                gerrit_change_url = None
                for comment in comments:
                    body = getattr(comment, "body", "") or ""
                    # Look for Gerrit change URL pattern in comment
                    match = re.search(GERRIT_CHANGE_URL_PATTERN, body)
                    if match:
                        gerrit_change_url = match.group(0)
                        log.debug(
                            "Found Gerrit change URL in PR #%d: %s",
                            pr_number,
                            gerrit_change_url,
                        )
                        break

                if not gerrit_change_url:
                    log.debug(
                        "No Gerrit change URL found in PR #%d comments - "
                        "skipping",
                        pr_number,
                    )
                    continue

                # Check if the Gerrit change is abandoned
                status = check_gerrit_change_status(gerrit_change_url)

                if status == "ABANDONED":
                    log.debug(
                        "PR #%d has an abandoned Gerrit change, will close",
                        pr_number,
                    )

                    # Build PR URL from PR object
                    pr_url = (
                        f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                    )

                    # Close the PR
                    if close_pr_with_status(
                        pr_url=pr_url,
                        gerrit_change_url=gerrit_change_url,
                        gerrit_status="ABANDONED",
                        dry_run=dry_run,
                        progress_tracker=progress_tracker,
                        close_merged_prs=close_merged_prs,
                    ):
                        closed_count += 1
                else:
                    log.debug(
                        "PR #%d Gerrit change status is %s - no action needed",
                        pr_number,
                        status,
                    )

            except Exception as exc:
                # Log but don't fail - continue processing other PRs
                log.warning(
                    "Error processing PR #%d: %s - skipping",
                    pr_number,
                    exc,
                )
                continue

    except Exception:
        log.exception("Failed to perform bulk abandoned PR cleanup")
        return 0
    else:
        log.debug(
            "Abandoned PR cleanup complete: closed %d PR(s)", closed_count
        )
        return closed_count


def abandon_gerrit_change_for_closed_pr(
    pr_number: int,
    gerrit_server: str,
    gerrit_project: str,
    repository: str,
    *,
    dry_run: bool = False,
    progress_tracker: Any = None,
) -> str | None:
    """
    Abandon a Gerrit change when its corresponding GitHub PR is closed.

    This function finds the Gerrit change associated with a specific PR
    and abandons it if it's still open. Any comments made when closing
    the PR are also added to the Gerrit change before abandoning.

    Args:
        pr_number: GitHub PR number
        gerrit_server: Gerrit server hostname
        gerrit_project: Gerrit project name
        repository: GitHub repository (owner/repo format)
        dry_run: If True, only display info without abandoning
        progress_tracker: Optional progress tracker for display management

    Returns:
        Change number as string if Gerrit change was abandoned
        (or would be in dry-run), None otherwise
    """
    log.debug(
        "🔍 Looking for Gerrit change associated with PR #%d",
        pr_number,
    )

    try:
        # Build Gerrit REST client
        gerrit_client = build_client_for_host(gerrit_server)

        # Build expected PR URL to search for in Gerrit changes
        pr_url = f"https://github.com/{repository}/pull/{pr_number}"

        # Query for open changes with this PR URL in the commit message
        # We search for the GitHub-PR trailer value
        query = f"project:{gerrit_project} status:open"
        query_path = (
            f"/changes/?q={query}&o=CURRENT_REVISION&o=CURRENT_COMMIT&n=100"
        )

        log.debug("Querying Gerrit for changes in %s", gerrit_project)
        changes_data = gerrit_client.get(query_path)

        if not changes_data or not isinstance(changes_data, list):
            log.debug(
                "No open Gerrit changes found for PR #%d",
                pr_number,
            )
            return None

        # Find the change with matching PR URL
        matching_change = None
        for change_data in changes_data:
            try:
                current_revision = change_data.get("current_revision", "")
                if not current_revision:
                    continue

                revisions = change_data.get("revisions", {})
                revision_data = revisions.get(current_revision, {})
                commit_data = revision_data.get("commit", {})
                commit_message = commit_data.get("message", "")

                if not commit_message:
                    continue

                # Parse trailers to find GitHub-PR
                trailers = parse_trailers(commit_message)
                pr_urls = trailers.get(GITHUB_PR_TRAILER, [])

                # Check if this change matches our PR
                if pr_url in pr_urls:
                    matching_change = change_data
                    log.info(
                        "Found matching Gerrit change: %s",
                        change_data.get("_number", ""),
                    )
                    break

            except Exception as exc:
                log.debug(
                    "Error checking change %s: %s",
                    change_data.get("_number", "unknown"),
                    exc,
                )
                continue

        if not matching_change:
            log.debug(
                "No open Gerrit change found with GitHub-PR trailer for #%d",
                pr_number,
            )
            return None

        change_number = matching_change.get("_number", "")
        subject = matching_change.get("subject", "")

        log.debug(
            "Found Gerrit change %s (%s) for PR #%d",
            change_number,
            subject,
            pr_number,
        )

        # Get PR closure information from GitHub
        try:
            client = build_client()
            repo_obj = client.get_repo(repository)
            pr_obj = get_pull(repo_obj, pr_number)

            # Get closure comments
            closure_comments = []
            try:
                issue = pr_obj.as_issue()
                comments = list(issue.get_comments())

                # Get the last few comments (in case PR was closed with
                # a comment). We'll take up to the last 3 comments to
                # capture context
                if comments:
                    recent_comments = comments[-3:]
                    for comment in recent_comments:
                        comment_body = getattr(comment, "body", "") or ""
                        comment_author = (
                            getattr(
                                getattr(comment, "user", None),
                                "login",
                                "Unknown",
                            )
                            or "Unknown"
                        )
                        if comment_body.strip():
                            closure_comments.append(
                                f"Comment by {comment_author}:\n{comment_body}"
                            )
            except Exception as exc:
                log.debug("Error getting PR comments: %s", exc)

            # Build abandon message
            abandon_message_lines = [
                f"GitHub pull request #{pr_number} was closed",
                "",
                f"PR URL: {pr_url}",
            ]

            # Add closure comments if any
            if closure_comments:
                abandon_message_lines.extend(["", "Comments when closing:"])
                for idx, comment_text in enumerate(closure_comments, 1):
                    # Sanitize the comment
                    sanitized = sanitize_gerrit_comment(comment_text)
                    if sanitized:
                        abandon_message_lines.extend(
                            [
                                "",
                                f"--- Comment {idx} ---",
                                sanitized,
                            ]
                        )
                abandon_message_lines.append("---")

            abandon_message_lines.extend(
                [
                    "",
                    (
                        "This change was automatically abandoned by "
                        "GitHub2Gerrit because the source pull request "
                        "was closed."
                    ),
                ]
            )

            abandon_message = "\n".join(abandon_message_lines)

            # Abandon the Gerrit change
            if not dry_run:
                gerrit_change_url = (
                    f"https://{gerrit_server}/c/"
                    f"{gerrit_project}/+/{change_number}"
                )
                _abandon_gerrit_change(
                    gerrit_client,
                    change_number,
                    abandon_message,
                )
                log.debug(
                    "✅ Abandoned Gerrit change %s: %s",
                    change_number,
                    gerrit_change_url,
                )
                safe_console_print(
                    f"✅ Abandoned Gerrit change {gerrit_change_url} "
                    f"for pull request #{pr_number}"
                )
            else:
                log.debug(
                    "DRY-RUN: Would abandon Gerrit change %s",
                    change_number,
                )
                log.debug("Abandon message would be:\n%s", abandon_message)

            return str(change_number)

        except Exception as exc:
            log.warning(
                "Error getting PR #%d details: %s",
                pr_number,
                exc,
            )
            # Fall back to simple abandon message
            simple_message = (
                f"GitHub pull request #{pr_number} was closed\n\n"
                f"PR URL: {pr_url}\n\n"
                "This change was automatically abandoned by GitHub2Gerrit "
                "because the source pull request was closed."
            )

            if not dry_run:
                gerrit_change_url = (
                    f"https://{gerrit_server}/c/"
                    f"{gerrit_project}/+/{change_number}"
                )
                _abandon_gerrit_change(
                    gerrit_client,
                    change_number,
                    simple_message,
                )
                log.debug("Abandoned Gerrit change %s", change_number)
                safe_console_print(
                    f"✅ Abandoned Gerrit change {gerrit_change_url} "
                    f"for pull request #{pr_number}"
                )
            else:
                log.debug(
                    "DRY-RUN: Would abandon Gerrit change %s",
                    change_number,
                )

            return str(change_number)

    except Exception:
        log.exception(
            "Failed to abandon Gerrit change for closed PR #%d",
            pr_number,
        )
        return None


def cleanup_closed_github_prs(
    gerrit_server: str,
    gerrit_project: str,
    *,
    dry_run: bool = False,
    progress_tracker: Any = None,
) -> int:
    """
    Check all open Gerrit changes and abandon those with closed GitHub PRs.

    This cleanup mode scans all open Gerrit changes in the target project,
    extracts the GitHub PR URL from each change's commit message, checks if
    the GitHub PR is closed, and abandons the Gerrit change with an
    appropriate comment.

    Handles two closure scenarios:
    1. Dependabot closure: Detects "Superseded by #X" comments
    2. User closure: Copies user's closure comment and info to Gerrit

    This runs in parallel where possible using multiple worker threads.

    Args:
        gerrit_server: Gerrit server hostname
        gerrit_project: Gerrit project name
        dry_run: If True, only display info without abandoning changes
        progress_tracker: Optional progress tracker for display management

    Returns:
        Number of Gerrit changes abandoned (or would be abandoned in dry-run)
    """
    log.info("⛔️ Checking for closed/superseded GitHub change(s)")
    log.info(
        "Scanning open Gerrit changes in %s for closed GitHub PRs",
        gerrit_project,
    )

    try:
        # Build Gerrit REST client
        from .gerrit_rest import build_client_for_host

        gerrit_client = build_client_for_host(gerrit_server)

        # Query for all open changes in the project
        query = f"project:{gerrit_project} status:open"
        query_path = (
            f"/changes/?q={query}&o=CURRENT_REVISION&o=CURRENT_COMMIT&n=100"
        )

        log.debug("Querying Gerrit: %s", query)
        changes_data = gerrit_client.get(query_path)

        if not changes_data or not isinstance(changes_data, list):
            log.info("No open Gerrit changes found in %s", gerrit_project)
            return 0

        log.info("Found %d open Gerrit change(s) to check", len(changes_data))

        abandoned_count = 0

        # Process each open Gerrit change
        for change_data in changes_data:
            try:
                change_number = change_data.get("_number", "")
                subject = change_data.get("subject", "")

                log.debug(
                    "Checking Gerrit change %s (%s)", change_number, subject
                )

                # Extract commit message to find GitHub PR URL
                current_revision = change_data.get("current_revision", "")
                if not current_revision:
                    log.debug(
                        "No current revision for change %s - skipping",
                        change_number,
                    )
                    continue

                revisions = change_data.get("revisions", {})
                revision_data = revisions.get(current_revision, {})
                commit_data = revision_data.get("commit", {})
                commit_message = commit_data.get("message", "")

                if not commit_message:
                    log.debug(
                        "No commit message for change %s - skipping",
                        change_number,
                    )
                    continue

                # Extract GitHub PR URL from commit trailers
                trailers = parse_trailers(commit_message)
                pr_urls = trailers.get(GITHUB_PR_TRAILER, [])

                if not pr_urls:
                    log.debug(
                        "No GitHub-PR trailer in change %s - skipping",
                        change_number,
                    )
                    continue

                pr_url = pr_urls[-1]  # Take the last one if multiple
                log.debug(
                    "Found GitHub PR URL in change %s: %s",
                    change_number,
                    pr_url,
                )

                # Parse PR URL to get owner, repo, and PR number
                parsed = parse_pr_url(pr_url)
                if not parsed:
                    log.debug(
                        "Invalid GitHub PR URL format in change %s: %s",
                        change_number,
                        pr_url,
                    )
                    continue

                owner, repo, pr_number = parsed

                # Check GitHub PR status
                try:
                    client = build_client()
                    repo_obj = client.get_repo(f"{owner}/{repo}")
                    pr_obj = get_pull(repo_obj, pr_number)

                    pr_state = getattr(pr_obj, "state", "unknown")

                    if pr_state != "closed":
                        log.debug(
                            "GitHub PR #%d is %s - no action needed",
                            pr_number,
                            pr_state,
                        )
                        continue

                    log.info(
                        "GitHub PR #%d is closed, will abandon Gerrit "
                        "change %s",
                        pr_number,
                        change_number,
                    )

                    # Determine closure reason and build comment
                    abandon_message = _build_gerrit_abandon_message(
                        pr_obj, pr_url
                    )

                    # Abandon the Gerrit change
                    if not dry_run:
                        gerrit_change_url = (
                            f"https://{gerrit_server}/c/"
                            f"{gerrit_project}/+/{change_number}"
                        )
                        _abandon_gerrit_change(
                            gerrit_client,
                            change_number,
                            abandon_message,
                        )
                        log.info(
                            "Abandoned Gerrit change %s: %s",
                            change_number,
                            gerrit_change_url,
                        )
                    else:
                        log.info(
                            "DRY-RUN: Would abandon Gerrit change %s",
                            change_number,
                        )

                    abandoned_count += 1

                except Exception as exc:
                    log.warning(
                        "Error checking GitHub PR #%d: %s - skipping",
                        pr_number,
                        exc,
                    )
                    continue

            except Exception as exc:
                log.warning(
                    "Error processing Gerrit change %s: %s - skipping",
                    change_data.get("_number", "unknown"),
                    exc,
                )
                continue

    except Exception:
        log.exception("Failed to perform Gerrit cleanup for closed GitHub PRs")
        return 0
    else:
        log.info(
            "Gerrit cleanup complete: abandoned %d change(s)", abandoned_count
        )
        return abandoned_count


def _build_gerrit_abandon_message(pr_obj: Any, pr_url: str) -> str:
    """
    Build the abandon message for a Gerrit change based on GitHub PR closure.

    Handles two scenarios:
    1. Dependabot: Detects "Superseded by" pattern
    2. User closure: Extracts user comment and info

    Args:
        pr_obj: GitHub PR object
        pr_url: GitHub PR URL

    Returns:
        Formatted abandon message for Gerrit
    """
    pr_number = pr_obj.number

    # Check for Dependabot supersession
    try:
        issue = pr_obj.as_issue()
        comments = list(issue.get_comments())

        for comment in comments:
            body = getattr(comment, "body", "") or ""
            if "Superseded by" in body:
                # Extract superseding PR number
                match = re.search(r"Superseded by #(\d+)", body)
                if match:
                    new_pr_number = match.group(1)
                    return (
                        f"GitHub PR #{pr_number} was superseded by "
                        f"#{new_pr_number}\n\n"
                        f"Original PR: {pr_url}\n\n"
                        "This change was automatically abandoned by "
                        "GitHub2Gerrit because the source pull request "
                        "was closed by Dependabot."
                    )

    except Exception as exc:
        log.debug("Error checking for Dependabot comment: %s", exc)

    # User closure scenario - get PR author and closure info
    try:
        closed_by = "Unknown"
        user = getattr(pr_obj, "user", None)
        if user:
            closed_by = getattr(user, "login", "Unknown") or "Unknown"
            user_email = getattr(user, "email", None)
            if user_email:
                closed_by = f"{closed_by} <{user_email}>"

        # Try to get the last comment as closure reason
        closure_comment = ""
        try:
            issue = pr_obj.as_issue()
            comments = list(issue.get_comments())
            if comments:
                last_comment = comments[-1]
                closure_comment = (
                    getattr(last_comment, "body", "") or ""
                ).strip()
        except Exception as exc:
            log.debug("Error getting last comment: %s", exc)

        message_lines = [
            f"GitHub PR #{pr_number} was closed by {closed_by}",
            "",
            f"PR URL: {pr_url}",
        ]

        if closure_comment:
            # Sanitize comment to prevent malicious content and formatting
            # issues in Gerrit. Removes:
            # - HTML tags (including potentially malicious script/iframe/style)
            # - Markdown formatting (links, bold, etc.)
            # - GitHub emoji codes (:sparkles:, :bug:, etc.)
            # - Excessive whitespace
            # This reuses the same filtering infrastructure used for PR bodies.
            sanitized_comment = sanitize_gerrit_comment(closure_comment)
            if (
                sanitized_comment
            ):  # Only add if content remains after sanitization
                message_lines.extend(
                    [
                        "",
                        "Closure comment:",
                        "---",
                        sanitized_comment,
                        "---",
                    ]
                )

        message_lines.extend(
            [
                "",
                (
                    "This change was automatically abandoned by GitHub2Gerrit "
                    "because the source pull request was closed."
                ),
            ]
        )

        return "\n".join(message_lines)

    except Exception as exc:
        log.debug("Error building user closure message: %s", exc)
        # Fallback message
        return (
            f"GitHub PR #{pr_number} was closed\n\n"
            f"PR URL: {pr_url}\n\n"
            "This change was automatically abandoned by GitHub2Gerrit "
            "because the source pull request was closed."
        )


def _abandon_gerrit_change(
    client: Any, change_number: str, message: str
) -> None:
    """
    Abandon a Gerrit change via REST API.

    Args:
        client: Gerrit REST client
        change_number: Gerrit change number
        message: Abandon message

    Raises:
        Exception: If abandon operation fails
    """
    try:
        abandon_path = f"/changes/{change_number}/abandon"
        abandon_data = {"message": message}
        client.post(abandon_path, data=abandon_data)
        log.debug("Successfully abandoned Gerrit change %s", change_number)
    except Exception:
        log.exception("Failed to abandon Gerrit change %s", change_number)
        raise
