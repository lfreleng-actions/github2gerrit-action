# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
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
from .core import Orchestrator
from .core import SubmissionResult
from .duplicate_detection import DuplicateChangeError
from .duplicate_detection import check_for_duplicates
from .github_api import build_client
from .github_api import get_pull
from .github_api import get_repo_from_env
from .github_api import iter_open_pulls
from .gitutils import run_cmd
from .models import GitHubContext
from .models import Inputs
from .ssh_common import build_git_ssh_command
from .ssh_common import build_non_interactive_ssh_env
from .utils import append_github_output
from .utils import env_bool
from .utils import env_str
from .utils import log_exception_conditionally
from .utils import parse_bool_env


class ConfigurationError(Exception):
    """Raised when configuration validation fails.

    This custom exception is used instead of typer.BadParameter to provide
    cleaner error messages to end users without exposing Python tracebacks.
    When caught, it displays user-friendly messages prefixed with
    "Configuration validation failed:" rather than raw exception details.
    """


def _parse_github_target(url: str) -> tuple[str | None, str | None, int | None]:
    """
    Parse a GitHub repository or pull request URL.

    Returns:
      (org, repo, pr_number) where pr_number may be None for repo URLs.
    """
    try:
        u = urlparse(url)
    except Exception:
        return None, None, None

    allow_ghe = env_bool("ALLOW_GHE_URLS", False)
    bad_hosts = {
        "gitlab.com",
        "www.gitlab.com",
        "bitbucket.org",
        "www.bitbucket.org",
    }
    if u.netloc in bad_hosts:
        return None, None, None
    if not allow_ghe and u.netloc not in ("github.com", "www.github.com"):
        return None, None, None

    parts = [p for p in (u.path or "").split("/") if p]
    if len(parts) < 2:
        return None, None, None

    owner, repo = parts[0], parts[1]
    pr_number: int | None = None
    if len(parts) >= 4 and parts[2] in ("pull", "pulls"):
        try:
            pr_number = int(parts[3])
        except Exception:
            pr_number = None

    return owner, repo, pr_number


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
    def format_usage(self, ctx: _ContextProto, formatter: _FormatterProto) -> None:
        # Force a simplified usage line without COMMAND [ARGS]...
        formatter.write_usage(ctx.command_path, "[OPTIONS] TARGET_URL", prefix="Usage: ")


# Error message constants to comply with TRY003
_MSG_MISSING_REQUIRED_INPUT = "Missing required input: {field_name}"
_MSG_INVALID_FETCH_DEPTH = "FETCH_DEPTH must be a positive integer"
_MSG_ISSUE_ID_MULTILINE = "Issue ID must be single line"

app: typer.Typer = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    cls=cast(Any, _SingleUsageGroup),
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

    def typed_app_command(*args: object, **kwargs: object) -> Callable[[F], F]: ...
else:
    typed_app_command = app.command


