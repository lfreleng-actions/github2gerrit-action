# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import TypeVar
from typing import cast
from urllib.parse import urlparse

import click
import typer

from . import models
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
from .github_api import get_pr_title_body
from .github_api import get_pull
from .github_api import get_repo_from_env
from .github_api import iter_open_pulls
from .gitutils import CommandError
from .gitutils import enumerate_reviewer_emails
from .gitutils import git
from .models import GitHubContext
from .models import Inputs
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
    """Get package version, trying importlib.metadata first, then fallback."""
    try:
        from importlib.metadata import version as stdlib_version

        return str(stdlib_version(package))
    except ImportError:
        from importlib_metadata import version as backport_version

        return str(backport_version(package))


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
        "dependabot[bot]",
        "dependabot",
        "pre-commit-ci[bot]",
        "pre-commit-ci",
    ]

    # Get PR author
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
            "PR #%s from '%s' rejected - known automation tools "
            "(dependabot, pre-commit-ci) required",
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


def _extract_and_display_pr_info(
    gh: GitHubContext,
    data: Inputs,
    progress_tracker: Any = None,
) -> None:
    """Extract PR information and display it with Rich formatting."""
    if not gh.pr_number:
        return

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
            return

        client = build_client(token)
        repo = get_repo_from_env(client)
        pr_obj = get_pull(repo, int(gh.pr_number))

        # Check PR state and exit if not processable
        pr_state = getattr(pr_obj, "state", "unknown")
        if pr_state != "open":
            _exit_for_pr_state_error(gh.pr_number, pr_state)

        # Check if automation_only is enabled and reject non-automation PRs
        _check_automation_only(pr_obj, gh, progress_tracker)

        # Extract PR information
        title, _body = get_pr_title_body(pr_obj)

        # Get additional PR details
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
        display_pr_info(pr_info, "Pull Request Details", progress_tracker)

    except GitHub2GerritError:
        # Let our structured errors propagate
        raise
    except Exception as exc:
        log.debug("Failed to display PR info: %s", exc)
        if "404" in str(exc) or "Not Found" in str(exc):
            _exit_for_pr_not_found(gh.pr_number, gh.repository)
        else:
            _exit_for_pr_fetch_error(exc)


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

    # Check for PR URL
    if len(parts) >= 4 and parts[2] in ("pull", "pulls"):
        try:
            pr_number = int(parts[3])
            return GitHubPRTarget(owner=owner, repo=repo, pr_number=pr_number)
        except Exception as exc:
            log.debug("Failed to parse PR number from URL: %s", exc)

    # Return repo target (may have None pr_number for compatibility)
    return GitHubRepoTarget(owner=owner, repo=repo)


APP_NAME = "github2gerrit"


if TYPE_CHECKING:
    BaseGroup = object
else:
    BaseGroup = click.Group


class _FormatterProto(Protocol):
    def write_usage(self, prog: str, args: str, prefix: str = ...) -> None: ...


class _ContextProto(Protocol):
    @property
    def command_path(self) -> str: ...


class _SingleUsageGroup(BaseGroup):
    def format_usage(
        self, ctx: _ContextProto, formatter: _FormatterProto
    ) -> None:
        # Force a simplified usage line without COMMAND [ARGS]...
        formatter.write_usage(
            ctx.command_path, "[OPTIONS] TARGET_URL", prefix="Usage: "
        )


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
        # Parse JSON
        lookup_data = json.loads(json_str)

        # Validate it's an array
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


# Show version information when --help is used or in GitHub Actions mode
if "--help" in sys.argv or _is_github_actions_context():
    try:
        app_version = get_version("github2gerrit")
        print(f"🏷️  github2gerrit version {app_version}")
    except Exception:
        print("⚠️  github2gerrit version information not available")

app: typer.Typer = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    cls=cast(Any, _SingleUsageGroup),
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

    def typed_app_command(
        *args: object, **kwargs: object
    ) -> Callable[[F], F]: ...
else:
    typed_app_command = app.command


