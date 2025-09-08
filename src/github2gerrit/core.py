# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
#
# High-level orchestrator scaffold for the GitHub PR -> Gerrit flow.
#
# This module defines the public orchestration surface and typed data models
# used to execute the end-to-end workflow. The major steps are implemented:
# configuration resolution, commit preparation (single or squash), pushing
# to Gerrit, querying results, and posting comments, with a dry-run mode
# for non-destructive validations.
#
# Design principles applied:
# - Single Responsibility: orchestration logic is grouped here; git/exec
#   helpers live in gitutils.py; CLI argument parsing lives in cli.py.
# - Strict typing: all public functions and data models are typed.
# - Central logging: use Python logging; callers can configure handlers.
# - Compatibility: inputs map 1:1 with the existing shell-based action.
#
# Capabilities overview:
# - Invoked by the Typer CLI entrypoint.
# - Reads .gitreview for Gerrit host/port/project when present; otherwise
#   resolves from explicit inputs.
# - Supports both "single commit" and "squash" submission strategies.
# - Pushes via git-review to refs/for/<branch> and manages Change-Id.
# - Queries Gerrit for URL/change-number and updates PR comments.

from __future__ import annotations

import logging
import os
import re
import shlex
import stat
import urllib.parse
import urllib.request
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .commit_normalization import normalize_commit_title
from .gerrit_urls import create_gerrit_url_builder
from .github_api import build_client
from .github_api import close_pr
from .github_api import create_pr_comment
from .github_api import get_pr_title_body
from .github_api import get_pull
from .github_api import get_recent_change_ids_from_comments
from .github_api import get_repo_from_env
from .github_api import iter_open_pulls
from .gitutils import CommandError
from .gitutils import GitError
from .gitutils import _parse_trailers
from .gitutils import git_cherry_pick
from .gitutils import git_commit_amend
from .gitutils import git_commit_new
from .gitutils import git_config
from .gitutils import git_last_commit_trailers
from .gitutils import git_show
from .gitutils import run_cmd
from .models import GitHubContext
from .models import Inputs
from .pr_content_filter import filter_pr_body
from .ssh_common import merge_known_hosts_content
from .utils import env_bool
from .utils import log_exception_conditionally


try:
    from pygerrit2 import GerritRestAPI
    from pygerrit2 import HTTPBasicAuth
except ImportError:
    GerritRestAPI = None
    HTTPBasicAuth = None

try:
    from .ssh_discovery import SSHDiscoveryError
    from .ssh_discovery import auto_discover_gerrit_host_keys
except ImportError:
    # Fallback if ssh_discovery module is not available
    auto_discover_gerrit_host_keys = None  # type: ignore[assignment]
    SSHDiscoveryError = Exception  # type: ignore[misc,assignment]

try:
    from .ssh_agent_setup import SSHAgentManager
    from .ssh_agent_setup import setup_ssh_agent_auth
except ImportError:
    # Fallback if ssh_agent_setup module is not available
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from .ssh_agent_setup import SSHAgentManager
        from .ssh_agent_setup import setup_ssh_agent_auth
    else:
        SSHAgentManager = None
        setup_ssh_agent_auth = None


log = logging.getLogger("github2gerrit.core")


# Error message constants to comply with TRY003
_MSG_ISSUE_ID_MULTILINE = "Issue ID must be single line"
_MSG_MISSING_PR_CONTEXT = "missing PR context"
_MSG_BAD_REPOSITORY_CONTEXT = "bad repository context"
_MSG_MISSING_GERRIT_SERVER = "missing GERRIT_SERVER"
_MSG_MISSING_GERRIT_PROJECT = "missing GERRIT_PROJECT"
_MSG_PYGERRIT2_REQUIRED_REST = "pygerrit2 is required to query Gerrit REST API"
_MSG_PYGERRIT2_REQUIRED_AUTH = "pygerrit2 is required for HTTP authentication"
_MSG_PYGERRIT2_MISSING = "pygerrit2 missing"
_MSG_PYGERRIT2_AUTH_MISSING = "pygerrit2 auth missing"


def _insert_issue_id_into_commit_message(message: str, issue_id: str) -> str:
    """
    Insert Issue ID into commit message after the first line.

    Format:
    Title line

    Issue-ID: CIMAN-33

    Rest of body...
    """
    if not issue_id.strip():
        return message

    # Validate that Issue ID is a single line string
    cleaned_issue_id = issue_id.strip()
    if "\n" in cleaned_issue_id or "\r" in cleaned_issue_id:
        raise ValueError(_MSG_ISSUE_ID_MULTILINE)

    # Format as proper Issue-ID trailer
    issue_line = cleaned_issue_id if cleaned_issue_id.startswith("Issue-ID:") else f"Issue-ID: {cleaned_issue_id}"

    lines = message.splitlines()
    if not lines:
        return message

    # Take the first line as title
    title = lines[0]

    # Build new message with Issue ID on third line
    new_lines = [title, "", issue_line]

    # Add rest of the body if it exists
    if len(lines) > 1:
        # Skip empty lines immediately after title to avoid double spacing
        body_start = 1
        while body_start < len(lines) and not lines[body_start].strip():
            body_start += 1

        if body_start < len(lines):
            new_lines.append("")  # Empty line before body
            new_lines.extend(lines[body_start:])

    return "\n".join(new_lines)


# ---------------------
# Utility functions
# ---------------------