@typed_app_command()
def main(
    ctx: typer.Context,
    target_url: str | None = typer.Argument(
        None,
        help="GitHub repository or PR URL",
        metavar="TARGET_URL",
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
    organization: str | None = typer.Option(
        None,
        "--organization",
        envvar="ORGANIZATION",
        help=("Organization (defaults to GITHUB_REPOSITORY_OWNER when unset)."),
    ),
    reviewers_email: str = typer.Option(
        "",
        "--reviewers-email",
        envvar="REVIEWERS_EMAIL",
        help="Comma-separated list of reviewer emails.",
    ),
    allow_ghe_urls: bool = typer.Option(
        False,
        "--allow-ghe-urls/--no-allow-ghe-urls",
        envvar="ALLOW_GHE_URLS",
        help="Allow non-github.com GitHub Enterprise URLs in direct URL mode.",
    ),
    preserve_github_prs: bool = typer.Option(
        False,
        "--preserve-github-prs",
        envvar="PRESERVE_GITHUB_PRS",
        help="Do not close GitHub PRs after pushing to Gerrit.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        envvar="DRY_RUN",
        help="Validate settings and PR metadata; do not write to Gerrit.",
    ),
    gerrit_server: str = typer.Option(
        "",
        "--gerrit-server",
        envvar="GERRIT_SERVER",
        help="Gerrit server hostname (optional; .gitreview preferred).",
    ),
    gerrit_server_port: str = typer.Option(
        "29418",
        "--gerrit-server-port",
        envvar="GERRIT_SERVER_PORT",
        help="Gerrit SSH port (default: 29418).",
    ),
    gerrit_project: str = typer.Option(
        "",
        "--gerrit-project",
        envvar="GERRIT_PROJECT",
        help="Gerrit project (optional; .gitreview preferred).",
    ),
    issue_id: str = typer.Option(
        "",
        "--issue-id",
        envvar="ISSUE_ID",
        help="Issue ID to include in commit message (e.g., Issue-ID: ABC-123).",
    ),
    allow_duplicates: bool = typer.Option(
        False,
        "--allow-duplicates",
        envvar="ALLOW_DUPLICATES",
        help="Allow submitting duplicate changes without error.",
    ),
    ci_testing: bool = typer.Option(
        False,
        "--ci-testing/--no-ci-testing",
        envvar="CI_TESTING",
        help="Enable CI testing mode (overrides .gitreview, handles unrelated repos).",
    ),
    duplicate_types: str = typer.Option(
        "open",
        "--duplicate-types",
        envvar="DUPLICATE_TYPES",
        help=(
            "Gerrit change states to evaluate when determining if a change should be considered a duplicate "
            '(comma-separated). E.g. "open,merged,abandoned". Default: "open".'
        ),
    ),
    normalise_commit: bool = typer.Option(
        True,
        "--normalise-commit/--no-normalise-commit",
        envvar="NORMALISE_COMMIT",
        help="Normalize commit messages to conventional commit format.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        envvar="G2G_VERBOSE",
        help="Verbose output (sets loglevel to DEBUG).",
    ),
) -> None:
    """
    Tool to convert GitHub pull requests into Gerrit changes

    - Providing a URL to a pull request: converts that pull request
      into a Gerrit change

    - Providing a URL to a GitHub repository converts all open pull
      requests into Gerrit changes

    - No arguments for CI/CD environment; reads parameters from
      environment variables
    """
    # Override boolean parameters with properly parsed environment variables
    # This ensures that string "false" from GitHub Actions is handled correctly
    if os.getenv("SUBMIT_SINGLE_COMMITS"):
        submit_single_commits = parse_bool_env(os.getenv("SUBMIT_SINGLE_COMMITS"))

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
    # Set up logging level based on verbose flag
    if verbose:
        os.environ["G2G_LOG_LEVEL"] = "DEBUG"
        _reconfigure_logging()
    # Normalize CLI options into environment for unified processing.
    # Explicitly set all boolean flags to ensure consistent behavior
    os.environ["SUBMIT_SINGLE_COMMITS"] = "true" if submit_single_commits else "false"
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
    os.environ["PRESERVE_GITHUB_PRS"] = "true" if preserve_github_prs else "false"
    os.environ["DRY_RUN"] = "true" if dry_run else "false"
    os.environ["NORMALISE_COMMIT"] = "true" if normalise_commit else "false"
    os.environ["ALLOW_GHE_URLS"] = "true" if allow_ghe_urls else "false"
    if gerrit_server:
        os.environ["GERRIT_SERVER"] = gerrit_server
    if gerrit_server_port:
        os.environ["GERRIT_SERVER_PORT"] = gerrit_server_port
    if gerrit_project:
        os.environ["GERRIT_PROJECT"] = gerrit_project
    if issue_id:
        os.environ["ISSUE_ID"] = issue_id
    os.environ["ALLOW_DUPLICATES"] = "true" if allow_duplicates else "false"
    os.environ["CI_TESTING"] = "true" if ci_testing else "false"
    if duplicate_types:
        os.environ["DUPLICATE_TYPES"] = duplicate_types
    # URL mode handling
    if target_url:
        org, repo, pr = _parse_github_target(target_url)
        log.debug("Parsed GitHub URL: org=%s, repo=%s, pr=%s", org, repo, pr)
        if org:
            os.environ["ORGANIZATION"] = org
            log.debug("Set ORGANIZATION=%s", org)
        if org and repo:
            github_repo = f"{org}/{repo}"
            os.environ["GITHUB_REPOSITORY"] = github_repo
            log.debug("Set GITHUB_REPOSITORY=%s", github_repo)
        if pr:
            os.environ["PR_NUMBER"] = str(pr)
            os.environ["SYNC_ALL_OPEN_PRS"] = "false"
            log.debug("Set PR_NUMBER=%s", pr)
        else:
            os.environ["SYNC_ALL_OPEN_PRS"] = "true"
            log.debug("Set SYNC_ALL_OPEN_PRS=true")
        os.environ["G2G_TARGET_URL"] = "1"
    # Debug: Show environment at CLI startup
    log.debug("CLI startup environment check:")
    for key in ["DRY_RUN", "CI_TESTING", "GERRIT_SERVER", "GERRIT_PROJECT"]:
        value = os.environ.get(key, "NOT_SET")
        log.debug("  %s = %s", key, value)

    # Delegate to common processing path
    try:
        _process()
    except typer.Exit:
        # Propagate expected exit codes (e.g., validation errors)
        raise
    except Exception as exc:
        log.debug("main(): _process failed: %s", exc)
        raise typer.Exit(code=1) from exc


def _setup_logging() -> logging.Logger:
    level_name = os.getenv("G2G_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s %(levelname)-8s %(name)s %(filename)s:%(lineno)d | %(message)s"
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
        organization=env_str("ORGANIZATION", env_str("GITHUB_REPOSITORY_OWNER")),
        reviewers_email=env_str("REVIEWERS_EMAIL", ""),
        preserve_github_prs=env_bool("PRESERVE_GITHUB_PRS", False),
        dry_run=env_bool("DRY_RUN", False),
        normalise_commit=env_bool("NORMALISE_COMMIT", True),
        gerrit_server=env_str("GERRIT_SERVER", ""),
        gerrit_server_port=env_str("GERRIT_SERVER_PORT", "29418"),
        gerrit_project=env_str("GERRIT_PROJECT"),
        issue_id=env_str("ISSUE_ID", ""),
        allow_duplicates=env_bool("ALLOW_DUPLICATES", False),
        ci_testing=env_bool("CI_TESTING", False),
        duplicates_filter=env_str("DUPLICATE_TYPES", "open"),
    )


def _process_bulk(data: Inputs, gh: GitHubContext) -> bool:
    client = build_client()
    repo = get_repo_from_env(client)

    all_urls: list[str] = []
    all_nums: list[str] = []
    all_shas: list[str] = []

    prs_list = list(iter_open_pulls(repo))
    log.info("Found %d open PRs to process", len(prs_list))

    # Result tracking for summary
    processed_count = 0
    succeeded_count = 0
    skipped_count = 0
    failed_count = 0

    # Use bounded parallel processing with shared clients
    max_workers = min(4, max(1, len(prs_list)))  # Cap at 4 workers

    def process_single_pr(
        pr_data: tuple[Any, models.GitHubContext],
    ) -> tuple[str, SubmissionResult | None, Exception | None]:
        """Process a single PR and return (status, result, exception)."""
        pr, per_ctx = pr_data
        pr_number = int(getattr(pr, "number", 0) or 0)

        if pr_number <= 0:
            return "invalid", None, None

        log.info("Starting processing of PR #%d", pr_number)
        log.debug(
            "Processing PR #%d in multi-PR mode with event_name=%s, event_action=%s",
            pr_number,
            gh.event_name,
            gh.event_action,
        )

        try:
            if data.duplicates_filter:
                os.environ["DUPLICATE_TYPES"] = data.duplicates_filter
            check_for_duplicates(per_ctx, allow_duplicates=data.allow_duplicates)
        except DuplicateChangeError as exc:
            log_exception_conditionally(log, "Skipping PR #%d", pr_number)
            log.warning(
                "Skipping PR #%d due to duplicate detection: %s. Use --allow-duplicates to override this check.",
                pr_number,
                exc,
            )
            return "skipped", None, exc

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                workspace = Path(temp_dir)
                orch = Orchestrator(workspace=workspace)
                result_multi = orch.execute(inputs=data, gh=per_ctx)
                return "success", result_multi, None
        except Exception as exc:
            log_exception_conditionally(log, "Failed to process PR #%d", pr_number)
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
        log.info("Processing %d PRs with %d parallel workers", len(pr_tasks), max_workers)

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
                            log.info("PR #%d created Gerrit change: %s", pr_number, url)
                    if result_multi.change_numbers:
                        all_nums.extend(result_multi.change_numbers)
                        log.info("PR #%d change numbers: %s", pr_number, result_multi.change_numbers)
                    if result_multi.commit_shas:
                        all_shas.extend(result_multi.commit_shas)
                elif status == "skipped":
                    skipped_count += 1
                elif status == "failed":
                    failed_count += 1
                    typer.echo(f"Failed to process PR #{pr_number}: {exc}")
                    log.info("Continuing to next PR despite failure")
                else:
                    failed_count += 1

            except Exception as exc:
                failed_count += 1
                log_exception_conditionally(log, "Failed to process PR #%d", pr_number)
                typer.echo(f"Failed to process PR #{pr_number}: {exc}")
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
            "gerrit_change_request_url": "\n".join(all_urls) if all_urls else "",
            "gerrit_change_request_num": "\n".join(all_nums) if all_nums else "",
            "gerrit_commit_sha": "\n".join(all_shas) if all_shas else "",
        }
    )

    # Summary block
    log.info("=" * 60)
    log.info("BULK PROCESSING SUMMARY:")
    log.info("  Total PRs processed: %d", processed_count)
    log.info("  Succeeded: %d", succeeded_count)
    log.info("  Skipped (duplicates): %d", skipped_count)
    log.info("  Failed: %d", failed_count)
    log.info("  Gerrit changes created: %d", len(all_urls))
    log.info("=" * 60)

    # Return True if no failures occurred
    return failed_count == 0