def _save_derived_parameters_after_success(data: Inputs) -> None:
    """Save derived parameters to config file after successful Gerrit
    submission."""
    try:
        # Get the organization used for derivation
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
        # parameters
        repository = os.getenv("GITHUB_REPOSITORY", "")
        apply_parameter_derivation(
            cfg, org_for_cfg, repository=repository, save_to_config=True
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
        "--allow-duplicates",
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
        "--close-merged-prs",
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
        help="Fetch depth for checkout.",
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
        "--preserve-github-prs",
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
    # BREAKING CHANGE v0.2.0: Default changed from True to False
    # for more flexible commit reconciliation
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
    automation_only: bool = typer.Option(
        True,
        "--automation-only/--no-automation-only",
        envvar="AUTOMATION_ONLY",
        help="Accept pull requests from known automation tools.",
    ),
) -> None:
    """
    Tool to convert GitHub pull requests into Gerrit changes

    - GitHub PR URL: creates a Gerrit change from the given pull request
    - Gerrit Change URL: finds and closes the source GitHub pull request
    - GitHub Repo URL: converts all open pull requests to Gerrit changes
    - No arguments: environment variables determine behaviour (CI/CD mode)
    """

    # Handle version flag first
    if version_flag:
        try:
            app_version = get_version("github2gerrit")
            typer.echo(f"github2gerrit version {app_version}")
        except Exception:
            typer.echo("Version information not available")
        sys.exit(int(ExitCode.SUCCESS))

    # Override boolean parameters with properly parsed environment variables
    # This ensures that string "false" from GitHub Actions is handled correctly
    if os.getenv("SUBMIT_SINGLE_COMMITS"):
        submit_single_commits = parse_bool_env(
            os.getenv("SUBMIT_SINGLE_COMMITS")
        )

    if os.getenv("USE_PR_AS_COMMIT"):
        use_pr_as_commit = parse_bool_env(os.getenv("USE_PR_AS_COMMIT"))

    if os.getenv("PRESERVE_GITHUB_PRS"):
        preserve_github_prs = parse_bool_env(os.getenv("PRESERVE_GITHUB_PRS"))

    if os.getenv("DRY_RUN"):
        dry_run = parse_bool_env(os.getenv("DRY_RUN"))

    if os.getenv("ALLOW_DUPLICATES"):
        allow_duplicates = parse_bool_env(os.getenv("ALLOW_DUPLICATES"))

    if os.getenv("CI_TESTING"):
        ci_testing = parse_bool_env(os.getenv("CI_TESTING"))

    if os.getenv("SIMILARITY_FILES"):
        similarity_files = parse_bool_env(os.getenv("SIMILARITY_FILES"))

    if os.getenv("ALLOW_ORPHAN_CHANGES"):
        allow_orphan_changes = parse_bool_env(os.getenv("ALLOW_ORPHAN_CHANGES"))

    if os.getenv("PERSIST_SINGLE_MAPPING_COMMENT"):
        persist_single_mapping_comment = parse_bool_env(
            os.getenv("PERSIST_SINGLE_MAPPING_COMMENT")
        )

    if os.getenv("LOG_RECONCILE_JSON"):
        log_reconcile_json = parse_bool_env(os.getenv("LOG_RECONCILE_JSON"))

    if os.getenv("AUTOMATION_ONLY"):
        automation_only = parse_bool_env(os.getenv("AUTOMATION_ONLY"))

    # Set up logging level based on verbose flag
    if verbose:
        os.environ["G2G_LOG_LEVEL"] = "DEBUG"
        _reconfigure_logging()

    # Initialize Rich-aware logging system
    setup_rich_aware_logging()

    # Log version to logs in GitHub Actions environment
    if _is_github_actions_context():
        try:
            app_version = get_version("github2gerrit")
            log.debug("github2gerrit version %s", app_version)
        except Exception:
            log.warning("Version information not available")

    # Show initial progress if Rich is available and progress is enabled
    if show_progress and not RICH_AVAILABLE:
        safe_console_print(
            "📋 Rich formatting not available - progress will be shown as "
            "simple text..."
        )

    # Store progress flag in environment for use by processing functions
    os.environ["G2G_SHOW_PROGRESS"] = "true" if show_progress else "false"
    # Normalize CLI options into environment for unified processing.
    # Explicitly set all boolean flags to ensure consistent behavior
    os.environ["SUBMIT_SINGLE_COMMITS"] = (
        "true" if submit_single_commits else "false"
    )
    os.environ["USE_PR_AS_COMMIT"] = "true" if use_pr_as_commit else "false"
    os.environ["FETCH_DEPTH"] = str(fetch_depth)
    if gerrit_known_hosts:
        os.environ["GERRIT_KNOWN_HOSTS"] = gerrit_known_hosts
    if gerrit_ssh_privkey_g2g:
        os.environ["GERRIT_SSH_PRIVKEY_G2G"] = gerrit_ssh_privkey_g2g
    if gerrit_ssh_user_g2g:
        os.environ["GERRIT_SSH_USER_G2G"] = gerrit_ssh_user_g2g
    if gerrit_ssh_user_g2g_email:
        os.environ["GERRIT_SSH_USER_G2G_EMAIL"] = gerrit_ssh_user_g2g_email
    resolved_org = _resolve_org(organization)
    if resolved_org:
        os.environ["ORGANIZATION"] = resolved_org
    if reviewers_email:
        os.environ["REVIEWERS_EMAIL"] = reviewers_email
    os.environ["PRESERVE_GITHUB_PRS"] = (
        "true" if preserve_github_prs else "false"
    )
    os.environ["DRY_RUN"] = "true" if dry_run else "false"
    os.environ["NORMALISE_COMMIT"] = "true" if normalise_commit else "false"
    os.environ["ALLOW_GHE_URLS"] = "true" if allow_ghe_urls else "false"
    if gerrit_server:
        os.environ["GERRIT_SERVER"] = gerrit_server
    if gerrit_server_port:
        os.environ["GERRIT_SERVER_PORT"] = str(gerrit_server_port)
    if gerrit_project:
        os.environ["GERRIT_PROJECT"] = gerrit_project

    # Handle Issue-ID lookup from JSON if provided
    resolved_issue_id = issue_id
    if not resolved_issue_id and issue_id_lookup_json:
        resolved_issue_id = _resolve_issue_id_from_json(
            issue_id_lookup_json, github_actor
        )

    if resolved_issue_id:
        os.environ["ISSUE_ID"] = resolved_issue_id
    os.environ["ALLOW_DUPLICATES"] = "true" if allow_duplicates else "false"
    os.environ["CI_TESTING"] = "true" if ci_testing else "false"
    os.environ["CLOSE_MERGED_PRS"] = "true" if close_merged_prs else "false"
    os.environ["FORCE"] = "true" if force else "false"
    os.environ["DUPLICATE_TYPES"] = duplicate_types
    if reuse_strategy:
        os.environ["REUSE_STRATEGY"] = reuse_strategy

    # Validate similarity parameters
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
    os.environ["SIMILARITY_FILES"] = "true" if similarity_files else "false"
    os.environ["ALLOW_ORPHAN_CHANGES"] = (
        "true" if allow_orphan_changes else "false"
    )
    os.environ["PERSIST_SINGLE_MAPPING_COMMENT"] = (
        "true" if persist_single_mapping_comment else "false"
    )
    os.environ["LOG_RECONCILE_JSON"] = "true" if log_reconcile_json else "false"
    os.environ["AUTOMATION_ONLY"] = "true" if automation_only else "false"
    # URL mode handling
    if target_url:
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
    # Debug: Show environment at CLI startup
    log.debug("CLI startup environment check:")
    for key in ["DRY_RUN", "CI_TESTING", "GERRIT_SERVER", "GERRIT_PROJECT"]:
        value = os.environ.get(key, "NOT_SET")
        log.debug("  %s = %s", key, value)

    # Delegate to common processing path
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


