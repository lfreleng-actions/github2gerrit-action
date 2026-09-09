# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

# aislop-ignore-file complexity/file-too-large -- cli.py hosts the Typer CLI
# surface and its orchestration helpers. Its overly long functions and deep
# nesting are decomposed here; splitting the module is a dedicated follow-up
# refactor tracked separately.

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn
from typing import TypeVar
from typing import cast
from urllib.parse import urlparse

import typer

from . import models
from .approvers import resolve_additional_approvers
from .config import _is_github_actions_context
from .config import apply_config_to_env
from .config import apply_parameter_derivation
from .config import load_org_config
from .constants import GERRIT_CHANGE_URL_PATTERN
from .core import Orchestrator
from .core import OrchestratorError
from .core import SubmissionResult
from .duplicate_detection import DuplicateChangeError
from .duplicate_detection import DuplicateDetector
from .duplicate_detection import check_for_duplicates
from .error_codes import ExitCode
from .error_codes import GitHub2GerritError
from .error_codes import convert_configuration_error
from .error_codes import convert_duplicate_error
from .error_codes import convert_orchestrator_error
from .error_codes import exit_for_configuration_error
from .error_codes import exit_for_duplicate_error
from .error_codes import exit_for_github_api_error
from .error_codes import exit_for_pr_not_found
from .error_codes import exit_for_pr_state_error
from .error_codes import exit_with_error
from .error_codes import is_github_api_permission_error
from .external_api import log_api_metrics_summary
from .gerrit_pr_closer import FORCE_ABANDONED_CLEANUP
from .gerrit_pr_closer import FORCE_GERRIT_CLEANUP
from .gerrit_pr_closer import abandon_gerrit_change_for_closed_pr
from .gerrit_pr_closer import check_gerrit_change_status
from .gerrit_pr_closer import cleanup_abandoned_prs_bulk
from .gerrit_pr_closer import cleanup_closed_github_prs
from .gerrit_pr_closer import close_pr_with_status
from .gerrit_pr_closer import extract_pr_url_from_gerrit_change
from .gerrit_pr_closer import parse_pr_url
from .gerrit_pr_closer import process_recent_commits_for_pr_closure
from .github_api import build_client
from .github_api import create_pr_comment
from .github_api import get_pr_title_body
from .github_api import get_pull
from .github_api import get_repo_from_env
from .github_api import iter_open_pulls
from .gitutils import CommandError
from .gitutils import enumerate_reviewer_emails
from .gitutils import git
from .models import GitHubContext
from .models import Inputs
from .netrc import NetrcParseError
from .netrc import get_credentials_for_host
from .pr_approval import APPROVAL_MARKER
from .pr_approval import ApprovalStatus
from .pr_approval import describe_approver_policy
from .pr_approval import evaluate_fork_approval
from .pr_approval import render_blocked_comment
from .pr_approval import render_cleared_comment
from .pr_commands import CMD_CHECK
from .pr_commands import MENTION_PREFIX
from .pr_commands import find_open_command
from .rich_display import RICH_AVAILABLE
from .rich_display import DummyProgressTracker
from .rich_display import G2GProgressTracker
from .rich_display import display_pr_info
from .rich_display import safe_console_print
from .rich_display import safe_typer_echo
from .rich_logging import setup_rich_aware_logging
from .utils import append_github_output
from .utils import env_bool
from .utils import env_str
from .utils import is_verbose_mode
from .utils import log_exception_conditionally
from .utils import parse_bool_env


def get_version(package: str) -> str:
    """Get the installed package version via importlib.metadata."""
    from importlib.metadata import version

    return str(version(package))


# Legacy error handling functions - now use centralized error_codes module
# These are kept for backward compatibility but delegate to the new system


def _exit_for_pr_state_error(pr_number: int, pr_state: str) -> None:
    """Exit with error message for invalid PR state."""
    exit_for_pr_state_error(pr_number, pr_state)


def _exit_for_pr_not_found(pr_number: int, repository: str) -> None:
    """Exit with error message for PR not found."""
    exit_for_pr_not_found(pr_number, repository)


# URL parsing result types for type-safe discriminated union
@dataclass(frozen=True)
class GitHubPRTarget:
    """Result from parsing a GitHub pull request URL."""

    owner: str | None
    repo: str | None
    pr_number: int | None


@dataclass(frozen=True)
class GitHubRepoTarget:
    """Result from parsing a GitHub repository URL (no PR)."""

    owner: str | None
    repo: str | None


@dataclass(frozen=True)
class GerritChangeTarget:
    """Result from parsing a Gerrit change URL."""

    change_url: str


# Union type for all possible target URL parse results
TargetURL = GitHubPRTarget | GitHubRepoTarget | GerritChangeTarget


def _exit_for_pr_fetch_error(exc: Exception) -> None:
    """Exit with error message for PR fetch failure."""
    if is_github_api_permission_error(exc):
        exit_for_github_api_error(
            message=(
                "❌ GitHub API query failed; provide a GITHUB_TOKEN with "
                "the required permissions"
            ),
            details="Failed to fetch PR details - check token permissions",
            exception=exc,
        )
    else:
        exit_with_error(
            ExitCode.GENERAL_ERROR,
            message=f"❌ Failed to fetch PR details: {exc}",
            exception=exc,
        )


def _check_automation_only(
    pr_obj: Any,
    gh: GitHubContext,
    progress_tracker: Any = None,
) -> None:
    """Check if PR is from automation tool when automation_only enabled."""
    # Default to True when AUTOMATION_ONLY is not set
    # (matches action.yaml default: "true")
    automation_only = env_bool("AUTOMATION_ONLY", True)

    if not automation_only:
        log.debug("AUTOMATION_ONLY disabled, accepting all PRs")
        return

    # Known automation tools
    known_automation_tools = [
        "Copilot",
        "dependabot[bot]",
        "dependabot",
        "pre-commit-ci[bot]",
        "pre-commit-ci",
    ]

    pr_author = getattr(getattr(pr_obj, "user", None), "login", "")
    if not pr_author:
        log.warning("Unable to determine PR author, allowing PR to proceed")
        return

    log.debug(
        "Checking PR author '%s' against known automation tools",
        pr_author,
    )

    # Check if author is in known automation tools list
    if pr_author not in known_automation_tools:
        log.warning(
            "PR #%s from '%s' rejected - only known automation tools "
            "are accepted when AUTOMATION_ONLY is enabled",
            gh.pr_number,
            pr_author,
        )

        # Close PR with comment
        try:
            from .github_api import close_pr

            comment = (
                "This GitHub mirror does not accept pull requests.\n"
                "Please submit changes to the project's Gerrit server."
            )

            close_pr(pr_obj, comment=comment)

            sys.exit(1)
        except Exception:
            log.exception("Failed to close non-automation PR")
            raise
    else:
        log.debug(
            "PR author '%s' is a known automation tool, proceeding",
            pr_author,
        )


def _resolve_pr_for_gate(gh: GitHubContext, data: Inputs) -> Any | None:
    """Fetch the pull request for the approval gate.

    Used when the display step returned nothing — most notably the
    no-token branch, which returns early. Without this the gate would
    fail open exactly when the environment is least well configured.
    """
    if not gh.pr_number:
        return None
    try:
        token = getattr(data, "github_token", "") or os.getenv(
            "GITHUB_TOKEN", ""
        )
        client = build_client(token)
        repo = get_repo_from_env(client)
        return get_pull(repo, int(gh.pr_number))
    except Exception as exc:
        log.debug("Could not resolve PR for the approval gate: %s", exc)
        return None


_FORK_UNPRIVILEGED_EVENTS: frozenset[str] = frozenset(
    {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
    }
)
"""Triggers that run unprivileged when the pull request is from a fork.

All three run from ``refs/pull/<N>/merge`` and print GitHub's fork
secret restriction in their own section of the events reference, so
their workflow file is fork-authored and the run receives no repository
secrets. ``pull_request_target``, ``issue_comment``, ``schedule`` and
``workflow_run`` run from the default branch and are deliberately
absent.

Naming them, rather than inferring the condition from an absent key
alone, keeps a genuinely unset secret loud everywhere else. A trigger
GitHub adds later therefore fails noisily rather than skipping in
silence, which is the safer direction for a diagnostic.
"""


def _skip_unprivileged_fork_run(data: Inputs, gh: GitHubContext) -> bool:
    """Report whether this run could never have reached Gerrit.

    A fork pull request delivered on an unprivileged trigger arrives
    without ``GERRIT_SSH_PRIVKEY_G2G``, because GitHub withholds
    secrets from runs whose workflow file comes from the fork. Neither
    of the two things that happen next is honest about that:

    * with ``G2G_USE_SSH_AGENT`` disabled, validation reports a missing
      required input, describing a misconfiguration the operator does
      not have, and
    * with it enabled — the default — ``_setup_ssh`` skips silently,
      the run fetches the fork, tells the contributor their pull
      request is approved and transferring, and only then fails at the
      push with an opaque SSH error.

    Stopping here is both truthful and cheap, and it happens before any
    fork content is fetched.

    Returns:
        ``True`` when the caller should stop, successfully. Missing
        provenance answers ``False`` so that a genuinely unset secret
        stays loud: skipping in silence is the worse failure, because
        it is indistinguishable from success.
    """
    if not _is_github_actions_context():
        return False
    if data.gerrit_ssh_privkey_g2g:
        return False
    if env_bool("G2G_NO_GERRIT", False) or data.dry_run:
        # Both are keyless by design. G2G_NO_GERRIT turns the Gerrit
        # network operations into no-ops, and a dry run returns from
        # ``Orchestrator.execute`` before SSH setup. The absent key is
        # the point in each case, not a symptom, so reading it as one
        # would stop the very exercise the mode exists for.
        return False
    if gh.event_name not in _FORK_UNPRIVILEGED_EVENTS:
        return False
    # The factual question, not the trust one. An unresolved head is
    # not known to be a fork, and treating it as one here would hide a
    # real misconfiguration on a same-repository pull request.
    if not gh.is_fork_pr:
        return False

    log.info(
        "⏩ Pull request #%s not processed by this run: GitHub withholds "
        "repository secrets from a '%s' run on a fork pull request, so "
        "no Gerrit key is available. This is how GitHub scopes the "
        "trigger, not a misconfiguration — a privileged trigger "
        "performs the transfer.",
        gh.pr_number,
        gh.event_name,
    )
    safe_console_print(
        f"⏩ Skipped: a '{gh.event_name}' run on a fork pull request "
        "receives no repository secrets",
        style="yellow",
    )
    return True


def _triggering_comment_body(gh: GitHubContext) -> str:
    """Return the body of the comment that triggered this run.

    Empty when the payload carries no comment, when the comment is on
    an ordinary issue rather than a pull request, or when that pull
    request is closed.

    ``issue_comment`` fires for all three. The ``issue`` object carries
    a ``pull_request`` member only for a pull request, and its
    ``state`` distinguishes one still open. A closed pull request has
    no gate left to lift, and letting the run continue would reach
    ``_exit_for_pr_state_error`` and put a failing check on the pull
    request — which any commenter could then do at will.

    Checked here rather than left to the workflow: the reusable
    workflow guards its job condition, but a composite-action caller
    writes their own ``on:`` block with no such guard, and a directive
    on issue N would otherwise be taken for pull request N.
    """
    evt = _load_event(gh.event_path)

    issue = evt.get("issue")
    if isinstance(issue, dict):
        if not issue.get("pull_request"):
            log.debug("Comment is on an issue rather than a pull request")
            return ""
        state = str(issue.get("state", "") or "").strip().lower()
        if state and state != "open":
            log.debug("Comment is on a %s pull request; nothing to do", state)
            return ""

    comment = evt.get("comment")
    if isinstance(comment, dict):
        body = comment.get("body")
        if isinstance(body, str):
            return body
    return ""


def _skip_unrequested_comment_run(gh: GitHubContext) -> bool:
    """Report whether a comment run should stop without doing anything.

    Subscribing to ``issue_comment`` would otherwise run the whole
    pipeline on every remark anyone makes on any pull request. The
    directive narrows that to comments actually asking for a re-check.

    This is a **noise filter, not a security control**. The comment's
    author is deliberately not consulted: asking the tool to look again
    grants nothing, because the answer comes from
    :func:`evaluate_fork_approval` re-reading the reviews. Anyone may
    therefore ring the doorbell, and it is logged rather than refused.

    Returns:
        ``True`` when the caller should stop, successfully.
    """
    if gh.event_name != "issue_comment":
        return False

    body = _triggering_comment_body(gh)
    if find_open_command(body, CMD_CHECK.name) is not None:
        log.info(
            "🔁 Re-checking PR #%s: a comment asked for it. Authorisation "
            "is re-read from the pull request's reviews, so the request "
            "itself confers nothing.",
            gh.pr_number,
        )
        return False

    log.debug(
        "Comment on PR #%s carries no '%s %s' directive; nothing to do",
        gh.pr_number,
        MENTION_PREFIX,
        CMD_CHECK.name,
    )
    return True


def _recover_pr_metadata(
    gh: GitHubContext, pr_obj: Any | None
) -> GitHubContext:
    """Recover pull request metadata from an already-fetched object.

    A second chance at what :func:`_augment_pr_refs_if_needed` tries
    first. That helper swallows its own failures and returns the
    context unresolved, while the pull request fetched moments later is
    a separate call that may well have succeeded — so the answer can be
    in hand and unused.

    Recovering only the provenance would be worse than recovering
    nothing, because it would resolve the question that decides whether
    to proceed while leaving the refs that decide *where*: an empty
    ``base_ref`` lets target-branch resolution fall back to a default
    branch, preparing a change against the wrong base. So this fills
    everything the object can answer, exactly as the first attempt
    does.
    Each field is recovered independently. A pull request can expose
    its refs while its head repository metadata is missing — a deleted
    fork answers ``null`` for ``head.repo`` — and tying the refs to
    the provenance would leave ``base_ref`` empty in exactly that
    case, so workspace setup would skip the target branch and
    resolution could fall back to a default one. Provenance that
    cannot be established stays unresolved, and therefore gated.

    Refs already present are left alone; the head SHA is replaced
    whenever anything else was missing, because the event's own value
    is the merge or default-branch commit rather than the pull request
    head.

    Returns *gh* unchanged when nothing was missing or the pull
    request cannot answer.
    """
    if pr_obj is None:
        return gh

    env_updates: dict[str, str] = {}
    head_repo = gh.head_repo
    base_ref = gh.base_ref
    head_ref = gh.head_ref
    sha = gh.sha

    if not head_repo:
        found = _head_repo_for_pr(pr_obj)
        if found:
            env_updates["PR_HEAD_REPO"] = head_repo = found
    if not base_ref:
        found = _ref_for_pr(pr_obj, "base")
        if found:
            env_updates["GITHUB_BASE_REF"] = base_ref = found
    if not head_ref:
        found = _ref_for_pr(pr_obj, "head")
        if found:
            env_updates["GITHUB_HEAD_REF"] = head_ref = found

    if not env_updates:
        return gh

    found = str(
        getattr(getattr(pr_obj, "head", object()), "sha", "") or ""
    ).strip()
    if found:
        env_updates["GITHUB_SHA"] = sha = found

    # The orchestrator reads these back out of the environment, so
    # writing them there is what makes the recovery visible to it.
    # The context itself is amended rather than re-read, so a value
    # the caller already held is never lost to an environment that
    # happens to disagree.
    for name, value in env_updates.items():
        os.environ[name] = value
    log.debug(
        "Recovered PR #%s metadata from the fetched pull request: %s",
        gh.pr_number,
        ", ".join(sorted(env_updates)),
    )

    return dataclasses.replace(
        gh,
        head_repo=head_repo,
        base_ref=base_ref,
        head_ref=head_ref,
        sha=sha,
    )


def _recheck_has_nothing_to_unblock(gh: GitHubContext) -> bool:
    """Report whether a re-check run has no gate to lift.

    A re-check exists to notice that a maintainer has authorised a
    gated pull request. A same-repository head never passed through
    the gate, so there is nothing to notice, and proceeding would
    resubmit an unchanged commit every time somebody comments on,
    approves or requests changes to a pull request.

    This matters most for the comment doorbell, which deliberately
    accepts any author on the grounds that asking the tool to look
    again grants nothing. That holds only because the gate answers the
    question — so without this, any commenter could drive an unchanged
    same-repository pull request through the submission pipeline at
    will.

    Uses ``head_is_trusted``, so a pull request whose provenance is
    unresolved is *not* short-circuited: the gate applies to those, and
    skipping them would leave them permanently unable to transfer.
    """
    return gh.event_name in models.RECHECK_EVENTS and gh.head_is_trusted