def _match_first_group(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    if not m:
        return None
    if m.groups():
        return m.group(1)
    return m.group(0)


def _is_valid_change_id(value: str) -> bool:
    # Gerrit Change-Id should match I<40-hex-chars> format
    # Be more strict to avoid accepting invalid Change-IDs
    if not value:
        return False
    # Standard Gerrit format: I followed by exactly 40 hex characters
    if len(value) == 41 and re.fullmatch(r"I[0-9a-fA-F]{40}", value):
        return True
    # Fallback for legacy or non-standard formats (keep some permissiveness)
    # but require it to start with 'I' and be reasonable length (10-40 chars)
    # and NOT look like a malformed hex ID
    return (
        value.startswith("I")
        and 10 <= len(value) <= 40
        and not re.fullmatch(r"I[0-9a-fA-F]+", value)  # Exclude hex-like patterns
        and bool(re.fullmatch(r"I[A-Za-z0-9._-]+", value))
    )


@dataclass(frozen=True)
class GerritInfo:
    host: str
    port: int
    project: str


@dataclass(frozen=True)
class RepoNames:
    # Gerrit repo path, e.g. "releng/builder"
    project_gerrit: str
    # GitHub repo name (no org/owner), e.g. "releng-builder"
    project_github: str


@dataclass(frozen=True)
class PreparedChange:
    # One or more Change-Id values that will be (or were) pushed.
    change_ids: list[str]
    # The commit shas created/pushed to Gerrit. May be empty until queried.
    commit_shas: list[str]


@dataclass(frozen=True)
class SubmissionResult:
    # URLs of created/updated Gerrit changes.
    change_urls: list[str]
    # Numeric change-ids in Gerrit (change number).
    change_numbers: list[str]
    # Associated patch set commit shas in Gerrit (if available).
    commit_shas: list[str]


class OrchestratorError(RuntimeError):
    """Raised on unrecoverable orchestration failures."""


class Orchestrator:
    """Coordinates the end-to-end PR -> Gerrit submission flow.

    Responsibilities (to be implemented):
    - Discover and validate environment and inputs.
    - Derive Gerrit connection and project names.
    - Prepare commits (single or squashed) and manage Change-Id.
    - Push to Gerrit using git-review with topic and reviewers.
    - Query Gerrit for URL/change-number and produce outputs.
    - Comment on the PR and optionally close it.
    """

    def __init__(
        self,
        *,
        workspace: Path,
    ) -> None:
        self.workspace = workspace
        # SSH configuration paths (set by _setup_ssh)
        self._ssh_key_path: Path | None = None
        self._ssh_known_hosts_path: Path | None = None
        self._ssh_agent_manager: SSHAgentManager | None = None
        self._use_ssh_agent: bool = False

    # ---------------
    # Public API
    # ---------------

    def execute(
        self,
        inputs: Inputs,
        gh: GitHubContext,
    ) -> SubmissionResult:
        """Run the full pipeline and return a structured result.

        This method defines the high-level call order. Sub-steps are
        placeholders and must be implemented with real logic. Until then,
        this raises NotImplementedError after logging the intended plan.

        Note: This method is "pure" with respect to external outputs (no direct
        GitHub output writes), but does perform internal environment mutations
        (e.g., G2G_TMP_BRANCH) for subprocess coordination within the workflow.
        """
        log.info("Starting PR -> Gerrit pipeline")
        self._guard_pull_request_context(gh)

        # Initialize git repository in workspace if it doesn't exist
        if not (self.workspace / ".git").exists():
            self._setup_git_workspace(inputs, gh)

        gitreview = self._read_gitreview(self.workspace / ".gitreview", gh)
        repo_names = self._derive_repo_names(gitreview, gh)
        log.debug("execute: inputs.dry_run=%s, inputs.ci_testing=%s", inputs.dry_run, inputs.ci_testing)
        gerrit = self._resolve_gerrit_info(gitreview, inputs, repo_names)

        log.debug("execute: resolved gerrit info: %s", gerrit)
        if inputs.dry_run:
            log.debug("execute: entering dry-run mode due to inputs.dry_run=True")
            # Perform preflight validations and exit without making changes
            self._dry_run_preflight(gerrit=gerrit, inputs=inputs, gh=gh, repo=repo_names)
            log.info("Dry run complete; skipping write operations to Gerrit")
            return SubmissionResult(change_urls=[], change_numbers=[], commit_shas=[])
        self._setup_ssh(inputs, gerrit)
        # Establish baseline non-interactive SSH/Git environment
        # for all child processes
        os.environ.update(self._ssh_env())

        # Ensure commit/tag signing is disabled before any commit operations
        # to avoid agent prompts
        try:
            git_config(
                "commit.gpgsign",
                "false",
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config("commit.gpgsign", "false", global_=True)
        try:
            git_config(
                "tag.gpgsign",
                "false",
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config("tag.gpgsign", "false", global_=True)

        if inputs.submit_single_commits:
            prep = self._prepare_single_commits(inputs, gh, gerrit)
        else:
            prep = self._prepare_squashed_commit(inputs, gh, gerrit)

        self._configure_git(gerrit, inputs)
        self._apply_pr_title_body_if_requested(inputs, gh)

        self._push_to_gerrit(
            gerrit=gerrit,
            repo=repo_names,
            branch=self._resolve_target_branch(),
            reviewers=self._resolve_reviewers(inputs),
            single_commits=inputs.submit_single_commits,
        )

        result = self._query_gerrit_for_results(
            gerrit=gerrit,
            repo=repo_names,
            change_ids=prep.change_ids,
        )

        self._add_backref_comment_in_gerrit(
            gerrit=gerrit,
            repo=repo_names,
            branch=self._resolve_target_branch(),
            commit_shas=result.commit_shas,
            gh=gh,
        )

        self._comment_on_pull_request(gh, gerrit, result)

        self._close_pull_request_if_required(gh)

        log.info("Pipeline complete: %s", result)
        self._cleanup_ssh()
        return result

    # ---------------
    # Step scaffolds
    # ---------------

    def _guard_pull_request_context(self, gh: GitHubContext) -> None:
        if gh.pr_number is None:
            raise OrchestratorError(_MSG_MISSING_PR_CONTEXT)
        log.debug("PR context OK: #%s", gh.pr_number)

    def _parse_gitreview_text(self, text: str) -> GerritInfo | None:
        host = _match_first_group(r"(?m)^host=(.+)$", text)
        port_s = _match_first_group(r"(?m)^port=(\d+)$", text)
        proj = _match_first_group(r"(?m)^project=(.+)$", text)
        if host and proj:
            project = proj.removesuffix(".git")
            port = int(port_s) if port_s else 29418
            return GerritInfo(
                host=host.strip(),
                port=port,
                project=project.strip(),
            )
        return None

    def _read_gitreview(
        self,
        path: Path,
        gh: GitHubContext | None = None,
    ) -> GerritInfo | None:
        """Read .gitreview and return GerritInfo if present.

        Expected keys:
          host=<hostname>
          port=<port>
          project=<repo/path>.git
        """
        if not path.exists():
            log.info(".gitreview not found locally; attempting remote fetch")
            # If invoked via direct URL or in environments with a token,
            # attempt to read .gitreview from the repository using the API.
            try:
                client = build_client()
                repo_obj: Any = get_repo_from_env(client)
                # Prefer a specific ref when available; otherwise default branch
                ref = os.getenv("GITHUB_HEAD_REF") or os.getenv("GITHUB_SHA")
                content = repo_obj.get_contents(".gitreview", ref=ref) if ref else repo_obj.get_contents(".gitreview")
                text_remote = (getattr(content, "decoded_content", b"") or b"").decode("utf-8")
                info_remote = self._parse_gitreview_text(text_remote)
                if info_remote:
                    log.debug("Parsed remote .gitreview: %s", info_remote)
                    return info_remote
                log.info("Remote .gitreview missing required keys; ignoring")
            except Exception as exc:
                log.debug("Remote .gitreview not available: %s", exc)
            # Attempt raw.githubusercontent.com as a fallback
            try:
                repo_full = ((gh.repository if gh else os.getenv("GITHUB_REPOSITORY", "")) or "").strip()
                branches: list[str] = []
                # Prefer PR head/base refs via GitHub API when running
                # from a direct URL when a token is available
                try:
                    if (
                        gh
                        and gh.pr_number
                        and os.getenv("G2G_TARGET_URL")
                        and (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"))
                    ):
                        client = build_client()
                        repo_obj = get_repo_from_env(client)
                        pr_obj = get_pull(repo_obj, int(gh.pr_number))
                        api_head = str(getattr(getattr(pr_obj, "head", object()), "ref", "") or "")
                        api_base = str(getattr(getattr(pr_obj, "base", object()), "ref", "") or "")
                        if api_head:
                            branches.append(api_head)
                        if api_base:
                            branches.append(api_base)
                except Exception as exc_api:
                    log.debug(
                        "Could not resolve PR refs via API for .gitreview: %s",
                        exc_api,
                    )
                if gh and gh.head_ref:
                    branches.append(gh.head_ref)
                if gh and gh.base_ref:
                    branches.append(gh.base_ref)
                branches.extend(["master", "main"])
                tried: set[str] = set()
                for br in branches:
                    if not br or br in tried:
                        continue
                    tried.add(br)
                    url = f"https://raw.githubusercontent.com/{repo_full}/refs/heads/{br}/.gitreview"
                    parsed = urllib.parse.urlparse(url)
                    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
                        continue
                    log.info("Fetching .gitreview via raw URL: %s", url)
                    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                        text_remote = resp.read().decode("utf-8")
                    info_remote = self._parse_gitreview_text(text_remote)
                    if info_remote:
                        log.debug("Parsed remote .gitreview: %s", info_remote)
                        return info_remote
            except Exception as exc2:
                log.debug("Raw .gitreview fetch failed: %s", exc2)
            log.info("Remote .gitreview not available via API or HTTP")
            log.info("Falling back to inputs/env")
            return None

        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            msg = f"failed to read .gitreview: {exc}"
            raise OrchestratorError(msg) from exc
        info_local = self._parse_gitreview_text(text)
        if not info_local:
            msg = "invalid .gitreview: missing host/project"
            raise OrchestratorError(msg)
        log.debug("Parsed .gitreview: %s", info_local)
        return info_local

    def _derive_repo_names(
        self,
        gitreview: GerritInfo | None,
        gh: GitHubContext,
    ) -> RepoNames:
        """Compute Gerrit and GitHub repo names following existing rules.

        - Gerrit project remains as-is (from .gitreview when present).
        - GitHub repo name is Gerrit project path with '/' replaced by '-'.
          If .gitreview is not available, derive from GITHUB_REPOSITORY.
        """
        if gitreview:
            gerrit_name = gitreview.project
            github_name = gerrit_name.replace("/", "-")
            names = RepoNames(
                project_gerrit=gerrit_name,
                project_github=github_name,
            )
            log.debug("Derived names from .gitreview: %s", names)
            return names

        # Fallback: use the repository name portion only.
        repo_full = gh.repository
        if not repo_full or "/" not in repo_full:
            raise OrchestratorError(_MSG_BAD_REPOSITORY_CONTEXT)
        owner, name = repo_full.split("/", 1)
        # Fallback: map all '-' to '/' for Gerrit path (e.g., 'my/repo/name')
        gerrit_name = name.replace("-", "/")
        names = RepoNames(project_gerrit=gerrit_name, project_github=name)
        log.debug("Derived names from context: %s", names)
        return names

    def _resolve_gerrit_info(
        self,
        gitreview: GerritInfo | None,
        inputs: Inputs,
        repo: RepoNames,
    ) -> GerritInfo:
        """Resolve Gerrit connection info from .gitreview or inputs."""
        log.debug("_resolve_gerrit_info: inputs.ci_testing=%s", inputs.ci_testing)
        log.debug("_resolve_gerrit_info: gitreview=%s", gitreview)

        # If CI testing flag is set, ignore .gitreview and use environment
        if inputs.ci_testing:
            log.info("CI_TESTING enabled: ignoring .gitreview file")
            gitreview = None

        if gitreview:
            log.debug("Using .gitreview settings: %s", gitreview)
            return gitreview

        host = inputs.gerrit_server.strip()
        if not host:
            raise OrchestratorError(_MSG_MISSING_GERRIT_SERVER)
        port_s = inputs.gerrit_server_port.strip() or "29418"
        try:
            port = int(port_s)
        except ValueError as exc:
            msg = "bad GERRIT_SERVER_PORT"
            raise OrchestratorError(msg) from exc

        project = inputs.gerrit_project.strip()
        if not project:
            if inputs.dry_run:
                project = repo.project_gerrit
                log.info("Dry run: using derived Gerrit project '%s'", project)
            elif os.getenv("G2G_TARGET_URL", "").strip():
                project = repo.project_gerrit
                log.info(
                    "Using derived Gerrit project '%s' from repository name",
                    project,
                )
            else:
                raise OrchestratorError(_MSG_MISSING_GERRIT_PROJECT)

        info = GerritInfo(host=host, port=port, project=project)
        log.debug("Resolved Gerrit info: %s", info)
        return info

    def _setup_ssh(self, inputs: Inputs, gerrit: GerritInfo) -> None:
        """Set up temporary SSH configuration for Gerrit access.

        This method creates tool-specific SSH files in the workspace without
        modifying user SSH configuration. Key features:

        - Creates temporary SSH key and known_hosts files
        - Uses GIT_SSH_COMMAND to specify exact SSH behavior
        - Prevents SSH agent scanning with IdentitiesOnly=yes
        - Host-specific configuration without global impact
        - Automatic cleanup when done

        Does not modify user files.
        """
        if not inputs.gerrit_ssh_privkey_g2g:
            log.debug("SSH private key not provided, skipping SSH setup")
            return

        # Auto-discover or augment host keys (merge missing types/[host]:port entries)
        effective_known_hosts = inputs.gerrit_known_hosts
        if auto_discover_gerrit_host_keys is not None:
            try:
                if not effective_known_hosts:
                    log.info("GERRIT_KNOWN_HOSTS not provided, attempting auto-discovery...")
                    discovered_keys = auto_discover_gerrit_host_keys(
                        gerrit_hostname=gerrit.host,
                        gerrit_port=gerrit.port,
                        organization=inputs.organization,
                        save_to_config=True,
                    )
                    if discovered_keys:
                        effective_known_hosts = discovered_keys
                        log.info(
                            "Successfully auto-discovered SSH host keys for %s:%d",
                            gerrit.host,
                            gerrit.port,
                        )
                    else:
                        log.warning("Auto-discovery failed, SSH host key verification may fail")
                else:
                    # Provided known_hosts exists; ensure it contains [host]:port entries and modern key types
                    lower = effective_known_hosts.lower()
                    bracket_host = f"[{gerrit.host}]:{gerrit.port}"
                    bracket_lower = bracket_host.lower()
                    needs_discovery = False
                    if bracket_lower not in lower:
                        needs_discovery = True
                    else:
                        # Confirm at least one known key type exists for the bracketed host
                        if (
                            f"{bracket_lower} ssh-ed25519" not in lower
                            and f"{bracket_lower} ecdsa-sha2" not in lower
                            and f"{bracket_lower} ssh-rsa" not in lower
                        ):
                            needs_discovery = True
                    if needs_discovery:
                        log.info(
                            "Augmenting provided GERRIT_KNOWN_HOSTS with discovered entries for %s:%d",
                            gerrit.host,
                            gerrit.port,
                        )
                        discovered_keys = auto_discover_gerrit_host_keys(
                            gerrit_hostname=gerrit.host,
                            gerrit_port=gerrit.port,
                            organization=inputs.organization,
                            save_to_config=True,
                        )
                        if discovered_keys:
                            # Use centralized merging logic
                            effective_known_hosts = merge_known_hosts_content(effective_known_hosts, discovered_keys)
                            log.info(
                                "Known hosts augmented with discovered entries for %s:%d",
                                gerrit.host,
                                gerrit.port,
                            )
                        else:
                            log.warning("Auto-discovery returned no keys; known_hosts not augmented")
            except Exception as exc:
                log.warning("SSH host key auto-discovery/augmentation failed: %s", exc)

        if not effective_known_hosts:
            log.debug("No SSH host keys available (manual or auto-discovered), skipping SSH setup")
            return

        # Check if SSH agent authentication is preferred
        use_ssh_agent = env_bool("G2G_USE_SSH_AGENT", default=True)

        if use_ssh_agent and setup_ssh_agent_auth is not None:
            # Try SSH agent first as it's more secure and avoids file permission issues
            if self._try_ssh_agent_setup(inputs, effective_known_hosts):
                return

            # Fall back to file-based SSH if agent setup fails
            log.info("Falling back to file-based SSH authentication")

        self._setup_file_based_ssh(inputs, effective_known_hosts)

    def _try_ssh_agent_setup(self, inputs: Inputs, effective_known_hosts: str) -> bool:
        """Try to setup SSH agent-based authentication.

        Args:
            inputs: Validated input configuration
            effective_known_hosts: Known hosts content

        Returns:
            True if SSH agent setup succeeded, False otherwise
        """
        if setup_ssh_agent_auth is None:
            log.debug("SSH agent module not available, falling back to file-based SSH")  # type: ignore[unreachable]
            return False

        try:
            log.info("Setting up SSH agent-based authentication (more secure)")
            self._ssh_agent_manager = setup_ssh_agent_auth(
                workspace=self.workspace,
                private_key_content=inputs.gerrit_ssh_privkey_g2g,
                known_hosts_content=effective_known_hosts,
            )
            self._use_ssh_agent = True
            log.info("SSH agent authentication configured successfully")

        except Exception as exc:
            log.warning("SSH agent setup failed, falling back to file-based SSH: %s", exc)
            if self._ssh_agent_manager:
                self._ssh_agent_manager.cleanup()
                self._ssh_agent_manager = None
            return False
        else:
            return True

    def _setup_file_based_ssh(self, inputs: Inputs, effective_known_hosts: str) -> None:
        """Setup file-based SSH authentication as fallback.

        Args:
            inputs: Validated input configuration
            effective_known_hosts: Known hosts content
        """
        log.info("Setting up file-based SSH configuration for Gerrit")
        log.debug("Using workspace-specific SSH files to avoid user changes")

        # Create tool-specific SSH directory in workspace to avoid touching
        # user files
        tool_ssh_dir = self.workspace / ".ssh-g2g"
        tool_ssh_dir.mkdir(mode=0o700, exist_ok=True)

        # Write SSH private key to tool-specific location with secure permissions
        key_path = tool_ssh_dir / "gerrit_key"

        # Use a more robust approach for creating the file with secure permissions
        key_content = inputs.gerrit_ssh_privkey_g2g.strip() + "\n"

        # Multiple strategies to create secure key file
        success = self._create_secure_key_file(key_path, key_content)

        if not success:
            # If all permission strategies fail, create in memory directory
            success = self._create_key_in_memory_fs(key_path, key_content)

        if not success:
            msg = (
                "Failed to create SSH key file with secure permissions. "
                "This may be due to CI environment restrictions. "
                "Consider using G2G_USE_SSH_AGENT=true (default) for SSH agent authentication."
            )
            raise RuntimeError(msg)

        # Write known hosts to tool-specific location
        known_hosts_path = tool_ssh_dir / "known_hosts"
        with open(known_hosts_path, "w", encoding="utf-8") as f:
            f.write(effective_known_hosts.strip() + "\n")
        known_hosts_path.chmod(0o644)
        log.debug("Known hosts written to %s", known_hosts_path)
        log.debug("Using isolated known_hosts to prevent user conflicts")

        # Store paths for later use in git commands
        self._ssh_key_path = key_path
        self._ssh_known_hosts_path = known_hosts_path

    def _create_secure_key_file(self, key_path: Path, key_content: str) -> bool:
        """Try multiple strategies to create a secure SSH key file.

        Args:
            key_path: Path where to create the key file
            key_content: SSH key content

        Returns:
            True if successful, False otherwise
        """

        strategies = [
            ("touch+chmod", self._strategy_touch_chmod),
            ("open+fchmod", self._strategy_open_fchmod),
            ("umask+open", self._strategy_umask_open),
            ("stat_constants", self._strategy_stat_constants),
        ]

        for strategy_name, strategy_func in strategies:
            try:
                log.debug("Trying SSH key creation strategy: %s", strategy_name)

                # Remove file if it exists to start fresh
                if key_path.exists():
                    key_path.unlink()

                # Try the strategy
                strategy_func(key_path, key_content)

                # Verify permissions
                actual_perms = oct(key_path.stat().st_mode)[-3:]
                if actual_perms == "600":
                    log.debug("SSH key created successfully with strategy: %s", strategy_name)
                    return True
                else:
                    log.debug("Strategy %s resulted in permissions %s", strategy_name, actual_perms)

            except Exception as exc:
                log.debug("Strategy %s failed: %s", strategy_name, exc)
                if key_path.exists():
                    try:
                        key_path.unlink()
                    except Exception as cleanup_exc:
                        log.debug("Failed to cleanup key file: %s", cleanup_exc)

        return False

    def _strategy_touch_chmod(self, key_path: Path, key_content: str) -> None:
        """Strategy: touch with mode, then write, then chmod."""
        key_path.touch(mode=0o600)
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(key_content)
        key_path.chmod(0o600)

    def _strategy_open_fchmod(self, key_path: Path, key_content: str) -> None:
        """Strategy: open with os.open and specific flags, then fchmod."""
        import os
        import stat

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        mode = stat.S_IRUSR | stat.S_IWUSR  # 0o600

        fd = os.open(str(key_path), flags, mode)
        try:
            os.fchmod(fd, mode)
            os.write(fd, key_content.encode("utf-8"))
        finally:
            os.close(fd)

    def _strategy_umask_open(self, key_path: Path, key_content: str) -> None:
        """Strategy: set umask, create file, restore umask."""
        import os

        original_umask = os.umask(0o077)  # Only owner can read/write
        try:
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(key_content)
            key_path.chmod(0o600)
        finally:
            os.umask(original_umask)

    def _strategy_stat_constants(self, key_path: Path, key_content: str) -> None:
        """Strategy: use stat constants for permission setting."""
        import os
        import stat

        with open(key_path, "w", encoding="utf-8") as f:
            f.write(key_content)

        # Try multiple permission setting approaches
        mode = stat.S_IRUSR | stat.S_IWUSR
        os.chmod(str(key_path), mode)
        key_path.chmod(mode)

    def _create_key_in_memory_fs(self, key_path: Path, key_content: str) -> bool:
        """Fallback: try to create key in memory filesystem."""
        import shutil
        import tempfile

        try:
            # Try to create in memory filesystem if available
            # Use secure temporary directories
            import tempfile

            temp_dir = tempfile.gettempdir()
            memory_dirs = [temp_dir]

            # Only add /dev/shm if it exists and is accessible
            import os

            dev_shm = Path("/dev/shm")  # noqa: S108
            if dev_shm.exists() and os.access("/dev/shm", os.W_OK):  # noqa: S108
                memory_dirs.insert(0, "/dev/shm")  # noqa: S108

            for memory_dir in memory_dirs:
                if not Path(memory_dir).exists():
                    continue

                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", dir=memory_dir, prefix="g2g_key_", suffix=".tmp", delete=False
                    ) as tmp_file:
                        tmp_file.write(key_content)
                        tmp_path = Path(tmp_file.name)

                    # Try to set permissions
                    tmp_path.chmod(0o600)
                    actual_perms = oct(tmp_path.stat().st_mode)[-3:]

                    if actual_perms == "600":
                        # Move to final location
                        shutil.move(str(tmp_path), str(key_path))
                        log.debug("Successfully created SSH key using memory filesystem: %s", memory_dir)
                        return True
                    else:
                        tmp_path.unlink()

                except Exception as exc:
                    log.debug("Memory filesystem strategy failed for %s: %s", memory_dir, exc)
                    try:
                        if tmp_path is not None and tmp_path.exists():
                            tmp_path.unlink()
                    except Exception as cleanup_exc:
                        log.debug("Failed to cleanup temporary key file: %s", cleanup_exc)

        except Exception as exc:
            log.debug("Memory filesystem fallback failed: %s", exc)

        return False

    @property
    def _build_git_ssh_command(self) -> str | None:
        """Generate GIT_SSH_COMMAND for secure, isolated SSH configuration.

        This prevents SSH from scanning the user's SSH agent or using
        unintended keys by setting IdentitiesOnly=yes and specifying
        exact key and known_hosts files.
        """
        if self._use_ssh_agent and self._ssh_agent_manager:
            return self._ssh_agent_manager.get_git_ssh_command()

        if not self._ssh_key_path or not self._ssh_known_hosts_path:
            return None

        # Delegate to centralized SSH command builder
        from .ssh_common import build_git_ssh_command

        return build_git_ssh_command(
            key_path=self._ssh_key_path,
            known_hosts_path=self._ssh_known_hosts_path,
        )

    def _ssh_env(self) -> dict[str, str]:
        """Centralized non-interactive SSH/Git environment."""
        from .ssh_common import build_non_interactive_ssh_env

        env = build_non_interactive_ssh_env()

        # Set GIT_SSH_COMMAND based on available configuration
        cmd = self._build_git_ssh_command
        if cmd:
            env["GIT_SSH_COMMAND"] = cmd
        else:
            # Fallback to basic non-interactive SSH command
            from .ssh_common import build_git_ssh_command

            env["GIT_SSH_COMMAND"] = build_git_ssh_command()

        # Override SSH agent settings if using SSH agent
        if self._use_ssh_agent and self._ssh_agent_manager:
            env.update(self._ssh_agent_manager.get_ssh_env())

        return env

    def _cleanup_ssh(self) -> None:
        """Clean up temporary SSH files created by this tool.

        Removes the workspace-specific .ssh-g2g directory and all contents.
        This ensures no temporary files are left behind.
        """
        log.debug("Cleaning up temporary SSH configuration files")

        try:
            # Clean up SSH agent if we used it
            if self._ssh_agent_manager:
                self._ssh_agent_manager.cleanup()
                self._ssh_agent_manager = None
                self._use_ssh_agent = False

            # Remove temporary SSH directory and all contents
            tool_ssh_dir = self.workspace / ".ssh-g2g"
            if tool_ssh_dir.exists():
                import shutil

                shutil.rmtree(tool_ssh_dir)
                log.debug("Cleaned up temporary SSH directory: %s", tool_ssh_dir)
        except Exception as exc:
            log.warning("Failed to clean up temporary SSH files: %s", exc)

    def _configure_git(
        self,
        gerrit: GerritInfo,
        inputs: Inputs,
    ) -> None:
        """Set git global config and initialize git-review if needed."""
        log.info("Configuring git and git-review for %s", gerrit.host)
        # Prefer repo-local config; fallback to global if needed
        try:
            git_config(
                "gitreview.username",
                inputs.gerrit_ssh_user_g2g,
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config("gitreview.username", inputs.gerrit_ssh_user_g2g, global_=True)
        try:
            git_config(
                "user.name",
                inputs.gerrit_ssh_user_g2g,
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config("user.name", inputs.gerrit_ssh_user_g2g, global_=True)
        try:
            git_config(
                "user.email",
                inputs.gerrit_ssh_user_g2g_email,
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config("user.email", inputs.gerrit_ssh_user_g2g_email, global_=True)
        # Disable GPG signing to avoid interactive prompts for signing keys
        try:
            git_config(
                "commit.gpgsign",
                "false",
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config("commit.gpgsign", "false", global_=True)
        try:
            git_config(
                "tag.gpgsign",
                "false",
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config("tag.gpgsign", "false", global_=True)

        # Ensure git-review host/port/project are configured
        # when .gitreview is absent
        try:
            git_config(
                "gitreview.hostname",
                gerrit.host,
                global_=False,
                cwd=self.workspace,
            )
            git_config(
                "gitreview.port",
                str(gerrit.port),
                global_=False,
                cwd=self.workspace,
            )
            git_config(
                "gitreview.project",
                gerrit.project,
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config("gitreview.hostname", gerrit.host, global_=True)
            git_config("gitreview.port", str(gerrit.port), global_=True)
            git_config("gitreview.project", gerrit.project, global_=True)

        # Add 'gerrit' remote if missing (required by git-review)
        try:
            run_cmd(
                ["git", "config", "--get", "remote.gerrit.url"],
                cwd=self.workspace,
            )
        except CommandError:
            ssh_user = inputs.gerrit_ssh_user_g2g.strip()
            remote_url = f"ssh://{ssh_user}@{gerrit.host}:{gerrit.port}/{gerrit.project}"
            log.info("Adding 'gerrit' remote: %s", remote_url)
            # Use our specific SSH configuration for adding remote
            env = self._ssh_env()
            run_cmd(
                ["git", "remote", "add", "gerrit", remote_url],
                check=False,
                cwd=self.workspace,
                env=env,
            )

        # Workaround for submodules commit-msg hook
        hooks_path = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=self.workspace).stdout.strip()
        try:
            git_config(
                "core.hooksPath",
                str(Path(hooks_path) / ".git" / "hooks"),
                cwd=self.workspace,
            )
        except GitError:
            git_config(
                "core.hooksPath",
                str(Path(hooks_path) / ".git" / "hooks"),
                global_=True,
            )
        # Initialize git-review (copies commit-msg hook)
        try:
            # Use our specific SSH configuration for git-review setup
            env = self._ssh_env()
            run_cmd(["git", "review", "-s", "-v"], cwd=self.workspace, env=env)
        except CommandError as exc:
            msg = f"Failed to initialize git-review: {exc}"
            raise OrchestratorError(msg) from exc

    def _prepare_single_commits(
        self,
        inputs: Inputs,
        gh: GitHubContext,
        gerrit: GerritInfo,
    ) -> PreparedChange:
        """Cherry-pick commits one-by-one and ensure Change-Id is present."""
        log.info("Preparing single-commit submission for PR #%s", gh.pr_number)
        branch = self._resolve_target_branch()
        # Determine commit range: commits in HEAD not in base branch
        base_ref = f"origin/{branch}"
        # Use our SSH command for git operations that might need SSH

        run_cmd(
            ["git", "fetch", "origin", branch],
            cwd=self.workspace,
            env=self._ssh_env(),
        )
        revs = run_cmd(
            ["git", "rev-list", "--reverse", f"{base_ref}..HEAD"],
            cwd=self.workspace,
        ).stdout
        commit_list = [c for c in revs.splitlines() if c.strip()]
        if not commit_list:
            log.info("No commits to submit; returning empty PreparedChange")
            return PreparedChange(change_ids=[], commit_shas=[])
        # Create temp branch from base sha; export for downstream
        base_sha = run_cmd(["git", "rev-parse", base_ref], cwd=self.workspace).stdout.strip()
        tmp_branch = f"g2g_tmp_{gh.pr_number or 'pr'!s}_{os.getpid()}"
        os.environ["G2G_TMP_BRANCH"] = tmp_branch
        run_cmd(["git", "checkout", "-b", tmp_branch, base_sha], cwd=self.workspace)
        change_ids: list[str] = []
        for csha in commit_list:
            run_cmd(["git", "checkout", tmp_branch], cwd=self.workspace)
            git_cherry_pick(csha, cwd=self.workspace)
            # Preserve author of the original commit
            author = run_cmd(
                ["git", "show", "-s", "--pretty=format:%an <%ae>", csha],
                cwd=self.workspace,
            ).stdout.strip()
            git_commit_amend(author=author, no_edit=True, signoff=True, cwd=self.workspace)
            # Extract newly added Change-Id from last commit trailers
            trailers = git_last_commit_trailers(keys=["Change-Id"], cwd=self.workspace)
            for cid in trailers.get("Change-Id", []):
                if cid:
                    change_ids.append(cid)
            # Return to base branch for next iteration context
            run_cmd(["git", "checkout", branch], cwd=self.workspace)
        # Deduplicate while preserving order
        seen = set()
        uniq_ids = []
        for cid in change_ids:
            if cid not in seen:
                uniq_ids.append(cid)
                seen.add(cid)
        run_cmd(["git", "log", "-n3", tmp_branch], cwd=self.workspace)
        if uniq_ids:
            log.info(
                "Generated %d unique Change-ID(s) for PR #%s: %s",
                len(uniq_ids),
                gh.pr_number,
                ", ".join(uniq_ids),
            )
        else:
            log.debug(
                "No Change-IDs collected during preparation for PR #%s (will be ensured via commit-msg hook)",
                gh.pr_number,
            )
        return PreparedChange(change_ids=uniq_ids, commit_shas=[])

    def _prepare_squashed_commit(
        self,
        inputs: Inputs,
        gh: GitHubContext,
        gerrit: GerritInfo,
    ) -> PreparedChange:
        """Squash PR commits into a single commit and handle Change-Id."""
        log.info("Preparing squashed commit for PR #%s", gh.pr_number)
        branch = self._resolve_target_branch()

        run_cmd(
            ["git", "fetch", "origin", branch],
            cwd=self.workspace,
            env=self._ssh_env(),
        )
        base_ref = f"origin/{branch}"
        base_sha = run_cmd(["git", "rev-parse", base_ref], cwd=self.workspace).stdout.strip()
        head_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=self.workspace).stdout.strip()

        # Create temp branch from base and merge-squash PR head
        tmp_branch = f"g2g_tmp_{gh.pr_number or 'pr'!s}_{os.getpid()}"
        os.environ["G2G_TMP_BRANCH"] = tmp_branch
        run_cmd(["git", "checkout", "-b", tmp_branch, base_sha], cwd=self.workspace)
        run_cmd(["git", "merge", "--squash", head_sha], cwd=self.workspace)

        def _collect_log_lines() -> list[str]:
            body = run_cmd(
                [
                    "git",
                    "log",
                    "--format=%B",
                    "--reverse",
                    f"{base_ref}..{head_sha}",
                ],
                cwd=self.workspace,
            ).stdout
            return [ln for ln in body.splitlines() if ln.strip()]

        def _parse_message_parts(
            lines: list[str],
        ) -> tuple[
            list[str],
            list[str],
            list[str],
        ]:
            change_ids: list[str] = []
            signed_off: list[str] = []
            message_lines: list[str] = []
            in_metadata_section = False
            for ln in lines:
                if ln.strip() in ("---", "```") or ln.startswith("updated-dependencies:"):
                    in_metadata_section = True
                    continue
                if in_metadata_section:
                    if ln.startswith(("- dependency-", "  dependency-")):
                        continue
                    if not ln.startswith(("  ", "-", "dependency-")) and ln.strip():
                        in_metadata_section = False
                # Skip Change-Id lines from body - they should only be in footer
                if ln.startswith("Change-Id:"):
                    log.debug("Skipping Change-Id from commit body: %s", ln.strip())
                    continue
                if ln.startswith("Signed-off-by:"):
                    signed_off.append(ln)
                    continue
                if not in_metadata_section:
                    message_lines.append(ln)
            signed_off = sorted(set(signed_off))
            return message_lines, signed_off, change_ids

        def _clean_title_line(title_line: str) -> str:
            # Remove markdown links
            title_line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title_line)
            # Remove trailing ellipsis/truncation
            title_line = re.sub(r"\s*[.]{3,}.*$", "", title_line)
            # Split on common separators to avoid leaking body content
            for separator in [". Bumps ", " Bumps ", ". - ", " - "]:
                if separator in title_line:
                    title_line = title_line.split(separator)[0].strip()
                    break
            # Remove simple markdown/formatting artifacts
            title_line = re.sub(r"[*_`]", "", title_line).strip()
            if len(title_line) > 100:
                break_points = [". ", "! ", "? ", " - ", ": "]
                for bp in break_points:
                    if bp in title_line[:100]:
                        title_line = title_line[: title_line.index(bp) + len(bp.strip())]
                        break
                else:
                    words = title_line[:100].split()
                    title_line = " ".join(words[:-1]) if len(words) > 1 else title_line[:100].rstrip()

            # Apply conventional commit normalization if enabled
            if inputs.normalise_commit and gh.pr_number:
                try:
                    # Get PR author for normalization context
                    client = build_client()
                    repo = get_repo_from_env(client)
                    pr_obj = get_pull(repo, int(gh.pr_number))
                    author = getattr(pr_obj, "user", {})
                    author_login = getattr(author, "login", "") if author else ""
                    title_line = normalize_commit_title(title_line, author_login, self.workspace)
                except Exception as e:
                    log.debug("Failed to apply commit normalization in squash mode: %s", e)

            return title_line

        def _build_clean_message_lines(message_lines: list[str]) -> list[str]:
            if not message_lines:
                return []
            title_line = _clean_title_line(message_lines[0].strip())
            out: list[str] = [title_line]
            if len(message_lines) > 1:
                body_start = 1
                while body_start < len(message_lines) and not message_lines[body_start].strip():
                    body_start += 1
                if body_start < len(message_lines):
                    out.append("")
                    out.extend(message_lines[body_start:])
            return out

        def _maybe_reuse_change_id(pr_str: str) -> str:
            reuse = ""
            sync_all_prs = os.getenv("SYNC_ALL_OPEN_PRS", "false").lower() == "true"
            if (
                not sync_all_prs
                and gh.event_name == "pull_request_target"
                and gh.event_action in ("reopened", "synchronize")
            ):
                try:
                    client = build_client()
                    repo = get_repo_from_env(client)
                    pr_obj = get_pull(repo, int(pr_str))
                    cand = get_recent_change_ids_from_comments(pr_obj, max_comments=50)
                    if cand:
                        reuse = cand[-1]
                        log.debug(
                            "Reusing Change-ID %s for PR #%s (single-PR mode)",
                            reuse,
                            pr_str,
                        )
                except Exception:
                    reuse = ""
            elif sync_all_prs:
                log.debug(
                    "Skipping Change-ID reuse for PR #%s (multi-PR mode)",
                    pr_str,
                )
            return reuse

        def _compose_commit_message(
            lines_in: list[str],
            signed_off: list[str],
            reuse_cid: str,
        ) -> str:
            msg = "\n".join(lines_in).strip()
            msg = _insert_issue_id_into_commit_message(msg, inputs.issue_id)

            # Build footer with proper trailer ordering
            footer_parts = []
            if signed_off:
                footer_parts.extend(signed_off)
            if reuse_cid:
                footer_parts.append(f"Change-Id: {reuse_cid}")

            if footer_parts:
                msg += "\n\n" + "\n".join(footer_parts)
            return msg

        # Build message parts
        raw_lines = _collect_log_lines()
        message_lines, signed_off, _existing_cids = _parse_message_parts(raw_lines)
        clean_lines = _build_clean_message_lines(message_lines)
        pr_str = str(gh.pr_number or "").strip()
        reuse_cid = _maybe_reuse_change_id(pr_str)
        commit_msg = _compose_commit_message(clean_lines, signed_off, reuse_cid)

        # Preserve primary author from the PR head commit
        author = run_cmd(
            ["git", "show", "-s", "--pretty=format:%an <%ae>", head_sha],
            cwd=self.workspace,
        ).stdout.strip()
        git_commit_new(
            message=commit_msg,
            author=author,
            signoff=True,
            cwd=self.workspace,
        )

        # Debug: Check commit message after creation
        actual_msg = run_cmd(
            ["git", "show", "-s", "--pretty=format:%B", "HEAD"],
            cwd=self.workspace,
        ).stdout.strip()
        log.debug("Commit message after creation:\n%s", actual_msg)

        # Ensure Change-Id via commit-msg hook (amend if needed)
        cids = self._ensure_change_id_present(gerrit, author)
        if cids:
            log.info(
                "Generated Change-ID(s) for PR #%s: %s",
                gh.pr_number,
                ", ".join(cids),
            )
        else:
            # Fallback detection: re-scan commit message for Change-Id trailers
            msg_after = run_cmd(["git", "show", "-s", "--pretty=format:%B", "HEAD"], cwd=self.workspace).stdout

            found = [m.strip() for m in re.findall(r"(?mi)^Change-Id:\s*([A-Za-z0-9._-]+)\s*$", msg_after)]
            if found:
                log.info(
                    "Detected Change-ID(s) after amend for PR #%s: %s",
                    gh.pr_number,
                    ", ".join(found),
                )
                cids = found
            else:
                log.warning("No Change-Id detected for PR #%s", gh.pr_number)
        return PreparedChange(change_ids=cids, commit_shas=[])

    def _apply_pr_title_body_if_requested(
        self,
        inputs: Inputs,
        gh: GitHubContext,
    ) -> None:
        """Optionally replace commit message with PR title/body."""
        if not inputs.use_pr_as_commit:
            log.debug("USE_PR_AS_COMMIT disabled; skipping")
            return
        log.info("Applying PR title/body to commit for PR #%s", gh.pr_number)
        pr = str(gh.pr_number or "").strip()
        if not pr:
            return
        # Fetch PR title/body via GitHub API (PyGithub)
        client = build_client()
        repo = get_repo_from_env(client)
        pr_obj = get_pull(repo, int(pr))
        title, body = get_pr_title_body(pr_obj)
        title = (title or "").strip()
        body = (body or "").strip()

        # Filter PR body content for Dependabot and other automated PRs
        author = getattr(pr_obj, "user", {})
        author_login = getattr(author, "login", "") if author else ""
        body = filter_pr_body(title, body, author_login)

        # Clean up title to ensure it's a proper first line for commit message
        if title:
            # Remove markdown links like [text](url) and keep just the text
            title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)
            # Remove any trailing ellipsis or truncation indicators
            title = re.sub(r"\s*[.]{3,}.*$", "", title)
            # Ensure title doesn't accidentally contain body content
            # Split on common separators and take only the first meaningful part
            for separator in [". Bumps ", " Bumps ", ". - ", " - "]:
                if separator in title:
                    title = title.split(separator)[0].strip()
                    break
            # Remove any remaining markdown or formatting artifacts
            title = re.sub(r"[*_`]", "", title)
            title = title.strip()

            # Apply conventional commit normalization if enabled
            if inputs.normalise_commit:
                title = normalize_commit_title(title, author_login, self.workspace)

        # Compose message; preserve existing trailers at footer
        # (Signed-off-by, Change-Id)
        current_body = git_show("HEAD", fmt="%B", cwd=self.workspace)
        # Extract existing trailers from current commit body
        lines_cur = current_body.splitlines()
        signed_lines = [ln for ln in lines_cur if ln.startswith("Signed-off-by:")]
        change_id_lines = [ln for ln in lines_cur if ln.startswith("Change-Id:")]
        github_hash_lines = [ln for ln in lines_cur if ln.startswith("GitHub-Hash:")]
        github_pr_lines = [ln for ln in lines_cur if ln.startswith("GitHub-PR:")]

        msg_parts = [title, "", body] if title or body else [current_body]
        commit_message = "\n".join(msg_parts).strip()

        # Add Issue-ID if provided
        commit_message = _insert_issue_id_into_commit_message(commit_message, inputs.issue_id)

        # Ensure GitHub-Hash is part of the body (not trailers)
        # to keep a blank line before Signed-off-by/Change-Id trailers.
        if github_hash_lines:
            gh_hash_line = github_hash_lines[-1]
        else:
            from .duplicate_detection import DuplicateDetector

            gh_val = DuplicateDetector._generate_github_change_hash(gh)
            gh_hash_line = f"GitHub-Hash: {gh_val}"

        commit_message += "\n\n" + gh_hash_line

        # Build trailers: Signed-off-by first, Change-Id last.
        trailers_out: list[str] = []
        if signed_lines:
            seen_so: set[str] = set()
            for ln in signed_lines:
                if ln not in seen_so:
                    trailers_out.append(ln)
                    seen_so.add(ln)
        if change_id_lines:
            trailers_out.append(change_id_lines[-1])

        # GitHub-PR trailer (must appear after Change-Id)
        if github_pr_lines:
            pr_line = github_pr_lines[-1]
        else:
            pr_line = f"GitHub-PR: {gh.server_url}/{gh.repository}/pull/{gh.pr_number}" if gh.pr_number else ""

        if trailers_out:
            commit_message += "\n\n" + "\n".join(trailers_out)
            if pr_line:
                commit_message += "\n" + pr_line
        elif pr_line:
            commit_message += "\n\n" + pr_line

        author = run_cmd(
            ["git", "show", "-s", "--pretty=format:%an <%ae>", "HEAD"],
            cwd=self.workspace,
            env=self._ssh_env(),
        ).stdout.strip()
        git_commit_amend(
            cwd=self.workspace,
            no_edit=False,
            signoff=not bool(signed_lines),
            author=author,
            message=commit_message,
        )

    def _push_to_gerrit(
        self,
        *,
        gerrit: GerritInfo,
        repo: RepoNames,
        branch: str,
        reviewers: str,
        single_commits: bool,
    ) -> None:
        """Push prepared commit(s) to Gerrit using git-review."""
        log.info(
            "Pushing changes to Gerrit %s:%s project=%s branch=%s",
            gerrit.host,
            gerrit.port,
            repo.project_gerrit,
            branch,
        )
        log.debug("Starting git review push operation...")
        if single_commits:
            tmp_branch = os.getenv("G2G_TMP_BRANCH", "tmp_branch")
            run_cmd(["git", "checkout", tmp_branch], cwd=self.workspace)
        prefix = os.getenv("G2G_TOPIC_PREFIX", "GH").strip() or "GH"
        pr_num = os.getenv("PR_NUMBER", "").strip()
        topic = f"{prefix}-{repo.project_github}-{pr_num}" if pr_num else f"{prefix}-{repo.project_github}"

        # Use our specific SSH configuration
        env = self._ssh_env()

        try:
            args = [
                "git",
                "review",
                "--yes",
                "-v",
                "-t",
                topic,
            ]
            revs = [r.strip() for r in (reviewers or "").split(",") if r.strip() and "@" in r and r.strip() != branch]
            for r in revs:
                args.extend(["--reviewer", r])
            # Branch as positional argument (not a flag)
            args.append(branch)

            if env_bool("CI_TESTING", False):
                log.info("CI_TESTING enabled: using synthetic orphan commit push path")
                self._create_orphan_commit_and_push(gerrit, repo, branch, reviewers, topic, env)
                return
            log.debug("Executing git review command: %s", " ".join(args))
            run_cmd(args, cwd=self.workspace, env=env)
            log.info("Successfully pushed changes to Gerrit")
        except CommandError as exc:
            # Check if this is a "no common ancestry" error in CI_TESTING mode
            if self._should_handle_unrelated_history(exc):
                log.info("Detected unrelated repository history. Creating orphan commit for CI testing...")
                self._create_orphan_commit_and_push(gerrit, repo, branch, reviewers, topic, env)
                return

            # Analyze the specific failure reason from git review output
            error_details = self._analyze_gerrit_push_failure(exc)
            log_exception_conditionally(log, "Gerrit push failed: %s", error_details)
            msg = f"Failed to push changes to Gerrit with git-review: {error_details}"
            raise OrchestratorError(msg) from exc
        # Cleanup temporary branch used during preparation
        tmp_branch = (os.getenv("G2G_TMP_BRANCH", "") or "").strip()
        if tmp_branch:
            # Switch back to the target branch, then delete the temp branch
            run_cmd(
                ["git", "checkout", f"origin/{branch}"],
                check=False,
                cwd=self.workspace,
                env=env,
            )
            run_cmd(
                ["git", "branch", "-D", tmp_branch],
                check=False,
                cwd=self.workspace,
                env=env,
            )

    def _should_handle_unrelated_history(self, exc: CommandError) -> bool:
        """Check if we should handle unrelated repository history in CI testing mode."""
        if not env_bool("CI_TESTING", False):
            return False

        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        combined_output = f"{stdout}\n{stderr}"

        combined_lower = combined_output.lower()
        phrases = (
            "no common ancestry",
            "no common ancestor",
            "do not have a common ancestor",
            "have no common ancestor",
            "have no commits in common",
            "refusing to merge unrelated histories",
            "unrelated histories",
            "unrelated history",
            "no merge base",
        )
        return any(p in combined_lower for p in phrases)

    def _create_orphan_commit_and_push(
        self, gerrit: GerritInfo, repo: RepoNames, branch: str, reviewers: str, topic: str, env: dict[str, str]
    ) -> None:
        """Create a synthetic commit on top of the remote base with the PR tree (CI testing mode)."""
        log.info("CI_TESTING: Creating synthetic commit on top of remote base for unrelated repository")

        try:
            # Capture the current PR commit message and tree
            commit_msg = run_cmd(["git", "log", "--format=%B", "-n", "1", "HEAD"], cwd=self.workspace).stdout.strip()
            pr_tree = run_cmd(["git", "show", "--quiet", "--format=%T", "HEAD"], cwd=self.workspace).stdout.strip()

            # Create/update a synthetic branch based on the remote base branch
            synth_branch = f"synth-{topic}"
            # Ensure remote ref exists locally (best-effort)
            run_cmd(["git", "fetch", "gerrit", branch], cwd=self.workspace, env=env, check=False)
            run_cmd(["git", "checkout", "-B", synth_branch, f"remotes/gerrit/{branch}"], cwd=self.workspace, env=env)

            # Replace working tree contents with the PR tree
            # 1) Remove current tracked files (ignore errors if none)
            run_cmd(["git", "rm", "-r", "--quiet", "."], cwd=self.workspace, env=env, check=False)
            # 2) Clean untracked files/dirs (preserve our SSH known_hosts dir)
            run_cmd(
                ["git", "clean", "-fdx", "-e", ".ssh-g2g", "-e", ".ssh-g2g/**"],
                cwd=self.workspace,
                env=env,
                check=False,
            )
            # 3) Checkout the PR tree into working directory
            run_cmd(["git", "checkout", pr_tree, "--", "."], cwd=self.workspace, env=env)
            run_cmd(["git", "add", "-A"], cwd=self.workspace, env=env)

            # Commit synthetic change with the same message (should already include Change-Id)
            import tempfile as _tempfile
            from pathlib import Path as _Path

            with _tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as _tf:
                # Ensure Signed-off-by for current committer (uploader) is present in the footer
                try:
                    committer_name = run_cmd(
                        ["git", "config", "--get", "user.name"],
                        cwd=self.workspace,
                    ).stdout.strip()
                except Exception:
                    committer_name = ""
                try:
                    committer_email = run_cmd(
                        ["git", "config", "--get", "user.email"],
                        cwd=self.workspace,
                    ).stdout.strip()
                except Exception:
                    committer_email = ""
                msg_to_write = commit_msg
                if committer_name and committer_email:
                    sob_line = f"Signed-off-by: {committer_name} <{committer_email}>"
                    if sob_line not in msg_to_write:
                        if not msg_to_write.endswith("\n"):
                            msg_to_write += "\n"
                        if not msg_to_write.endswith("\n\n"):
                            msg_to_write += "\n"
                        msg_to_write += sob_line
                _tf.write(msg_to_write)
                _tf.flush()
                _tmp_msg_path = _Path(_tf.name)
            try:
                run_cmd(["git", "commit", "-F", str(_tmp_msg_path)], cwd=self.workspace, env=env)
            finally:
                from contextlib import suppress

                with suppress(Exception):
                    _tmp_msg_path.unlink(missing_ok=True)

            # Push directly to refs/for/<branch> with topic and reviewers to avoid rebase behavior
            push_ref = f"refs/for/{branch}%topic={topic}"
            revs = [r.strip() for r in (reviewers or "").split(",") if r.strip() and "@" in r and r.strip() != branch]
            for r in revs:
                push_ref += f",r={r}"
            run_cmd(["git", "push", "--no-follow-tags", "gerrit", f"HEAD:{push_ref}"], cwd=self.workspace, env=env)
            log.info("Successfully pushed synthetic commit to Gerrit")

        except CommandError as orphan_exc:
            error_details = self._analyze_gerrit_push_failure(orphan_exc)
            msg = f"Failed to push orphan commit to Gerrit: {error_details}"
            raise OrchestratorError(msg) from orphan_exc

    def _analyze_gerrit_push_failure(self, exc: CommandError) -> str:
        """Analyze git review failure and provide helpful error message."""
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        combined_output = f"{stdout}\n{stderr}"
        combined_lower = combined_output.lower()

        # Check for SSH host key verification failures first
        if (
            "host key verification failed" in combined_lower
            or "no ed25519 host key is known" in combined_lower
            or "no rsa host key is known" in combined_lower
            or "no ecdsa host key is known" in combined_lower
        ):
            return (
                "SSH host key verification failed. The GERRIT_KNOWN_HOSTS "
                "value is missing or contains an outdated host key for the "
                "Gerrit server. The tool will attempt to auto-discover "
                "host keys "
                "on the next run, or you can manually run "
                "'ssh-keyscan -p 29418 <gerrit-host>' "
                "to get the current host keys."
            )
        elif "authenticity of host" in combined_lower and "can't be established" in combined_lower:
            return (
                "SSH host key unknown. The GERRIT_KNOWN_HOSTS value does not "
                "contain the host key for the Gerrit server. "
                "The tool will attempt "
                "to auto-discover host keys on the next run, or you can "
                "manually run "
                "'ssh-keyscan -p 29418 <gerrit-host>' to get the host keys."
            )
        # Check for specific SSH key issues before general permission denied
        elif "key_load_public" in combined_lower and "invalid format" in combined_lower:
            return "SSH key format is invalid. Check that the SSH private key is properly formatted."
        elif "no matching host key type found" in combined_lower:
            return "SSH key type not supported by server. The server may not accept this SSH key algorithm."
        elif "authentication failed" in combined_lower:
            return "SSH authentication failed - check SSH key, username, and server configuration"
        # Check for connection timeout/refused before "could not read" check
        elif "connection timed out" in combined_lower or "connection refused" in combined_lower:
            return "Connection failed - check network connectivity and Gerrit server availability"
        # Check for specific SSH publickey-only authentication failures
        elif "permission denied (publickey)" in combined_lower and not any(
            auth_method in combined_lower for auth_method in ["gssapi", "password", "keyboard"]
        ):
            return (
                "SSH public key authentication failed. The SSH key may be "
                "invalid, not authorized for this user, or the wrong key type."
            )
        # Check for general SSH permission issues
        elif "permission denied" in combined_lower:
            return "SSH permission denied - check SSH key and user permissions"
        elif "could not read from remote repository" in combined_lower:
            return "Could not read from remote repository - check SSH authentication and repository access permissions"
        # Check for Gerrit-specific issues
        elif "missing issue-id" in combined_lower:
            return "Missing Issue-ID in commit message."
        elif "commit not associated to any issue" in combined_lower:
            return "Commit not associated to any issue."
        elif "remote rejected" in combined_lower and "refs/for/" in combined_lower:
            # Extract specific rejection reason from output
            lines = combined_output.split("\n")
            for line in lines:
                if "! [remote rejected]" in line:
                    # Extract the reason in parentheses
                    if "(" in line and ")" in line:
                        reason = line[line.find("(") + 1 : line.find(")")]
                        return f"Gerrit rejected the push: {reason}"
                    return f"Gerrit rejected the push: {line.strip()}"
            return "Gerrit rejected the push for an unknown reason"
        else:
            return f"Unknown error: {exc}"

    def _query_gerrit_for_results(
        self,
        *,
        gerrit: GerritInfo,
        repo: RepoNames,
        change_ids: Sequence[str],
    ) -> SubmissionResult:
        """Query Gerrit for change URL/number and patchset sha via REST."""
        log.info("Querying Gerrit for submitted change(s) via REST")

        # pygerrit2 netrc filter is already applied in execute() unless verbose mode

        # Create centralized URL builder (auto-discovers base path)
        url_builder = create_gerrit_url_builder(gerrit.host)

        # Get authentication credentials
        http_user = os.getenv("GERRIT_HTTP_USER", "").strip() or os.getenv("GERRIT_SSH_USER_G2G", "").strip()
        http_pass = os.getenv("GERRIT_HTTP_PASSWORD", "").strip()

        # Build Gerrit REST client (prefer HTTP basic auth if provided)
        if GerritRestAPI is None:
            raise OrchestratorError(_MSG_PYGERRIT2_REQUIRED_REST)

        def _create_rest_client(base_url: str) -> Any:
            """Helper to create REST client with optional auth."""
            if http_user and http_pass:
                if HTTPBasicAuth is None:
                    raise OrchestratorError(_MSG_PYGERRIT2_REQUIRED_AUTH)
                if GerritRestAPI is None:
                    raise OrchestratorError(_MSG_PYGERRIT2_REQUIRED_REST)
                return GerritRestAPI(url=base_url, auth=HTTPBasicAuth(http_user, http_pass))
            else:
                if GerritRestAPI is None:
                    raise OrchestratorError(_MSG_PYGERRIT2_REQUIRED_REST)
                return GerritRestAPI(url=base_url)

        # Try API URLs in order of preference (client creation happens in retry loop)
        urls: list[str] = []
        nums: list[str] = []
        shas: list[str] = []
        for cid in change_ids:
            if not cid:
                continue
            # Limit results to 1, filter by project and open status,
            # include current revision
            query = f"limit:1 is:open project:{repo.project_gerrit} {cid}"
            path = f"/changes/?q={query}&o=CURRENT_REVISION&n=1"
            # Build single API base URL via centralized discovery
            api_base_url = url_builder.api_url()
            # Build Gerrit REST client with retry/timeout
            from .gerrit_rest import build_client_for_host

            client = build_client_for_host(
                gerrit.host,
                timeout=8.0,
                max_attempts=5,
                http_user=http_user or None,
                http_password=http_pass or None,
            )
            try:
                log.debug("Gerrit API base URL (discovered): %s", api_base_url)
                changes = client.get(path)
            except Exception as exc:
                log.warning("Failed to query change via REST for %s: %s", cid, exc)
                continue
            if not changes:
                continue
            change = changes[0]
            # Type guard to ensure mapping-like before dict access
            if isinstance(change, dict):
                num = str(change.get("_number", ""))
                current_rev = change.get("current_revision", "")
            else:
                # Unexpected type; skip this result
                continue
            # Construct a stable web URL for the change
            if num:
                change_url = url_builder.change_url(repo.project_gerrit, int(num))
                urls.append(change_url)
                nums.append(num)
            if current_rev:
                shas.append(current_rev)

        return SubmissionResult(change_urls=urls, change_numbers=nums, commit_shas=shas)

    def _setup_git_workspace(self, inputs: Inputs, gh: GitHubContext) -> None:
        """Initialize and set up git workspace for PR processing."""
        from .gitutils import run_cmd

        # Try modern git init with explicit branch first
        try:
            run_cmd(["git", "init", "--initial-branch=master"], cwd=self.workspace)
        except Exception:
            # Fallback for older git versions (hint filtered at logging level)
            run_cmd(["git", "init"], cwd=self.workspace)

        # Add GitHub remote
        repo_full = gh.repository.strip() if gh.repository else ""
        server_url = gh.server_url or "https://github.com"
        server_url = server_url.rstrip("/")
        repo_url = f"{server_url}/{repo_full}.git"
        run_cmd(
            ["git", "remote", "add", "origin", repo_url],
            cwd=self.workspace,
        )

        # Fetch PR head
        if gh.pr_number:
            pr_ref = f"refs/pull/{gh.pr_number}/head:refs/remotes/origin/pr/{gh.pr_number}/head"
            run_cmd(
                [
                    "git",
                    "fetch",
                    f"--depth={inputs.fetch_depth}",
                    "origin",
                    pr_ref,
                ],
                cwd=self.workspace,
            )
            # Checkout PR head
            pr_head_ref = f"refs/remotes/origin/pr/{gh.pr_number}/head"
            run_cmd(
                ["git", "checkout", "-B", "g2g_pr_head", pr_head_ref],
                cwd=self.workspace,
            )

    def _install_commit_msg_hook(self, gerrit: GerritInfo) -> None:
        """Manually install commit-msg hook from Gerrit."""
        from .external_api import curl_download

        hooks_dir = self.workspace / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        hook_path = hooks_dir / "commit-msg"

        # Download commit-msg hook using centralized curl framework
        try:
            # Create centralized URL builder for hook URLs
            url_builder = create_gerrit_url_builder(gerrit.host)
            hook_url = url_builder.hook_url("commit-msg")

            # Localized error raiser and short messages to satisfy TRY rules
            def _raise_orch(msg: str) -> None:
                raise OrchestratorError(msg)  # noqa: TRY301

            _MSG_HOOK_SIZE_BOUNDS = "commit-msg hook size outside expected bounds"
            _MSG_HOOK_READ_FAILED = "failed reading commit-msg hook"
            _MSG_HOOK_NO_SHEBANG = "commit-msg hook missing shebang"
            _MSG_HOOK_BAD_CONTENT = "commit-msg hook content lacks expected markers"

            # Use centralized curl download with retry/logging/metrics
            return_code, status_code = curl_download(
                url=hook_url,
                output_path=str(hook_path),
                timeout=30.0,
                follow_redirects=True,
                silent=True,
            )

            size = hook_path.stat().st_size
            log.debug(
                "curl fetch of commit-msg: url=%s http_status=%s size=%dB rc=%s",
                hook_url,
                status_code,
                size,
                return_code,
            )
            # Sanity checks on size
            if size < 128 or size > 65536:
                _raise_orch(_MSG_HOOK_SIZE_BOUNDS)

            # Validate content characteristics
            text_head = ""
            try:
                with open(hook_path, "rb") as fh:
                    head = fh.read(2048)
                text_head = head.decode("utf-8", errors="ignore")
            except Exception:
                _raise_orch(_MSG_HOOK_READ_FAILED)

            if not text_head.startswith("#!"):
                _raise_orch(_MSG_HOOK_NO_SHEBANG)
            # Look for recognizable strings
            if not any(m in text_head for m in ("Change-Id", "Gerrit Code Review", "add_change_id")):
                _raise_orch(_MSG_HOOK_BAD_CONTENT)

            # Make hook executable (single chmod)
            hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)
            log.debug("Successfully installed commit-msg hook from %s", hook_url)

        except Exception as exc:
            log.warning("Failed to install commit-msg hook via centralized curl: %s", exc)
            msg = f"Could not install commit-msg hook: {exc}"
            raise OrchestratorError(msg) from exc

    def _ensure_change_id_present(self, gerrit: GerritInfo, author: str) -> list[str]:
        """Ensure the last commit has a Change-Id.

        Installs the commit-msg hook and amends the commit if needed.
        """
        trailers = git_last_commit_trailers(keys=["Change-Id"], cwd=self.workspace)
        existing_change_ids = trailers.get("Change-Id", [])

        if existing_change_ids:
            log.debug("Found existing Change-Id(s) in footer: %s", existing_change_ids)
            # Clean up any duplicate Change-IDs in the message body
            self._clean_change_ids_from_body(author)
            return [c for c in existing_change_ids if c]

        log.debug("No Change-Id found; attempting to install commit-msg hook and amend commit")
        try:
            self._install_commit_msg_hook(gerrit)
            git_commit_amend(
                no_edit=True,
                signoff=True,
                author=author,
                cwd=self.workspace,
            )
        except Exception as exc:
            log.warning(
                "Commit-msg hook installation failed, falling back to direct Change-Id injection: %s",
                exc,
            )
            # Fallback: generate a Change-Id and append to the commit
            # message directly
            import time

            current_msg = run_cmd(
                ["git", "show", "-s", "--pretty=format:%B", "HEAD"],
                cwd=self.workspace,
            ).stdout
            seed = f"{current_msg}\n{time.time()}"
            import hashlib as _hashlib  # local alias to satisfy linters

            change_id = "I" + _hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]

            # Clean message and ensure proper footer placement
            cleaned_msg = self._clean_commit_message_for_change_id(current_msg)
            new_msg = cleaned_msg.rstrip() + "\n\n" + f"Change-Id: {change_id}\n"
            git_commit_amend(
                no_edit=False,
                signoff=True,
                author=author,
                message=new_msg,
                cwd=self.workspace,
            )
        # Debug: Check commit message after amend
        actual_msg = run_cmd(
            ["git", "show", "-s", "--pretty=format:%B", "HEAD"],
            cwd=self.workspace,
        ).stdout.strip()
        log.debug("Commit message after amend:\n%s", actual_msg)
        trailers = git_last_commit_trailers(keys=["Change-Id"], cwd=self.workspace)
        return [c for c in trailers.get("Change-Id", []) if c]

    def _clean_change_ids_from_body(self, author: str) -> None:
        """Remove any Change-Id lines from the commit message body, keeping only footer trailers."""
        current_msg = run_cmd(
            ["git", "show", "-s", "--pretty=format:%B", "HEAD"],
            cwd=self.workspace,
        ).stdout

        cleaned_msg = self._clean_commit_message_for_change_id(current_msg)

        if cleaned_msg != current_msg:
            log.debug("Cleaned Change-Id lines from commit message body")
            git_commit_amend(
                no_edit=False,
                signoff=True,
                author=author,
                message=cleaned_msg,
                cwd=self.workspace,
            )

    def _clean_commit_message_for_change_id(self, message: str) -> str:
        """Remove Change-Id lines from message body while preserving footer trailers."""
        lines = message.splitlines()

        # Parse proper trailers using the fixed trailer parser
        trailers = _parse_trailers(message)
        change_id_trailers = trailers.get("Change-Id", [])
        signed_off_trailers = trailers.get("Signed-off-by", [])
        other_trailers = {k: v for k, v in trailers.items() if k not in ["Change-Id", "Signed-off-by"]}

        # Find trailer section by working backwards to find continuous trailer block
        trailer_start = len(lines)

        # Work backwards to find where trailers start
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            if not line:
                # Found blank line - trailers are after this
                trailer_start = i + 1
                break
            elif ":" not in line:
                # Non-trailer line - trailers start after this
                trailer_start = i + 1
                break
            else:
                # Potential trailer line - check if it's a valid trailer
                key, val = line.split(":", 1)
                k = key.strip()
                v = val.strip()
                if not (k and v and not k.startswith(" ") and not k.startswith("\t")):
                    # Invalid trailer format - trailers start after this
                    trailer_start = i + 1
                    break

        # Process body lines (before trailers) and remove any Change-Id references
        body_lines = []
        for i in range(trailer_start):
            line = lines[i]
            # Remove any Change-Id references from body lines
            if "Change-Id:" in line:
                # If line starts with Change-Id:, skip it entirely
                if line.strip().startswith("Change-Id:"):
                    log.debug("Removing Change-Id line from body: %s", line.strip())
                    continue
                else:
                    # If Change-Id is mentioned within the line, remove that part
                    original_line = line
                    # Remove Change-Id: followed by the ID value

                    # Pattern to match "Change-Id: <value>" where value can contain word chars, hyphens, etc.
                    line = re.sub(r"Change-Id:\s*[A-Za-z0-9._-]+\b", "", line)
                    # Clean up extra whitespace
                    line = re.sub(r"\s+", " ", line).strip()
                    if line != original_line:
                        log.debug("Cleaned Change-Id reference from body line: %s -> %s", original_line.strip(), line)
            body_lines.append(line)

        # Remove trailing empty lines from body
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()

        result = "\n".join(body_lines)

        # Add proper footer trailers if any exist
        footer_parts = []
        if signed_off_trailers:
            _seen_so: set[str] = set()
            _uniq_so: list[str] = []
            for s in signed_off_trailers:
                if s not in _seen_so:
                    _uniq_so.append(s)
                    _seen_so.add(s)
            footer_parts.extend([f"Signed-off-by: {s}" for s in _uniq_so])
        # Add other trailers
        for key, values in other_trailers.items():
            footer_parts.extend([f"{key}: {v}" for v in values])
        if change_id_trailers:
            footer_parts.extend([f"Change-Id: {c}" for c in change_id_trailers])

        if footer_parts:
            result += "\n\n" + "\n".join(footer_parts)

        return result

    def _add_backref_comment_in_gerrit(
        self,
        *,
        gerrit: GerritInfo,
        repo: RepoNames,
        branch: str,
        commit_shas: Sequence[str],
        gh: GitHubContext,
    ) -> None:
        """Post a comment in Gerrit pointing back to the GitHub PR and run."""
        if not commit_shas:
            log.warning("No commit shas to comment on in Gerrit")
            return

        # Check if back-reference comments are disabled
        if os.getenv("G2G_SKIP_GERRIT_COMMENTS", "").lower() in (
            "true",
            "1",
            "yes",
        ):
            log.info("Skipping back-reference comments (G2G_SKIP_GERRIT_COMMENTS=true)")
            return

        log.info("Adding back-reference comment in Gerrit")
        user = os.getenv("GERRIT_SSH_USER_G2G", "")
        server = gerrit.host
        pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"
        run_url = f"{gh.server_url}/{gh.repository}/actions/runs/{gh.run_id}" if gh.run_id else "N/A"
        message = f"GHPR: {pr_url} | Action-Run: {run_url}"
        log.info("Adding back-reference comment: %s", message)
        for csha in commit_shas:
            if not csha:
                continue
            try:
                log.debug("Executing SSH command for commit %s", csha)
                # Build SSH command based on available authentication method
                if self._ssh_key_path and self._ssh_known_hosts_path:
                    # File-based SSH authentication
                    ssh_cmd = [
                        "ssh",
                        "-F",
                        "/dev/null",
                        "-i",
                        str(self._ssh_key_path),
                        "-o",
                        f"UserKnownHostsFile={self._ssh_known_hosts_path}",
                        "-o",
                        "IdentitiesOnly=yes",
                        "-o",
                        "IdentityAgent=none",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        "PasswordAuthentication=no",
                        "-o",
                        "PubkeyAcceptedKeyTypes=+ssh-rsa",
                        "-n",
                        "-p",
                        str(gerrit.port),
                        f"{user}@{server}",
                        (
                            "gerrit review -m "
                            f"{shlex.quote(message)} "
                            "--branch "
                            f"{shlex.quote(branch)} "
                            "--project "
                            f"{shlex.quote(repo.project_gerrit)} "
                            f"{shlex.quote(csha)}"
                        ),
                    ]
                elif self._use_ssh_agent and self._ssh_agent_manager and self._ssh_agent_manager.known_hosts_path:
                    # SSH agent authentication with known_hosts
                    ssh_cmd = [
                        "ssh",
                        "-F",
                        "/dev/null",
                        "-o",
                        f"UserKnownHostsFile={self._ssh_agent_manager.known_hosts_path}",
                        "-o",
                        "IdentitiesOnly=no",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "PreferredAuthentications=publickey",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        "PasswordAuthentication=no",
                        "-o",
                        "PubkeyAcceptedKeyTypes=+ssh-rsa",
                        "-o",
                        "ConnectTimeout=10",
                        "-n",
                        "-p",
                        str(gerrit.port),
                        f"{user}@{server}",
                        (
                            "gerrit review -m "
                            f"{shlex.quote(message)} "
                            "--branch "
                            f"{shlex.quote(branch)} "
                            "--project "
                            f"{shlex.quote(repo.project_gerrit)} "
                            f"{shlex.quote(csha)}"
                        ),
                    ]
                else:
                    # Fallback - minimal SSH command (for tests)
                    ssh_cmd = [
                        "ssh",
                        "-F",
                        "/dev/null",
                        "-o",
                        "IdentitiesOnly=yes",
                        "-o",
                        "IdentityAgent=none",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        "PasswordAuthentication=no",
                        "-o",
                        "PubkeyAcceptedKeyTypes=+ssh-rsa",
                        "-n",
                        "-p",
                        str(gerrit.port),
                        f"{user}@{server}",
                        (
                            "gerrit review -m "
                            f"{shlex.quote(message)} "
                            "--branch "
                            f"{shlex.quote(branch)} "
                            "--project "
                            f"{shlex.quote(repo.project_gerrit)} "
                            f"{shlex.quote(csha)}"
                        ),
                    ]

                log.debug("Final SSH command: %s", " ".join(ssh_cmd))
                run_cmd(
                    ssh_cmd,
                    cwd=self.workspace,
                    env=self._ssh_env(),
                )
                log.info(
                    "Successfully added back-reference comment for %s: %s",
                    csha,
                    message,
                )
            except CommandError as exc:
                log.warning(
                    "Failed to add back-reference comment for %s (non-fatal): %s",
                    csha,
                    exc,
                )
                if exc.stderr:
                    log.debug("SSH stderr: %s", exc.stderr)
                if exc.stdout:
                    log.debug("SSH stdout: %s", exc.stdout)
                log.info(
                    "Back-reference comment failed but change was successfully "
                    "submitted. You can set G2G_SKIP_GERRIT_COMMENTS=true to "
                    "disable these comments."
                )
                # Continue processing - this is not a fatal error
            except Exception as exc:
                log.warning(
                    "Failed to add back-reference comment for %s (non-fatal): %s",
                    csha,
                    exc,
                )
                log.debug("Back-reference comment failure details:", exc_info=True)
                # Continue processing - this is not a fatal error

    def _comment_on_pull_request(
        self,
        gh: GitHubContext,
        gerrit: GerritInfo,
        result: SubmissionResult,
    ) -> None:
        """Post a comment on the PR with the Gerrit change URL(s)."""
        # Respect CI_TESTING: do not attempt to update the source/origin PR
        if os.getenv("CI_TESTING", "").strip().lower() in ("1", "true", "yes"):
            log.debug("Source/origin pull request will NOT be updated with Gerrit change when CI_TESTING set true")
            return
        log.info("Adding reference comment on PR #%s", gh.pr_number)
        if not gh.pr_number:
            return
        urls = result.change_urls or []
        org = os.getenv("ORGANIZATION", gh.repository_owner)
        # Create centralized URL builder for organization link
        url_builder = create_gerrit_url_builder(gerrit.host)
        org_url = url_builder.web_url()
        text = f"The pull-request PR-{gh.pr_number} is submitted to Gerrit [{org}]({org_url})!\n\n"
        if urls:
            text += "To follow up on the change visit:\n\n" + "\n".join(urls)
        try:
            client = build_client()
            repo = get_repo_from_env(client)
            # At this point, gh.pr_number is non-None due to earlier guard.
            pr_obj = get_pull(repo, int(gh.pr_number))
            create_pr_comment(pr_obj, text)
            # Also post a succinct one-line comment
            # for each Gerrit change URL
            for u in urls:
                create_pr_comment(
                    pr_obj,
                    f"Change raised in Gerrit by GitHub2Gerrit: {u}",
                )
        except Exception as exc:
            log.warning("Failed to add PR comment: %s", exc)

    def _close_pull_request_if_required(
        self,
        gh: GitHubContext,
    ) -> None:
        """Close the PR if policy requires (pull_request_target events).

        When PRESERVE_GITHUB_PRS is true, skip closing PRs (useful for testing).
        """
        # Respect PRESERVE_GITHUB_PRS to avoid closing PRs during tests
        preserve = os.getenv("PRESERVE_GITHUB_PRS", "").strip().lower()
        if preserve in ("1", "true", "yes"):
            log.info(
                "PRESERVE_GITHUB_PRS is enabled; skipping PR close for #%s",
                gh.pr_number,
            )
            return
        # The current shell action closes PR on pull_request_target events.
        if gh.event_name != "pull_request_target":
            log.debug("Event is not pull_request_target; not closing PR")
            return
        log.info("Closing PR #%s", gh.pr_number)
        try:
            client = build_client()
            repo = get_repo_from_env(client)
            pr_number = gh.pr_number
            if pr_number is None:
                return
            pr_obj = get_pull(repo, pr_number)
            close_pr(pr_obj, comment="Auto-closing pull request")
        except Exception as exc:
            log.warning("Failed to close PR #%s: %s", gh.pr_number, exc)

    def _dry_run_preflight(
        self,
        *,
        gerrit: GerritInfo,
        inputs: Inputs,
        gh: GitHubContext,
        repo: RepoNames,
    ) -> None:
        """Validate config, DNS, and credentials in dry-run mode.

        - Resolve Gerrit host via DNS
        - Verify SSH (TCP) reachability on the Gerrit port
        - Verify Gerrit REST endpoint is reachable; if credentials are provided,
          verify authentication by querying /accounts/self
        - Verify GitHub token by fetching repository and PR metadata
        - Do NOT perform any write operations
        """
        import socket

        log.info("Dry-run: starting preflight checks")
        if os.getenv("G2G_DRYRUN_DISABLE_NETWORK", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            log.info("Dry-run: network checks disabled (G2G_DRYRUN_DISABLE_NETWORK)")
            log.info(
                "Dry-run targets: Gerrit project=%s branch=%s topic_prefix=GH-%s",
                repo.project_gerrit,
                self._resolve_target_branch(),
                repo.project_github,
            )
            if inputs.reviewers_email:
                log.info("Reviewers (from inputs/config): %s", inputs.reviewers_email)
            elif os.getenv("REVIEWERS_EMAIL"):
                log.info(
                    "Reviewers (from environment): %s",
                    os.getenv("REVIEWERS_EMAIL"),
                )
            return

        # DNS resolution for Gerrit host
        try:
            socket.getaddrinfo(gerrit.host, None)
            log.info("DNS resolution for Gerrit host '%s' succeeded", gerrit.host)
        except Exception as exc:
            msg = "DNS resolution failed"
            raise OrchestratorError(msg) from exc

        # SSH (TCP) reachability on Gerrit port
        try:
            with socket.create_connection((gerrit.host, gerrit.port), timeout=5):
                pass
            log.info(
                "SSH TCP connectivity to %s:%s verified",
                gerrit.host,
                gerrit.port,
            )
        except Exception as exc:
            msg = "SSH TCP connectivity failed"
            raise OrchestratorError(msg) from exc

        # Gerrit REST reachability and optional auth check
        base_path = os.getenv("GERRIT_HTTP_BASE_PATH", "").strip().strip("/")
        http_user = os.getenv("GERRIT_HTTP_USER", "").strip() or os.getenv("GERRIT_SSH_USER_G2G", "").strip()
        http_pass = os.getenv("GERRIT_HTTP_PASSWORD", "").strip()
        self._verify_gerrit_rest(gerrit.host, base_path, http_user, http_pass)

        # GitHub token and metadata checks
        try:
            client = build_client()
            repo_obj = get_repo_from_env(client)
            if gh.pr_number is not None:
                pr_obj = get_pull(repo_obj, gh.pr_number)
                log.info("GitHub PR #%s metadata loaded successfully", gh.pr_number)
                try:
                    title, _ = get_pr_title_body(pr_obj)
                    log.info("GitHub PR title: %s", title)
                except Exception as exc:
                    log.debug("Failed to read PR title: %s", exc)
            else:
                # Enumerate at least one open PR to validate scope
                prs = list(iter_open_pulls(repo_obj))
                log.info(
                    "GitHub repository '%s' open PR count: %d",
                    gh.repository,
                    len(prs),
                )
        except Exception as exc:
            msg = "GitHub API validation failed"
            raise OrchestratorError(msg) from exc

        # Log effective targets
        log.info(
            "Dry-run targets: Gerrit project=%s branch=%s topic_prefix=GH-%s",
            repo.project_gerrit,
            self._resolve_target_branch(),
            repo.project_github,
        )
        if inputs.reviewers_email:
            log.info("Reviewers (from inputs/config): %s", inputs.reviewers_email)
        elif os.getenv("REVIEWERS_EMAIL"):
            log.info("Reviewers (from environment): %s", os.getenv("REVIEWERS_EMAIL"))

    def _verify_gerrit_rest(
        self,
        host: str,
        base_path: str,
        http_user: str,
        http_pass: str,
    ) -> None:
        """Probe Gerrit REST endpoint with optional auth.

        Uses the centralized URL builder to construct the API endpoint
        for consistent URL handling across the application.
        """

        def _build_client(url: str) -> Any:
            if http_user and http_pass:
                if GerritRestAPI is None:
                    raise OrchestratorError(_MSG_PYGERRIT2_MISSING)
                if HTTPBasicAuth is None:
                    raise OrchestratorError(_MSG_PYGERRIT2_AUTH_MISSING)
                return GerritRestAPI(url=url, auth=HTTPBasicAuth(http_user, http_pass))
            else:
                if GerritRestAPI is None:
                    raise OrchestratorError(_MSG_PYGERRIT2_MISSING)
                return GerritRestAPI(url=url)

        def _probe(url: str) -> None:
            rest: Any = _build_client(url)
            if http_user and http_pass:
                _ = rest.get("/accounts/self")
                log.info(
                    "Gerrit REST authenticated access verified for user '%s'",
                    http_user,
                )
            else:
                _ = rest.get("/dashboard/self")
                log.info("Gerrit REST endpoint reachable (unauthenticated)")

        # Create centralized URL builder for REST probing
        url_builder = create_gerrit_url_builder(host, base_path)
        api_url = url_builder.api_url()

        try:
            _probe(api_url)
        except Exception as exc:
            log.warning("Gerrit REST probe failed for %s: %s", api_url, exc)

    # ---------------
    # Helpers
    # ---------------

    def _resolve_target_branch(self) -> str:
        # Preference order:
        # 1) GERRIT_BRANCH (explicit override)
        # 2) GITHUB_BASE_REF (provided in Actions PR context)
        # 3) origin/HEAD default (if available)
        # 4) 'main' as a common default
        # 5) 'master' as a legacy default
        b = os.getenv("GERRIT_BRANCH", "").strip()
        if b:
            return b
        b = os.getenv("GITHUB_BASE_REF", "").strip()
        if b:
            return b
        # Try resolve origin/HEAD -> origin/<branch>
        try:
            from .gitutils import git_quiet

            res = git_quiet(
                ["rev-parse", "--abbrev-ref", "origin/HEAD"],
                cwd=self.workspace,
            )
            if res.returncode == 0:
                name = (res.stdout or "").strip()
                branch = name.split("/", 1)[1] if "/" in name else name
                if branch:
                    return branch
        except Exception as exc:
            log.debug("origin/HEAD probe failed: %s", exc)
        # Prefer 'master' when present
        try:
            from .gitutils import git_quiet

            res3 = git_quiet(
                ["show-ref", "--verify", "refs/remotes/origin/master"],
                cwd=self.workspace,
            )
            if res3.returncode == 0:
                return "master"
        except Exception as exc:
            log.debug("origin/master probe failed: %s", exc)
        # Fall back to 'main' if present
        try:
            from .gitutils import git_quiet

            res2 = git_quiet(
                ["show-ref", "--verify", "refs/remotes/origin/main"],
                cwd=self.workspace,
            )
            if res2.returncode == 0:
                return "main"
        except Exception as exc:
            log.debug("origin/main probe failed: %s", exc)
        return "master"

    def _resolve_reviewers(self, inputs: Inputs) -> str:
        # If empty, use the Gerrit SSH user's email as default.
        if inputs.reviewers_email.strip():
            return inputs.reviewers_email.strip()
        return inputs.gerrit_ssh_user_g2g_email.strip()

    def _get_last_change_ids_from_head(self) -> list[str]:
        """Return Change-Id trailer(s) from HEAD commit, if present."""
        try:
            trailers = git_last_commit_trailers(keys=["Change-Id"])
        except GitError:
            return []
        values = trailers.get("Change-Id", [])
        return [v for v in values if v]

    def _validate_change_ids(self, ids: Iterable[str]) -> list[str]:
        """Basic validation for Change-Id strings."""
        out: list[str] = []
        for cid in ids:
            c = cid.strip()
            if not c:
                continue
            if not _is_valid_change_id(c):
                log.debug("Ignoring invalid Change-Id: %s", c)
                continue
            out.append(c)
        return out


# ---------------------
# Utility functions
# ---------------------

# moved _is_valid_change_id above its first use