def _setup_logging() -> logging.Logger:
    level_name = os.getenv("G2G_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = (
        "%(asctime)s %(levelname)-8s %(name)s %(filename)s:%(lineno)d | "
        "%(message)s"
    )
    logging.basicConfig(level=level, format=fmt)
    return logging.getLogger(APP_NAME)


def _reconfigure_logging() -> None:
    """Reconfigure logging level based on current environment variables."""
    level_name = os.getenv("G2G_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.getLogger().setLevel(level)
    for handler in logging.getLogger().handlers:
        handler.setLevel(level)


log = _setup_logging()


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
    )


def _process_bulk(data: Inputs, gh: GitHubContext) -> bool:
    # Initialize progress tracker for processing
    show_progress = env_bool("G2G_SHOW_PROGRESS", True)
    target = gh.repository

    progress_tracker: G2GProgressTracker | DummyProgressTracker
    if show_progress:
        progress_tracker = G2GProgressTracker(target)
        progress_tracker.start()
        progress_tracker.update_operation("🔍 Examining pull requests")
    else:
        progress_tracker = DummyProgressTracker("GitHub to Gerrit", target)

    client = build_client()
    repo = get_repo_from_env(client)

    all_urls: list[str] = []
    all_nums: list[str] = []
    all_shas: list[str] = []

    prs_list = list(iter_open_pulls(repo))
    log.debug("Found %d open PRs to process", len(prs_list))

    # Early exit if no PRs to process
    if len(prs_list) == 0:
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

    progress_tracker.update_operation(
        f"🔨 Processing {len(prs_list)} open pull requests..."
    )

    # Result tracking for summary
    processed_count = 0
    succeeded_count = 0
    skipped_count = 0
    failed_count = 0

    # Use bounded parallel processing with shared clients
    max_workers = min(
        4, len(prs_list)
    )  # Cap at 4 workers (no need for max(1, ...) since we exit early for 0)

    def process_single_pr(
        pr_data: tuple[Any, models.GitHubContext],
    ) -> tuple[str, SubmissionResult | None, Exception | None]:
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

        # Update progress tracker for this PR
        progress_tracker.update_operation(f"Processing PR #{pr_number}...")
        progress_tracker.pr_processed()

        # Check if automation_only is enabled and reject non-automation PRs
        try:
            _check_automation_only(pr, per_ctx, progress_tracker)
        except SystemExit:
            # PR was rejected and closed, skip processing
            log.debug("PR #%d rejected by automation_only check", pr_number)
            return "skipped", None, None

        try:
            if data.duplicates_filter:
                os.environ["DUPLICATE_TYPES"] = data.duplicates_filter
            # Generate expected GitHub hash for trailer-aware duplicate
            # detection
            expected_github_hash = (
                DuplicateDetector._generate_github_change_hash(per_ctx)
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

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                workspace = Path(temp_dir)
                orch = Orchestrator(workspace=workspace)
                result_multi = orch.execute(inputs=data, gh=per_ctx)
                if progress_tracker and result_multi.change_urls:
                    if any(
                        "new" in url.lower() for url in result_multi.change_urls
                    ):
                        progress_tracker.change_submitted()
                    else:
                        progress_tracker.change_updated()
                return "success", result_multi, None
        except GitHub2GerritError:
            # Let our structured errors propagate to top level
            raise
        except (OrchestratorError, ConfigurationError) as exc:
            # Convert and propagate structured errors
            if isinstance(exc, OrchestratorError):
                converted_error = convert_orchestrator_error(exc)
            else:  # ConfigurationError
                converted_error = convert_configuration_error(exc)
            raise converted_error from exc
        except Exception as exc:
            progress_tracker.add_error(f"PR #{pr_number} processing failed")
            log_exception_conditionally(
                log, "Failed to process PR #%d", pr_number
            )
            return "failed", None, exc

    # Prepare PR processing tasks
    pr_tasks = []
    for pr in prs_list:
        pr_number = int(getattr(pr, "number", 0) or 0)
        if pr_number <= 0:
            continue

        per_ctx = models.GitHubContext(
            event_name=gh.event_name,
            event_action=gh.event_action,
            event_path=gh.event_path,
            repository=gh.repository,
            repository_owner=gh.repository_owner,
            server_url=gh.server_url,
            run_id=gh.run_id,
            sha=gh.sha,
            base_ref=gh.base_ref,
            head_ref=gh.head_ref,
            pr_number=pr_number,
        )
        pr_tasks.append((pr, per_ctx))

    # Process PRs in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        log.debug(
            "Processing %d PRs with %d parallel workers",
            len(pr_tasks),
            max_workers,
        )

        # Submit all tasks
        future_to_pr = {
            executor.submit(process_single_pr, pr_task): pr_task[1].pr_number
            for pr_task in pr_tasks
            if pr_task[1].pr_number is not None
        }

        # Collect results as they complete
        for future in as_completed(future_to_pr):
            pr_number = future_to_pr[future]
            processed_count += 1

            try:
                status, result_multi, exc = future.result()

                if status == "success" and result_multi:
                    succeeded_count += 1
                    if result_multi.change_urls:
                        all_urls.extend(result_multi.change_urls)
                        for url in result_multi.change_urls:
                            log.info("Gerrit change URL: %s", url)
                            log.info(
                                "PR #%d created Gerrit change: %s",
                                pr_number,
                                url,
                            )
                    if result_multi.change_numbers:
                        all_nums.extend(result_multi.change_numbers)
                        log.info(
                            "PR #%d change numbers: %s",
                            pr_number,
                            result_multi.change_numbers,
                        )
                    if result_multi.commit_shas:
                        all_shas.extend(result_multi.commit_shas)
                elif status == "skipped":
                    skipped_count += 1
                elif status == "failed":
                    failed_count += 1
                    safe_typer_echo(
                        f"Failed to process PR #{pr_number}: {exc}",
                        progress_tracker=progress_tracker,
                        err=True,
                    )
                    log.info("Continuing to next PR despite failure")
                else:
                    failed_count += 1

            except GitHub2GerritError as exc:
                # Let our structured errors propagate to top level
                raise
            except (
                OrchestratorError,
                DuplicateChangeError,
                ConfigurationError,
            ) as exc:
                # Convert and propagate structured errors
                if isinstance(exc, OrchestratorError):
                    converted_error = convert_orchestrator_error(exc)
                elif isinstance(exc, DuplicateChangeError):
                    converted_error = convert_duplicate_error(exc)
                else:  # ConfigurationError
                    converted_error = convert_configuration_error(exc)
                raise converted_error from exc
            except Exception as exc:
                failed_count += 1
                log_exception_conditionally(
                    log, "Failed to process PR #%d", pr_number
                )
                safe_typer_echo(
                    f"Failed to process PR #{pr_number}: {exc}",
                    progress_tracker=progress_tracker,
                    err=True,
                )
                log.info("Continuing to next PR despite failure")

    # Aggregate results and provide summary
    if all_urls:
        os.environ["GERRIT_CHANGE_REQUEST_URL"] = "\n".join(all_urls)
    if all_nums:
        os.environ["GERRIT_CHANGE_REQUEST_NUM"] = "\n".join(all_nums)
    if all_shas:
        os.environ["GERRIT_COMMIT_SHA"] = "\n".join(all_shas)

    append_github_output(
        {
            "gerrit_change_request_url": "\n".join(all_urls)
            if all_urls
            else "",
            "gerrit_change_request_num": "\n".join(all_nums)
            if all_nums
            else "",
            "gerrit_commit_sha": "\n".join(all_shas) if all_shas else "",
        }
    )

    # Stop progress tracker and show final results
    if failed_count == 0:
        progress_tracker.update_operation("Processing completed ✅")
    else:
        progress_tracker.add_error("Some PRs failed processing")
    # Aggregate results and provide summary
    progress_tracker.stop()

    # Show summary after progress tracker is stopped
    if show_progress and RICH_AVAILABLE:
        summary = progress_tracker.get_summary()
        safe_console_print(
            f"⏱️ Total time: {summary.get('elapsed_time', 'unknown')}"
        )
        safe_console_print(f"📊 PRs processed: {processed_count}")
        safe_console_print(f"✅ Succeeded: {succeeded_count}")
        safe_console_print(f"⏭️  Skipped: {skipped_count}")
        if failed_count > 0:
            safe_console_print(f"❌ Failed: {failed_count}")
        safe_console_print(f"🔗 Gerrit changes created: {len(all_urls)}")

        # Final completion message
        if failed_count == 0:
            safe_console_print("\nProcessing completed ✅", style="green")
        else:
            safe_console_print(
                f"\nProcessing completed with {failed_count} failure(s) ⚠️",
                style="yellow",
            )

    # Summary block
    log.debug("=" * 60)
    log.debug("PROCESSING SUMMARY:")
    log.debug("  Total PRs processed: %d", processed_count)
    log.debug("  Succeeded: %d", succeeded_count)
    log.debug("  Skipped (duplicates): %d", skipped_count)
    log.debug("  Failed: %d", failed_count)
    log.debug("  Gerrit changes created: %d", len(all_urls))
    log.debug("=" * 60)

    # Return True if no failures occurred
    return failed_count == 0