def _process_single(data: Inputs, gh: GitHubContext) -> bool:
    # Create temporary directory for all git operations
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)

        try:
            _prepare_local_checkout(workspace, gh, data)
        except Exception as exc:
            log.debug("Local checkout preparation failed: %s", exc)

        orch = Orchestrator(workspace=workspace)
        pipeline_success = False
        try:
            result = orch.execute(inputs=data, gh=gh)
            pipeline_success = True
        except Exception as exc:
            log.debug("Execution failed; continuing to write outputs: %s", exc)

            result = SubmissionResult(change_urls=[], change_numbers=[], commit_shas=[])
        if result.change_urls:
            os.environ["GERRIT_CHANGE_REQUEST_URL"] = "\n".join(result.change_urls)
            # Output Gerrit change URL(s) to console
            for url in result.change_urls:
                log.info("Gerrit change URL: %s", url)
        if result.change_numbers:
            os.environ["GERRIT_CHANGE_REQUEST_NUM"] = "\n".join(result.change_numbers)
        if result.commit_shas:
            os.environ["GERRIT_COMMIT_SHA"] = "\n".join(result.commit_shas)

        # Also write outputs to GITHUB_OUTPUT if available
        append_github_output(
            {
                "gerrit_change_request_url": "\n".join(result.change_urls) if result.change_urls else "",
                "gerrit_change_request_num": "\n".join(result.change_numbers) if result.change_numbers else "",
                "gerrit_commit_sha": "\n".join(result.commit_shas) if result.commit_shas else "",
            }
        )

        return pipeline_success