def _check_fork_approval(
    pr_obj: Any | None,
    gh: GitHubContext,
    progress_tracker: Any = None,
) -> tuple[bool, str]:
    """Report whether a pull request may transfer to Gerrit.

    Same-repository pull requests are unaffected: their head branch
    already implies write access. Everything else — forks, and pull
    requests whose provenance could not be established — requires an
    approving review from a trusted maintainer, bound to the current
    head commit.

    Only a head positively known to live in the base repository skips
    the gate. Unknown provenance does not: `is_fork_pr` answers a
    factual question and reports ``False`` when it cannot tell, whereas
    ``head_is_trusted`` answers the authorisation question and reports
    ``False`` in the same case. Using the latter keeps the gate closed
    when metadata is missing, which is exactly when it matters most.

    The gate applies to unattended runs only. A direct CLI invocation
    is a person acting deliberately with their own credentials, and
    needs no second signature.

    Runs before any fork content is fetched and before the Gerrit key
    is materialised, so a blocked pull request never reaches either.

    Returns:
        ``(allowed, approved_sha)``. ``approved_sha`` is the commit the
        gate authorised, empty when no gate applied. It is returned
        rather than stored globally because bulk runs process several
        pull requests concurrently, where shared state would let one
        worker clear or overwrite another's constraint.

        Blocking is not an error: the pull request is simply waiting
        for a human, and a failed check would misrepresent that.
    """
    if gh.head_is_trusted:
        return True, ""

    if not _is_github_actions_context():
        # Direct CLI invocation. The operator chose this pull request
        # and is using their own credentials, so they are the authority
        # the gate would otherwise be looking for. The gate exists for
        # the unattended path, where the tool acts on a shared identity
        # with nobody watching.
        log.debug(
            "Not running under GitHub Actions; approval gate not applied "
            "to PR #%s",
            gh.pr_number,
        )
        return True, ""

    if pr_obj is None:
        # Fail closed. Reaching here means the pull request could not
        # be resolved, so approval cannot be established either way.
        log.warning(
            "🛑 Pull request #%s not transferred to Gerrit: the tool "
            "could not resolve the pull request to check for approval",
            gh.pr_number,
        )
        return False, ""

    head_sha = str(
        getattr(getattr(pr_obj, "head", None), "sha", "") or ""
    ).strip()
    author = str(
        getattr(getattr(pr_obj, "user", None), "login", "") or ""
    ).strip()

    # Opt-in approver sources are read from the *base* side of the
    # pull request, taken from GitHub's own metadata. Using the head
    # would let a fork nominate the people permitted to authorise it.
    base = getattr(pr_obj, "base", None)
    extra_approvers = resolve_additional_approvers(
        base_repo=getattr(base, "repo", None),
        base_ref=str(getattr(base, "ref", "") or "").strip(),
    )

    status = evaluate_fork_approval(
        pr_obj,
        head_sha=head_sha,
        author_login=author,
        extra_approvers=extra_approvers,
    )

    if status.approved:
        log.info(
            "✅ Pull request #%s authorised: %s",
            gh.pr_number,
            status.reason,
        )
        # The workspace fetch reads refs/pull/<N>/head, which the
        # contributor can move after this check. Carry what was
        # approved so the fetch can refuse anything else.
        _clear_fork_approval_notice(pr_obj, status, head_sha)
        return True, head_sha

    log.warning(
        "🛑 Pull request #%s not transferred to Gerrit: %s. "
        "Reviews count from: %s",
        gh.pr_number,
        status.reason,
        describe_approver_policy(),
    )
    safe_console_print(
        f"🛑 Awaiting maintainer approval: {status.reason}",
        style="yellow",
        progress_tracker=progress_tracker,
    )

    _post_fork_approval_notice(pr_obj, status, head_sha)
    return False, ""


def _edit_owned_marker_comment(pr_obj: Any, body: str) -> bool:
    """Replace the tool's own approval notice, if one exists.

    The marker is not proof of authorship — anyone may paste it into a
    comment. Ownership is established by *attempting the edit*: the API
    refuses to edit another user's comment, so a failure means the
    comment was not ours and the search continues.

    Returns:
        ``True`` when a notice of ours was updated.
    """
    try:
        issue = pr_obj.as_issue()
        comments = list(issue.get_comments())
    except Exception as exc:
        log.debug("Could not read comments for approval notice: %s", exc)
        return False

    # Newest first: if several carry the marker, keep the most recent.
    for comment in reversed(comments):
        if APPROVAL_MARKER not in (getattr(comment, "body", "") or ""):
            continue
        try:
            comment.edit(body)
        except Exception as exc:
            log.debug(
                "Could not edit comment carrying the approval marker; "
                "it is probably not ours: %s",
                exc,
            )
            continue
        else:
            return True
    return False


def _clear_fork_approval_notice(
    pr_obj: Any,
    status: ApprovalStatus,
    head_sha: str,
) -> None:
    """Retract an earlier block notice once approval arrives.

    Only edits an existing notice; it does not create one. A pull
    request that was never blocked has nothing to retract, and adding a
    comment to say so would be noise.

    Best-effort: failing to tidy a comment must not stop a transfer
    that has been authorised.
    """
    if env_bool("CI_TESTING", False):
        return
    _edit_owned_marker_comment(
        pr_obj, render_cleared_comment(status, head_sha=head_sha)
    )


def _post_fork_approval_notice(
    pr_obj: Any,
    status: ApprovalStatus,
    head_sha: str,
) -> None:
    """Explain the block on the pull request, editing any prior notice.

    Best-effort throughout: a comment API failure must never turn a
    block into a crash, nor a block into a pass.
    """
    if env_bool("CI_TESTING", False):
        return

    body = render_blocked_comment(status, head_sha=head_sha)
    if _edit_owned_marker_comment(pr_obj, body):
        return

    try:
        create_pr_comment(pr_obj, body)
    except Exception as exc:
        log.debug("Could not post fork approval notice: %s", exc)


def _extract_and_display_pr_info(
    gh: GitHubContext,
    data: Inputs,
    progress_tracker: Any = None,
) -> Any | None:
    """Extract PR information and display it with Rich formatting.

    Returns the pull request object when one was fetched, so callers
    can reuse it instead of making a further API call.
    """
    if not gh.pr_number:
        return None

    try:
        # Get GitHub token from inputs if available, fallback to environment
        token = ""
        if hasattr(data, "github_token") and data.github_token:
            token = data.github_token
        else:
            token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            safe_console_print(
                "⚠️  No GITHUB_TOKEN available - skipping PR info display",
                style="yellow",
                progress_tracker=progress_tracker,
            )
            return None

        client = build_client(token)
        repo = get_repo_from_env(client)
        pr_obj = get_pull(repo, int(gh.pr_number))

        # Check PR state and exit if not processable
        pr_state = getattr(pr_obj, "state", "unknown")
        if pr_state != "open":
            _exit_for_pr_state_error(gh.pr_number, pr_state)

        # Check if automation_only is enabled and reject non-automation PRs
        _check_automation_only(pr_obj, gh, progress_tracker)

        title, _body = get_pr_title_body(pr_obj)

        pr_info = {
            "Repository": gh.repository,
            "PR Number": gh.pr_number,
            "Title": title or "No title",
            "Author": getattr(getattr(pr_obj, "user", None), "login", "Unknown")
            or "Unknown",
            "Base Branch": gh.base_ref or "unknown",
            "SHA": gh.sha or "unknown",
            "URL": f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}",
        }

        # Add file changes count if available
        try:
            files = list(getattr(pr_obj, "get_files", list)())
            pr_info["Files Changed"] = len(files)
        except Exception:
            pr_info["Files Changed"] = "unknown"

        # Display the PR information
        display_pr_info(
            pr_info, "Pull Request Details", progress_tracker=progress_tracker
        )

    except GitHub2GerritError:
        # Let our structured errors propagate
        raise
    except Exception as exc:
        log.debug("Failed to display PR info: %s", exc)
        if "404" in str(exc) or "Not Found" in str(exc):
            _exit_for_pr_not_found(gh.pr_number, gh.repository)
        else:
            _exit_for_pr_fetch_error(exc)
    else:
        return pr_obj
    return None


class ConfigurationError(Exception):
    """Raised when configuration validation fails.

    This custom exception is used instead of typer.BadParameter to provide
    cleaner error messages to end users without exposing Python tracebacks.
    When caught, it displays user-friendly messages prefixed with
    "Configuration validation failed:" rather than raw exception details.
    """


def _parse_target_url(url: str) -> TargetURL:
    """
    Parse a GitHub or Gerrit URL into a type-safe result.

    Args:
        url: GitHub PR/repo URL or Gerrit change URL

    Returns:
        One of:
        - GitHubPRTarget: For GitHub PR URLs (owner, repo, pr_number)
        - GitHubRepoTarget: For GitHub repo URLs without PR (owner, repo)
        - GerritChangeTarget: For Gerrit change URLs (change_url)
    """
    # Check if it's a Gerrit change URL using shared pattern constant
    if re.match(GERRIT_CHANGE_URL_PATTERN, url):
        return GerritChangeTarget(change_url=url)

    # Otherwise, parse as GitHub URL
    return _parse_github_target(url)


def _parse_github_target(url: str) -> GitHubPRTarget | GitHubRepoTarget:
    """
    Parse a GitHub repository or pull request URL.

    Args:
        url: GitHub URL to parse

    Returns:
        GitHubPRTarget if URL contains a PR number, otherwise GitHubRepoTarget
    """
    try:
        u = urlparse(url)
    except Exception:
        return GitHubRepoTarget(owner=None, repo=None)

    allow_ghe = env_bool("ALLOW_GHE_URLS", False)
    bad_hosts = {
        "gitlab.com",
        "www.gitlab.com",
        "bitbucket.org",
        "www.bitbucket.org",
    }
    if u.netloc in bad_hosts:
        return GitHubRepoTarget(owner=None, repo=None)
    if not allow_ghe and u.netloc not in ("github.com", "www.github.com"):
        return GitHubRepoTarget(owner=None, repo=None)

    parts = [p for p in (u.path or "").split("/") if p]
    if len(parts) < 2:
        return GitHubRepoTarget(owner=None, repo=None)

    owner, repo = parts[0], parts[1]

    if len(parts) >= 4 and parts[2] in ("pull", "pulls"):
        try:
            pr_number = int(parts[3])
            return GitHubPRTarget(owner=owner, repo=repo, pr_number=pr_number)
        except Exception as exc:
            log.debug("Failed to parse PR number from URL: %s", exc)

    # Return repo target (may have None pr_number for compatibility)
    return GitHubRepoTarget(owner=owner, repo=repo)


APP_NAME = "github2gerrit"


# Error message constants to comply with TRY003
_MSG_MISSING_REQUIRED_INPUT = "Missing required input: {field_name}"
_MSG_INVALID_FETCH_DEPTH = "FETCH_DEPTH must be a positive integer"
_MSG_ISSUE_ID_MULTILINE = "Issue ID must be single line"


def _resolve_issue_id_from_json(json_str: str, github_actor: str) -> str:
    """
    Resolve Issue-ID from JSON lookup table.

    Args:
        json_str: JSON array with format
            [{"key": "username", "value": "ISSUE-ID"}]
        github_actor: GitHub username to lookup

    Returns:
        Resolved Issue-ID or empty string if not found/invalid
    """
    if not json_str or not github_actor:
        return ""

    try:
        lookup_data = json.loads(json_str)

        if not isinstance(lookup_data, list):
            log.warning(
                "⚠️ Warning: Issue-ID JSON was not valid (expected array)"
            )
            print("⚠️ Warning: Issue-ID JSON was not valid")
            return ""

        # Search for matching key
        for entry in lookup_data:
            if not isinstance(entry, dict):
                continue
            if entry.get("key") == github_actor:
                issue_id = entry.get("value", "")
                if issue_id:
                    log.debug(
                        "Resolved Issue-ID from JSON lookup: %s -> %s",
                        github_actor,
                        issue_id,
                    )
                    return str(issue_id)

        # No match found - return empty string
        log.debug(
            "No Issue-ID found in JSON lookup for GitHub actor: %s",
            github_actor,
        )

    except json.JSONDecodeError as exc:
        log.warning(
            "⚠️ Warning: Issue-ID JSON was not valid (parse error: %s)",
            exc,
        )
        print("⚠️ Warning: Issue-ID JSON was not valid")
    except Exception as exc:
        log.warning(
            "⚠️ Warning: Issue-ID JSON lookup failed: %s",
            exc,
        )

    return ""


def _print_version_banner() -> None:
    """Print the resolved package version, or a fallback notice.

    Defined as a function so the ``print`` calls do not execute at module
    scope during import; the banner is only emitted when explicitly invoked.
    """
    try:
        app_version = get_version("github2gerrit")
        print(f"🏷️  github2gerrit version {app_version}")
    # The banner is cosmetic: a missing or unreadable package version
    # must never block CLI startup.
    # aislop-ignore-next-line swallowed-exception
    except Exception as exc:
        print(f"⚠️  github2gerrit version information not available: {exc}")


# Show version information when --help is used or in GitHub Actions mode
if "--help" in sys.argv or _is_github_actions_context():
    _print_version_banner()

app: typer.Typer = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help=(
        "Tool to convert GitHub pull requests into Gerrit changes "
        "and close merged PRs"
    ),
)


def _resolve_org(default_org: str | None) -> str:
    if default_org:
        return default_org
    gh_owner = os.getenv("GITHUB_REPOSITORY_OWNER")
    if gh_owner:
        return gh_owner
    # Fallback to empty string for compatibility with existing action
    return ""


if TYPE_CHECKING:
    F = TypeVar("F", bound=Callable[..., object])

    # Typed stub so basedpyright treats @typed_app_command as a typed
    # decorator. The body raises rather than using an ellipsis so static
    # analysers do not model it as a procedure that returns None; this
    # branch is never executed at runtime.
    def typed_app_command(*args: object, **kwargs: object) -> Callable[[F], F]:
        raise NotImplementedError
else:
    typed_app_command = app.command


def _save_derived_parameters_after_success(data: Inputs) -> None:
    """Save derived parameters to config file after successful Gerrit
    submission."""
    try:
        org_for_cfg = (
            data.organization
            or os.getenv("ORGANIZATION")
            or os.getenv("GITHUB_REPOSITORY_OWNER")
        )

        if not org_for_cfg:
            log.debug("No organization available for derived parameter saving")
            return

        # Load current config to check what needs derivation
        cfg = load_org_config(org_for_cfg)

        # Apply derivation with saving enabled to capture newly derived
        # parameters.  The result is written to the configuration file
        # only: this runs after submission and never calls
        # apply_config_to_env, so nothing derived here reaches the
        # process environment and recording it as derived provenance
        # would describe environment values this call did not set.
        repository = os.getenv("GITHUB_REPOSITORY", "")
        apply_parameter_derivation(
            cfg,
            org_for_cfg,
            repository=repository,
            save_to_config=True,
            mark_derived=False,
        )

        log.debug(
            "Derived parameters saved to configuration after successful "
            "Gerrit submission for organization '%s'",
            org_for_cfg,
        )
    except Exception as exc:
        log.warning(
            "Failed to save derived parameters after successful submission: %s",
            exc,
        )


def _maybe_exit_for_version_or_disabled(version_flag: bool) -> None:
    """Handle the --version flag and the G2G_DISABLED short-circuit."""
    if version_flag:
        try:
            app_version = get_version("github2gerrit")
            typer.echo(f"github2gerrit version {app_version}")
        except Exception:
            typer.echo("Version information not available")
        sys.exit(int(ExitCode.SUCCESS))

    # Check if GitHub2Gerrit is disabled via environment variable
    if env_bool("G2G_DISABLED"):
        typer.echo(
            "\U0001f6d1 GitHub2Gerrit is disabled by check of "
            "G2G_DISABLED variable"
        )
        sys.exit(int(ExitCode.SUCCESS))


def _resolve_bool_override(
    ctx: typer.Context, param_name: str, env_var: str, current: bool
) -> bool:
    """Return *current* if the CLI flag was explicit, else parse env.

    This ensures that string "false" from GitHub Actions is handled
    correctly (Typer/Click treats any non-empty string as truthy). The
    env-var override is only applied when the parameter was NOT explicitly
    provided on the command line, so CLI flags always take precedence.
    """
    source = ctx.get_parameter_source(param_name)  # pyright: ignore[reportAttributeAccessIssue]
    # Compare by member name rather than by identity with a
    # ParameterSource member. typer vendors its own copy of click under
    # typer._click from 0.26 onward, so there ctx.get_parameter_source
    # returns a typer._click.core.ParameterSource member, which never
    # compares equal to the click.core.ParameterSource member of the
    # same name. Below 0.26 typer uses the installed click and the two
    # do match, which is why this arrived as a typer upgrade rather than
    # a code change. Matching on .name is correct under both.
    if source is not None and source.name == "COMMANDLINE":
        return current
    env_val = os.getenv(env_var)
    if env_val is not None:
        return parse_bool_env(env_val)
    return current


def _apply_bool_env_overrides(
    ctx: typer.Context, options: dict[str, tuple[bool, str]]
) -> None:
    """Resolve boolean CLI/env overrides and normalise them into env.

    ``options`` maps each snake_case parameter name to a ``(value, env_var)``
    tuple, where ``value`` is what Typer parsed and ``env_var`` is the
    environment variable that backs it. The env var is passed explicitly
    rather than assumed to be the upper-case parameter name, because
    several options use prefixed variables (e.g. verbose uses G2G_VERBOSE).
    """
    for param_name, (current, env_var) in options.items():
        resolved = _resolve_bool_override(ctx, param_name, env_var, current)
        os.environ[env_var] = "true" if resolved else "false"


