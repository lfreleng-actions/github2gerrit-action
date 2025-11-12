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
from .gitutils import git_show
from .rich_display import display_pr_info
from .trailers import GITHUB_PR_TRAILER
from .trailers import parse_trailers


log = logging.getLogger(__name__)


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
    log.info("Found GitHub PR: %s/%s#%d", owner, repo, pr_number)

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
        display_pr_info(pr_info, "Pull Request Details", progress_tracker)

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
            log.info("Closing GitHub PR #%d...", pr_number)
            close_pr(pr_obj, comment=comment)
            log.info("SUCCESS: Closed GitHub PR #%d", pr_number)
        else:
            # Comment only, don't close
            log.info(
                "Adding abandoned notification comment to PR #%d...", pr_number
            )
            create_pr_comment(pr_obj, comment)
            log.info(
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
    force: bool = False,
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
        force: If True, close PR regardless of Gerrit change status
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
                f"The corresponding Gerrit change has been **merged**: "
                f"{gerrit_change_url}",
                "",
            ]
        )
    else:
        comment_lines.extend(
            [
                "The corresponding Gerrit change has been **merged**.",
                "",
            ]
        )

    comment_lines.extend(
        [
            (
                "The changes from this PR are now part of the main codebase "
                "via Gerrit."
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
                "This pull request is being closed because the Gerrit review "
                "was abandoned and `CLOSE_MERGED_PRS` is enabled."
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
    force: bool = False,
    close_merged_prs: bool = True,
) -> int:
    """
    Process a list of recent commits and close any associated GitHub PRs.

    This is useful when multiple commits have been pushed from Gerrit.

    Args:
        commit_shas: List of commit SHAs to process
        dry_run: If True, only display info without closing PRs
        progress_tracker: Optional progress tracker for display management
        force: If True, close PRs regardless of Gerrit change status
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
            force=force,
            close_merged_prs=close_merged_prs,
        ):
            closed_count += 1

    log.info("Closed %d GitHub PR(s)", closed_count)
    return closed_count