def _prepare_local_checkout(workspace: Path, gh: GitHubContext, data: Inputs) -> None:
    repo_full = gh.repository.strip() if gh.repository else ""
    server_url = gh.server_url or os.getenv("GITHUB_SERVER_URL", "https://github.com")
    server_url = (server_url or "https://github.com").rstrip("/")
    base_ref = gh.base_ref or ""
    pr_num_str: str = str(gh.pr_number) if gh.pr_number else "0"

    if not repo_full:
        return

    # Try SSH first for private repos if available, then fall back to HTTPS/API
    repo_ssh_url = f"git@{server_url.replace('https://', '').replace('http://', '')}:{repo_full}.git"
    repo_https_url = f"{server_url}/{repo_full}.git"

    run_cmd(["git", "init"], cwd=workspace)

    # Determine which URL to use and set up authentication
    env: dict[str, str] = {}
    repo_url = repo_https_url  # Default to HTTPS

    # Check if we should try SSH for private repos
    use_ssh = False
    respect_user_ssh = os.getenv("G2G_RESPECT_USER_SSH", "false").lower() in ("true", "1", "yes")
    gerrit_ssh_privkey = os.getenv("GERRIT_SSH_PRIVKEY_G2G")

    log.debug(
        "GitHub repo access decision: SSH URL available=%s, G2G_RESPECT_USER_SSH=%s, GERRIT_SSH_PRIVKEY_G2G=%s",
        repo_ssh_url.startswith("git@"),
        respect_user_ssh,
        bool(gerrit_ssh_privkey),
    )

    if repo_ssh_url.startswith("git@"):
        # For private repos, only try SSH if G2G_RESPECT_USER_SSH is explicitly enabled
        # Don't use SSH just because GERRIT_SSH_PRIVKEY_G2G is set (that's for Gerrit, not GitHub)
        if respect_user_ssh:
            use_ssh = True
            repo_url = repo_ssh_url
            log.debug("Using SSH for GitHub repo access due to G2G_RESPECT_USER_SSH=true")
        else:
            log.debug("Not using SSH for GitHub repo access - G2G_RESPECT_USER_SSH not enabled")

    if use_ssh:
        env = {
            "GIT_SSH_COMMAND": build_git_ssh_command(),
            **build_non_interactive_ssh_env(),
        }
        log.debug("Using SSH URL for private repo: %s", repo_url)
    else:
        log.debug("Using HTTPS URL: %s", repo_url)

    run_cmd(["git", "remote", "add", "origin", repo_url], cwd=workspace)

    # Fetch base branch and PR head with fallback to API archive
    fetch_success = False

    if base_ref:
        try:
            branch_ref = f"refs/heads/{base_ref}:refs/remotes/origin/{base_ref}"
            run_cmd(
                [
                    "git",
                    "fetch",
                    f"--depth={data.fetch_depth}",
                    "origin",
                    branch_ref,
                ],
                cwd=workspace,
                env=env,
            )
        except Exception as exc:
            log.debug("Base branch fetch failed for %s: %s", base_ref, exc)

    if pr_num_str:
        try:
            pr_ref = f"refs/pull/{pr_num_str}/head:refs/remotes/origin/pr/{pr_num_str}/head"
            run_cmd(
                [
                    "git",
                    "fetch",
                    f"--depth={data.fetch_depth}",
                    "origin",
                    pr_ref,
                ],
                cwd=workspace,
                env=env,
            )
            run_cmd(
                [
                    "git",
                    "checkout",
                    "-B",
                    "g2g_pr_head",
                    f"refs/remotes/origin/pr/{pr_num_str}/head",
                ],
                cwd=workspace,
                env=env,
            )
            fetch_success = True
        except Exception as exc:
            log.warning("Git fetch failed, attempting API archive fallback: %s", exc)
            # Try API archive fallback for private repos
            try:
                _fallback_to_api_archive(workspace, gh, data, pr_num_str)
                fetch_success = True
            except Exception as api_exc:
                log.exception("API archive fallback also failed")
                raise exc from api_exc

    if not fetch_success and pr_num_str:
        msg = f"Failed to prepare checkout for PR #{pr_num_str}"
        raise RuntimeError(msg)