def _process_single(
    data: Inputs,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None = None,
) -> tuple[bool, SubmissionResult]:
    # Create temporary directory for all git operations
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)

        orch = Orchestrator(workspace=workspace)

        try:
            if progress_tracker:
                progress_tracker.update_operation("📋 Preparing local checkout")
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

        if progress_tracker:
            progress_tracker.update_operation(
                "🔐 Configuring SSH authentication"
            )

        log.debug("Configuring SSH authentication for Gerrit access")
        log.debug(
            "Gerrit server: %s:%s", data.gerrit_server, data.gerrit_server_port
        )

        if progress_tracker:
            progress_tracker.update_operation("⬆️ Extracting commit information")

        log.debug("Extracting commit information from PR")
        log.debug("PR commits range: base_sha..head_sha (not available)")

        pipeline_success = False
        try:
            if progress_tracker:
                progress_tracker.update_operation("⏩ Submitting to Gerrit")
            log.debug("Starting Gerrit submission process")
            log.debug("Dry run mode: %s", data.dry_run)
            log.debug("About to call orch.execute() - where issues often occur")

            try:
                # Pass operation mode to orchestrator
                operation_mode = os.getenv("G2G_OPERATION_MODE", "unknown")
                result = orch.execute(
                    inputs=data, gh=gh, operation_mode=operation_mode
                )
                log.debug("orch.execute() completed successfully")
            except Exception as exc:
                # Log error at debug level only - error_codes will handle
                # user display
                log.debug("Exception during orch.execute(): %s", exc)
                raise

            pipeline_success = True
            log.debug("Gerrit submission completed successfully")

            # Save derived parameters to config after successful submission
            _save_derived_parameters_after_success(data)

            if result.change_urls:
                log.debug("Generated change URLs: %s", result.change_urls)
            if progress_tracker:
                progress_tracker.pr_processed()
            if progress_tracker and result.change_urls:
                if any("new" in url.lower() for url in result.change_urls):
                    progress_tracker.change_submitted()
                else:
                    progress_tracker.change_updated()
        except GitHub2GerritError:
            # Let our structured errors propagate to top level
            raise
        except (
            OrchestratorError,
            DuplicateChangeError,
            ConfigurationError,
        ) as exc:
            # Enhanced error handling for UPDATE operations
            operation_mode = os.getenv("G2G_OPERATION_MODE", "unknown")

            if operation_mode == "update" and isinstance(
                exc, OrchestratorError
            ):
                error_msg = str(exc)
                if (
                    "no existing change found" in error_msg.lower()
                    or "UPDATE operation requires" in error_msg
                ):
                    safe_console_print(
                        "❌ UPDATE FAILED: Cannot update non-existent "
                        "Gerrit change",
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
                        "   To create a new change, trigger the 'opened' "
                        "workflow action.",
                        style="yellow",
                        progress_tracker=progress_tracker,
                    )
                    if progress_tracker:
                        progress_tracker.add_error(
                            "No existing change found for UPDATE"
                        )

            # Convert and propagate structured errors
            if isinstance(exc, OrchestratorError):
                converted_error = convert_orchestrator_error(exc)
            elif isinstance(exc, DuplicateChangeError):
                converted_error = convert_duplicate_error(exc)
            else:  # ConfigurationError
                converted_error = convert_configuration_error(exc)
            raise converted_error from exc
        except Exception as exc:
            # Enhanced error handling for CommandError to show git command
            # details
            if isinstance(exc, CommandError):
                # Always show the basic error message
                cmd_str = " ".join(exc.cmd) if exc.cmd else "unknown command"
                basic_error = f"Git command failed: {cmd_str}"
                if exc.returncode is not None:
                    basic_error += f" (exit code: {exc.returncode})"

                # In verbose mode, show detailed stdout/stderr
                if is_verbose_mode():
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
                            progress_tracker.add_error(
                                f"Details: {exc.stderr.strip()}"
                            )
                else:
                    # In non-verbose mode, show basic error with hint to enable
                    # verbose
                    hint_msg = (
                        basic_error
                        + "\n💡 Run with VERBOSE=true for detailed git output"
                    )
                    safe_console_print(f"❌ {hint_msg}", style="red")
                    if progress_tracker:
                        progress_tracker.add_error(basic_error)
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

            result = SubmissionResult(
                change_urls=[], change_numbers=[], commit_shas=[]
            )
        if result.change_urls:
            os.environ["GERRIT_CHANGE_REQUEST_URL"] = "\n".join(
                result.change_urls
            )
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

        return pipeline_success, result