def _apply_netrc_options(
    no_netrc: bool,
    netrc_file: Path | None,
    netrc_optional: bool,
    gerrit_server: str,
) -> None:
    """Store netrc options in env and load credentials when enabled."""
    os.environ["G2G_NO_NETRC"] = "true" if no_netrc else "false"
    if netrc_file:
        os.environ["G2G_NETRC_FILE"] = str(netrc_file)
    os.environ["G2G_NETRC_OPTIONAL"] = "true" if netrc_optional else "false"

    if no_netrc:
        return
    gerrit_host = gerrit_server or os.getenv("GERRIT_SERVER", "")
    if not gerrit_host:
        return
    try:
        netrc_creds = get_credentials_for_host(
            host=gerrit_host,
            netrc_file=netrc_file,
            use_netrc=True,
            netrc_optional=netrc_optional,
        )
        if netrc_creds:
            # Set HTTP credentials from netrc if not already set
            if not os.getenv("GERRIT_HTTP_USER"):
                os.environ["GERRIT_HTTP_USER"] = netrc_creds.login
            if not os.getenv("GERRIT_HTTP_PASSWORD"):
                os.environ["GERRIT_HTTP_PASSWORD"] = netrc_creds.password
            log.debug(
                "Loaded Gerrit HTTP credentials for %s from .netrc",
                gerrit_host,
            )
    except FileNotFoundError:
        if not netrc_optional:
            safe_typer_echo("❌ No .netrc file found and --netrc-required set")
            sys.exit(int(ExitCode.CONFIGURATION_ERROR))
    except NetrcParseError as e:
        safe_typer_echo(f"❌ Failed to parse .netrc file: {e}")
        sys.exit(int(ExitCode.CONFIGURATION_ERROR))


def _configure_logging_and_progress(verbose: bool, show_progress: bool) -> None:
    """Configure logging verbosity and progress-related environment."""
    if verbose:
        os.environ["G2G_LOG_LEVEL"] = "DEBUG"
        _reconfigure_logging()

    setup_rich_aware_logging()

    if _is_github_actions_context():
        try:
            app_version = get_version("github2gerrit")
            log.debug("github2gerrit version %s", app_version)
        except Exception as exc:
            log.warning("Version information not available: %s", exc)

    # Show initial progress if Rich is available and progress is enabled
    if show_progress and not RICH_AVAILABLE:
        safe_console_print(
            "📋 Rich formatting not available - progress will be shown as "
            "simple text..."
        )

    # Store progress flag in environment for use by processing functions
    os.environ["G2G_SHOW_PROGRESS"] = "true" if show_progress else "false"


def _export_general_options(
    fetch_depth: int,
    organization: str | None,
    reviewers_email: str,
    normalise_commit: bool,
    allow_ghe_urls: bool,
) -> None:
    """Normalise general scalar CLI options into the process environment."""
    os.environ["FETCH_DEPTH"] = str(fetch_depth)
    resolved_org = _resolve_org(organization)
    if resolved_org:
        os.environ["ORGANIZATION"] = resolved_org
    if reviewers_email:
        os.environ["REVIEWERS_EMAIL"] = reviewers_email
    os.environ["NORMALISE_COMMIT"] = "true" if normalise_commit else "false"
    os.environ["ALLOW_GHE_URLS"] = "true" if allow_ghe_urls else "false"


def _export_gerrit_ssh_options(
    gerrit_known_hosts: str,
    gerrit_ssh_privkey_g2g: str,
    gerrit_ssh_user_g2g: str,
    gerrit_ssh_user_g2g_email: str,
) -> None:
    """Export Gerrit SSH-related CLI options into the environment."""
    if gerrit_known_hosts:
        os.environ["GERRIT_KNOWN_HOSTS"] = gerrit_known_hosts
    if gerrit_ssh_privkey_g2g:
        os.environ["GERRIT_SSH_PRIVKEY_G2G"] = gerrit_ssh_privkey_g2g
    if gerrit_ssh_user_g2g:
        os.environ["GERRIT_SSH_USER_G2G"] = gerrit_ssh_user_g2g
    if gerrit_ssh_user_g2g_email:
        os.environ["GERRIT_SSH_USER_G2G_EMAIL"] = gerrit_ssh_user_g2g_email


def _export_gerrit_server_options(
    gerrit_server: str, gerrit_server_port: int, gerrit_project: str
) -> None:
    """Export Gerrit server-location CLI options into the environment."""
    if gerrit_server:
        os.environ["GERRIT_SERVER"] = gerrit_server
    if gerrit_server_port:
        os.environ["GERRIT_SERVER_PORT"] = str(gerrit_server_port)
    if gerrit_project:
        os.environ["GERRIT_PROJECT"] = gerrit_project


def _export_issue_and_commit_options(
    issue_id: str,
    issue_id_lookup_json: str,
    github_actor: str,
    commit_rules_json: str,
) -> None:
    """Resolve the Issue-ID and export commit-message related options."""
    # Handle Issue-ID lookup from JSON if provided
    resolved_issue_id = issue_id
    if not resolved_issue_id and issue_id_lookup_json:
        resolved_issue_id = _resolve_issue_id_from_json(
            issue_id_lookup_json, github_actor
        )
    if resolved_issue_id:
        os.environ["ISSUE_ID"] = resolved_issue_id

    # Always set COMMIT_RULES_JSON so passing an empty string via CLI
    # clears any pre-existing env var rather than leaving stale rules.
    os.environ["COMMIT_RULES_JSON"] = commit_rules_json or ""


def _export_flag_options(
    close_merged_prs: bool,
    force: bool,
    duplicate_types: str,
    reuse_strategy: str,
) -> None:
    """Export closure/duplicate related CLI options into the environment."""
    os.environ["CLOSE_MERGED_PRS"] = "true" if close_merged_prs else "false"
    os.environ["FORCE"] = "true" if force else "false"
    os.environ["DUPLICATE_TYPES"] = duplicate_types
    if reuse_strategy:
        os.environ["REUSE_STRATEGY"] = reuse_strategy


def _apply_similarity_options(
    similarity_subject: float, similarity_update_factor: float
) -> None:
    """Validate similarity thresholds and normalise them into env."""
    if not (0.0 <= similarity_subject <= 1.0):
        msg = (
            f"similarity_subject must be between 0.0 and 1.0, "
            f"got {similarity_subject}"
        )
        raise typer.BadParameter(msg)
    if not (0.0 <= similarity_update_factor <= 1.0):
        msg = (
            f"similarity_update_factor must be between 0.0 and 1.0, "
            f"got {similarity_update_factor}"
        )
        raise typer.BadParameter(msg)

    os.environ["SIMILARITY_SUBJECT"] = str(similarity_subject)
    os.environ["SIMILARITY_UPDATE_FACTOR"] = str(similarity_update_factor)


def _apply_target_url_to_env(target_url: str | None) -> None:
    """Parse a target URL argument and export the derived env vars."""
    if not target_url:
        return

    parsed = _parse_target_url(target_url)

    if isinstance(parsed, GerritChangeTarget):
        # Gerrit change URL - respect user's close_merged_prs flag
        log.debug("Parsed Gerrit change URL: %s", parsed.change_url)
        # Note: CLOSE_MERGED_PRS was already set based on user flag
        # earlier. We respect that setting instead of forcing it to true:
        #   - true (default): Close the GitHub PR
        #   - false: Add comment to PR but leave it open
        os.environ["G2G_GERRIT_CHANGE_URL"] = parsed.change_url
        log.debug(
            "Gerrit change URL mode with CLOSE_MERGED_PRS=%s",
            os.environ.get("CLOSE_MERGED_PRS", "true"),
        )
        url_type = "gerrit_change"
    elif isinstance(parsed, GitHubPRTarget):
        # GitHub PR URL
        log.debug(
            "Parsed GitHub PR URL: owner=%s, repo=%s, pr_number=%s",
            parsed.owner,
            parsed.repo,
            parsed.pr_number,
        )
        if parsed.owner:
            os.environ["ORGANIZATION"] = parsed.owner
            log.debug("Set ORGANIZATION=%s", parsed.owner)
        if parsed.owner and parsed.repo:
            github_repo = f"{parsed.owner}/{parsed.repo}"
            os.environ["GITHUB_REPOSITORY"] = github_repo
            log.debug("Set GITHUB_REPOSITORY=%s", github_repo)
        if parsed.pr_number:
            os.environ["PR_NUMBER"] = str(parsed.pr_number)
            os.environ["SYNC_ALL_OPEN_PRS"] = "false"
            log.debug("Set PR_NUMBER=%s", parsed.pr_number)
        else:
            os.environ["SYNC_ALL_OPEN_PRS"] = "true"
            log.debug("Set SYNC_ALL_OPEN_PRS=true")
        url_type = "github_pr"
    else:  # GitHubRepoTarget
        # GitHub repo URL (no PR)
        log.debug(
            "Parsed GitHub repo URL: owner=%s, repo=%s",
            parsed.owner,
            parsed.repo,
        )
        if parsed.owner:
            os.environ["ORGANIZATION"] = parsed.owner
            log.debug("Set ORGANIZATION=%s", parsed.owner)
        if parsed.owner and parsed.repo:
            github_repo = f"{parsed.owner}/{parsed.repo}"
            os.environ["GITHUB_REPOSITORY"] = github_repo
            log.debug("Set GITHUB_REPOSITORY=%s", github_repo)
        os.environ["SYNC_ALL_OPEN_PRS"] = "true"
        log.debug("Set SYNC_ALL_OPEN_PRS=true")
        url_type = "github_repo"

    # Store the target URL in env for downstream use
    # Note: This stores the actual URL string (e.g., "https://github.com/...")
    # All downstream code uses boolean checks (truthy/falsy), so this works
    # correctly and provides better debugging/logging than storing "1"
    os.environ["G2G_TARGET_URL"] = target_url
    os.environ["G2G_TARGET_URL_TYPE"] = url_type


def _log_startup_environment() -> None:
    """Emit a debug snapshot of key environment variables at startup."""
    log.debug("CLI startup environment check:")
    for key in ["DRY_RUN", "CI_TESTING", "GERRIT_SERVER", "GERRIT_PROJECT"]:
        value = os.environ.get(key, "NOT_SET")
        log.debug("  %s = %s", key, value)


def _run_processing() -> None:
    """Delegate to the common processing path with structured handling."""
    try:
        _process()
    except GitHub2GerritError as exc:
        # Our structured errors handle display and exit themselves
        exc.display_and_exit()
    except (KeyboardInterrupt, SystemExit, typer.Exit):
        # Don't catch system interrupts or exits
        raise
    except (OrchestratorError, DuplicateChangeError, ConfigurationError) as exc:
        # Convert known errors to centralized error handling
        if isinstance(exc, OrchestratorError):
            converted_error = convert_orchestrator_error(exc)
        elif isinstance(exc, DuplicateChangeError):
            converted_error = convert_duplicate_error(exc)
        else:  # ConfigurationError
            converted_error = convert_configuration_error(exc)
        converted_error.display_and_exit()
    except Exception as exc:
        log.debug("main(): _process failed: %s", exc)
        exit_with_error(
            ExitCode.GENERAL_ERROR,
            message="❌ Operation failed; check logs for details",
            exception=exc,
        )