def _fallback_to_api_archive(workspace: Path, gh: GitHubContext, data: Inputs, pr_num_str: str) -> None:
    """Fallback to GitHub API archive download for private repos."""
    import io
    import json
    import shutil
    import zipfile
    from urllib.request import Request
    from urllib.request import urlopen

    log.info("Attempting API archive fallback for PR #%s", pr_num_str)

    # Get GitHub token for authenticated requests
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        msg = "GITHUB_TOKEN required for API archive fallback"
        raise RuntimeError(msg)

    # Build API URLs
    repo_full = gh.repository
    server_url = gh.server_url or "https://github.com"

    # Construct GitHub API base URL properly
    if "github.com" in server_url:
        # For github.com, use api.github.com
        api_base = "https://api.github.com"
    elif server_url.startswith("https://"):
        # For GitHub Enterprise, append /api/v3
        api_base = server_url.rstrip("/") + "/api/v3"
    else:
        # Fallback for unexpected formats
        api_base = "https://api.github.com"

    # Get PR details to find head SHA
    pr_api_url = f"{api_base}/repos/{repo_full}/pulls/{pr_num_str}"
    log.debug("GitHub API PR URL: %s", pr_api_url)

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "github2gerrit",
    }

    try:
        req = Request(pr_api_url, headers=headers)  # noqa: S310
        with urlopen(req, timeout=30) as response:  # noqa: S310
            pr_data = json.loads(response.read().decode())
    except Exception:
        log.exception("Failed to fetch PR data from GitHub API")
        log.debug("PR API URL was: %s", pr_api_url)
        raise

    head_sha = pr_data["head"]["sha"]

    # Download archive
    archive_url = f"{api_base}/repos/{repo_full}/zipball/{head_sha}"
    log.debug("GitHub API archive URL: %s", archive_url)

    try:
        req = Request(archive_url, headers=headers)  # noqa: S310
        with urlopen(req, timeout=120) as response:  # noqa: S310
            archive_data = response.read()
    except Exception:
        log.exception("Failed to download archive from GitHub API")
        log.debug("Archive URL was: %s", archive_url)
        raise

    # Extract archive
    with zipfile.ZipFile(io.BytesIO(archive_data)) as zf:
        # Find the root directory in the archive (usually repo-sha format)
        members = zf.namelist()
        root_dir = None
        for member in members:
            if "/" in member:
                root_dir = member.split("/")[0]
                break

        if not root_dir:
            msg = "Could not find root directory in archive"
            raise RuntimeError(msg)

        # Extract to temporary location then move contents
        extract_path = workspace / "archive_temp"
        zf.extractall(extract_path)

        # Move contents from extracted root to workspace
        extracted_root = extract_path / root_dir
        for item in extracted_root.iterdir():
            if item.name == ".git":
                continue  # Skip .git if present
            dest = workspace / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            item.rename(dest)

        # Clean up
        shutil.rmtree(extract_path)

    # Set up git for the extracted content
    if not (workspace / ".git").exists():
        run_cmd(["git", "init"], cwd=workspace)

    # Create a commit for the PR content
    run_cmd(["git", "add", "."], cwd=workspace)
    run_cmd(
        [
            "git",
            "commit",
            "-m",
            f"PR #{pr_num_str} content from API archive",
            "--author",
            "GitHub API <noreply@github.com>",
        ],
        cwd=workspace,
    )

    # Create the expected branch
    run_cmd(["git", "checkout", "-B", "g2g_pr_head"], cwd=workspace)

    log.info("Successfully extracted PR #%s content via API archive", pr_num_str)