def _load_effective_inputs() -> Inputs:
    # Build inputs from environment (used by URL callback path)
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

    # Get repository for GERRIT_PROJECT derivation
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
                )
                log.debug("Derived reviewers: %s", data.reviewers_email)
        except Exception as exc:
            log.debug("Could not derive reviewers from git config: %s", exc)

    return data


def _augment_pr_refs_if_needed(gh: GitHubContext) -> GitHubContext:
    # When a target URL was provided via CLI, G2G_TARGET_URL contains
    # the actual URL string. We use a truthy check (non-empty string is truthy)
    # to detect when running in direct URL mode vs GitHub Actions CI mode.
    if (
        os.getenv("G2G_TARGET_URL")
        and gh.pr_number
        and (not gh.head_ref or not gh.base_ref)
    ):
        try:
            client = build_client()
            repo = get_repo_from_env(client)
            pr_obj = get_pull(repo, int(gh.pr_number))
            base_ref = str(
                getattr(getattr(pr_obj, "base", object()), "ref", "") or ""
            )
            head_ref = str(
                getattr(getattr(pr_obj, "head", object()), "ref", "") or ""
            )
            head_sha = str(
                getattr(getattr(pr_obj, "head", object()), "sha", "") or ""
            )
            if base_ref:
                os.environ["GITHUB_BASE_REF"] = base_ref
                log.debug("Resolved base_ref via GitHub API: %s", base_ref)
            if head_ref:
                os.environ["GITHUB_HEAD_REF"] = head_ref
                log.debug("Resolved head_ref via GitHub API: %s", head_ref)
            if head_sha:
                os.environ["GITHUB_SHA"] = head_sha
                log.debug("Resolved head sha via GitHub API: %s", head_sha)
            return _read_github_context()
        except Exception as exc:
            log.debug("Could not resolve PR refs via GitHub API: %s", exc)
    return gh


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
            "☑️ No action required: Gerrit change did NOT originate in GitHub"
        )
        log.debug(no_action_msg)
        safe_console_print(no_action_msg)
        return

    # Check if close_merged_prs is enabled
    close_merged_prs = env_bool("CLOSE_MERGED_PRS", True)

    # Check Gerrit change status
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

    # Log status information
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

    # Parse PR URL to get owner info for auto-discovery
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

    # Initialize progress tracker
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

                # Extract commit SHAs from the push event
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