@typed_app_command()
def main(
    ctx: typer.Context,
    target_url: str | None = typer.Argument(
        None,
        help="GitHub PR/repo URL or Gerrit change URL",
        metavar="TARGET_URL",
    ),
    allow_duplicates: bool = typer.Option(
        True,
        "--allow-duplicates/--no-allow-duplicates",
        envvar="ALLOW_DUPLICATES",
        help="Allow submitting duplicate changes without error.",
    ),
    allow_ghe_urls: bool = typer.Option(
        False,
        "--allow-ghe-urls/--no-allow-ghe-urls",
        envvar="ALLOW_GHE_URLS",
        help="Allow non-github.com GitHub Enterprise URLs in direct URL mode.",
    ),
    allow_orphan_changes: bool = typer.Option(
        False,
        "--allow-orphan-changes/--no-allow-orphan-changes",
        envvar="ALLOW_ORPHAN_CHANGES",
        help="Keep unmatched Gerrit changes without warning.",
    ),
    ci_testing: bool = typer.Option(
        False,
        "--ci-testing/--no-ci-testing",
        envvar="CI_TESTING",
        help="Enable CI testing mode (overrides .gitreview, handles "
        "unrelated repos).",
    ),
    close_merged_prs: bool = typer.Option(
        True,
        "--close-merged-prs/--no-close-merged-prs",
        envvar="CLOSE_MERGED_PRS",
        help="Close GitHub PRs when corresponding Gerrit changes are merged.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        envvar="FORCE",
        help=(
            "Force PR closure regardless of Gerrit change status "
            "(abandoned, etc)."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        envvar="DRY_RUN",
        help="Validate settings and PR metadata; do not write to Gerrit.",
    ),
    duplicate_types: str = typer.Option(
        "open",
        "--duplicate-types",
        envvar="DUPLICATE_TYPES",
        help=(
            "Gerrit change states to evaluate when determining if a change "
            "should be considered a duplicate "
            '(comma-separated). E.g. "open,merged,abandoned". Default: "open".'
        ),
    ),
    fetch_depth: int = typer.Option(
        10,
        "--fetch-depth",
        envvar="FETCH_DEPTH",
        help=(
            "Git history depth. Also the git log window for merged-PR "
            "reconciliation, so lowering it narrows how far back a "
            "merged change is matched."
        ),
    ),
    gerrit_known_hosts: str = typer.Option(
        "",
        "--gerrit-known-hosts",
        envvar="GERRIT_KNOWN_HOSTS",
        help="Known hosts entries for Gerrit SSH (single or multi-line).",
    ),
    gerrit_project: str = typer.Option(
        "",
        "--gerrit-project",
        envvar="GERRIT_PROJECT",
        help="Gerrit project (optional; .gitreview preferred).",
    ),
    gerrit_server: str = typer.Option(
        "",
        "--gerrit-server",
        envvar="GERRIT_SERVER",
        help="Gerrit server hostname (optional; .gitreview preferred).",
    ),
    gerrit_server_port: int = typer.Option(
        29418,
        "--gerrit-server-port",
        envvar="GERRIT_SERVER_PORT",
        help="Gerrit SSH port (default: 29418).",
    ),
    gerrit_ssh_privkey_g2g: str = typer.Option(
        "",
        "--gerrit-ssh-privkey-g2g",
        envvar="GERRIT_SSH_PRIVKEY_G2G",
        help="SSH private key content used to authenticate to Gerrit.",
    ),
    gerrit_ssh_user_g2g: str = typer.Option(
        "",
        "--gerrit-ssh-user-g2g",
        envvar="GERRIT_SSH_USER_G2G",
        help="Gerrit SSH username (e.g. automation bot account).",
    ),
    gerrit_ssh_user_g2g_email: str = typer.Option(
        "",
        "--gerrit-ssh-user-g2g-email",
        envvar="GERRIT_SSH_USER_G2G_EMAIL",
        help="Email address for the Gerrit SSH user.",
    ),
    issue_id: str = typer.Option(
        "",
        "--issue-id",
        envvar="ISSUE_ID",
        help="Issue ID to include in commit message (e.g., Issue-ID: ABC-123).",
    ),
    issue_id_lookup_json: str = typer.Option(
        "",
        "--issue-id-lookup-json",
        envvar="ISSUE_ID_LOOKUP_JSON",
        help=(
            "JSON array mapping GitHub actors to Issue IDs "
            "for automatic lookup."
        ),
    ),
    commit_rules_json: str = typer.Option(
        "",
        "--commit-rules",
        envvar="COMMIT_RULES_JSON",
        help=(
            "JSON object defining commit message rules with per-project "
            "and per-actor overrides. Supports arbitrary key-value lines "
            "placed in the commit body or trailer block."
        ),
    ),
    log_reconcile_json: bool = typer.Option(
        True,
        "--log-reconcile-json/--no-log-reconcile-json",
        envvar="LOG_RECONCILE_JSON",
        help="Emit structured JSON reconciliation summary.",
    ),
    normalise_commit: bool = typer.Option(
        True,
        "--normalise-commit/--no-normalise-commit",
        envvar="NORMALISE_COMMIT",
        help="Normalize commit messages to conventional commit format.",
    ),
    organization: str | None = typer.Option(
        None,
        "--organization",
        envvar="ORGANIZATION",
        help=("Organization (defaults to GITHUB_REPOSITORY_OWNER when unset)."),
    ),
    persist_single_mapping_comment: bool = typer.Option(
        True,
        "--persist-single-mapping-comment/--no-persist-single-mapping-comment",
        envvar="PERSIST_SINGLE_MAPPING_COMMENT",
        help="Replace existing mapping comment instead of appending.",
    ),
    preserve_github_prs: bool = typer.Option(
        True,
        "--preserve-github-prs/--no-preserve-github-prs",
        envvar="PRESERVE_GITHUB_PRS",
        help="Do not close GitHub PRs after pushing to Gerrit.",
    ),
    reuse_strategy: str = typer.Option(
        "topic+comment",
        "--reuse-strategy",
        envvar="REUSE_STRATEGY",
        help="Strategy for reusing Change-IDs: topic, comment, "
        "topic+comment, none.",
    ),
    reviewers_email: str = typer.Option(
        "",
        "--reviewers-email",
        envvar="REVIEWERS_EMAIL",
        help="Email(s) of reviewers (comma-separated).",
    ),
    github_actor: str = typer.Option(
        "",
        "--github-actor",
        envvar="GITHUB_ACTOR",
        help="GitHub actor (username) who triggered the workflow.",
    ),
    show_progress: bool = typer.Option(
        True,
        "--progress/--no-progress",
        envvar="G2G_SHOW_PROGRESS",
        help="Show real-time progress updates with Rich formatting.",
    ),
    similarity_files: bool = typer.Option(
        False,
        "--similarity-files/--no-similarity-files",
        envvar="SIMILARITY_FILES",
        help="Require exact file signature match for reconciliation.",
    ),
    similarity_subject: float = typer.Option(
        0.7,
        "--similarity-subject",
        envvar="SIMILARITY_SUBJECT",
        help="Subject token Jaccard similarity threshold (0.0-1.0).",
    ),
    similarity_update_factor: float = typer.Option(
        0.75,
        "--similarity-update-factor",
        envvar="SIMILARITY_UPDATE_FACTOR",
        help=(
            "Multiplier for similarity threshold on UPDATE operations "
            "(0.0-1.0). Applied as threshold * factor."
        ),
    ),
    submit_single_commits: bool = typer.Option(
        False,
        "--submit-single-commits",
        envvar="SUBMIT_SINGLE_COMMITS",
        help="Submit one commit at a time to the Gerrit repository.",
    ),
    use_pr_as_commit: bool = typer.Option(
        False,
        "--use-pr-as-commit",
        envvar="USE_PR_AS_COMMIT",
        help="Use PR title and body as the commit message.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        envvar="G2G_VERBOSE",
        help="Verbose output (enables DEBUG logging including Rich displays).",
    ),
    version_flag: bool = typer.Option(
        False,
        "--version",
        help="Show version and exit.",
    ),
    create_missing: bool = typer.Option(
        False,
        "--create-missing/--no-create-missing",
        envvar="CREATE_MISSING",
        help=(
            "Create a Gerrit change when an UPDATE operation cannot find "
            "an existing one. Also triggered by '@github2gerrit create "
            "missing change' PR comment."
        ),
    ),
    automation_only: bool = typer.Option(
        True,
        "--automation-only/--no-automation-only",
        envvar="AUTOMATION_ONLY",
        help="Accept pull requests from known automation tools.",
    ),
    no_netrc: bool = typer.Option(
        False,
        "--no-netrc",
        help="Disable .netrc credential lookup for Gerrit HTTP authentication",
        envvar="G2G_NO_NETRC",
    ),
    netrc_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--netrc-file",
        help="Explicit path to .netrc file for Gerrit HTTP credentials",
        envvar="G2G_NETRC_FILE",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    netrc_optional: bool = typer.Option(
        True,
        "--netrc-optional/--netrc-required",
        help="Whether to fail if .netrc file is not found (default: optional)",
        envvar="G2G_NETRC_OPTIONAL",
    ),
) -> None:
    """
    Tool to convert GitHub pull requests into Gerrit changes

    - GitHub PR URL: creates a Gerrit change from the given pull request
    - Gerrit Change URL: finds and closes the source GitHub pull request
    - GitHub Repo URL: converts all open pull requests to Gerrit changes
    - No arguments: environment variables determine behaviour (CI/CD mode)
    """

    _maybe_exit_for_version_or_disabled(version_flag)

    _apply_bool_env_overrides(
        ctx,
        {
            "submit_single_commits": (
                submit_single_commits,
                "SUBMIT_SINGLE_COMMITS",
            ),
            "use_pr_as_commit": (use_pr_as_commit, "USE_PR_AS_COMMIT"),
            "preserve_github_prs": (
                preserve_github_prs,
                "PRESERVE_GITHUB_PRS",
            ),
            "dry_run": (dry_run, "DRY_RUN"),
            "allow_duplicates": (allow_duplicates, "ALLOW_DUPLICATES"),
            "ci_testing": (ci_testing, "CI_TESTING"),
            "similarity_files": (similarity_files, "SIMILARITY_FILES"),
            "allow_orphan_changes": (
                allow_orphan_changes,
                "ALLOW_ORPHAN_CHANGES",
            ),
            "persist_single_mapping_comment": (
                persist_single_mapping_comment,
                "PERSIST_SINGLE_MAPPING_COMMENT",
            ),
            "log_reconcile_json": (log_reconcile_json, "LOG_RECONCILE_JSON"),
            "create_missing": (create_missing, "CREATE_MISSING"),
            "automation_only": (automation_only, "AUTOMATION_ONLY"),
        },
    )

    _apply_netrc_options(no_netrc, netrc_file, netrc_optional, gerrit_server)
    _configure_logging_and_progress(verbose, show_progress)

    _export_general_options(
        fetch_depth,
        organization,
        reviewers_email,
        normalise_commit,
        allow_ghe_urls,
    )
    _export_gerrit_ssh_options(
        gerrit_known_hosts,
        gerrit_ssh_privkey_g2g,
        gerrit_ssh_user_g2g,
        gerrit_ssh_user_g2g_email,
    )
    _export_gerrit_server_options(
        gerrit_server, gerrit_server_port, gerrit_project
    )
    _export_issue_and_commit_options(
        issue_id, issue_id_lookup_json, github_actor, commit_rules_json
    )
    _export_flag_options(
        close_merged_prs, force, duplicate_types, reuse_strategy
    )

    _apply_similarity_options(similarity_subject, similarity_update_factor)
    _apply_target_url_to_env(target_url)
    _log_startup_environment()
    _run_processing()


def _setup_logging() -> logging.Logger:
    level_name = os.getenv("G2G_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    fmt = (
        "%(asctime)s %(levelname)-8s %(name)s %(filename)s:%(lineno)d | "
        "%(message)s"
    )
    logging.basicConfig(level=level, format=fmt)
    return logging.getLogger(APP_NAME)


def _reconfigure_logging() -> None:
    """Reconfigure logging level based on current environment variables."""
    level_name = os.getenv("G2G_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.getLogger().setLevel(level)
    for handler in logging.getLogger().handlers:
        handler.setLevel(level)


log = _setup_logging()


def _convert_and_raise_structured_error(exc: Exception) -> NoReturn:
    """Convert a known structured error and re-raise it.

    Maps OrchestratorError, DuplicateChangeError and ConfigurationError to
    their centralized equivalents before re-raising, preserving the
    original exception as the cause.
    """
    if isinstance(exc, OrchestratorError):
        converted_error = convert_orchestrator_error(exc)
    elif isinstance(exc, DuplicateChangeError):
        converted_error = convert_duplicate_error(exc)
    else:  # ConfigurationError
        converted_error = convert_configuration_error(exc)
    raise converted_error from exc


def _record_change_tracker_result(
    progress_tracker: Any, result: SubmissionResult
) -> None:
    """Update the progress tracker based on submitted vs updated changes."""
    if not (progress_tracker and result.change_urls):
        return
    if any("new" in url.lower() for url in result.change_urls):
        progress_tracker.change_submitted()
    else:
        progress_tracker.change_updated()


def _run_gerrit_cleanup_tasks(
    data: Inputs,
    gh: GitHubContext,
    *,
    no_gerrit: bool,
    gerrit_cleanup_info_log: bool = False,
) -> None:
    """Run abandoned-PR and Gerrit cleanup sweeps when enabled.

    Both sweeps are skipped when ``no_gerrit`` is set (there is no Gerrit
    server to query). ``gerrit_cleanup_info_log`` selects the log level of
    the Gerrit-cleanup banner (info vs debug) to preserve the original
    per-call-site logging.
    """
    if FORCE_ABANDONED_CLEANUP and not no_gerrit:
        try:
            log.debug("Running abandoned PR cleanup...")
            if gh.repository and "/" in gh.repository:
                owner, repo = gh.repository.split("/", 1)
                cleanup_abandoned_prs_bulk(
                    owner=owner,
                    repo=repo,
                    dry_run=data.dry_run,
                    progress_tracker=None,
                    close_merged_prs=env_bool("CLOSE_MERGED_PRS", True),
                )
        except Exception as exc:
            log.warning("Abandoned PR cleanup failed: %s", exc)

    if FORCE_GERRIT_CLEANUP and not no_gerrit:
        try:
            if gerrit_cleanup_info_log:
                log.info("Running Gerrit cleanup for closed GitHub PRs...")
            else:
                log.debug("Running Gerrit cleanup for closed GitHub PRs...")
            if data.gerrit_server and data.gerrit_project:
                cleanup_closed_github_prs(
                    gerrit_server=data.gerrit_server,
                    gerrit_project=data.gerrit_project,
                    dry_run=data.dry_run,
                    progress_tracker=None,
                )
        except Exception as exc:
            log.warning("Gerrit cleanup failed: %s", exc)


def _build_inputs_from_env() -> Inputs:
    return Inputs(
        submit_single_commits=env_bool("SUBMIT_SINGLE_COMMITS", False),
        use_pr_as_commit=env_bool("USE_PR_AS_COMMIT", False),
        fetch_depth=int(env_str("FETCH_DEPTH", "10") or "10"),
        gerrit_known_hosts=env_str("GERRIT_KNOWN_HOSTS"),
        gerrit_ssh_privkey_g2g=env_str("GERRIT_SSH_PRIVKEY_G2G"),
        gerrit_ssh_user_g2g=env_str("GERRIT_SSH_USER_G2G"),
        gerrit_ssh_user_g2g_email=env_str("GERRIT_SSH_USER_G2G_EMAIL"),
        github_token=env_str("GITHUB_TOKEN"),
        organization=env_str(
            "ORGANIZATION", env_str("GITHUB_REPOSITORY_OWNER")
        ),
        reviewers_email=env_str("REVIEWERS_EMAIL", ""),
        preserve_github_prs=env_bool("PRESERVE_GITHUB_PRS", True),
        dry_run=env_bool("DRY_RUN", False),
        normalise_commit=env_bool("NORMALISE_COMMIT", True),
        gerrit_server=env_str("GERRIT_SERVER", ""),
        gerrit_server_port=int(
            env_str("GERRIT_SERVER_PORT", "29418") or "29418"
        ),
        gerrit_project=env_str("GERRIT_PROJECT"),
        issue_id=env_str("ISSUE_ID", ""),
        issue_id_lookup_json=env_str("ISSUE_ID_LOOKUP_JSON", ""),
        commit_rules_json=env_str("COMMIT_RULES_JSON", ""),
        allow_duplicates=env_bool("ALLOW_DUPLICATES", True),
        ci_testing=env_bool("CI_TESTING", False),
        duplicates_filter=env_str("DUPLICATE_TYPES", "open"),
        reuse_strategy=env_str("REUSE_STRATEGY", "topic+comment"),
        similarity_subject=float(env_str("SIMILARITY_SUBJECT", "0.7") or "0.7"),
        similarity_update_factor=float(
            env_str("SIMILARITY_UPDATE_FACTOR", "0.75") or "0.75"
        ),
        similarity_files=env_bool("SIMILARITY_FILES", False),
        allow_orphan_changes=env_bool("ALLOW_ORPHAN_CHANGES", False),
        persist_single_mapping_comment=env_bool(
            "PERSIST_SINGLE_MAPPING_COMMENT", True
        ),
        log_reconcile_json=env_bool("LOG_RECONCILE_JSON", True),
        create_missing=env_bool("CREATE_MISSING", False),
    )


_BulkPrResult = tuple[str, SubmissionResult | None, Exception | None]


@dataclass
class _BulkAggregate:
    """Mutable accumulator for bulk multi-PR processing results."""

    all_urls: list[str] = field(default_factory=list)
    all_nums: list[str] = field(default_factory=list)
    all_shas: list[str] = field(default_factory=list)
    processed_count: int = 0
    succeeded_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0


def _build_bulk_progress_tracker(
    show_progress: bool, target: str
) -> G2GProgressTracker | DummyProgressTracker:
    if show_progress:
        tracker = G2GProgressTracker(target)
        tracker.start()
        tracker.update_operation("🔍 Examining pull requests")
        return tracker
    return DummyProgressTracker("GitHub to Gerrit", target)


def _finish_bulk_no_prs(
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
) -> bool:
    """Emit empty outputs when there are no open PRs to process."""
    log.debug("No open PRs found; skipping processing")
    progress_tracker.update_operation("⏩ No pull requests to process")
    progress_tracker.stop()

    # Still emit outputs (empty values)
    append_github_output(
        {
            "gerrit_change_request_url": "",
            "gerrit_change_request_num": "",
            "gerrit_commit_sha": "",
        }
    )
    return True  # Success (no failures because no work)


def _head_repo_for_pr(pr: Any) -> str:
    """Return the ``owner/repo`` of a PyGithub pull request's head.

    Used in bulk mode, where each pull request carries its own
    provenance rather than inheriting it from the triggering event.
    Returns an empty string when unavailable.
    """
    head = getattr(pr, "head", None)
    repo = getattr(head, "repo", None)
    full_name = getattr(repo, "full_name", "")
    return str(full_name or "").strip()


def _ref_for_pr(pr: Any, side: str) -> str:
    """Return ``pr.<side>.ref`` as a string, or empty when unavailable."""
    end = getattr(pr, side, None)
    return str(getattr(end, "ref", "") or "").strip()


def _build_bulk_pr_tasks(
    gh: GitHubContext, prs_list: list[Any]
) -> list[tuple[Any, models.GitHubContext]]:
    """Build per-PR contexts for each valid open pull request."""
    pr_tasks: list[tuple[Any, models.GitHubContext]] = []
    for pr in prs_list:
        pr_number = int(getattr(pr, "number", 0) or 0)
        if pr_number <= 0:
            continue

        # Bulk runs are triggered by workflow_dispatch or a repository
        # URL, so the outer context carries no PR refs. Take each pull
        # request's own refs so .gitreview resolves against the branch
        # the PR actually targets.
        per_ctx = models.GitHubContext(
            event_name=gh.event_name,
            event_action=gh.event_action,
            event_path=gh.event_path,
            repository=gh.repository,
            repository_owner=gh.repository_owner,
            server_url=gh.server_url,
            run_id=gh.run_id,
            sha=gh.sha,
            base_ref=_ref_for_pr(pr, "base") or gh.base_ref,
            head_ref=_ref_for_pr(pr, "head") or gh.head_ref,
            pr_number=pr_number,
            head_repo=_head_repo_for_pr(pr),
        )
        pr_tasks.append((pr, per_ctx))
    return pr_tasks


def _check_bulk_pr_duplicates(
    data: Inputs,
    per_ctx: models.GitHubContext,
    pr_number: int,
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
) -> _BulkPrResult | None:
    """Return a skip result if the PR is a duplicate, else None."""
    try:
        if data.duplicates_filter:
            os.environ["DUPLICATE_TYPES"] = data.duplicates_filter
        # Generate expected GitHub hash for trailer-aware duplicate
        # detection
        expected_github_hash = DuplicateDetector._generate_github_change_hash(
            per_ctx
        )
        check_for_duplicates(
            per_ctx,
            allow_duplicates=data.allow_duplicates,
            expected_github_hash=expected_github_hash,
        )
    except DuplicateChangeError as exc:
        progress_tracker.duplicate_skipped()
        log_exception_conditionally(log, "Skipping PR #%d", pr_number)
        log.warning(
            "Skipping PR #%d due to duplicate detection: %s. Use "
            "--allow-duplicates to override this check.",
            pr_number,
            exc,
        )
        return "skipped", None, exc
    return None


def _submit_bulk_pr(
    data: Inputs,
    per_ctx: models.GitHubContext,
    pr_number: int,
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
    approved_sha: str = "",
) -> _BulkPrResult:
    """Run the orchestrator for a single PR in multi-PR mode."""
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            orch = Orchestrator(workspace=workspace, approved_sha=approved_sha)
            result_multi = orch.execute(inputs=data, gh=per_ctx)
            _record_change_tracker_result(progress_tracker, result_multi)
            return "success", result_multi, None
    except GitHub2GerritError:
        # Let our structured errors propagate to top level
        raise
    except (OrchestratorError, ConfigurationError) as exc:
        # Convert and propagate structured errors
        _convert_and_raise_structured_error(exc)
    except Exception as exc:
        progress_tracker.add_error(f"PR #{pr_number} processing failed")
        log_exception_conditionally(log, "Failed to process PR #%d", pr_number)
        return "failed", None, exc


def _process_bulk_pr(
    pr_data: tuple[Any, models.GitHubContext],
    data: Inputs,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
) -> _BulkPrResult:
    """Process a single PR and return (status, result, exception)."""
    pr, per_ctx = pr_data
    pr_number = int(getattr(pr, "number", 0) or 0)

    if pr_number <= 0:
        return "invalid", None, None

    log.debug("Starting processing of PR #%d", pr_number)
    log.debug(
        "Processing PR #%d in multi-PR mode with event_name=%s, "
        "event_action=%s",
        pr_number,
        gh.event_name,
        gh.event_action,
    )

    progress_tracker.update_operation(f"Processing PR #{pr_number}...")
    progress_tracker.pr_processed()

    # Check if automation_only is enabled and reject non-automation PRs
    try:
        _check_automation_only(pr, per_ctx, progress_tracker)
    except SystemExit:
        # PR was rejected and closed, skip processing
        log.debug("PR #%d rejected by automation_only check", pr_number)
        return "skipped", None, None

    # Fork PRs need a maintainer's approval before anything is fetched
    allowed, approved_sha = _check_fork_approval(pr, per_ctx, progress_tracker)
    if not allowed:
        return "skipped", None, None

    skip_result = _check_bulk_pr_duplicates(
        data, per_ctx, pr_number, progress_tracker
    )
    if skip_result is not None:
        return skip_result

    return _submit_bulk_pr(
        data, per_ctx, pr_number, progress_tracker, approved_sha
    )


def _record_bulk_success(
    agg: _BulkAggregate, pr_number: int | None, result_multi: SubmissionResult
) -> None:
    """Aggregate URLs, numbers and SHAs from a successful PR result."""
    agg.succeeded_count += 1
    if result_multi.change_urls:
        agg.all_urls.extend(result_multi.change_urls)
        for url in result_multi.change_urls:
            log.info("Gerrit change URL: %s", url)
            log.info(
                "PR #%d created Gerrit change: %s",
                pr_number,
                url,
            )
    if result_multi.change_numbers:
        agg.all_nums.extend(result_multi.change_numbers)
        log.info(
            "PR #%d change numbers: %s",
            pr_number,
            result_multi.change_numbers,
        )
    if result_multi.commit_shas:
        agg.all_shas.extend(result_multi.commit_shas)


def _collect_bulk_future_result(
    agg: _BulkAggregate,
    future: Future[_BulkPrResult],
    pr_number: int | None,
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
) -> None:
    """Fold one completed PR future into the aggregate counters."""
    try:
        status, result_multi, exc = future.result()

        if status == "success" and result_multi:
            _record_bulk_success(agg, pr_number, result_multi)
        elif status == "skipped":
            agg.skipped_count += 1
        elif status == "failed":
            agg.failed_count += 1
            safe_typer_echo(
                f"Failed to process PR #{pr_number}: {exc}",
                progress_tracker=progress_tracker,
                err=True,
            )
            log.info("Continuing to next PR despite failure")
        else:
            agg.failed_count += 1

    except GitHub2GerritError:
        # Let our structured errors propagate to top level
        raise
    except (
        OrchestratorError,
        DuplicateChangeError,
        ConfigurationError,
    ) as exc:
        # Convert and propagate structured errors
        _convert_and_raise_structured_error(exc)
    except Exception as exc:
        agg.failed_count += 1
        log_exception_conditionally(log, "Failed to process PR #%d", pr_number)
        safe_typer_echo(
            f"Failed to process PR #{pr_number}: {exc}",
            progress_tracker=progress_tracker,
            err=True,
        )
        log.info("Continuing to next PR despite failure")


def _run_bulk_pr_tasks(
    data: Inputs,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
    pr_tasks: list[tuple[Any, models.GitHubContext]],
    max_workers: int,
) -> _BulkAggregate:
    """Run PR tasks with bounded parallelism and aggregate results."""
    agg = _BulkAggregate()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        log.debug(
            "Processing %d PRs with %d parallel workers",
            len(pr_tasks),
            max_workers,
        )

        # Submit all tasks
        future_to_pr = {
            executor.submit(
                _process_bulk_pr, pr_task, data, gh, progress_tracker
            ): pr_task[1].pr_number
            for pr_task in pr_tasks
            if pr_task[1].pr_number is not None
        }

        # Collect results as they complete
        for future in as_completed(future_to_pr):
            pr_number = future_to_pr[future]
            agg.processed_count += 1
            _collect_bulk_future_result(
                agg, future, pr_number, progress_tracker
            )
    return agg


def _emit_bulk_outputs(agg: _BulkAggregate) -> None:
    """Write aggregated bulk results to the environment and GITHUB_OUTPUT."""
    if agg.all_urls:
        os.environ["GERRIT_CHANGE_REQUEST_URL"] = "\n".join(agg.all_urls)
    if agg.all_nums:
        os.environ["GERRIT_CHANGE_REQUEST_NUM"] = "\n".join(agg.all_nums)
    if agg.all_shas:
        os.environ["GERRIT_COMMIT_SHA"] = "\n".join(agg.all_shas)

    append_github_output(
        {
            "gerrit_change_request_url": "\n".join(agg.all_urls)
            if agg.all_urls
            else "",
            "gerrit_change_request_num": "\n".join(agg.all_nums)
            if agg.all_nums
            else "",
            "gerrit_commit_sha": "\n".join(agg.all_shas)
            if agg.all_shas
            else "",
        }
    )


def _print_bulk_console_summary(
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
    agg: _BulkAggregate,
) -> None:
    """Print the Rich console summary after bulk processing."""
    summary = progress_tracker.get_summary()
    safe_console_print(
        f"⏳ Total time: {summary.get('elapsed_time', 'unknown')}"
    )
    safe_console_print(f"📊 PRs processed: {agg.processed_count}")
    safe_console_print(f"✅ Succeeded: {agg.succeeded_count}")
    safe_console_print(f"⏭️  Skipped: {agg.skipped_count}")
    if agg.failed_count > 0:
        safe_console_print(f"❌ Failed: {agg.failed_count}")
    safe_console_print(f"🔗 Gerrit changes created: {len(agg.all_urls)}")

    # Final completion message
    if agg.failed_count == 0:
        safe_console_print("\nProcessing completed ✅", style="green")
    else:
        safe_console_print(
            f"\nProcessing completed with {agg.failed_count} failure(s) ⚠️",
            style="yellow",
        )


def _finish_bulk_progress(
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
    agg: _BulkAggregate,
    show_progress: bool,
) -> None:
    """Stop the progress tracker and show the completion summary."""
    if agg.failed_count == 0:
        progress_tracker.update_operation("Processing completed ✅")
    else:
        progress_tracker.add_error("Some PRs failed processing")
    # Aggregate results and provide summary
    progress_tracker.stop()

    # Show summary after progress tracker is stopped
    if show_progress and RICH_AVAILABLE:
        _print_bulk_console_summary(progress_tracker, agg)


def _log_bulk_summary(agg: _BulkAggregate) -> None:
    """Emit the debug-level bulk processing summary block."""
    log.debug("=" * 60)
    log.debug("PROCESSING SUMMARY:")
    log.debug("  Total PRs processed: %d", agg.processed_count)
    log.debug("  Succeeded: %d", agg.succeeded_count)
    log.debug("  Skipped (duplicates): %d", agg.skipped_count)
    log.debug("  Failed: %d", agg.failed_count)
    log.debug("  Gerrit changes created: %d", len(agg.all_urls))
    log.debug("=" * 60)


def _process_bulk(data: Inputs, gh: GitHubContext) -> bool:
    show_progress = env_bool("G2G_SHOW_PROGRESS", True)
    target = gh.repository

    progress_tracker = _build_bulk_progress_tracker(show_progress, target)

    client = build_client()
    repo = get_repo_from_env(client)

    prs_list = list(iter_open_pulls(repo))
    log.debug("Found %d open PRs to process", len(prs_list))

    # Early exit if no PRs to process
    if len(prs_list) == 0:
        return _finish_bulk_no_prs(progress_tracker)

    progress_tracker.update_operation(
        f"🔨 Processing {len(prs_list)} open pull requests..."
    )

    # Use bounded parallel processing with shared clients
    max_workers = min(
        4, len(prs_list)
    )  # Cap at 4 workers (no need for max(1, ...) since we exit early for 0)

    pr_tasks = _build_bulk_pr_tasks(gh, prs_list)
    agg = _run_bulk_pr_tasks(data, gh, progress_tracker, pr_tasks, max_workers)

    _emit_bulk_outputs(agg)
    _finish_bulk_progress(progress_tracker, agg, show_progress)
    _log_bulk_summary(agg)

    # Return True if no failures occurred
    return agg.failed_count == 0


def _prepare_single_checkout(
    orch: Orchestrator,
    workspace: Path,
    data: Inputs,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None,
) -> None:
    """Prepare the local workspace checkout for single-PR processing."""
    try:
        if progress_tracker:
            progress_tracker.update_operation("📂 Preparing local checkout")
        log.debug(
            "Preparing workspace checkout in temporary directory: %s",
            workspace,
        )
        log.debug("Repository URL: %s", gh.repository)
        log.debug("Fetch depth: %s", data.fetch_depth)

        try:
            log.debug("About to call _prepare_workspace_checkout")
            orch._prepare_workspace_checkout(inputs=data, gh=gh)
            log.debug("Workspace checkout completed successfully")
        except Exception:
            log.exception("Workspace checkout failed")
            log.debug("Full checkout exception details:", exc_info=True)
            raise
    except Exception as exc:
        log.debug("Local checkout preparation failed: %s", exc)
        if progress_tracker:
            progress_tracker.add_error("Checkout preparation failed")


def _log_single_pre_submit(
    data: Inputs,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None,
) -> None:
    """Emit SSH and commit-info progress updates before submission."""
    if progress_tracker:
        progress_tracker.update_operation("🔐 Configuring SSH authentication")

    log.debug("Configuring SSH authentication for Gerrit access")
    log.debug(
        "Gerrit server: %s:%s", data.gerrit_server, data.gerrit_server_port
    )

    if progress_tracker:
        progress_tracker.update_operation("🔍 Extracting commit information")

    log.debug("Extracting commit information from PR")
    log.debug("PR commits range: base_sha..head_sha (not available)")


def _execute_single_orchestrator(
    orch: Orchestrator, data: Inputs, gh: GitHubContext
) -> SubmissionResult:
    """Execute the orchestrator with the current operation mode."""
    try:
        # Pass operation mode to orchestrator
        operation_mode = os.getenv("G2G_OPERATION_MODE", "unknown")
        result = orch.execute(inputs=data, gh=gh, operation_mode=operation_mode)
        log.debug("orch.execute() completed successfully")
    except Exception as exc:
        # Log error at debug level only - error_codes will handle
        # user display
        log.debug("Exception during orch.execute(): %s", exc)
        raise
    return result


def _emit_single_outputs(result: SubmissionResult) -> None:
    """Write single-PR results to the environment and GITHUB_OUTPUT."""
    if result.change_urls:
        os.environ["GERRIT_CHANGE_REQUEST_URL"] = "\n".join(result.change_urls)
        # Log Gerrit change URL(s) for debugging
        for url in result.change_urls:
            log.debug("Gerrit change URL: %s", url)
    if result.change_numbers:
        os.environ["GERRIT_CHANGE_REQUEST_NUM"] = "\n".join(
            result.change_numbers
        )
    if result.commit_shas:
        os.environ["GERRIT_COMMIT_SHA"] = "\n".join(result.commit_shas)

    # Also write outputs to GITHUB_OUTPUT if available
    append_github_output(
        {
            "gerrit_change_request_url": "\n".join(result.change_urls)
            if result.change_urls
            else "",
            "gerrit_change_request_num": "\n".join(result.change_numbers)
            if result.change_numbers
            else "",
            "gerrit_commit_sha": "\n".join(result.commit_shas)
            if result.commit_shas
            else "",
        }
    )


def _report_missing_update_change(
    exc: OrchestratorError,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None,
) -> None:
    """Show guidance when an UPDATE targets a non-existent Gerrit change."""
    error_msg = str(exc)
    if (
        "no existing change found" in error_msg.lower()
        or "UPDATE operation requires" in error_msg
    ):
        safe_console_print(
            "❌ UPDATE FAILED: Cannot update non-existent Gerrit change",
            style="red",
            progress_tracker=progress_tracker,
        )
        safe_console_print(
            f"💡 PR #{gh.pr_number} has not been previously "
            f"processed by GitHub2Gerrit.",
            style="yellow",
            progress_tracker=progress_tracker,
        )
        safe_console_print(
            "   To create a new change, set CREATE_MISSING=true "
            "or add a '@github2gerrit create missing change' "
            "comment on the PR.",
            style="yellow",
            progress_tracker=progress_tracker,
        )
        if progress_tracker:
            progress_tracker.add_error("No existing change found for UPDATE")


def _handle_single_structured_error(
    exc: OrchestratorError | DuplicateChangeError | ConfigurationError,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None,
) -> NoReturn:
    """Convert a structured error, adding UPDATE-specific guidance first."""
    # Enhanced error handling for UPDATE operations
    operation_mode = os.getenv("G2G_OPERATION_MODE", "unknown")

    if operation_mode == "update" and isinstance(exc, OrchestratorError):
        _report_missing_update_change(exc, gh, progress_tracker)

    # Convert and propagate structured errors
    _convert_and_raise_structured_error(exc)


def _report_verbose_command_error(
    exc: CommandError,
    basic_error: str,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None,
) -> None:
    """Show detailed git stdout/stderr for a CommandError in verbose mode."""
    detailed_msg = basic_error
    if exc.stdout and exc.stdout.strip():
        detailed_msg += f"\nGit stdout: {exc.stdout.strip()}"
    if exc.stderr and exc.stderr.strip():
        detailed_msg += f"\nGit stderr: {exc.stderr.strip()}"

    # Show debugging suggestion for merge failures
    if "merge --squash" in " ".join(exc.cmd or []):
        detailed_msg += (
            "\n💡 For local debugging: run 'git status' and "
            "check for merge conflicts or uncommitted changes"
        )

    safe_console_print(f"❌ {detailed_msg}", style="red")
    if progress_tracker:
        progress_tracker.add_error(basic_error)
        if exc.stderr and exc.stderr.strip():
            progress_tracker.add_error(f"Details: {exc.stderr.strip()}")


def _report_single_command_error(
    exc: CommandError,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None,
) -> None:
    """Render a CommandError with git command details."""
    # Always show the basic error message
    cmd_str = " ".join(exc.cmd) if exc.cmd else "unknown command"
    basic_error = f"Git command failed: {cmd_str}"
    if exc.returncode is not None:
        basic_error += f" (exit code: {exc.returncode})"

    # In verbose mode, show detailed stdout/stderr
    if is_verbose_mode():
        _report_verbose_command_error(exc, basic_error, progress_tracker)
    else:
        # In non-verbose mode, show basic error with hint to enable
        # verbose
        hint_msg = (
            basic_error + "\n💡 Run with VERBOSE=true for detailed git output"
        )
        safe_console_print(f"❌ {hint_msg}", style="red")
        if progress_tracker:
            progress_tracker.add_error(basic_error)


def _report_single_execution_error(
    exc: Exception,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None,
) -> None:
    """Report a non-structured execution failure for single-PR processing."""
    # Enhanced error handling for CommandError to show git command
    # details
    if isinstance(exc, CommandError):
        _report_single_command_error(exc, progress_tracker)
    else:
        # For other exceptions, use original handling
        error_msg = str(exc)
        if progress_tracker:
            progress_tracker.add_error(f"Execution failed: {error_msg}")
        else:
            safe_console_print(f"❌ Error: {error_msg}", style="red")

    log.debug("Execution failed; continuing to write outputs: %s", exc)

    # In verbose mode, also log the full exception with traceback
    if is_verbose_mode():
        log.exception("Full exception details:")


def _run_single_submission(
    orch: Orchestrator,
    data: Inputs,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None,
) -> tuple[bool, SubmissionResult]:
    """Execute the Gerrit submission pipeline for a single PR."""
    pipeline_success = False
    try:
        if progress_tracker:
            progress_tracker.update_operation("⏩ Submitting to Gerrit")
        log.debug("Starting Gerrit submission process")
        log.debug("Dry run mode: %s", data.dry_run)
        log.debug("About to call orch.execute() - where issues often occur")

        result = _execute_single_orchestrator(orch, data, gh)

        pipeline_success = True
        log.debug("Gerrit submission completed successfully")

        # Save derived parameters to config after successful submission
        _save_derived_parameters_after_success(data)

        if result.change_urls:
            log.debug("Generated change URLs: %s", result.change_urls)
        if progress_tracker:
            progress_tracker.pr_processed()
        _record_change_tracker_result(progress_tracker, result)
    except GitHub2GerritError:
        # Let our structured errors propagate to top level
        raise
    except (
        OrchestratorError,
        DuplicateChangeError,
        ConfigurationError,
    ) as exc:
        _handle_single_structured_error(exc, gh, progress_tracker)
    except Exception as exc:
        _report_single_execution_error(exc, progress_tracker)
        result = SubmissionResult(
            change_urls=[], change_numbers=[], commit_shas=[]
        )
    return pipeline_success, result


def _process_single(
    data: Inputs,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None = None,
    approved_sha: str = "",
) -> tuple[bool, SubmissionResult]:
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        orch = Orchestrator(workspace=workspace, approved_sha=approved_sha)
        _prepare_single_checkout(orch, workspace, data, gh, progress_tracker)
        _log_single_pre_submit(data, progress_tracker)
        pipeline_success, result = _run_single_submission(
            orch, data, gh, progress_tracker
        )
        _emit_single_outputs(result)
        return pipeline_success, result


def _load_effective_inputs() -> Inputs:
    data = _build_inputs_from_env()

    # Detect GitHub CI mode and configure accordingly
    github_mode = _is_github_mode()
    log.debug("GitHub CI mode detected: %s", github_mode)

    if not github_mode and not os.getenv("G2G_RESPECT_USER_SSH"):
        log.debug("Local execution detected, enabling user SSH config respect")
        os.environ["G2G_RESPECT_USER_SSH"] = "true"

    # Load per-org configuration and apply to environment before validation
    org_for_cfg = (
        data.organization
        or os.getenv("ORGANIZATION")
        or os.getenv("GITHUB_REPOSITORY_OWNER")
    )
    cfg = load_org_config(org_for_cfg)

    repository = os.getenv("GITHUB_REPOSITORY", "")

    # Apply dynamic parameter derivation for missing Gerrit parameters
    cfg = apply_parameter_derivation(
        cfg, org_for_cfg, repository=repository, save_to_config=False
    )

    # Debug: Show what configuration would be applied
    log.debug("Configuration to apply: %s", cfg)
    if "DRY_RUN" in cfg:
        log.warning(
            "Configuration contains DRY_RUN=%s, this may override "
            "environment DRY_RUN=%s",
            cfg["DRY_RUN"],
            os.getenv("DRY_RUN"),
        )

    apply_config_to_env(cfg)

    # Refresh inputs after applying configuration to environment
    data = _build_inputs_from_env()

    # Derive reviewers from local git config if running locally and unset
    # G2G_TARGET_URL is set to the actual URL string when in direct URL mode
    if not os.getenv("REVIEWERS_EMAIL") and (
        os.getenv("G2G_TARGET_URL") or not os.getenv("GITHUB_EVENT_NAME")
    ):
        try:
            emails = enumerate_reviewer_emails()
            if emails:
                os.environ["REVIEWERS_EMAIL"] = ",".join(emails)
                data = Inputs(
                    submit_single_commits=data.submit_single_commits,
                    use_pr_as_commit=data.use_pr_as_commit,
                    fetch_depth=data.fetch_depth,
                    gerrit_known_hosts=data.gerrit_known_hosts,
                    gerrit_ssh_privkey_g2g=data.gerrit_ssh_privkey_g2g,
                    gerrit_ssh_user_g2g=data.gerrit_ssh_user_g2g,
                    gerrit_ssh_user_g2g_email=data.gerrit_ssh_user_g2g_email,
                    github_token=data.github_token,
                    organization=data.organization,
                    reviewers_email=os.environ["REVIEWERS_EMAIL"],
                    preserve_github_prs=data.preserve_github_prs,
                    dry_run=data.dry_run,
                    normalise_commit=data.normalise_commit,
                    gerrit_server=data.gerrit_server,
                    gerrit_server_port=data.gerrit_server_port,
                    gerrit_project=data.gerrit_project,
                    issue_id=data.issue_id,
                    issue_id_lookup_json=data.issue_id_lookup_json,
                    commit_rules_json=data.commit_rules_json,
                    allow_duplicates=data.allow_duplicates,
                    ci_testing=data.ci_testing,
                    duplicates_filter=data.duplicates_filter,
                    reuse_strategy=data.reuse_strategy,
                    similarity_subject=data.similarity_subject,
                    similarity_update_factor=data.similarity_update_factor,
                    similarity_files=data.similarity_files,
                    allow_orphan_changes=data.allow_orphan_changes,
                    persist_single_mapping_comment=data.persist_single_mapping_comment,
                    log_reconcile_json=data.log_reconcile_json,
                    create_missing=data.create_missing,
                )
                log.debug("Derived reviewers: %s", data.reviewers_email)
        except Exception as exc:
            log.debug("Could not derive reviewers from git config: %s", exc)

    return data


def _augment_pr_refs_if_needed(gh: GitHubContext) -> GitHubContext:
    """Fill in PR refs and provenance that the event context lacks.

    Direct URL invocations and specific-PR ``workflow_dispatch`` runs
    carry no pull request payload, so ``base_ref``, ``head_ref`` and
    ``head_repo`` all arrive empty.  Provenance in particular must not
    stay unknown: ``_read_gitreview`` treats an unresolved head
    repository as untrusted, so leaving it blank needlessly forfeits the
    PR tree's own ``.gitreview`` on same-repository pull requests.
    """
    needs_refs = not gh.head_ref or not gh.base_ref
    needs_provenance = not gh.head_repo
    if not gh.pr_number or not (needs_refs or needs_provenance):
        return gh

    # Deliberately not gated on event name. Pull request events populate
    # both refs and provenance from the payload and so return above;
    # anything still missing them here (workflow_dispatch, issue_comment,
    # direct URL) needs the API regardless of how it was triggered.
    try:
        client = build_client()
        repo = get_repo_from_env(client)
        pr_obj = get_pull(repo, int(gh.pr_number))
        base_ref = _ref_for_pr(pr_obj, "base")
        head_ref = _ref_for_pr(pr_obj, "head")
        head_repo = _head_repo_for_pr(pr_obj)
        head_sha = str(
            getattr(getattr(pr_obj, "head", object()), "sha", "") or ""
        )
        if base_ref:
            os.environ["GITHUB_BASE_REF"] = base_ref
            log.debug("Resolved base_ref via GitHub API: %s", base_ref)
        if head_ref:
            os.environ["GITHUB_HEAD_REF"] = head_ref
            log.debug("Resolved head_ref via GitHub API: %s", head_ref)
        if head_repo:
            os.environ["PR_HEAD_REPO"] = head_repo
            log.debug("Resolved head repository via GitHub API: %s", head_repo)
        if head_sha:
            os.environ["GITHUB_SHA"] = head_sha
            log.debug("Resolved head sha via GitHub API: %s", head_sha)
    except Exception as exc:
        log.debug("Could not resolve PR refs via GitHub API: %s", exc)
        return gh
    else:
        return _read_github_context()


def _process_close_gerrit_change(
    data: Inputs,
    gh: GitHubContext,
    gerrit_change_url: str,
    *,
    force: bool = False,
) -> None:
    """
    Process a single Gerrit change URL to close its source GitHub PR.

    Args:
        data: Input configuration
        gh: GitHub context
        gerrit_change_url: Full Gerrit change URL to process
        force: If True, bypass status checks for MERGED/ABANDONED changes

    For abandoned Gerrit changes:
    - If CLOSE_MERGED_PRS is true: Close PR with abandoned comment
    - If CLOSE_MERGED_PRS is false: Add abandoned notification comment only

    When force=False (default):
    - MERGED or ABANDONED changes will raise an error (these are final states)
    - Use --force flag to override and process anyway

    This function reports status but does not raise errors if PRs are not
    found or already closed.
    """
    log.debug("Processing Gerrit change: %s", gerrit_change_url)

    # First, check if this Gerrit change originated from GitHub
    pr_url = extract_pr_url_from_gerrit_change(gerrit_change_url)
    if not pr_url:
        no_action_msg = (
            "✅ No action required: Gerrit change did NOT originate in GitHub"
        )
        log.debug(no_action_msg)
        safe_console_print(no_action_msg)
        return

    # Check if close_merged_prs is enabled
    close_merged_prs = env_bool("CLOSE_MERGED_PRS", True)

    status = check_gerrit_change_status(gerrit_change_url)

    # Validate status: without --force, reject MERGED/ABANDONED changes
    if not force and status in ("MERGED", "ABANDONED"):
        error_msg = (
            f"Gerrit change is already {status}. "
            "This is a final state that normally should not trigger "
            "PR closure. Use --force flag to bypass this check and "
            "close the PR anyway."
        )
        log.error(error_msg)
        raise GitHub2GerritError(
            ExitCode.GERRIT_CHANGE_ALREADY_FINAL,
            error_msg,
        )

    # Branches vary in log level and nested close_merged_prs handling,
    # so a dispatch table would obscure this status logging.
    # aislop-ignore-next-line python-repetitive-dispatch
    if status == "ABANDONED":
        if close_merged_prs:
            log.debug(
                "Gerrit change was ABANDONED; will close PR with "
                "abandoned comment (CLOSE_MERGED_PRS=true, force=%s)",
                force,
            )
        else:
            log.debug(
                "Gerrit change was ABANDONED; will add comment only "
                "(CLOSE_MERGED_PRS=false, force=%s)",
                force,
            )
    elif status == "NEW":
        log.warning(
            "Gerrit change is still NEW (not merged yet), but proceeding"
        )
    elif status == "UNKNOWN":
        log.warning(
            "Cannot verify Gerrit change status; proceeding with caution"
        )
    elif status == "MERGED":
        log.debug("Gerrit change confirmed as MERGED (force=%s)", force)

    log.debug("Found GitHub PR URL: %s", pr_url)

    parsed = parse_pr_url(pr_url)
    if not parsed:
        log.error("Failed to parse PR URL: %s", pr_url)
        return

    owner, repo, pr_number = parsed
    log.debug("Closing GitHub PR: %s/%s#%d", owner, repo, pr_number)

    # Auto-discover organization from PR URL if not already set
    if not data.organization and owner:
        log.debug("Auto-discovered organization from PR URL: %s", owner)
        os.environ["ORGANIZATION"] = owner

    # Use consolidated helper function to close the PR
    try:
        close_pr_with_status(
            pr_url=pr_url,
            gerrit_change_url=gerrit_change_url,
            gerrit_status=status,
            dry_run=data.dry_run,
            progress_tracker=None,
            close_merged_prs=close_merged_prs,
        )
    # log.exception records the caught error and traceback; keep
    # processing so one PR failure cannot abort the remaining closures.
    # aislop-ignore-next-line silent-recovery
    except Exception:
        log.exception("Failed to close GitHub PR #%d", pr_number)


def _process_close_merged_prs(data: Inputs, gh: GitHubContext) -> None:
    """
    Process mode for closing GitHub PRs when Gerrit changes are merged.

    This mode is triggered when a Gerrit change is merged and synced to
    GitHub. It extracts the GitHub PR URL from recent commits and closes
    the corresponding PRs.

    When CLOSE_MERGED_PRS is enabled, PRs are closed. For abandoned changes,
    behavior depends on CLOSE_MERGED_PRS: if true, close with abandoned comment;
    if false, only add a comment.

    This function is designed to be non-fatal - it reports status but does
    not raise errors if PRs are not found or already closed.
    """
    log.info("🔄 Processing merged Gerrit changes to close GitHub PRs")

    show_progress = env_bool("G2G_SHOW_PROGRESS", True)
    target = gh.repository

    progress_tracker: G2GProgressTracker | DummyProgressTracker
    if show_progress:
        progress_tracker = G2GProgressTracker(target)
        progress_tracker.start()
        progress_tracker.update_operation("Analyzing recent commits...")
    else:
        progress_tracker = DummyProgressTracker("Gerrit PR Closer", target)

    try:
        # Get commits from the push event payload if available
        # This is more reliable than a sliding window because it only processes
        # commits that were actually part of this push event
        log.debug("Fetching commits from push event for GitHub PR trailers")

        commit_shas = []

        # Try to read commits from the GitHub push event payload
        if gh.event_path and gh.event_path.exists():
            try:
                with gh.event_path.open() as f:
                    event_payload = json.load(f)

                if "commits" in event_payload:
                    commit_shas = [
                        commit["id"]
                        for commit in event_payload["commits"]
                        if "id" in commit
                    ]
                    log.debug(
                        "Found %d commit(s) in push event payload",
                        len(commit_shas),
                    )

                # If no commits in payload, fall back to after commit
                if not commit_shas and "after" in event_payload:
                    after_sha = event_payload["after"]
                    if (
                        after_sha
                        and after_sha
                        != "0000000000000000000000000000000000000000"
                    ):
                        commit_shas = [after_sha]
                        log.debug(
                            "Using 'after' SHA from push event: %s",
                            after_sha[:8],
                        )
            except Exception as exc:
                log.debug(
                    "Could not read commits from push event payload: %s", exc
                )

        # Fallback: use recent commits from git log (sliding window approach)
        if not commit_shas:
            log.debug("No commits found in push event, falling back to git log")
            result = git(["log", f"-{data.fetch_depth}", "--format=%H"])
            commit_shas = [
                line.strip()
                for line in result.stdout.strip().split("\n")
                if line.strip()
            ]

        if not commit_shas:
            log.info("No commits found to analyze")
            progress_tracker.stop()
            return

        log.info(
            "Found %d commit(s) to analyze for PR closure", len(commit_shas)
        )

        progress_tracker.update_operation(
            f"Processing {len(commit_shas)} commit(s) for PR closure..."
        )

        # Process commits and close PRs (non-fatal)
        dry_run = data.dry_run
        close_merged_prs = env_bool("CLOSE_MERGED_PRS", True)
        closed_count = process_recent_commits_for_pr_closure(
            commit_shas,
            dry_run=dry_run,
            progress_tracker=progress_tracker,
            close_merged_prs=close_merged_prs,
        )

        progress_tracker.stop()

        # Report final status - always successful
        if dry_run:
            if closed_count > 0:
                log.info("DRY-RUN: Would close %d GitHub PR(s)", closed_count)
            else:
                log.info("DRY-RUN: No GitHub PRs would be closed")
        else:
            if closed_count > 0:
                log.info("SUCCESS: Closed %d GitHub PR(s)", closed_count)
            else:
                log.info("No GitHub PRs needed closing")

    except Exception as exc:
        # Even on unexpected errors, log as warning but don't fail the workflow
        progress_tracker.stop()
        log.warning("Error during PR closure reconciliation: %s", exc)
        log.info("PR closure reconciliation completed with warnings")


def _validate_inputs_or_raise(data: Inputs) -> None:
    """Validate inputs, converting configuration errors for propagation."""
    try:
        _validate_inputs(data)
    except ConfigurationError as exc:
        log_exception_conditionally(log, "Configuration validation failed")
        converted_error = convert_configuration_error(exc)
        raise converted_error from exc


def _apply_no_gerrit_overrides(data: Inputs, *, no_gerrit: bool) -> Inputs:
    """Force DRY_RUN when G2G_NO_GERRIT is set and rebuild inputs."""
    if not no_gerrit:
        return data
    log.info(
        "🧪 G2G_NO_GERRIT enabled: forcing DRY_RUN=true and "
        "G2G_DRYRUN_DISABLE_NETWORK=true"
    )
    os.environ["DRY_RUN"] = "true"
    os.environ["G2G_DRYRUN_DISABLE_NETWORK"] = "true"
    # Rebuild inputs so the rest of the pipeline sees dry_run=True
    return _load_effective_inputs()


def _log_configured_gerrit_server(data: Inputs) -> None:
    """Log the configured Gerrit server (DNS validation deferred)."""
    # Log configured Gerrit server if present.  DNS validation is
    # handled by Orchestrator._resolve_gerrit_info() which covers
    # both explicit GERRIT_SERVER and .gitreview-derived hosts in
    # a single, consolidated code path.
    gerrit_host = data.gerrit_server or ""
    if gerrit_host:
        log.debug(
            "Gerrit server '%s' configured; DNS validation "
            "deferred to Orchestrator",
            gerrit_host.strip(),
        )


def _log_abandoned_change_url(
    data: Inputs, gh: GitHubContext, change_number: str
) -> None:
    """Log the abandoned Gerrit change URL, falling back to its number."""
    try:
        from .gerrit_urls import create_gerrit_url_builder

        _url_builder = create_gerrit_url_builder(data.gerrit_server)
        gerrit_change_url = _url_builder.change_url(
            data.gerrit_project,
            int(change_number),
        )
        log.debug(
            "Successfully abandoned Gerrit change %s for pull request #%s",
            gerrit_change_url,
            gh.pr_number,
        )
    except Exception:
        log.debug(
            "Successfully abandoned Gerrit change %s for pull request #%s",
            change_number,
            gh.pr_number,
        )


def _abandon_change_for_closed_pr(data: Inputs, gh: GitHubContext) -> None:
    """Abandon the Gerrit change associated with a closed pull request."""
    if gh.pr_number is None:
        return
    try:
        log.debug(
            "Checking for Gerrit change to abandon for PR #%s",
            gh.pr_number,
        )
        change_number = abandon_gerrit_change_for_closed_pr(
            pr_number=gh.pr_number,
            gerrit_server=data.gerrit_server,
            gerrit_project=data.gerrit_project,
            repository=gh.repository,
            dry_run=data.dry_run,
            progress_tracker=None,
        )
        if change_number:
            _log_abandoned_change_url(data, gh, change_number)
            # Console output already done by
            # abandon_gerrit_change_for_closed_pr
        else:
            log.debug(
                "No open Gerrit change found for pull request #%s",
                gh.pr_number,
            )
    except Exception as exc:
        log.warning(
            "Failed to abandon Gerrit change for PR #%s: %s",
            gh.pr_number,
            exc,
        )


def _handle_pr_closed(
    data: Inputs, gh: GitHubContext, *, no_gerrit: bool
) -> None:
    """Perform Gerrit cleanup for a closed pull request."""
    pr_num = gh.pr_number or "unknown"
    if no_gerrit:
        log.debug(
            "🚪 Pull request #%s closed; Gerrit cleanup skipped "
            "(G2G_NO_GERRIT)",
            pr_num,
        )
        safe_console_print(
            f"🚪 Pull request #{pr_num} closed; "
            "Gerrit cleanup skipped (G2G_NO_GERRIT)"
        )
    else:
        log.debug(
            "🚪 Pull request #%s closed; performing Gerrit cleanup",
            pr_num,
        )
        safe_console_print(
            f"🚪 Pull request #{pr_num} closed; performing Gerrit cleanup"
        )

    # Debug log prerequisites for abandoning Gerrit change
    log.debug(
        "Cleanup prerequisites - PR: %s, Server: %s, Project: %s, Repo: %s",
        gh.pr_number,
        data.gerrit_server,
        data.gerrit_project,
        gh.repository,
    )

    # First, abandon the specific Gerrit change for this closed PR
    # Skip in G2G_NO_GERRIT: no Gerrit server to query
    if (
        not no_gerrit
        and gh.pr_number
        and data.gerrit_server
        and data.gerrit_project
        and gh.repository
    ):
        _abandon_change_for_closed_pr(data, gh)

    _run_gerrit_cleanup_tasks(data, gh, no_gerrit=no_gerrit)

    log.debug(
        "Cleanup operations completed for closed PR #%s",
        gh.pr_number or "unknown",
    )


def _handle_pr_operation_mode(
    data: Inputs,
    gh: GitHubContext,
    operation_mode: models.PROperationMode,
    *,
    no_gerrit: bool,
) -> bool:
    """Log the PR operation mode; handle CLOSE. Return True if fully done."""
    if operation_mode == models.PROperationMode.UNKNOWN:
        return False

    log.debug("🔍 Detected PR operation mode: %s", operation_mode.value)
    if operation_mode == models.PROperationMode.UPDATE:
        log.debug(
            "📝 PR update (synchronize) event - will update existing "
            "Gerrit change"
        )
    elif operation_mode == models.PROperationMode.CREATE:
        log.debug("🆕 New PR (opened) event - will create new Gerrit change")
    elif operation_mode == models.PROperationMode.EDIT:
        log.debug("✏️  PR edit event - will sync metadata to Gerrit change")
    elif operation_mode == models.PROperationMode.CLOSE:
        _handle_pr_closed(data, gh, no_gerrit=no_gerrit)
        return True
    return False


def _handle_gerrit_change_events(
    data: Inputs, gh: GitHubContext, *, no_gerrit: bool
) -> bool:
    """Process Gerrit change events and push cleanup.

    Return True when processing is complete and the caller should stop.
    """
    gerrit_event_change_url = os.getenv("GERRIT_CHANGE_URL")
    gerrit_event_type = os.getenv("GERRIT_EVENT_TYPE")

    if gerrit_event_change_url and gerrit_event_type:
        # Format event type for display
        event_display = gerrit_event_type.replace("change-", "").capitalize()
        merge_message = (
            f"🔄 {event_display} Gerrit change: {gerrit_event_change_url}"
        )
        log.debug(merge_message)
        safe_console_print(merge_message)

        # Skip in G2G_NO_GERRIT: Gerrit REST calls are not possible
        if no_gerrit:
            log.info("G2G_NO_GERRIT: skipping Gerrit change processing")
        else:
            force = env_bool("FORCE", False)
            _process_close_gerrit_change(
                data, gh, gerrit_event_change_url, force=force
            )

        # Continue with cleanup tasks even if no PR was found/closed
        # (Gerrit change might not have originated from GitHub)

    # Legacy G2G_GERRIT_CHANGE_URL support (direct CLI usage)
    gerrit_change_url = os.getenv("G2G_GERRIT_CHANGE_URL") or ""
    if gerrit_change_url and not gerrit_event_change_url:
        log.info("🔄 Gerrit change URL provided: %s", gerrit_change_url)
        # Skip in G2G_NO_GERRIT: Gerrit REST calls are not possible
        if no_gerrit:
            log.info("G2G_NO_GERRIT: skipping Gerrit change processing")
        else:
            log.info("Finding and closing source GitHub pull request")
            force = env_bool("FORCE", False)
            _process_close_gerrit_change(
                data, gh, gerrit_change_url, force=force
            )

        # Continue with cleanup tasks

    # Run cleanup tasks for Gerrit events and legacy G2G_GERRIT_CHANGE_URL
    if gerrit_event_change_url or gerrit_change_url:
        _run_gerrit_cleanup_tasks(data, gh, no_gerrit=no_gerrit)
        # Exit successfully after cleanup
        return True
    if gh.event_name == "push" and env_bool("CLOSE_MERGED_PRS", True):
        log.info("🔄 Detected push event with CLOSE_MERGED_PRS enabled")
        log.info("Processing merged Gerrit changes to close GitHub PRs")
        _process_close_merged_prs(data, gh)
        _run_gerrit_cleanup_tasks(
            data, gh, no_gerrit=no_gerrit, gerrit_cleanup_info_log=True
        )
        return True
    return False


def _handle_bulk_mode(
    data: Inputs, gh: GitHubContext, *, no_gerrit: bool
) -> bool:
    """Run bulk mode for URL/workflow_dispatch. Return True if handled."""
    sync_all = env_bool("SYNC_ALL_OPEN_PRS", False)
    # When a target URL was provided via CLI, G2G_TARGET_URL is set
    # to the actual URL string (truthy check works for non-empty strings)
    if not (
        sync_all
        and (
            gh.event_name == "workflow_dispatch" or os.getenv("G2G_TARGET_URL")
        )
    ):
        return False

    bulk_success = _process_bulk(data, gh)

    try:
        log_api_metrics_summary()
    except Exception as exc:
        log.debug("Failed to log API metrics summary: %s", exc)

    # Final success/failure message for processing
    # Note: Success message already shown in _process_bulk summary
    # Only log to debug here for consistency
    if bulk_success:
        log.debug("Processing completed ✅")
        _run_gerrit_cleanup_tasks(
            data, gh, no_gerrit=no_gerrit, gerrit_cleanup_info_log=True
        )
    else:
        # exit_with_error already displays the message via
        # safe_console_print
        log.error(
            "Processing failed, exit status %d ❌",
            ExitCode.GENERAL_ERROR.value,
        )
        exit_with_error(
            ExitCode.GENERAL_ERROR,
            message=(
                f"Processing failed, exit status "
                f"{ExitCode.GENERAL_ERROR.value} ❌"
            ),
        )

    return True


def _handle_single_pr_duplicate_error(
    exc: DuplicateChangeError,
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
) -> NoReturn:
    """Report a detected duplicate Gerrit change and exit."""
    if progress_tracker:
        progress_tracker.add_error("Duplicate change detected")
        progress_tracker.stop()

    # Display clear Rich console output for duplicate detection
    if exc.urls:
        urls_display = ", ".join(exc.urls)
        safe_console_print(
            f"❌ Duplicate Gerrit change blocked submission: {urls_display}",
            style="red",
            progress_tracker=progress_tracker,
        )
    else:
        safe_console_print(
            "❌ Duplicate Gerrit change blocked submission",
            style="red",
            progress_tracker=progress_tracker,
        )

    safe_console_print(
        "💡 Use --allow-duplicates to override this check.",
        style="yellow",
        progress_tracker=progress_tracker,
    )
    exit_for_duplicate_error(
        message=(
            "❌ Duplicate change detected; use --allow-duplicates to override"
        ),
        details=str(exc),
        exception=exc,
    )


def _check_single_pr_duplicates(
    data: Inputs,
    gh: GitHubContext,
    operation_mode: models.PROperationMode,
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
) -> None:
    """Check for duplicate Gerrit changes before single-PR submission."""
    # For UPDATE operations, skip duplicate check - we EXPECT a change
    if operation_mode == models.PROperationMode.UPDATE:
        log.debug(
            "⏩ Skipping duplicate check for UPDATE operation "
            "(change expected to exist)"
        )
        return
    try:
        if data.duplicates_filter:
            os.environ["DUPLICATE_TYPES"] = data.duplicates_filter
        # Generate expected GitHub hash for trailer-aware duplicate
        # detection
        expected_github_hash = DuplicateDetector._generate_github_change_hash(
            gh
        )

        # Only check for duplicates if not allowed
        if not data.allow_duplicates:
            if progress_tracker:
                progress_tracker.update_operation("+ Checking for duplicates")
            log.debug(
                "Starting duplicate detection for PR #%s in %s",
                gh.pr_number,
                gh.repository,
            )
            log.debug(
                "Expected GitHub hash for duplicate detection: %s",
                expected_github_hash,
            )
            check_for_duplicates(
                gh,
                allow_duplicates=data.allow_duplicates,
                expected_github_hash=expected_github_hash,
            )
            log.debug("Duplicate check completed successfully")
            if progress_tracker:
                progress_tracker.update_operation(
                    "✅ Duplicate check completed"
                )
        else:
            log.debug(
                "Skipping duplicate check for PR #%s (allow_duplicates=True)",
                gh.pr_number,
            )
    except DuplicateChangeError as exc:
        _handle_single_pr_duplicate_error(exc, progress_tracker)


def _print_single_pr_summary(
    progress_tracker: G2GProgressTracker | DummyProgressTracker,
    pipeline_success: bool,
    result: SubmissionResult,
) -> None:
    """Print the Rich console summary after single-PR processing."""
    summary = progress_tracker.get_summary() if progress_tracker else {}
    safe_console_print("")
    safe_console_print(
        "✅ Operation completed!"
        if pipeline_success
        else "❌ Operation failed!",
        style="green" if pipeline_success else "red",
    )
    safe_console_print(
        f"⏳ Total time: {summary.get('elapsed_time', 'unknown')}"
    )
    if summary.get("prs_processed", 0) > 0:
        safe_console_print(f"📊 PRs processed: {summary['prs_processed']}")
    if summary.get("changes_submitted", 0) > 0:
        safe_console_print(
            f"🔄 Changes submitted: {summary['changes_submitted']}"
        )
    if summary.get("changes_updated", 0) > 0:
        safe_console_print(f"📝 Changes updated: {summary['changes_updated']}")

    # Show Gerrit change URL(s) in final summary
    if pipeline_success and result.change_urls:
        for url in result.change_urls:
            safe_console_print(f"🔗 Gerrit change: {url}", style="green")


def _handle_single_pr(
    data: Inputs,
    gh: GitHubContext,
    operation_mode: models.PROperationMode,
    *,
    no_gerrit: bool,
) -> None:
    """Execute the single-PR submission pipeline."""
    if not gh.pr_number:
        log.error(
            "PR_NUMBER is empty. This tool requires a valid pull request "
            "context. Current event: %s",
            gh.event_name,
        )
        exit_for_configuration_error(
            message=(
                "❌ Configuration validation failed; missing pull request "
                "context"
            ),
            details=(
                f"PR_NUMBER is empty - requires valid pull request context "
                f"(current event: {gh.event_name})"
            ),
        )

    # Store operation mode in environment for downstream use
    os.environ["G2G_OPERATION_MODE"] = operation_mode.value

    # Execute single-PR submission
    # Initialize progress tracker
    show_progress = env_bool("G2G_SHOW_PROGRESS", True)
    target = (
        f"{gh.repository}/pull/{gh.pr_number}"
        if gh.pr_number
        else gh.repository
    )

    progress_tracker: G2GProgressTracker | DummyProgressTracker
    if show_progress:
        progress_tracker = G2GProgressTracker(target)
        progress_tracker.start()
        progress_tracker.update_operation("Getting source PR details...")
    else:
        progress_tracker = DummyProgressTracker("GitHub to Gerrit", target)

    # Augment PR refs via API when in URL mode and token present
    gh = _augment_pr_refs_if_needed(gh)

    if _recheck_has_nothing_to_unblock(gh):
        log.info(
            "Re-check (%s) for PR #%s, whose head is in this repository; "
            "nothing to unblock, so no transfer is needed",
            gh.event_name,
            gh.pr_number,
        )
        sys.exit(int(ExitCode.SUCCESS))

    # Display PR information with Rich formatting
    approved_sha = ""
    if gh.pr_number:
        pr_obj = _extract_and_display_pr_info(gh, data, progress_tracker)

        # Fork PRs need a maintainer's approval before anything is
        # fetched and before the Gerrit key is materialised. Resolve
        # the PR here when the display step could not, so a missing
        # token cannot skip the gate.
        if pr_obj is None:
            pr_obj = _resolve_pr_for_gate(gh, data)

        # Second chance at the pull request's own metadata, and a
        # second look at the short-circuit above. An issue_comment
        # payload carries none of it, so the check before this
        # depended on _augment_pr_refs_if_needed's API call; that
        # helper swallows a failure and returns the context
        # unresolved, while the fetch just above is a separate call
        # that may well have succeeded. Without this, one transient
        # failure would let a commenter resubmit an unchanged
        # same-repository pull request — exactly what the
        # short-circuit exists to prevent — and would leave the base
        # ref empty for the transfer that followed.
        gh = _recover_pr_metadata(gh, pr_obj)
        if _recheck_has_nothing_to_unblock(gh):
            log.info(
                "Re-check (%s) for PR #%s, whose head is in this "
                "repository; nothing to unblock, so no transfer is needed",
                gh.event_name,
                gh.pr_number,
            )
            sys.exit(int(ExitCode.SUCCESS))

        allowed, approved_sha = _check_fork_approval(
            pr_obj, gh, progress_tracker
        )
        if not allowed:
            sys.exit(int(ExitCode.SUCCESS))

    # Check for duplicates in single-PR mode (before workspace setup)
    if gh.pr_number and not env_bool("SYNC_ALL_OPEN_PRS", False):
        _check_single_pr_duplicates(data, gh, operation_mode, progress_tracker)

    progress_tracker.update_operation("🤝 Processing pull request")

    log.debug("Starting single PR processing pipeline")
    log.debug("Processing PR #%s from %s", gh.pr_number, gh.repository)
    log.debug("Target Gerrit server: %s", data.gerrit_server)
    log.debug("Target Gerrit project: %s", data.gerrit_project)
    pipeline_success, result = _process_single(
        data, gh, progress_tracker, approved_sha
    )

    # Run abandoned-PR and Gerrit cleanup if the pipeline was successful
    # Skip in G2G_NO_GERRIT: no Gerrit server to query
    if pipeline_success:
        _run_gerrit_cleanup_tasks(data, gh, no_gerrit=no_gerrit)

    try:
        log_api_metrics_summary()
    except Exception as exc:
        log.debug("Failed to log API metrics summary: %s", exc)

    # Stop progress tracker and show final results
    # Clean up progress tracker
    progress_tracker.stop()

    # Show summary after progress tracker is stopped
    if show_progress and RICH_AVAILABLE:
        _print_single_pr_summary(progress_tracker, pipeline_success, result)

    # Final success/failure message after all cleanup
    if pipeline_success:
        log.debug("Submission pipeline completed SUCCESSFULLY ✅")
    else:
        log.debug("Submission pipeline FAILED ❌")
        exit_with_error(
            ExitCode.GENERAL_ERROR,
            message="❌ Submission pipeline failed; check logs for details",
        )

    return


def _process() -> None:
    data = _load_effective_inputs()

    # Context before validation. A run that GitHub denied secrets to
    # has nothing to validate, and reporting the absent key as a
    # missing input would misdescribe the cause on an outside
    # contributor's pull request. Reading the context is pure and
    # cannot raise, so the reorder costs nothing.
    gh = _read_github_context()
    if _skip_unprivileged_fork_run(data, gh):
        return
    if _skip_unrequested_comment_run(gh):
        return

    _validate_inputs_or_raise(data)

    # G2G_NO_GERRIT reuses the existing DRY_RUN +
    # G2G_DRYRUN_DISABLE_NETWORK code paths so that all tool logic runs
    # but Gerrit network operations are no-ops. Cleanup tasks
    # (abandoned-PR / Gerrit-change sweeps) are also suppressed because
    # they would hit a non-existent server.
    no_gerrit = env_bool("G2G_NO_GERRIT", False)
    data = _apply_no_gerrit_overrides(data, no_gerrit=no_gerrit)

    # Display config AFTER G2G_NO_GERRIT evaluation so the table
    # reflects the actual runtime values (e.g. DRY_RUN forced true).
    _display_effective_config(data, gh)

    _log_configured_gerrit_server(data)

    # Detect PR operation mode for routing
    operation_mode = gh.get_operation_mode()
    if _handle_pr_operation_mode(data, gh, operation_mode, no_gerrit=no_gerrit):
        return

    if _handle_gerrit_change_events(data, gh, no_gerrit=no_gerrit):
        return

    # Test mode: short-circuit after validation
    if env_bool("G2G_TEST_MODE", False):
        log.debug("Validation complete. Ready to execute submission pipeline.")
        safe_typer_echo(
            "Validation complete. Ready to execute submission pipeline."
        )
        return

    if _handle_bulk_mode(data, gh, no_gerrit=no_gerrit):
        return

    _handle_single_pr(data, gh, operation_mode, no_gerrit=no_gerrit)


def _mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep)}"