def _load_effective_inputs() -> Inputs:
    # Build inputs from environment (used by URL callback path)
    data = _build_inputs_from_env()

    # Load per-org configuration and apply to environment before validation
    org_for_cfg = data.organization or os.getenv("ORGANIZATION") or os.getenv("GITHUB_REPOSITORY_OWNER")
    cfg = load_org_config(org_for_cfg)

    # Apply dynamic parameter derivation for missing Gerrit parameters
    cfg = apply_parameter_derivation(cfg, org_for_cfg, save_to_config=True)

    # Debug: Show what configuration would be applied
    log.debug("Configuration to apply: %s", cfg)
    if "DRY_RUN" in cfg:
        log.warning(
            "Configuration contains DRY_RUN=%s, this may override environment DRY_RUN=%s",
            cfg["DRY_RUN"],
            os.getenv("DRY_RUN"),
        )

    apply_config_to_env(cfg)

    # Refresh inputs after applying configuration to environment
    data = _build_inputs_from_env()

    # Derive reviewers from local git config if running locally and unset
    if not os.getenv("REVIEWERS_EMAIL") and (os.getenv("G2G_TARGET_URL") or not os.getenv("GITHUB_EVENT_NAME")):
        try:
            from .gitutils import enumerate_reviewer_emails

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
                    organization=data.organization,
                    reviewers_email=os.environ["REVIEWERS_EMAIL"],
                    preserve_github_prs=data.preserve_github_prs,
                    dry_run=data.dry_run,
                    normalise_commit=data.normalise_commit,
                    gerrit_server=data.gerrit_server,
                    gerrit_server_port=data.gerrit_server_port,
                    gerrit_project=data.gerrit_project,
                    issue_id=data.issue_id,
                    allow_duplicates=data.allow_duplicates,
                    ci_testing=data.ci_testing,
                    duplicates_filter=data.duplicates_filter,
                )
                log.info("Derived reviewers: %s", data.reviewers_email)
        except Exception as exc:
            log.debug("Could not derive reviewers from git config: %s", exc)

    return data