def _process() -> None:
    data = _load_effective_inputs()

    # Validate inputs
    try:
        _validate_inputs(data)
    except ConfigurationError as exc:
        log_exception_conditionally(log, "Configuration validation failed")
        converted_error = convert_configuration_error(exc)
        raise converted_error from exc

    gh = _read_github_context()
    _display_effective_config(data, gh)

    # Detect PR operation mode for routing
    operation_mode = gh.get_operation_mode()
    if operation_mode != models.PROperationMode.UNKNOWN:
        log.debug("🔍 Detected PR operation mode: %s", operation_mode.value)
        if operation_mode == models.PROperationMode.UPDATE:
            log.debug(
                "📝 PR update (synchronize) event - will update existing "
                "Gerrit change"
            )
        elif operation_mode == models.PROperationMode.CREATE:
            log.debug(
                "🆕 New PR (opened) event - will create new Gerrit change"
            )
        elif operation_mode == models.PROperationMode.EDIT:
            log.debug("✏️  PR edit event - will sync metadata to Gerrit change")
        elif operation_mode == models.PROperationMode.CLOSE:
            pr_num = gh.pr_number or "unknown"
            log.debug(
                "🚪 Pull request #%s closed; performing Gerrit cleanup",
                pr_num,
            )
            safe_console_print(
                f"🚪 Pull request #{pr_num} closed; performing Gerrit cleanup"
            )

            # Debug log prerequisites for abandoning Gerrit change
            log.debug(
                "Cleanup prerequisites - PR: %s, Server: %s, "
                "Project: %s, Repo: %s",
                gh.pr_number,
                data.gerrit_server,
                data.gerrit_project,
                gh.repository,
            )

            # First, abandon the specific Gerrit change for this closed PR
            if (
                gh.pr_number
                and data.gerrit_server
                and data.gerrit_project
                and gh.repository
            ):
                try:
                    log.debug(
                        "🔍 Checking for Gerrit change to abandon for PR #%s",
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
                        gerrit_change_url = (
                            f"https://{data.gerrit_server}/c/"
                            f"{data.gerrit_project}/+/{change_number}"
                        )
                        log.debug(
                            "✅ Successfully abandoned Gerrit change %s "
                            "for pull request #%s",
                            gerrit_change_url,
                            gh.pr_number,
                        )
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

            # Run abandoned PR cleanup if enabled
            if FORCE_ABANDONED_CLEANUP:
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

            # Run Gerrit cleanup if enabled
            if FORCE_GERRIT_CLEANUP:
                try:
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

            log.debug(
                "✅ Cleanup operations completed for closed PR #%s",
                gh.pr_number or "unknown",
            )
            return

    # Close merged/abandoned PRs: handle Gerrit events closing GitHub PRs
    # This runs in three scenarios:
    # 1. Push events (when Gerrit syncs back to GitHub)
    #    with CLOSE_MERGED_PRS enabled
    # 2. Direct Gerrit change URL provided in CLI
    # 3. Gerrit event dispatched via workflow_dispatch (GERRIT_CHANGE_URL set)

    # Check for Gerrit event inputs from workflow_dispatch
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
        log.info("Finding and closing source GitHub pull request")
        force = env_bool("FORCE", False)
        _process_close_gerrit_change(data, gh, gerrit_change_url, force=force)

        # Continue with cleanup tasks

    # Run cleanup tasks for Gerrit events and legacy G2G_GERRIT_CHANGE_URL
    if gerrit_event_change_url or gerrit_change_url:
        # Run abandoned PR cleanup if enabled
        if FORCE_ABANDONED_CLEANUP:
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

        # Run Gerrit cleanup if enabled
        if FORCE_GERRIT_CLEANUP:
            try:
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

        # Exit successfully after cleanup
        return
    elif gh.event_name == "push" and env_bool("CLOSE_MERGED_PRS", True):
        log.info("🔄 Detected push event with CLOSE_MERGED_PRS enabled")
        log.info("Processing merged Gerrit changes to close GitHub PRs")
        _process_close_merged_prs(data, gh)

        # Run abandoned PR cleanup if enabled
        if FORCE_ABANDONED_CLEANUP:
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

        # Run Gerrit cleanup if enabled
        if FORCE_GERRIT_CLEANUP:
            try:
                log.info("Running Gerrit cleanup for closed GitHub PRs...")
                if data.gerrit_server and data.gerrit_project:
                    cleanup_closed_github_prs(
                        gerrit_server=data.gerrit_server,
                        gerrit_project=data.gerrit_project,
                        dry_run=data.dry_run,
                        progress_tracker=None,
                    )
            except Exception as exc:
                log.warning("Gerrit cleanup failed: %s", exc)

        return

    # Test mode: short-circuit after validation
    if env_bool("G2G_TEST_MODE", False):
        log.debug("Validation complete. Ready to execute submission pipeline.")
        safe_typer_echo(
            "Validation complete. Ready to execute submission pipeline."
        )
        return

    # Bulk mode for URL/workflow_dispatch
    sync_all = env_bool("SYNC_ALL_OPEN_PRS", False)
    # When a target URL was provided via CLI, G2G_TARGET_URL is set
    # to the actual URL string (truthy check works for non-empty strings)
    if sync_all and (
        gh.event_name == "workflow_dispatch" or os.getenv("G2G_TARGET_URL")
    ):
        bulk_success = _process_bulk(data, gh)

        # Log external API metrics summary
        try:
            log_api_metrics_summary()
        except Exception as exc:
            log.debug("Failed to log API metrics summary: %s", exc)

        # Final success/failure message for processing
        # Note: Success message already shown in _process_bulk summary
        # Only log to debug here for consistency
        if bulk_success:
            log.debug("Processing completed ✅")

            # Run abandoned PR cleanup if enabled
            if FORCE_ABANDONED_CLEANUP:
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

            # Run Gerrit cleanup if enabled
            if FORCE_GERRIT_CLEANUP:
                try:
                    log.info("Running Gerrit cleanup for closed GitHub PRs...")
                    if data.gerrit_server and data.gerrit_project:
                        cleanup_closed_github_prs(
                            gerrit_server=data.gerrit_server,
                            gerrit_project=data.gerrit_project,
                            dry_run=data.dry_run,
                            progress_tracker=None,
                        )
                except Exception as exc:
                    log.warning("Gerrit cleanup failed: %s", exc)
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

        return

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

    # Test mode handled earlier

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

    # Display PR information with Rich formatting
    if gh.pr_number:
        _extract_and_display_pr_info(gh, data, progress_tracker)

    # Check for duplicates in single-PR mode (before workspace setup)
    # For UPDATE operations, skip duplicate check - we EXPECT a change to exist
    if gh.pr_number and not env_bool("SYNC_ALL_OPEN_PRS", False):
        if operation_mode == models.PROperationMode.UPDATE:
            log.debug(
                "⏩ Skipping duplicate check for UPDATE operation "
                "(change expected to exist)"
            )
        else:
            try:
                if data.duplicates_filter:
                    os.environ["DUPLICATE_TYPES"] = data.duplicates_filter
                # Generate expected GitHub hash for trailer-aware duplicate
                # detection
                expected_github_hash = (
                    DuplicateDetector._generate_github_change_hash(gh)
                )

                # Only check for duplicates if not allowed
                if not data.allow_duplicates:
                    if progress_tracker:
                        progress_tracker.update_operation(
                            "+ Checking for duplicates"
                        )
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
                        "Skipping duplicate check for PR #%s "
                        "(allow_duplicates=True)",
                        gh.pr_number,
                    )
            except DuplicateChangeError as exc:
                if progress_tracker:
                    progress_tracker.add_error("Duplicate change detected")
                    progress_tracker.stop()

                # Display clear Rich console output for duplicate detection
                if exc.urls:
                    urls_display = ", ".join(exc.urls)
                    safe_console_print(
                        f"❌ Duplicate Gerrit change blocked submission: "
                        f"{urls_display}",
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
                        "❌ Duplicate change detected; use "
                        "--allow-duplicates to override"
                    ),
                    details=str(exc),
                    exception=exc,
                )

    progress_tracker.update_operation("🤝 Processing pull request")

    log.debug("Starting single PR processing pipeline")
    log.debug("Processing PR #%s from %s", gh.pr_number, gh.repository)
    log.debug("Target Gerrit server: %s", data.gerrit_server)
    log.debug("Target Gerrit project: %s", data.gerrit_project)
    pipeline_success, result = _process_single(data, gh, progress_tracker)

    # Run abandoned PR cleanup if enabled and pipeline was successful
    if pipeline_success and FORCE_ABANDONED_CLEANUP:
        try:
            log.debug("Running abandoned PR cleanup...")
            # Extract owner and repo from gh.repository (format: "owner/repo")
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
            # Don't fail the whole pipeline if cleanup fails
            log.warning("Abandoned PR cleanup failed: %s", exc)

    # Run Gerrit cleanup if enabled and pipeline was successful
    if pipeline_success and FORCE_GERRIT_CLEANUP:
        try:
            log.debug("Running Gerrit cleanup for closed GitHub PRs...")
            if data.gerrit_server and data.gerrit_project:
                cleanup_closed_github_prs(
                    gerrit_server=data.gerrit_server,
                    gerrit_project=data.gerrit_project,
                    dry_run=data.dry_run,
                    progress_tracker=None,
                )
        except Exception as exc:
            # Don't fail the whole pipeline if cleanup fails
            log.warning("Gerrit cleanup failed: %s", exc)

    # Log external API metrics summary
    try:
        log_api_metrics_summary()
    except Exception as exc:
        log.debug("Failed to log API metrics summary: %s", exc)

    # Stop progress tracker and show final results
    # Clean up progress tracker
    progress_tracker.stop()

    # Show summary after progress tracker is stopped
    if show_progress and RICH_AVAILABLE:
        summary = progress_tracker.get_summary() if progress_tracker else {}
        safe_console_print(
            "\n✅ Operation completed!"
            if pipeline_success
            else "\n❌ Operation failed!",
            style="green" if pipeline_success else "red",
        )
        safe_console_print(
            f"⏱️ Total time: {summary.get('elapsed_time', 'unknown')}"
        )
        if summary.get("prs_processed", 0) > 0:
            safe_console_print(f"📊 PRs processed: {summary['prs_processed']}")
        if summary.get("changes_submitted", 0) > 0:
            safe_console_print(
                f"🔄 Changes submitted: {summary['changes_submitted']}"
            )
        if summary.get("changes_updated", 0) > 0:
            safe_console_print(
                f"📝 Changes updated: {summary['changes_updated']}"
            )

        # Show Gerrit change URL(s) in final summary
        if pipeline_success and result.change_urls:
            for url in result.change_urls:
                safe_console_print(f"🔗 Gerrit change: {url}", style="green")

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

    # Validate fetch depth is a positive integer
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
            return "☑️ Available, Unused"
        else:
            return "❎ Unavailable, Unused"
    elif use_ssh_agent and agent_running:
        return "✅ Available, Used"
    elif agent_running:
        return "☑️ Available, Unused"
    else:
        return "❎ Unavailable, Unused"