def _load_event(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return cast(
            dict[str, Any], json.loads(path.read_text(encoding="utf-8"))
        )
    except Exception as exc:
        log.warning("Failed to parse GITHUB_EVENT_PATH: %s", exc)
        return {}


def _extract_pr_number(evt: dict[str, Any]) -> int | None:
    # Try standard pull_request payload
    pr = evt.get("pull_request")
    if isinstance(pr, dict) and isinstance(pr.get("number"), int):
        return int(pr["number"])

    # Try issues payload (when used on issues events)
    issue = evt.get("issue")
    if isinstance(issue, dict) and isinstance(issue.get("number"), int):
        return int(issue["number"])

    # Try a direct number field
    if isinstance(evt.get("number"), int):
        return int(evt["number"])

    return None


def _read_head_repo(evt: dict[str, Any]) -> str:
    """Return the ``owner/repo`` of the pull request head repository.

    Prefers the ``PR_HEAD_REPO`` environment variable supplied by the
    composite action, falling back to the raw event payload so the CLI
    behaves identically when invoked outside that wrapper.

    Returns an empty string when the provenance cannot be established.
    """
    env_value = os.getenv("PR_HEAD_REPO", "").strip()
    if env_value:
        return env_value

    pr = evt.get("pull_request")
    if isinstance(pr, dict):
        head = pr.get("head")
        if isinstance(head, dict):
            repo = head.get("repo")
            if isinstance(repo, dict):
                full_name = repo.get("full_name")
                if isinstance(full_name, str):
                    return full_name.strip()
    return ""


def _read_github_context() -> GitHubContext:
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    event_action = ""
    event_path_str = os.getenv("GITHUB_EVENT_PATH")
    event_path = Path(event_path_str) if event_path_str else None

    evt = _load_event(event_path)
    if isinstance(evt.get("action"), str):
        event_action = evt["action"]

    repository = os.getenv("GITHUB_REPOSITORY", "")
    repository_owner = os.getenv("GITHUB_REPOSITORY_OWNER", "")
    server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    sha = os.getenv("GITHUB_SHA", "")

    base_ref = os.getenv("GITHUB_BASE_REF", "")
    head_ref = os.getenv("GITHUB_HEAD_REF", "")
    head_repo = _read_head_repo(evt)

    pr_number = _extract_pr_number(evt)
    if pr_number is None:
        env_pr = os.getenv("PR_NUMBER")
        if env_pr and env_pr.isdigit():
            pr_number = int(env_pr)

    ctx = models.GitHubContext(
        event_name=event_name,
        event_action=event_action,
        event_path=event_path,
        repository=repository,
        repository_owner=repository_owner,
        server_url=server_url,
        run_id=run_id,
        sha=sha,
        base_ref=base_ref,
        head_ref=head_ref,
        pr_number=pr_number,
        head_repo=head_repo,
    )
    return ctx


def _validate_inputs(data: Inputs) -> None:
    if data.use_pr_as_commit and data.submit_single_commits:
        msg = (
            "USE_PR_AS_COMMIT and SUBMIT_SINGLE_COMMITS cannot be enabled "
            "at the same time"
        )
        raise ConfigurationError(msg)

    # Context-aware validation: different requirements for GH Actions vs CLI
    is_github_actions = _is_github_actions_context()

    # SSH private key is required unless using SSH agent
    # NOTE: When use_ssh_agent=True, we defer SSH agent validation to runtime
    # in _setup_ssh() where we check if an agent is available and has keys
    # loaded using "ssh-add -l". If SSH agent validation fails at runtime,
    # the system gracefully falls back to file-based SSH authentication.
    # This design avoids duplicate validation and allows for agents that may
    # become available after this early validation phase.
    use_ssh_agent = env_bool("G2G_USE_SSH_AGENT", default=True)
    required_fields = [] if use_ssh_agent else ["gerrit_ssh_privkey_g2g"]

    # Gerrit parameters can be derived in GH Actions if organization available
    # In local CLI context, we're more strict about explicit configuration
    if is_github_actions:
        # In GitHub Actions: allow derivation if organization is available
        if not data.organization:
            required_fields.extend(
                [
                    "gerrit_ssh_user_g2g",
                    "gerrit_ssh_user_g2g_email",
                ]
            )
    else:
        # In local CLI: require explicit values or organization + derivation
        # This prevents unexpected behavior when running locally
        missing_gerrit_params = [
            field
            for field in ["gerrit_ssh_user_g2g", "gerrit_ssh_user_g2g_email"]
            if not getattr(data, field)
        ]
        if missing_gerrit_params:
            if data.organization:
                log.info(
                    "Gerrit parameters can be derived from "
                    "organization '%s'. Missing: %s. Set "
                    "G2G_ENABLE_DERIVATION=false to disable derivation.",
                    data.organization,
                    ", ".join(missing_gerrit_params),
                )
                # Derivation enabled by default, can be disabled explicitly
                if not env_bool("G2G_ENABLE_DERIVATION", True):
                    required_fields.extend(missing_gerrit_params)
            else:
                required_fields.extend(missing_gerrit_params)

    for field_name in required_fields:
        if not getattr(data, field_name):
            log.error("Missing required input: %s", field_name)
            if field_name in [
                "gerrit_ssh_user_g2g",
                "gerrit_ssh_user_g2g_email",
            ]:
                if data.organization:
                    log.error(
                        "These fields can be derived automatically from "
                        "organization '%s' (derivation enabled by default). "
                        "Check that G2G_ENABLE_DERIVATION is not set to false.",
                        data.organization,
                    )
                else:
                    log.error(
                        "These fields require either explicit values or an "
                        "ORGANIZATION for derivation"
                    )
            raise ConfigurationError(
                _MSG_MISSING_REQUIRED_INPUT.format(field_name=field_name)
            )

    if data.fetch_depth <= 0:
        log.error("Invalid FETCH_DEPTH: %s", data.fetch_depth)
        raise ConfigurationError(_MSG_INVALID_FETCH_DEPTH)

    # Validate Issue ID is a single line string if provided
    if data.issue_id and ("\n" in data.issue_id or "\r" in data.issue_id):
        raise ConfigurationError(_MSG_ISSUE_ID_MULTILINE)


def _is_github_mode() -> bool:
    """Detect if running in GitHub CI environment.

    Returns:
        True if running in GitHub CI, False if running locally
    """
    return (
        os.getenv("GITHUB_ACTIONS") == "true"
        or os.getenv("GITHUB_EVENT_NAME", "").strip() != ""
    )


def _get_ssh_agent_status() -> str:
    """Get SSH agent availability and usage status."""
    import shutil

    # Check if SSH agent is available
    ssh_agent_available = shutil.which("ssh-agent") is not None

    # Check if SSH agent is configured to be used
    use_ssh_agent = env_bool("G2G_USE_SSH_AGENT", default=True)

    # Check if SSH agent is currently running (SSH_AUTH_SOCK exists)
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    agent_running = ssh_auth_sock and os.path.exists(ssh_auth_sock)

    # Check if we have explicit SSH private key
    has_private_key = bool(os.getenv("GERRIT_SSH_PRIVKEY_G2G", "").strip())

    if not ssh_agent_available:
        return "❎ Unavailable, Unused"
    elif has_private_key:
        # SSH key explicitly provided - don't use agent
        if agent_running:
            return "✅ Available, Unused"
        else:
            return "❎ Unavailable, Unused"
    elif use_ssh_agent and agent_running:
        return "✅ Available, Used"
    elif agent_running:
        return "✅ Available, Unused"
    else:
        return "❎ Unavailable, Unused"


@dataclass(frozen=True)
class _ConfigDisplay:
    """Precomputed values for rendering the effective-config table."""

    mode_label: str
    mode_description: str
    no_gerrit_enabled: bool
    is_closing_pr_mode: bool
    known_hosts_status: str
    privkey_status: str
    github_token_status: str
    ssh_agent_status: str
    cleanup_abandoned: bool
    cleanup_gerrit: bool


def _compute_config_mode_description(
    github_mode: bool,
    gh: GitHubContext,
    target_url_type: str,
    *,
    is_closing_pr_mode: bool,
) -> str:
    """Derive the human-readable mode description for the config table."""
    if is_closing_pr_mode:
        return "✅ Closing GitHub pull request"
    if target_url_type == "github_pr" or gh.pr_number:
        return "✅ Gerrit change from pull request"
    if target_url_type == "github_repo":
        return "✅ Gerrit changes from repository"
    if github_mode:
        return "✅ GitHub Actions mode"
    return "✅ CLI mode"


def _compute_config_credential_statuses(
    data: Inputs, *, github_mode: bool
) -> tuple[str, str, str]:
    """Return known-hosts, private-key and token status indicators."""
    # Avoid displaying sensitive values - use context-appropriate indicators
    # For known hosts: ❎ in local mode (normal), ❌ in CI mode (error)
    if data.gerrit_known_hosts:
        known_hosts_status = "✅"
    else:
        known_hosts_status = "❌" if github_mode else "❎"

    # For SSH private key: ❎ in local mode (normal), ❌ in CI mode (error)
    if data.gerrit_ssh_privkey_g2g:
        privkey_status = "✅"
    else:
        privkey_status = "❌" if github_mode else "❎"

    # For GitHub token: ❎ in local mode (optional), ❌ in CI mode (required)
    if data.github_token:
        github_token_status = "✅"  # noqa: S105
    else:
        github_token_status = "❌" if github_mode else "❎"
    return known_hosts_status, privkey_status, github_token_status


def _add_closing_pr_config(
    config_info: dict[str, str], data: Inputs, ctx: _ConfigDisplay
) -> None:
    """Populate the minimal config shown while closing a pull request."""
    # In PR closing mode, only show minimal relevant config
    if data.dry_run:
        config_info["DRY_RUN"] = str(data.dry_run)

    # Show organization if set (helps identify which GitHub org to query)
    if data.organization:
        config_info["ORGANIZATION"] = data.organization

    # Show GitHub token status (required for closing PRs)
    config_info["GITHUB_TOKEN"] = ctx.github_token_status


def _add_full_config(
    config_info: dict[str, str], data: Inputs, ctx: _ConfigDisplay
) -> None:
    """Populate the full config shown in Gerrit change creation mode."""
    # In Gerrit change creation mode, show full config
    # Only show non-default boolean values
    if data.submit_single_commits:
        config_info["SUBMIT_SINGLE_COMMITS"] = str(data.submit_single_commits)
    if data.use_pr_as_commit:
        config_info["USE_PR_AS_COMMIT"] = str(data.use_pr_as_commit)
    if data.dry_run:
        config_info["DRY_RUN"] = str(data.dry_run)

    # Only show non-default fetch depth
    if data.fetch_depth != 10:
        config_info["FETCH_DEPTH"] = str(data.fetch_depth)

    # SSH user and email first
    if data.gerrit_ssh_user_g2g:
        config_info["GERRIT_SSH_USER_G2G"] = data.gerrit_ssh_user_g2g
    if data.gerrit_ssh_user_g2g_email:
        config_info["GERRIT_SSH_USER_G2G_EMAIL"] = (
            data.gerrit_ssh_user_g2g_email
        )
    if data.organization:
        config_info["ORGANIZATION"] = data.organization
    if data.reviewers_email:
        config_info["REVIEWERS_EMAIL"] = data.reviewers_email

    # Only show non-default boolean values
    if data.preserve_github_prs:
        config_info["PRESERVE_GITHUB_PRS"] = str(data.preserve_github_prs)
    if data.ci_testing:
        config_info["CI_TESTING"] = str(data.ci_testing)

    # Show Issue ID if provided
    if data.issue_id:
        config_info["ISSUE_ID"] = data.issue_id
    else:
        config_info["ISSUE_ID"] = "❎ Not provided"

    # Show Gerrit settings if they have values
    if data.gerrit_server:
        config_info["GERRIT_SERVER"] = data.gerrit_server
    # Only show non-default port (29418 is default)
    if data.gerrit_server_port and data.gerrit_server_port != 29418:
        config_info["GERRIT_SERVER_PORT"] = str(data.gerrit_server_port)
    if data.gerrit_project:
        config_info["GERRIT_PROJECT"] = data.gerrit_project

    # Move credentials to bottom of table
    # Always show known hosts status
    if data.gerrit_known_hosts or not data.gerrit_known_hosts:
        config_info["GERRIT_KNOWN_HOSTS"] = ctx.known_hosts_status
    config_info["GERRIT_SSH_PRIVKEY_G2G"] = ctx.privkey_status
    config_info["GITHUB_TOKEN"] = ctx.github_token_status
    config_info["SSH_AGENT"] = ctx.ssh_agent_status

    # Show cleanup abandoned status if enabled
    if ctx.cleanup_abandoned:
        config_info["CLEANUP_ABANDONED"] = "✅"

    # Show Gerrit cleanup status if enabled
    if ctx.cleanup_gerrit:
        config_info["CLEANUP_GERRIT"] = "✅"


def _build_config_info(data: Inputs, ctx: _ConfigDisplay) -> dict[str, str]:
    """Build the ordered configuration mapping for display."""
    # Build configuration data, filtering out empty/default values
    # Order items logically: Mode first, then behavioral settings,
    # then credentials
    config_info: dict[str, str] = {}

    # Mode first - always show
    config_info[ctx.mode_label] = ctx.mode_description

    # Show G2G_NO_GERRIT regardless of operation mode so logs always
    # indicate when the run is using test infrastructure.
    if ctx.no_gerrit_enabled:
        config_info["G2G_NO_GERRIT"] = "🧪"

    if ctx.is_closing_pr_mode:
        _add_closing_pr_config(config_info, data, ctx)
    else:
        _add_full_config(config_info, data, ctx)
    return config_info


def _display_effective_config(data: Inputs, gh: GitHubContext) -> None:
    """Display effective configuration in a formatted table."""
    # Use env_bool for consistent boolean parsing across the codebase
    no_gerrit_enabled = env_bool("G2G_NO_GERRIT", False)

    # Detect mode and display prominently
    github_mode = _is_github_mode()
    mode_label = "GITHUB_MODE" if github_mode else "CLI_MODE"

    # Determine operation mode based on context and target URL
    target_url_type = os.getenv("G2G_TARGET_URL_TYPE", "")
    close_merged_prs = env_bool("CLOSE_MERGED_PRS", True)

    # Determine if we're in "close PR" mode (not creating Gerrit changes)
    is_closing_pr_mode = (
        gh.event_name == "push" and close_merged_prs
    ) or target_url_type == "gerrit_change"

    # Check if abandoned cleanup is enabled
    cleanup_abandoned = FORCE_ABANDONED_CLEANUP or env_bool(
        "CLEANUP_ABANDONED", False
    )

    # Check if Gerrit cleanup is enabled
    cleanup_gerrit = FORCE_GERRIT_CLEANUP or env_bool("CLEANUP_GERRIT", False)

    mode_description = _compute_config_mode_description(
        github_mode,
        gh,
        target_url_type,
        is_closing_pr_mode=is_closing_pr_mode,
    )

    known_hosts_status, privkey_status, github_token_status = (
        _compute_config_credential_statuses(data, github_mode=github_mode)
    )
    ssh_agent_status = _get_ssh_agent_status()

    ctx = _ConfigDisplay(
        mode_label=mode_label,
        mode_description=mode_description,
        no_gerrit_enabled=no_gerrit_enabled,
        is_closing_pr_mode=is_closing_pr_mode,
        known_hosts_status=known_hosts_status,
        privkey_status=privkey_status,
        github_token_status=github_token_status,
        ssh_agent_status=ssh_agent_status,
        cleanup_abandoned=cleanup_abandoned,
        cleanup_gerrit=cleanup_gerrit,
    )
    config_info = _build_config_info(data, ctx)

    # Display the configuration table
    display_pr_info(config_info, "GitHub2Gerrit Configuration")

    # Log GitHub context at debug level only
    log.debug(
        "GitHub context: event_name=%s, event_action=%s, repository=%s, "
        "repository_owner=%s, pr_number=%s, base_ref=%s, head_ref=%s, sha=%s",
        gh.event_name,
        gh.event_action,
        gh.repository,
        gh.repository_owner,
        gh.pr_number,
        gh.base_ref,
        gh.head_ref,
        gh.sha,
    )


if __name__ == "__main__":
    # Invoke the Typer app when executed as a script.
    # Example:
    #   python -m github2gerrit.cli --help
    app()