def _augment_pr_refs_if_needed(gh: GitHubContext) -> GitHubContext:
    if os.getenv("G2G_TARGET_URL") and gh.pr_number and (not gh.head_ref or not gh.base_ref):
        try:
            client = build_client()
            repo = get_repo_from_env(client)
            pr_obj = get_pull(repo, int(gh.pr_number))
            base_ref = str(getattr(getattr(pr_obj, "base", object()), "ref", "") or "")
            head_ref = str(getattr(getattr(pr_obj, "head", object()), "ref", "") or "")
            head_sha = str(getattr(getattr(pr_obj, "head", object()), "sha", "") or "")
            if base_ref:
                os.environ["GITHUB_BASE_REF"] = base_ref
                log.info("Resolved base_ref via GitHub API: %s", base_ref)
            if head_ref:
                os.environ["GITHUB_HEAD_REF"] = head_ref
                log.info("Resolved head_ref via GitHub API: %s", head_ref)
            if head_sha:
                os.environ["GITHUB_SHA"] = head_sha
                log.info("Resolved head sha via GitHub API: %s", head_sha)
            return _read_github_context()
        except Exception as exc:
            log.debug("Could not resolve PR refs via GitHub API: %s", exc)
    return gh


def _process() -> None:
    data = _load_effective_inputs()

    # Validate inputs
    try:
        _validate_inputs(data)
    except ConfigurationError as exc:
        log_exception_conditionally(log, "Configuration validation failed")
        typer.echo(f"Configuration validation failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    gh = _read_github_context()
    _log_effective_config(data, gh)

    # Test mode: short-circuit after validation
    if env_bool("G2G_TEST_MODE", False):
        log.info("Validation complete. Ready to execute submission pipeline.")
        typer.echo("Validation complete. Ready to execute submission pipeline.")
        return

    # Bulk mode for URL/workflow_dispatch
    sync_all = env_bool("SYNC_ALL_OPEN_PRS", False)
    if sync_all and (gh.event_name == "workflow_dispatch" or os.getenv("G2G_TARGET_URL")):
        bulk_success = _process_bulk(data, gh)

        # Log external API metrics summary
        try:
            from .external_api import log_api_metrics_summary

            log_api_metrics_summary()
        except Exception as exc:
            log.debug("Failed to log API metrics summary: %s", exc)

        # Final success/failure message for bulk processing
        if bulk_success:
            log.info("Bulk processing completed SUCCESSFULLY ✅")
        else:
            log.error("Bulk processing FAILED ❌")
            raise typer.Exit(code=1)

        return

    if not gh.pr_number:
        log.error(
            "PR_NUMBER is empty. This tool requires a valid pull request context. Current event: %s",
            gh.event_name,
        )
        typer.echo(
            f"PR_NUMBER is empty. This tool requires a valid pull request context. Current event: {gh.event_name}",
            err=True,
        )
        raise typer.Exit(code=2)

    # Test mode handled earlier

    # Execute single-PR submission
    # Augment PR refs via API when in URL mode and token present
    gh = _augment_pr_refs_if_needed(gh)

    # Check for duplicates in single-PR mode (before workspace setup)
    if gh.pr_number and not env_bool("SYNC_ALL_OPEN_PRS", False):
        try:
            if data.duplicates_filter:
                os.environ["DUPLICATE_TYPES"] = data.duplicates_filter
            check_for_duplicates(gh, allow_duplicates=data.allow_duplicates)
        except DuplicateChangeError as exc:
            log_exception_conditionally(
                log,
                "Duplicate detection blocked submission for PR #%d",
                gh.pr_number,
            )
            log.info("Use --allow-duplicates to override this check.")
            raise typer.Exit(code=3) from exc

    pipeline_success = _process_single(data, gh)

    # Log external API metrics summary
    try:
        from .external_api import log_api_metrics_summary

        log_api_metrics_summary()
    except Exception as exc:
        log.debug("Failed to log API metrics summary: %s", exc)

    # Final success/failure message after all cleanup
    if pipeline_success:
        log.info("Submission pipeline completed SUCCESSFULLY ✅")
    else:
        log.error("Submission pipeline FAILED ❌")
        raise typer.Exit(code=1)

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
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
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
        msg = "USE_PR_AS_COMMIT and SUBMIT_SINGLE_COMMITS cannot be enabled at the same time"
        raise ConfigurationError(msg)

    # Context-aware validation: different requirements for GH Actions vs CLI
    is_github_actions = _is_github_actions_context()

    # SSH private key is always required
    required_fields = ["gerrit_ssh_privkey_g2g"]

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
            field for field in ["gerrit_ssh_user_g2g", "gerrit_ssh_user_g2g_email"] if not getattr(data, field)
        ]
        if missing_gerrit_params:
            if data.organization:
                log.info(
                    "Local CLI usage: Gerrit parameters can be derived from "
                    "organization '%s'. Missing: %s. Consider setting "
                    "G2G_ENABLE_DERIVATION=true to enable derivation.",
                    data.organization,
                    ", ".join(missing_gerrit_params),
                )
                # Allow derivation in local mode only if explicitly enabled
                if not env_bool("G2G_ENABLE_DERIVATION", False):
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
                    if is_github_actions:
                        log.error(
                            "These fields can be derived automatically from "
                            "organization '%s' if G2G_ENABLE_DERIVATION=true",
                            data.organization,
                        )
                    else:
                        log.error(
                            "These fields can be derived from organization '%s'",
                            data.organization,
                        )
                        log.error("Set G2G_ENABLE_DERIVATION=true to enable")
                else:
                    log.error("These fields require either explicit values or an ORGANIZATION for derivation")
            raise ConfigurationError(_MSG_MISSING_REQUIRED_INPUT.format(field_name=field_name))

    # Validate fetch depth is a positive integer
    if data.fetch_depth <= 0:
        log.error("Invalid FETCH_DEPTH: %s", data.fetch_depth)
        raise ConfigurationError(_MSG_INVALID_FETCH_DEPTH)

    # Validate Issue ID is a single line string if provided
    if data.issue_id and ("\n" in data.issue_id or "\r" in data.issue_id):
        raise ConfigurationError(_MSG_ISSUE_ID_MULTILINE)