def _display_effective_config(data: Inputs, gh: GitHubContext) -> None:
    """Display effective configuration in a formatted table."""
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

    if is_closing_pr_mode:
        mode_description = "✅ Closing GitHub pull request"
    elif target_url_type == "github_pr" or gh.pr_number:
        mode_description = "✅ Gerrit change from pull request"
    elif target_url_type == "github_repo":
        mode_description = "✅ Gerrit changes from repository"
    elif github_mode:
        mode_description = "✅ GitHub Actions mode"
    else:
        mode_description = "✅ CLI mode"

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
    ssh_agent_status = _get_ssh_agent_status()

    # Build configuration data, filtering out empty/default values
    # Order items logically: Mode first, then behavioral settings,
    # then credentials
    config_info = {}

    # Mode first - always show
    config_info[mode_label] = mode_description

    if is_closing_pr_mode:
        # In PR closing mode, only show minimal relevant config
        if data.dry_run:
            config_info["DRY_RUN"] = str(data.dry_run)

        # Show organization if set (helps identify which GitHub org to query)
        if data.organization:
            config_info["ORGANIZATION"] = data.organization

        # Show GitHub token status (required for closing PRs)
        config_info["GITHUB_TOKEN"] = github_token_status
    else:
        # In Gerrit change creation mode, show full config
        # Only show non-default boolean values
        if data.submit_single_commits:
            config_info["SUBMIT_SINGLE_COMMITS"] = str(
                data.submit_single_commits
            )
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
            config_info["GERRIT_KNOWN_HOSTS"] = known_hosts_status
        config_info["GERRIT_SSH_PRIVKEY_G2G"] = privkey_status
        config_info["GITHUB_TOKEN"] = github_token_status
        config_info["SSH_AGENT"] = ssh_agent_status

        # Show cleanup abandoned status if enabled
        if cleanup_abandoned:
            config_info["CLEANUP_ABANDONED"] = "☑️"

        # Show Gerrit cleanup status if enabled
        if cleanup_gerrit:
            config_info["CLEANUP_GERRIT"] = "☑️"

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