def _log_effective_config(data: Inputs, gh: GitHubContext) -> None:
    # Avoid logging sensitive values
    safe_privkey = _mask_secret(data.gerrit_ssh_privkey_g2g)
    log.info("Effective configuration (sanitized):")
    log.info("  SUBMIT_SINGLE_COMMITS: %s", data.submit_single_commits)
    log.info("  USE_PR_AS_COMMIT: %s", data.use_pr_as_commit)
    log.info("  FETCH_DEPTH: %s", data.fetch_depth)
    known_hosts_status = "<provided>" if data.gerrit_known_hosts else "<will auto-discover>"
    log.info("  GERRIT_KNOWN_HOSTS: %s", known_hosts_status)
    log.info("  GERRIT_SSH_PRIVKEY_G2G: %s", safe_privkey)
    log.info("  GERRIT_SSH_USER_G2G: %s", data.gerrit_ssh_user_g2g)
    log.info("  GERRIT_SSH_USER_G2G_EMAIL: %s", data.gerrit_ssh_user_g2g_email)
    log.info("  ORGANIZATION: %s", data.organization)
    log.info("  REVIEWERS_EMAIL: %s", data.reviewers_email or "")
    log.info("  PRESERVE_GITHUB_PRS: %s", data.preserve_github_prs)
    log.info("  DRY_RUN: %s", data.dry_run)
    log.info("  CI_TESTING: %s", data.ci_testing)
    log.info("  GERRIT_SERVER: %s", data.gerrit_server)
    log.info("  GERRIT_SERVER_PORT: %s", data.gerrit_server_port or "")
    log.info("  GERRIT_PROJECT: %s", data.gerrit_project or "")
    log.info("GitHub context:")
    log.info("  event_name: %s", gh.event_name)
    log.info("  event_action: %s", gh.event_action)
    log.info("  repository: %s", gh.repository)
    log.info("  repository_owner: %s", gh.repository_owner)
    log.info("  pr_number: %s", gh.pr_number)
    log.info("  base_ref: %s", gh.base_ref)
    log.info("  head_ref: %s", gh.head_ref)
    log.info("  sha: %s", gh.sha)


if __name__ == "__main__":
    # Invoke the Typer app when executed as a script.
    # Example:
    #   python -m github2gerrit.cli --help
    app()
