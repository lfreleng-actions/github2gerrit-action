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

import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import stat
import urllib.parse
import urllib.request
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request
from urllib.request import urlopen

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
from .mapping_comment import ChangeIdMapping
from .mapping_comment import serialize_mapping_comment
from .models import GitHubContext
from .models import Inputs
from .pr_content_filter import filter_pr_body
from .reconcile_matcher import LocalCommit
from .reconcile_matcher import create_local_commit
from .ssh_common import merge_known_hosts_content
from .utils import env_bool
from .utils import is_verbose_mode


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


# Removed _insert_issue_id_into_commit_message - dead code
# All commit message building now uses _build_commit_message_with_trailers


def _clean_ellipses_from_message(message: str) -> str:
    """Clean ellipses from commit message content."""
    if not message:
        return message

    lines = message.splitlines()
    cleaned_lines = []

    for line in lines:
        # Skip lines that are just "..." or whitespace + "..."
        stripped = line.strip()
        if stripped == "..." or stripped == "…":
            continue

        # Remove trailing ellipses from lines
        cleaned_line = re.sub(r"\s*\.{3,}\s*$", "", line)
        cleaned_line = re.sub(r"\s*…\s*$", "", cleaned_line)
        cleaned_lines.append(cleaned_line)

    return "\n".join(cleaned_lines)


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
        and not re.fullmatch(
            r"I[0-9a-fA-F]+", value
        )  # Exclude hex-like patterns
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

    def all_change_ids(self) -> list[str]:
        """
        Return all Change-Ids (copy) for post-push comment emission.
        """
        return list(self.change_ids)


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

    def _get_gerrit_change_details(
        self,
        gerrit: GerritInfo,
        change_id: str,
    ) -> dict[str, Any] | None:
        """
        Get detailed information about a Gerrit change.

        Args:
            gerrit: Gerrit connection information
            change_id: The Change-Id to query

        Returns:
            Change details dict or None if not found
        """
        try:
            from .gerrit_rest import build_client_for_host

            # Use centralized URL builder
            client = build_client_for_host(gerrit.host)

            # Query by change_id
            encoded_id = urllib.parse.quote(change_id, safe="")
            query_path = (
                f"/changes/{encoded_id}?o=CURRENT_REVISION&o=CURRENT_COMMIT"
            )

            log.debug("Querying change details for: %s", change_id)
            change = client.get(query_path)

            if change:
                log.debug(
                    "Retrieved change details: subject=%s, status=%s",
                    change.get("subject", ""),
                    change.get("status", ""),
                )
                return dict(change) if isinstance(change, dict) else None

        except Exception as exc:
            log.debug("Failed to get change details for %s: %s", change_id, exc)

        return None

    def _update_gerrit_change_metadata(
        self,
        gerrit: GerritInfo,
        change_id: str,
        title: str | None = None,
        description: str | None = None,
    ) -> bool:
        """
        Update Gerrit change metadata (title/description) via REST API.

        Preserves existing GitHub2Gerrit metadata block and trailers from
        the current commit message.

        Args:
            gerrit: Gerrit connection information
            change_id: The Change-Id to update
            title: New commit subject/title (optional)
            description: New commit message body (optional)

        Returns:
            True if update succeeded, False otherwise
        """
        if not title and not description:
            log.debug("No metadata to update")
            return True

        try:
            # Use centralized URL builder with automatic credential resolution.
            # Credential priority: CLI args > .netrc > environment variables.
            # This call uses .netrc > environment (no CLI args passed).
            from .gerrit_rest import build_client_for_host

            client = build_client_for_host(gerrit.host)

            # Check if client has authentication
            if not client.is_authenticated:
                log.debug(
                    "Cannot update Gerrit change metadata: "
                    "No credentials found (check .netrc or environment)"
                )
                return False

            encoded_id = urllib.parse.quote(change_id, safe="")

            # Get current commit message to preserve G2G metadata and trailers
            current_change = self._get_gerrit_change_details(gerrit, change_id)
            existing_g2g_metadata = ""
            existing_trailers = ""

            if current_change:
                # Extract current commit message
                rev = str(current_change.get("current_revision") or "")
                revisions = current_change.get("revisions") or {}
                if rev and rev in revisions:
                    commit_data = revisions[rev].get("commit", {})
                    current_msg = commit_data.get("message", "")

                    # Extract G2G metadata block if present
                    g2g_start = current_msg.find("\nGitHub2Gerrit Metadata:")
                    if g2g_start != -1:
                        # Find where trailers start after G2G metadata
                        g2g_section = current_msg[g2g_start:]
                        trailer_start = -1
                        for line in g2g_section.split("\n"):
                            trailer_prefixes = [
                                "Issue-ID:",
                                "Signed-off-by:",
                                "Change-Id:",
                                "GitHub-PR:",
                                "GitHub-Hash:",
                                "Co-authored-by:",
                            ]
                            if any(
                                line.strip().startswith(prefix)
                                for prefix in trailer_prefixes
                            ):
                                trailer_start = current_msg.find(
                                    line, g2g_start
                                )
                                break

                        if trailer_start != -1:
                            existing_g2g_metadata = current_msg[
                                g2g_start:trailer_start
                            ].rstrip()
                            existing_trailers = current_msg[trailer_start:]
                        else:
                            # No trailers found, G2G metadata extends to end
                            existing_g2g_metadata = current_msg[g2g_start:]
                    else:
                        # No G2G metadata, just extract trailers
                        lines = current_msg.split("\n")
                        for i in range(len(lines) - 1, -1, -1):
                            line = lines[i].strip()
                            trailer_prefixes = [
                                "Issue-ID:",
                                "Signed-off-by:",
                                "Change-Id:",
                                "GitHub-PR:",
                                "GitHub-Hash:",
                                "Co-authored-by:",
                            ]
                            if line and any(
                                line.startswith(prefix)
                                for prefix in trailer_prefixes
                            ):
                                existing_trailers = "\n".join(lines[i:])
                                break

            # Build new commit message preserving metadata and trailers
            if title and description:
                new_message = f"{title}\n\n{description}"
            elif title:
                new_message = title
            else:
                new_message = description or ""

            # Append preserved G2G metadata block
            if existing_g2g_metadata:
                new_message = new_message.rstrip() + existing_g2g_metadata

            # Append preserved trailers
            if existing_trailers:
                if not new_message.endswith("\n\n"):
                    new_message = new_message.rstrip() + "\n\n"
                new_message += existing_trailers

            # Update commit message via REST API
            # PUT /changes/{change-id}/message
            update_data = {"message": new_message}

            log.info(
                "Updating Gerrit change %s metadata via REST API",
                change_id,
            )
            log.debug("New message (first 100 chars): %s", new_message[:100])

            result = client.put(
                f"/changes/{encoded_id}/message",
                data=update_data,
            )

            if result:
                log.info("✅ Successfully updated Gerrit change metadata")
                return True
            else:
                log.warning(
                    "Gerrit change metadata update returned empty result"
                )
                return False

        except Exception as exc:
            log.warning("Failed to update Gerrit change metadata: %s", exc)
            return False

    def _sync_gerrit_change_metadata(
        self,
        gh: GitHubContext,
        gerrit: GerritInfo,
        change_ids: list[str],
    ) -> None:
        """
        Sync PR title to Gerrit change(s) when PR is edited.

        This only syncs the PR title (subject line) to Gerrit when the
        PR title is changed on GitHub. The commit message body comes from
        the actual git commit pushed to Gerrit, not from this sync.

        Args:
            gh: GitHub context
            gerrit: Gerrit connection info
            change_ids: List of Change-IDs to potentially update
        """
        if not change_ids or not gh.pr_number:
            return

        try:
            # Get PR title and body
            client = build_client()
            repo = get_repo_from_env(client)
            pr_obj = get_pull(repo, int(gh.pr_number))

            pr_title, pr_body = get_pr_title_body(pr_obj)
            pr_title = (pr_title or "").strip()
            pr_body = (pr_body or "").strip()

            if not pr_title:
                log.debug("PR has no title, skipping metadata sync")
                return

            log.debug(
                "PR metadata: title=%s, body_len=%d",
                pr_title[:50] + ("..." if len(pr_title) > 50 else ""),
                len(pr_body),
            )

            # Check each change and update if needed
            for change_id in change_ids:
                change = self._get_gerrit_change_details(gerrit, change_id)
                if not change:
                    log.warning(
                        "Could not retrieve change details for %s",
                        change_id,
                    )
                    continue

                gerrit_subject = (change.get("subject") or "").strip()

                # Compare titles (subject lines)
                if gerrit_subject != pr_title:
                    log.debug(
                        "📝 PR title differs from Gerrit subject, updating..."
                    )
                    log.debug("PR title: %s", pr_title)
                    log.debug("Gerrit subject: %s", gerrit_subject)

                    # Update with PR title and body
                    self._update_gerrit_change_metadata(
                        gerrit=gerrit,
                        change_id=change_id,
                        title=pr_title,
                        description=pr_body,
                    )
                else:
                    log.debug(
                        "Gerrit change subject matches PR title, "
                        "no update needed"
                    )

        except Exception as exc:
            log.debug("Failed to sync metadata to Gerrit: %s", exc)

    def _verify_patchset_creation(
        self,
        gerrit: GerritInfo,
        change_ids: list[str],
        expected_operation: str = "update",
    ) -> None:
        """
        Verify that patchsets were created/updated correctly in Gerrit.

        For UPDATE operations, verify that:
        - The Change-IDs match what we expected
        - The changes exist and are not abandoned
        - New patchsets were created (patchset number > 1)

        Args:
            gerrit: Gerrit connection information
            change_ids: List of Change-IDs that should have been updated
            expected_operation: "update" or "edit" for logging purposes

        Raises:
            OrchestratorError: If verification fails critically
        """
        if not change_ids:
            log.debug("No change IDs to verify")
            return

        try:
            from .gerrit_rest import build_client_for_host

            # Use centralized URL builder
            client = build_client_for_host(gerrit.host)

            verification_results = []

            for change_id in change_ids:
                try:
                    encoded_id = urllib.parse.quote(change_id, safe="")
                    query_path = f"/changes/{encoded_id}?o=CURRENT_REVISION"

                    change = client.get(query_path)

                    if not change:
                        log.warning(
                            "⚠️  Could not verify change %s - not found",
                            change_id,
                        )
                        verification_results.append(
                            {
                                "change_id": change_id,
                                "status": "not_found",
                                "verified": False,
                            }
                        )
                        continue

                    status = change.get("status", "UNKNOWN")
                    current_revision = change.get("current_revision", "")
                    revisions = change.get("revisions", {})

                    # Get patchset number
                    patchset_num = 0
                    if current_revision and current_revision in revisions:
                        patchset_num = revisions[current_revision].get(
                            "_number", 0
                        )

                    change_number = change.get("_number", "unknown")
                    subject = change.get("subject", "")[:60]

                    verification_results.append(
                        {
                            "change_id": change_id,
                            "change_number": change_number,
                            "status": status,
                            "patchset": patchset_num,
                            "subject": subject,
                            "verified": True,
                        }
                    )

                    # Log detailed info
                    if patchset_num > 1:
                        log.debug(
                            "✅ Verified %s: Change %s, patchset %d, status=%s",
                            expected_operation.upper(),
                            change_number,
                            patchset_num,
                            status,
                        )
                    elif patchset_num == 1:
                        log.warning(
                            "⚠️  Change %s has patchset 1 - may be newly "
                            "created instead of updated",
                            change_number,
                        )
                    else:
                        log.warning(
                            "⚠️  Could not determine patchset number "
                            "for change %s",
                            change_number,
                        )

                    if status == "ABANDONED":
                        log.warning(
                            "⚠️  Change %s is ABANDONED - update may not "
                            "be visible",
                            change_number,
                        )

                except Exception as exc:
                    log.debug("Failed to verify change %s: %s", change_id, exc)
                    verification_results.append(
                        {
                            "change_id": change_id,
                            "status": "error",
                            "verified": False,
                            "error": str(exc),
                        }
                    )

            # Summary
            verified_count = sum(
                1 for r in verification_results if r.get("verified")
            )
            total_count = len(verification_results)

            if verified_count == total_count:
                log.debug(
                    "✅ Verification complete: %d/%d changes verified",
                    verified_count,
                    total_count,
                )
            else:
                log.warning(
                    "⚠️  Verification incomplete: %d/%d changes verified",
                    verified_count,
                    total_count,
                )

            # Store verification results for potential later use
            self._verification_results = verification_results

        except Exception as exc:
            log.warning("Patchset verification failed (non-fatal): %s", exc)

    def _find_existing_change_for_pr(
        self,
        gh: GitHubContext,
        gerrit: GerritInfo,
    ) -> list[str]:
        """
        Find existing Gerrit change(s) for a given PR using multiple strategies.

        This method attempts to locate existing Gerrit changes associated with
        the current PR using the following strategies in order:
        1. Topic-based query (most reliable)
        2. GitHub-Hash trailer matching
        3. GitHub-PR trailer URL matching
        4. Mapping comment parsing from PR comments

        Args:
            gh: GitHub context containing PR information
            gerrit: Gerrit connection information

        Returns:
            List of Change-IDs for existing changes (empty if none found)
        """
        if not gh.pr_number:
            log.debug("No PR number provided, cannot find existing changes")
            return []

        change_ids: list[str] = []

        # Build expected metadata for matching
        expected_pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"
        meta_trailers = self._build_pr_metadata_trailers(gh)
        expected_github_hash = ""
        for trailer in meta_trailers:
            if trailer.startswith("GitHub-Hash:"):
                expected_github_hash = trailer.split(":", 1)[1].strip()
                break

        log.debug(
            "Searching for existing changes: PR=%s, GitHub-Hash=%s",
            expected_pr_url,
            expected_github_hash,
        )

        # Strategy 1: Topic-based query (most reliable)
        try:
            from .gerrit_query import query_changes_by_topic

            # Construct topic name
            if "/" in gh.repository:
                repo_name = gh.repository.split("/")[-1]
            else:
                repo_name = gh.repository
            topic = f"GH-{gh.repository_owner}-{repo_name}-{gh.pr_number}"

            log.debug("Querying Gerrit for topic: %s", topic)

            # Build client using centralized URL builder
            from .gerrit_rest import build_client_for_host

            client = build_client_for_host(gerrit.host)

            # Query for NEW and MERGED changes (not abandoned)
            changes = query_changes_by_topic(
                client,
                topic,
                statuses=["NEW", "MERGED"],
            )

            if changes:
                change_ids = [c.change_id for c in changes if c.change_id]
                log.info(
                    "✅ Found %d existing change(s) by topic: %s",
                    len(change_ids),
                    ", ".join(change_ids[:3])
                    + ("..." if len(change_ids) > 3 else ""),
                )
                return change_ids
            else:
                log.debug("No changes found for topic: %s", topic)

        except Exception as exc:
            log.debug("Topic-based query failed: %s", exc)

        # Strategy 2 & 3: Query by GitHub-Hash and GitHub-PR trailers
        if expected_github_hash:
            try:
                from .gerrit_rest import build_client_for_host

                # Use centralized URL builder
                client = build_client_for_host(gerrit.host)

                # Build query for changes with matching GitHub-Hash trailer

                query = (
                    f"project:{gerrit.project} message:{expected_github_hash}"
                )
                encoded_q = urllib.parse.quote(query, safe="")
                query_path = f"/changes/?q={encoded_q}&n=50&o=CURRENT_REVISION"

                log.debug("Querying for GitHub-Hash: %s", expected_github_hash)

                data = client.get(query_path)
                if isinstance(data, list) and data:
                    # Filter to only those with matching GitHub-Hash in
                    # commit message
                    for change in data:
                        rev = str(change.get("current_revision") or "")
                        revisions = change.get("revisions") or {}
                        if rev and rev in revisions:
                            commit_data = revisions[rev].get("commit", {})
                            commit_msg = commit_data.get("message", "")
                            expected_hash_line = (
                                f"GitHub-Hash: {expected_github_hash}"
                            )
                            if expected_hash_line in commit_msg:
                                cid = change.get("change_id", "")
                                if cid and cid not in change_ids:
                                    change_ids.append(cid)

                    if change_ids:
                        log.info(
                            "✅ Found %d change(s) by GitHub-Hash trailer",
                            len(change_ids),
                        )
                        return change_ids

            except Exception as exc:
                log.debug("GitHub-Hash trailer query failed: %s", exc)

        # Strategy 4: Parse mapping comments from PR
        try:
            from .mapping_comment import parse_mapping_comments

            client_gh = build_client()
            repo = get_repo_from_env(client_gh)
            pr_obj = get_pull(repo, int(gh.pr_number))

            issue = pr_obj.as_issue()
            comments = list(issue.get_comments())
            comment_bodies = [c.body or "" for c in comments]

            mapping = parse_mapping_comments(comment_bodies)

            if mapping and mapping.change_ids:
                # Validate consistency
                from .mapping_comment import validate_mapping_consistency

                if validate_mapping_consistency(
                    mapping,
                    expected_pr_url,
                    expected_github_hash,
                ):
                    change_ids = mapping.change_ids
                    log.debug(
                        "✅ Found %d change(s) from mapping comment",
                        len(change_ids),
                    )
                    return change_ids
                else:
                    log.warning(
                        "Mapping comment found but consistency check failed"
                    )

        except Exception as exc:
            log.debug("Mapping comment parsing failed: %s", exc)

        log.warning(
            "⚠️  No existing Gerrit changes found for PR #%s",
            gh.pr_number,
        )
        return []

    # Phase 1 helper: build deterministic PR metadata trailers
    # Phase 3 introduces reconciliation helpers below for reusing prior
    # Change-Ids
    def _build_pr_metadata_trailers(self, gh: GitHubContext) -> list[str]:
        """
        Build GitHub PR metadata trailers (GitHub-PR, GitHub-Hash).

        Always deterministic:
        - GitHub-PR: full PR URL
        - GitHub-Hash: stable hash derived from server/repo/pr_number

        Returns:
            List of trailer lines (without preceding newlines).
        """
        trailers: list[str] = []
        try:
            pr_num = gh.pr_number
        except Exception:
            pr_num = None
        if not pr_num:
            return trailers
        pr_url = f"{gh.server_url}/{gh.repository}/pull/{pr_num}"
        trailers.append(f"GitHub-PR: {pr_url}")
        try:
            from .duplicate_detection import DuplicateDetector

            gh_hash = DuplicateDetector._generate_github_change_hash(gh)
            trailers.append(f"GitHub-Hash: {gh_hash}")
        except Exception as exc:
            log.debug("Failed to compute GitHub-Hash trailer: %s", exc)
        return trailers

    def _build_g2g_metadata_block(
        self,
        gh: GitHubContext,
        mode: str,
        topic: str,
        change_ids: list[str] | None = None,
    ) -> str:
        """
        Build GitHub2Gerrit metadata block for inclusion in commit message.

        This metadata block helps with reconciliation when changes are merged
        or abandoned in Gerrit.

        Args:
            gh: GitHub context
            mode: "squash" or "multi-commit"
            topic: Gerrit topic name
            change_ids: Optional list of Change-IDs for multi-commit mode

        Returns:
            Formatted metadata block
        """
        lines = ["", "GitHub2Gerrit Metadata:"]
        lines.append(f"Mode: {mode}")
        lines.append(f"Topic: {topic}")

        # Add digest if available from reconciliation
        plan_snapshot = getattr(self, "_reconciliation_plan", None)
        if isinstance(plan_snapshot, dict):
            digest = plan_snapshot.get("digest", "") or ""
            if digest:
                lines.append(f"Digest: {digest}")

        # For multi-commit mode, include all Change-IDs
        if change_ids and len(change_ids) > 1:
            lines.append(f"Change-Ids: {', '.join(change_ids)}")

        return "\n".join(lines)

    def _build_commit_message_with_trailers(
        self,
        base_message: str,
        inputs: Inputs,
        gh: GitHubContext,
        *,
        change_id: str | None = None,
        preserve_existing: bool = True,
        include_g2g_metadata: bool = False,
        g2g_mode: str | None = None,
        g2g_topic: str | None = None,
        g2g_change_ids: list[str] | None = None,
    ) -> str:
        """
        Build complete commit message with all trailers in proper order.

        This is the single source of truth for trailer management.

        Trailer order:
        1. Issue-ID (if provided)
        2. Signed-off-by (preserved or added)
        3. Change-ID (if provided or preserved)
        4. GitHub-PR
        5. GitHub-Hash

        Args:
            base_message: The base commit message (subject + body)
            inputs: User inputs including issue_id
            gh: GitHub context
            change_id: Optional Change-ID to inject
            preserve_existing: Whether to preserve existing trailers
            include_g2g_metadata: Whether to include GitHub2Gerrit
                metadata block
            g2g_mode: Mode for metadata block (squash/multi-commit)
            g2g_topic: Topic for metadata block
            g2g_change_ids: Change-IDs for metadata block

        Returns:
            Complete commit message with all trailers properly ordered
        """
        from .gitutils import _parse_trailers

        # Parse existing trailers if preserving
        existing_trailers = {}
        if preserve_existing:
            existing_trailers = _parse_trailers(base_message)

        # Split message into body and trailers
        lines = base_message.splitlines()
        body_lines = []

        # Find where trailers start (working backwards)
        trailer_start = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            if not line:
                continue
            # Common trailer patterns
            if any(
                line.startswith(prefix)
                for prefix in [
                    "Issue-ID:",
                    "Signed-off-by:",
                    "Change-Id:",
                    "GitHub-PR:",
                    "GitHub-Hash:",
                    "Co-authored-by:",
                ]
            ):
                trailer_start = i
            else:
                break

        # Body is everything before trailers
        body_lines = lines[:trailer_start]
        base_body = "\n".join(body_lines).rstrip()

        # Build trailers in proper order
        trailers_ordered: list[str] = []

        # 1. Issue-ID (if provided)
        if inputs.issue_id.strip():
            issue_line = (
                inputs.issue_id.strip()
                if inputs.issue_id.strip().startswith("Issue-ID:")
                else f"Issue-ID: {inputs.issue_id.strip()}"
            )
            # Check if not already in the trailers
            if "Issue-ID" not in existing_trailers:
                trailers_ordered.append(issue_line)
                # Log and display Issue-ID addition (only once)
                issue_id_value = inputs.issue_id.strip().replace(
                    "Issue-ID: ", ""
                )
                log.debug(
                    "✅ Added Issue-ID %s to commit message", issue_id_value
                )
                print(f"✅ Added Issue-ID {issue_id_value} to commit message")
        elif preserve_existing and "Issue-ID" in existing_trailers:
            # Preserve existing Issue-ID
            for issue_id_val in existing_trailers["Issue-ID"]:
                trailers_ordered.append(f"Issue-ID: {issue_id_val}")

        # 2. Signed-off-by (preserve existing)
        if preserve_existing and "Signed-off-by" in existing_trailers:
            seen_sob: set[str] = set()
            for sob_val in existing_trailers["Signed-off-by"]:
                sob_line = f"Signed-off-by: {sob_val}"
                if sob_line not in seen_sob:
                    trailers_ordered.append(sob_line)
                    seen_sob.add(sob_line)

        # 3. Change-ID
        if change_id:
            # Use provided Change-ID
            trailers_ordered.append(f"Change-Id: {change_id}")
        elif preserve_existing and "Change-Id" in existing_trailers:
            # Preserve existing Change-ID (use last one)
            cid_val = existing_trailers["Change-Id"][-1]
            trailers_ordered.append(f"Change-Id: {cid_val}")

        # 4 & 5. GitHub metadata (GitHub-PR, GitHub-Hash)
        gh_metadata = self._build_pr_metadata_trailers(gh)
        for gh_trailer in gh_metadata:
            # Check if not already present
            if gh_trailer not in trailers_ordered:
                trailers_ordered.append(gh_trailer)

        # Add GitHub2Gerrit metadata block before trailers if requested
        if include_g2g_metadata and g2g_mode and g2g_topic:
            metadata_block = self._build_g2g_metadata_block(
                gh, g2g_mode, g2g_topic, g2g_change_ids
            )
            base_body = base_body + "\n" + metadata_block

        # Assemble final message
        if trailers_ordered:
            final_message = base_body + "\n\n" + "\n".join(trailers_ordered)
        else:
            final_message = base_body

        return final_message

    def _emit_change_id_map_comment(
        self,
        *,
        gh_context: GitHubContext | None,
        change_ids: list[str],
        multi: bool,
        topic: str,
        replace_existing: bool = True,
    ) -> None:
        """
        Emit or update a machine-parseable PR comment enumerating Change-Ids.

        Args:
            gh_context: GitHub context information
            change_ids: Ordered list of Change-IDs to emit
            multi: True for multi-commit mode, False for squash
            topic: Gerrit topic name
            replace_existing: If True, replace existing mapping comment
        """
        if not gh_context or not gh_context.pr_number:
            return

        # Sanitize and dedupe while preserving order
        seen: set[str] = set()
        ordered: list[str] = []
        for cid in change_ids:
            if cid and cid not in seen:
                ordered.append(cid)
                seen.add(cid)
        if not ordered:
            return

        try:
            from .github_api import build_client
            from .github_api import create_pr_comment
            from .github_api import get_pull
            from .github_api import get_repo_from_env
        except Exception as exc:
            log.debug("GitHub API imports failed for comment emission: %s", exc)
            return

        try:
            client = build_client()
            repo = get_repo_from_env(client)
            pr_obj = get_pull(repo, int(gh_context.pr_number))

            # Build metadata
            mode_str = "multi-commit" if multi else "squash"
            meta = self._build_pr_metadata_trailers(gh_context)
            gh_hash = ""
            for trailer in meta:
                if trailer.startswith("GitHub-Hash:"):
                    gh_hash = trailer.split(":", 1)[1].strip()
                    break

            pr_url = (
                f"{gh_context.server_url}/{gh_context.repository}/pull/"
                f"{gh_context.pr_number}"
            )

            # Create mapping comment using utility
            # Include reconciliation digest if available
            digest = ""
            plan_snapshot = getattr(self, "_reconciliation_plan", None)
            if isinstance(plan_snapshot, dict):
                digest = plan_snapshot.get("digest", "") or ""
            comment_body = serialize_mapping_comment(
                pr_url=pr_url,
                mode=mode_str,
                topic=topic,
                change_ids=ordered,
                github_hash=gh_hash,
                digest=digest or None,
            )

            if replace_existing:
                # Try to find and update existing mapping comment
                issue = pr_obj.as_issue()
                comments = list(issue.get_comments())

                from .mapping_comment import find_mapping_comments
                from .mapping_comment import update_mapping_comment_body

                comment_indices = find_mapping_comments(
                    [c.body or "" for c in comments]
                )
                if comment_indices:
                    # Update the latest mapping comment
                    latest_idx = comment_indices[-1]
                    latest_comment = comments[latest_idx]

                    # Create new mapping for update
                    new_mapping = ChangeIdMapping(
                        pr_url=pr_url,
                        mode=mode_str,
                        topic=topic,
                        change_ids=ordered,
                        github_hash=gh_hash,
                        digest=digest,
                    )

                    body = latest_comment.body or ""
                    updated_body = update_mapping_comment_body(
                        body, new_mapping
                    )
                    latest_comment.edit(updated_body)  # type: ignore[attr-defined]
                    log.debug(
                        "Updated existing mapping comment for PR #%s",
                        gh_context.pr_number,
                    )
                    return

            # Create new comment if no existing one or replace_existing is False
            create_pr_comment(pr_obj, comment_body)
            log.debug(
                "Emitted Change-Id map comment for PR #%s with %d id(s)",
                gh_context.pr_number,
                len(ordered),
            )

        except Exception as exc:
            log.debug(
                "Failed to emit Change-Id mapping comment for PR #%s: %s",
                getattr(gh_context, "pr_number", "?"),
                exc,
            )

    # ------------------------------------------------------------------
    # Create-missing fallback helpers
    # ------------------------------------------------------------------

    def _should_create_missing(
        self,
        inputs: Inputs,
        gh: GitHubContext,
    ) -> bool:
        """Decide whether to fall back from UPDATE to CREATE.

        Returns ``True`` when either the ``--create-missing`` CLI flag
        is active **or** a ``@github2gerrit create missing change``
        comment is present on the PR.
        """
        # 1. Explicit CLI / environment flag
        if inputs.create_missing:
            log.info(
                "✅ --create-missing flag is set; authorising CREATE fallback"
            )
            return True

        # 2. Scan PR comments for the directive
        if not gh.pr_number:
            return False

        try:
            from .pr_commands import CMD_CREATE_MISSING
            from .pr_commands import find_command

            client_gh = build_client()
            repo = get_repo_from_env(client_gh)
            pr_obj = get_pull(repo, int(gh.pr_number))

            issue = pr_obj.as_issue()
            comment_bodies = [c.body or "" for c in issue.get_comments()]

            match = find_command(comment_bodies, CMD_CREATE_MISSING.name)
            if match is not None:
                log.info(
                    "✅ Found '@github2gerrit %s' in PR #%s comment #%d; "
                    "authorising CREATE fallback",
                    match.raw_text,
                    gh.pr_number,
                    match.comment_index,
                )
                return True

            log.debug(
                "No @github2gerrit create-missing command found in "
                "PR #%s comments",
                gh.pr_number,
            )
        except Exception as exc:
            log.debug(
                "Failed to check PR comments for create-missing command: %s",
                exc,
            )

        return False

    def _post_create_missing_notice(
        self,
        gh: GitHubContext,
    ) -> None:
        """Post a comment on the PR noting the CREATE fallback."""
        if not gh.pr_number:
            return
        # Respect CI_TESTING
        if os.getenv("CI_TESTING", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            return
        try:
            client_gh = build_client()
            repo = get_repo_from_env(client_gh)
            pr_obj = get_pull(repo, int(gh.pr_number))
            create_pr_comment(
                pr_obj,
                "🔄 **GitHub2Gerrit**: No existing Gerrit change found for "
                "this PR.\n"
                "Creating a new Gerrit change (fallback from UPDATE "
                "operation).\n\n"
                "_Triggered by `@github2gerrit create missing change` "
                "comment or `--create-missing` flag._",
            )
        except Exception as exc:
            log.warning(
                "Failed to post create-missing notice on PR #%s: %s",
                gh.pr_number,
                exc,
            )

    def _enforce_existing_change_for_update(
        self,
        gh: GitHubContext,
        gerrit: GerritInfo,
    ) -> list[str]:
        """
        Enforce that an existing change is found for UPDATE operations.

        For PR synchronize events, we expect a Gerrit change to already exist.
        This method finds it and raises an error if not found.

        Args:
            gh: GitHub context
            gerrit: Gerrit connection info

        Returns:
            List of Change-IDs that must be reused

        Raises:
            OrchestratorError: If no existing change found for UPDATE operation
        """
        change_ids = self._find_existing_change_for_pr(gh, gerrit)

        if not change_ids:
            if "/" in gh.repository:
                repo_name = gh.repository.split("/")[-1]
            else:
                repo_name = gh.repository
            topic = f"GH-{gh.repository_owner}-{repo_name}-{gh.pr_number}"

            msg = (
                f"UPDATE operation requires existing Gerrit change, but "
                f"none found. "
                f"PR #{gh.pr_number} should have an existing change with "
                f"topic '{topic}'. "
                f"This usually means:\n"
                f"1. The PR was not previously processed by GitHub2Gerrit\n"
                f"2. The Gerrit change was abandoned or deleted\n"
                f"3. The topic was manually changed in Gerrit\n"
                f"Consider using 'opened' event type or check Gerrit for "
                f"the change."
            )
            raise OrchestratorError(msg)

        log.debug(
            "✅ Found %d existing change(s) for UPDATE operation",
            len(change_ids),
        )
        return change_ids

    def _perform_robust_reconciliation(
        self,
        inputs: Inputs,
        gh: GitHubContext,
        gerrit: GerritInfo,
        local_commits: list[LocalCommit],
    ) -> list[str]:
        """
        Delegate to extracted reconciliation module.
        Captures reconciliation plan for later verification (digest check).
        """
        if not local_commits:
            self._reconciliation_plan = None
            return []
        # Lazy import to avoid cycles
        from .orchestrator import perform_reconciliation
        from .orchestrator.reconciliation import (
            _compute_plan_digest as _plan_digest,
        )

        meta_trailers = self._build_pr_metadata_trailers(gh)
        expected_pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"
        expected_github_hash = ""
        for trailer in meta_trailers:
            if trailer.startswith("GitHub-Hash:"):
                expected_github_hash = trailer.split(":", 1)[1].strip()
                break

            # Check if this is an update operation
            operation_mode = os.getenv("G2G_OPERATION_MODE", "unknown")
            is_update_op = operation_mode == "update"

            change_ids = perform_reconciliation(
                inputs=inputs,
                gh=gh,
                gerrit=gerrit,
                local_commits=local_commits,
                expected_pr_url=expected_pr_url,
                expected_github_hash=expected_github_hash or None,
                is_update_operation=is_update_op,
            )
        # Store lightweight plan snapshot (only fields needed for verify)
        try:
            self._reconciliation_plan = {
                "change_ids": change_ids,
                "digest": _plan_digest(change_ids),
            }
        except Exception:
            # Non-fatal; verification will gracefully degrade
            self._reconciliation_plan = None
        return change_ids

    def _verify_reconciliation_digest(
        self,
        gh: GitHubContext,
        gerrit: GerritInfo,
    ) -> None:
        """
        Verification phase: re-query Gerrit by topic and compare digest.

        - Rebuild observed Change-Id ordering aligned to original plan order
        - Compute observed digest
        - Emit VERIFICATION_SUMMARY log line
        - If mismatch and VERIFY_DIGEST_STRICT=true raise OrchestratorError

        Assumes self._reconciliation_plan set by reconciliation step:
          {
            "change_ids": [...],
            "digest": "<sha12>"
          }
        """
        plan = getattr(self, "_reconciliation_plan", None)
        if not plan:
            log.debug("No reconciliation plan present; skipping verification")
            return
        planned_ids = plan.get("change_ids") or []
        planned_digest = plan.get("digest") or ""
        if not planned_ids or not planned_digest:
            log.debug("Incomplete plan data; skipping verification")
            return

        topic = (
            f"GH-{gh.repository_owner}-{gh.repository.split('/')[-1]}-"
            f"{gh.pr_number}"
        )
        try:
            from .gerrit_query import query_changes_by_topic
            from .gerrit_rest import build_client_for_host

            # Use centralized URL builder
            client = build_client_for_host(gerrit.host)
            # Re-query only NEW changes; merged ones are stable but keep for
            # compatibility with earlier reuse logic if needed.
            changes = query_changes_by_topic(
                client,
                topic,
                statuses=["NEW", "MERGED"],
            )
            # Map change_id -> change for quick lookup
            id_set = {c.change_id for c in changes}
            # Preserve original ordering: filter plan list by those still
            # present, then append any newly discovered (unexpected) ones.
            observed_ordered: list[str] = [
                cid for cid in planned_ids if cid in id_set
            ]
            extras = [cid for cid in id_set if cid not in observed_ordered]
            if extras:
                observed_ordered.extend(sorted(extras))
            # Compute digest identical to reconciliation module logic
            from .orchestrator.reconciliation import (
                _compute_plan_digest as _plan_digest,
            )

            observed_digest = _plan_digest(observed_ordered)
            match = observed_digest == planned_digest
            summary = {
                "planned_digest": planned_digest,
                "observed_digest": observed_digest,
                "match": match,
                "planned_count": len(planned_ids),
                "observed_count": len(observed_ordered),
                "extras": extras,
            }
            log.info(
                "VERIFICATION_SUMMARY json=%s",
                json.dumps(summary, separators=(",", ":")),
            )
            if not match:
                msg = (
                    "Reconciliation digest mismatch (planned != observed). "
                    "Enable stricter diagnostics or inspect Gerrit topic drift."
                )
                if os.getenv("VERIFY_DIGEST_STRICT", "true").lower() in (
                    "1",
                    "true",
                    "yes",
                ):
                    self._raise_verification_error(msg)
                log.warning(msg)
        except OrchestratorError:
            # Re-raise verification errors unchanged
            raise
        except Exception as exc:
            log.debug("Verification phase failed (non-fatal): %s", exc)

    def _raise_verification_error(self, msg: str) -> None:
        """Helper to raise verification errors (extracted for TRY301
        compliance)."""
        raise OrchestratorError(msg)

    def _extract_local_commits_for_reconciliation(
        self,
        inputs: Inputs,
        gh: GitHubContext,
    ) -> list[LocalCommit]:
        """
        Extract local commits as LocalCommit objects for reconciliation.

        Args:
            inputs: Configuration inputs
            gh: GitHub context

        Returns:
            List of LocalCommit objects representing local commits to be
            submitted
        """
        branch = self._resolve_target_branch()
        base_ref = f"origin/{branch}"

        # Get commit range: commits in HEAD not in base branch
        try:
            # Ensure workspace is prepared (consolidated git fetch)
            self._ensure_workspace_prepared(branch)

            revs = run_cmd(
                ["git", "rev-list", "--reverse", f"{base_ref}..HEAD"],
                cwd=self.workspace,
            ).stdout

            commit_list = [c.strip() for c in revs.splitlines() if c.strip()]

        except (CommandError, GitError) as exc:
            log.warning("Failed to extract commit range: %s", exc)
            return []

        if not commit_list:
            log.debug("No commits found in range %s..HEAD", base_ref)
            return []

        local_commits = []

        for index, commit_sha in enumerate(commit_list):
            try:
                # Get commit subject
                subject = run_cmd(
                    ["git", "show", "-s", "--pretty=format:%s", commit_sha],
                    cwd=self.workspace,
                ).stdout.strip()

                # Get full commit message
                commit_message = run_cmd(
                    ["git", "show", "-s", "--pretty=format:%B", commit_sha],
                    cwd=self.workspace,
                ).stdout

                # Get modified files
                files_output = run_cmd(
                    [
                        "git",
                        "show",
                        "--name-only",
                        "--pretty=format:",
                        commit_sha,
                    ],
                    cwd=self.workspace,
                ).stdout

                files = [
                    f.strip() for f in files_output.splitlines() if f.strip()
                ]

                # Create LocalCommit object
                local_commit = create_local_commit(
                    index=index,
                    sha=commit_sha,
                    subject=subject,
                    files=files,
                    commit_message=commit_message,
                )

                local_commits.append(local_commit)

            except (CommandError, GitError) as exc:
                log.warning(
                    "Failed to extract commit info for %s: %s",
                    commit_sha[:8],
                    exc,
                )
                continue

        log.debug(
            "Extracted %d local commits for reconciliation", len(local_commits)
        )
        return local_commits

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
        # Secure temporary directory for SSH files (outside workspace)
        self._ssh_temp_dir: Path | None = None
        # Store inputs for access by helper methods
        self._inputs: Inputs | None = None
        # Track discovered SSH keys for later config saving (after successful
        # submission)
        self._discovered_ssh_keys: str | None = None
        self._ssh_discovery_organization: str | None = None
        # Track derived parameters for later config saving (after successful
        # submission)
        self._derived_parameters: dict[str, str] | None = None
        self._derived_parameters_organization: str | None = None
        # Track workspace preparation state to avoid redundant fetches
        self._workspace_prepared: bool = False
        self._prepared_branch: str | None = None
        # Track git-review setup state to avoid redundant setup
        self._git_review_initialized: bool = False

    # ---------------
    # Public API
    # ---------------

    def execute(
        self,
        inputs: Inputs,
        gh: GitHubContext,
        operation_mode: str | None = None,
    ) -> SubmissionResult:
        """Run the full pipeline and return a structured result.

        This method defines the high-level call order. Sub-steps are
        placeholders and must be implemented with real logic. Until then,
        this raises NotImplementedError after logging the intended plan.

        Note: This method is "pure" with respect to external outputs (no direct
        GitHub output writes), but does perform internal environment mutations
        (e.g., G2G_TMP_BRANCH) for subprocess coordination within the workflow.
        """
        log.debug("Starting PR -> Gerrit pipeline")
        self._inputs = inputs  # Store for access by helper methods
        self._guard_pull_request_context(gh)

        # Determine operation mode
        if operation_mode is None:
            operation_mode = os.getenv("G2G_OPERATION_MODE", "unknown")

        is_update_operation = operation_mode == "update"
        is_edit_operation = operation_mode == "edit"

        if is_update_operation:
            log.debug("📝 Executing UPDATE operation (PR synchronize event)")
        elif is_edit_operation:
            log.debug("✏️  Executing EDIT operation (PR edited event)")
        else:
            log.debug("🆕 Executing CREATE operation (new PR or unknown event)")

        # Initialize git repository in workspace if it doesn't exist
        if not (self.workspace / ".git").exists():
            self._prepare_workspace_checkout(inputs=inputs, gh=gh)

        gitreview = self._read_gitreview(self.workspace / ".gitreview", gh)
        repo_names = self._derive_repo_names(gitreview, gh)
        log.debug(
            "execute: inputs.dry_run=%s, inputs.ci_testing=%s",
            inputs.dry_run,
            inputs.ci_testing,
        )
        gerrit = self._resolve_gerrit_info(gitreview, inputs, repo_names)

        log.debug("execute: resolved gerrit info: %s", gerrit)
        if inputs.dry_run:
            log.debug(
                "execute: entering dry-run mode due to inputs.dry_run=True"
            )
            # Perform preflight validations and exit without making changes
            self._dry_run_preflight(
                gerrit=gerrit, inputs=inputs, gh=gh, repo=repo_names
            )
            log.debug("Dry run complete; skipping write operations to Gerrit")
            return SubmissionResult(
                change_urls=[], change_numbers=[], commit_shas=[]
            )
        self._setup_ssh(inputs, gerrit)
        # Reset workspace preparation state for this execution
        self._workspace_prepared = False
        self._prepared_branch = None
        self._git_review_initialized = False

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

        # Configure git identity BEFORE any merge operations that create commits
        self._ensure_git_user_identity(inputs)

        self._configure_git(gerrit, inputs)

        # Phase 3: Robust reconciliation with multi-pass matching
        # For UPDATE operations, enforce finding existing changes
        forced_reuse_ids: list[str] = []
        if is_update_operation or is_edit_operation:
            log.debug("🔍 Searching for existing Gerrit change(s) to update...")
            try:
                forced_reuse_ids = self._enforce_existing_change_for_update(
                    gh, gerrit
                )
                log.debug(
                    "✅ Will update existing change(s): %s",
                    ", ".join(forced_reuse_ids[:3])
                    + ("..." if len(forced_reuse_ids) > 3 else ""),
                )
            except OrchestratorError:
                # UPDATE found no existing Gerrit change — check whether
                # we should fall back to CREATE instead of failing.
                should_create = self._should_create_missing(inputs, gh)

                if not should_create:
                    raise  # propagate the original error

                # --- Fall back to CREATE mode ---
                log.warning(
                    "🔄 Falling back from UPDATE → CREATE for PR #%s "
                    "(create-missing authorised)",
                    gh.pr_number,
                )
                is_update_operation = False
                is_edit_operation = False
                operation_mode = "create"
                os.environ["G2G_OPERATION_MODE"] = "create"
                os.environ["G2G_FALLBACK_CREATE"] = "true"

                # Notify on the PR that we are creating from scratch
                self._post_create_missing_notice(gh)

        # Optimization: Determine reuse Change-IDs BEFORE preparing commits.
        # This avoids redundant preparation calls - previously we prepared
        # commits once initially, then if reuse_ids were found, we prepared
        # them again, discarding the first preparation's work. Now we determine
        # reuse_ids first, then prepare commits only once with the correct IDs.
        reuse_ids: list[str] = []
        if inputs.submit_single_commits:
            # Extract local commits for multi-commit reconciliation
            local_commits = self._extract_local_commits_for_reconciliation(
                inputs, gh
            )

            # Use forced reuse IDs for UPDATE operations, otherwise reconcile
            if forced_reuse_ids:
                reuse_ids = forced_reuse_ids
            else:
                reuse_ids = self._perform_robust_reconciliation(
                    inputs, gh, gerrit, local_commits
                )
        else:
            # For squash mode, use modern reconciliation with single commit
            local_commits = self._extract_local_commits_for_reconciliation(
                inputs, gh
            )
            # Limit to first commit for squash mode
            single_commit = local_commits[:1] if local_commits else []

            # Use forced reuse IDs for UPDATE operations, otherwise reconcile
            if forced_reuse_ids:
                reuse_ids = forced_reuse_ids[:1]  # Only first for squash
            else:
                reuse_ids = self._perform_robust_reconciliation(
                    inputs, gh, gerrit, single_commit
                )

        # Now prepare commits once with the correct reuse_ids
        if inputs.submit_single_commits:
            prep = self._prepare_single_commits(
                inputs,
                gh,
                gerrit,
                reuse_change_ids=reuse_ids if reuse_ids else None,
            )
        else:
            prep = self._prepare_squashed_commit(
                inputs,
                gh,
                gerrit,
                reuse_change_ids=reuse_ids[:1] if reuse_ids else None,
            )

        self._apply_pr_title_body_if_requested(inputs, gh, operation_mode)

        # Store context for downstream push/comment emission (Phase 2)
        self._gh_context_for_push = gh
        self._push_to_gerrit(
            gerrit=gerrit,
            repo=repo_names,
            branch=self._resolve_target_branch(),
            reviewers=self._resolve_reviewers(inputs),
            single_commits=inputs.submit_single_commits,
            prepared=prep,
        )

        result = self._query_gerrit_for_results(
            gerrit=gerrit,
            repo=repo_names,
            change_ids=prep.change_ids,
        )

        # Verify patchset creation for UPDATE operations
        if is_update_operation or is_edit_operation:
            log.debug("🔍 Verifying patchset creation...")
            self._verify_patchset_creation(
                gerrit=gerrit,
                change_ids=prep.change_ids,
                expected_operation="update" if is_update_operation else "edit",
            )

        # Sync metadata for UPDATE and EDIT operations
        if is_update_operation or is_edit_operation:
            log.debug("🔄 Syncing PR metadata to Gerrit change(s)...")
            self._sync_gerrit_change_metadata(
                gh=gh,
                gerrit=gerrit,
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

        # Validate that no unexpected files were committed
        self._validate_committed_files(gh, result)

        self._close_pull_request_if_required(gh)

        log.debug("Pipeline complete: %s", result)
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
                content = (
                    repo_obj.get_contents(".gitreview", ref=ref)
                    if ref
                    else repo_obj.get_contents(".gitreview")
                )
                text_remote = (
                    getattr(content, "decoded_content", b"") or b""
                ).decode("utf-8")
                info_remote = self._parse_gitreview_text(text_remote)
                if info_remote:
                    log.debug("Parsed remote .gitreview: %s", info_remote)
                    return info_remote
                log.info("Remote .gitreview missing required keys; ignoring")
            except Exception as exc:
                log.debug("Remote .gitreview not available: %s", exc)
            # Attempt raw.githubusercontent.com as a fallback
            try:
                repo_full = (
                    (
                        gh.repository
                        if gh
                        else os.getenv("GITHUB_REPOSITORY", "")
                    )
                    or ""
                ).strip()
                branches: list[str] = []
                # Prefer PR head/base refs via GitHub API when running
                # from a direct URL when a token is available
                try:
                    # When a target URL was provided via CLI, G2G_TARGET_URL
                    # contains the actual URL string (truthy check)
                    if (
                        gh
                        and gh.pr_number
                        and os.getenv("G2G_TARGET_URL")
                        and (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"))
                    ):
                        client = build_client()
                        repo_obj = get_repo_from_env(client)
                        pr_obj = get_pull(repo_obj, int(gh.pr_number))
                        api_head = str(
                            getattr(
                                getattr(pr_obj, "head", object()), "ref", ""
                            )
                            or ""
                        )
                        api_base = str(
                            getattr(
                                getattr(pr_obj, "base", object()), "ref", ""
                            )
                            or ""
                        )
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
                    if (
                        parsed.scheme != "https"
                        or parsed.netloc != "raw.githubusercontent.com"
                    ):
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
        _owner, name = repo_full.split("/", 1)
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
        log.debug(
            "_resolve_gerrit_info: inputs.ci_testing=%s", inputs.ci_testing
        )
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
        port_s = str(inputs.gerrit_server_port).strip() or "29418"
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
            # When a target URL was provided via CLI (G2G_TARGET_URL is set),
            # use the derived Gerrit project name from the repository
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
        log.debug(
            "Starting SSH setup for Gerrit %s:%d", gerrit.host, gerrit.port
        )
        if not inputs.gerrit_ssh_privkey_g2g:
            log.debug("SSH private key not provided, skipping SSH setup")
            return

        log.debug("SSH private key provided, proceeding with SSH configuration")

        # Check for ssh-keyscan availability early if auto-discovery needed
        if (
            auto_discover_gerrit_host_keys is not None
            and not inputs.gerrit_known_hosts
        ):
            import shutil

            keyscan_path = shutil.which("ssh-keyscan")
            if not keyscan_path:
                log.error(
                    "❌ ssh-keyscan not found in PATH but is required for SSH "
                    "host key auto-discovery"
                )
                log.error(
                    "Available tools in PATH: %s",
                    ", ".join(
                        [
                            tool
                            for tool in [
                                "ssh",
                                "ssh-keygen",
                                "ssh-add",
                                "ssh-agent",
                                "ssh-keyscan",
                            ]
                            if shutil.which(tool)
                        ]
                    ),
                )
                log.error("To fix this issue:")
                log.error("1. Install openssh-client package, OR")
                log.error("2. Provide GERRIT_KNOWN_HOSTS manually")
            else:
                log.debug("✅ ssh-keyscan found at: %s", keyscan_path)

        # Auto-discover or augment host keys (merge missing
        # types/[host]:port entries)
        effective_known_hosts = inputs.gerrit_known_hosts
        if auto_discover_gerrit_host_keys is not None:
            try:
                if not effective_known_hosts:
                    log.info(
                        "🔍 GERRIT_KNOWN_HOSTS not provided, attempting "
                        "auto-discovery for %s:%d...",
                        gerrit.host,
                        gerrit.port,
                    )
                    log.debug(
                        "Auto-discovery params: host=%s, port=%d, org=%s",
                        gerrit.host,
                        gerrit.port,
                        inputs.organization,
                    )

                    discovered_keys = auto_discover_gerrit_host_keys(
                        gerrit_hostname=gerrit.host,
                        gerrit_port=gerrit.port,
                        organization=inputs.organization,
                    )
                    if discovered_keys:
                        effective_known_hosts = discovered_keys
                        log.info(
                            "✅ Successfully auto-discovered SSH host keys for "
                            "%s:%d",
                            gerrit.host,
                            gerrit.port,
                        )
                    else:
                        log.error(
                            "❌ Auto-discovery failed for %s:%d - SSH host key "
                            "verification will likely fail. Check network "
                            "connectivity and ssh-keyscan availability.",
                            gerrit.host,
                            gerrit.port,
                        )
                else:
                    # Provided known_hosts exists; ensure it contains
                    # [host]:port entries and modern key types
                    lower = effective_known_hosts.lower()
                    bracket_host = f"[{gerrit.host}]:{gerrit.port}"
                    bracket_lower = bracket_host.lower()
                    needs_discovery = False
                    if bracket_lower not in lower:
                        needs_discovery = True
                    else:
                        # Confirm at least one known key type exists for the
                        # bracketed host
                        if (
                            f"{bracket_lower} ssh-ed25519" not in lower
                            and f"{bracket_lower} ecdsa-sha2" not in lower
                            and f"{bracket_lower} ssh-rsa" not in lower
                        ):
                            needs_discovery = True
                    if needs_discovery:
                        log.info(
                            "Augmenting provided GERRIT_KNOWN_HOSTS with "
                            "discovered entries for %s:%d",
                            gerrit.host,
                            gerrit.port,
                        )
                        discovered_keys = auto_discover_gerrit_host_keys(
                            gerrit_hostname=gerrit.host,
                            gerrit_port=gerrit.port,
                            organization=inputs.organization,
                        )
                        if discovered_keys:
                            # Use centralized merging logic
                            effective_known_hosts = merge_known_hosts_content(
                                effective_known_hosts, discovered_keys
                            )
                            log.info(
                                "Known hosts augmented with discovered entries "
                                "for %s:%d",
                                gerrit.host,
                                gerrit.port,
                            )
                        else:
                            log.warning(
                                "Auto-discovery returned no keys; known_hosts "
                                "not augmented"
                            )
            except Exception:
                log.exception(
                    "❌ SSH host key auto-discovery/augmentation failed "
                    "for %s:%d",
                    gerrit.host,
                    gerrit.port,
                )

        if not effective_known_hosts:
            log.warning(
                "⚠️  No SSH host keys available (manual or auto-discovered) "
                "for %s:%d. SSH connections may fail due to host key "
                "verification. Consider setting GERRIT_KNOWN_HOSTS or ensure "
                "ssh-keyscan is available.",
                gerrit.host,
                gerrit.port,
            )
            return

        # Check if SSH agent authentication is preferred
        use_ssh_agent = env_bool("G2G_USE_SSH_AGENT", default=True)
        log.debug(
            "SSH agent preference: use_ssh_agent=%s, "
            "setup_ssh_agent_auth available=%s",
            use_ssh_agent,
            setup_ssh_agent_auth is not None,
        )

        if use_ssh_agent and setup_ssh_agent_auth is not None:
            # Try SSH agent first as it's more secure and avoids file
            # permission issues. This performs runtime validation:
            # 1. Checks if SSH agent is running (SSH_AUTH_SOCK)
            # 2. Validates agent has keys loaded using "ssh-add -l"
            # 3. Falls back gracefully if validation fails
            log.debug("Attempting SSH agent-based authentication")
            if self._try_ssh_agent_setup(inputs, effective_known_hosts):
                log.debug("SSH agent setup successful")
                return

            # Fall back to file-based SSH if agent setup fails
            # This provides robust fallback when SSH agent is unavailable,
            # has no keys loaded, or encounters any other issues
            log.info("Falling back to file-based SSH authentication")

        log.debug("Using file-based SSH authentication")
        self._setup_file_based_ssh(inputs, effective_known_hosts)

    def _try_ssh_agent_setup(
        self, inputs: Inputs, effective_known_hosts: str
    ) -> bool:
        """Try to setup SSH agent-based authentication.

        Performs comprehensive SSH agent validation:
        1. Checks for existing SSH agent (SSH_AUTH_SOCK environment)
        2. Validates agent has keys loaded using "ssh-add -l"
        3. If existing agent has no keys but private key provided, starts new
        4. If no agent and no key provided, fails gracefully

        This method implements the runtime SSH agent validation that was
        deferred from early CLI validation to avoid duplicate checks and
        allow for dynamic agent availability.

        Args:
            inputs: Validated input configuration
            effective_known_hosts: Known hosts content

        Returns:
            True if SSH agent setup succeeded, False otherwise (triggers
            fallback)
        """
        if setup_ssh_agent_auth is None:
            return False  # type: ignore[unreachable]

        try:
            log.debug("Setting up SSH agent-based authentication (more secure)")
            log.debug("Workspace: %s", self.workspace)
            log.debug(
                "Private key length: %d characters",
                len(inputs.gerrit_ssh_privkey_g2g),
            )
            log.debug(
                "Known hosts length: %d characters", len(effective_known_hosts)
            )

            # Create secure separate temp directory for SSH agent if needed
            import secrets
            import tempfile

            if not self._ssh_temp_dir:
                # Use secure random suffix to prevent predictable paths
                secure_suffix = secrets.token_hex(8)
                self._ssh_temp_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f"g2g_ssh_{secure_suffix}_",
                        dir=tempfile.gettempdir(),
                    )
                )
                # Ensure directory has restrictive permissions
                self._ssh_temp_dir.chmod(0o700)
                log.debug(
                    "Created secure SSH temp directory: %s", self._ssh_temp_dir
                )

            self._ssh_agent_manager = setup_ssh_agent_auth(
                workspace=self._ssh_temp_dir,
                private_key_content=inputs.gerrit_ssh_privkey_g2g,
                known_hosts_content=effective_known_hosts,
            )
            self._use_ssh_agent = True
            log.debug("SSH agent authentication configured successfully")

        except Exception as exc:
            log.debug("SSH agent setup failed: %s", exc)
            log.warning(
                "SSH agent setup failed, falling back to file-based SSH: %s",
                exc,
            )
            # Clean up any partial SSH agent setup before fallback
            if self._ssh_agent_manager:
                self._ssh_agent_manager.cleanup()
                self._ssh_agent_manager = None
            # Return False to trigger fallback to file-based SSH authentication
            # This graceful degradation ensures the workflow continues even when
            # SSH agent validation fails (e.g., no agent running, no keys)
            return False
        else:
            return True

    def _setup_file_based_ssh(
        self, inputs: Inputs, effective_known_hosts: str
    ) -> None:
        """Setup file-based SSH authentication as fallback.

        Args:
            inputs: Validated input configuration
            effective_known_hosts: Known hosts content
        """
        log.debug("Using file-based SSH configuration for Gerrit")
        log.debug(
            "Using secure temporary SSH files outside workspace to prevent "
            "artifacts"
        )
        log.debug(
            "Private key length: %d characters",
            len(inputs.gerrit_ssh_privkey_g2g),
        )
        log.debug(
            "Known hosts length: %d characters", len(effective_known_hosts)
        )

        # Create secure tool-specific SSH directory outside workspace to prevent
        # SSH artifacts from being accidentally committed
        import secrets
        import tempfile

        if not self._ssh_temp_dir:
            # Use secure random suffix to prevent predictable paths
            secure_suffix = secrets.token_hex(8)
            self._ssh_temp_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"g2g_ssh_{secure_suffix}_",
                    dir=tempfile.gettempdir(),
                )
            )
            # Ensure directory has restrictive permissions
            self._ssh_temp_dir.chmod(0o700)
            log.debug(
                "Created secure SSH temp directory: %s", self._ssh_temp_dir
            )
        tool_ssh_dir = self._ssh_temp_dir
        tool_ssh_dir.mkdir(mode=0o700, exist_ok=True)

        # Write SSH private key to tool-specific location with secure
        # permissions
        key_path = tool_ssh_dir / "gerrit_key"
        log.debug("SSH key file path: %s", key_path)

        # Use a more robust approach for creating the file with secure
        # permissions
        key_content = inputs.gerrit_ssh_privkey_g2g.strip() + "\n"

        # Multiple strategies to create secure key file
        log.debug("Attempting to create secure key file")
        success = self._create_secure_key_file(key_path, key_content)
        log.debug("Secure key file creation success: %s", success)

        if not success:
            # If all permission strategies fail, create in memory directory
            log.debug("Falling back to memory-based key file creation")
            success = self._create_key_in_memory_fs(key_path, key_content)
            log.debug("Memory-based key file creation success: %s", success)

        if not success:
            msg = (
                "Failed to create SSH key file with secure permissions. "
                "This may be due to CI environment restrictions. "
                "Consider using G2G_USE_SSH_AGENT=true (default) for SSH "
                "agent authentication."
            )
            log.error("SSH key file creation failed: %s", msg)
            raise RuntimeError(msg)

        log.debug("SSH key file created successfully: %s", key_path)

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
        log.debug("File-based SSH setup completed successfully")

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
                    log.debug(
                        "SSH key created successfully with strategy: %s",
                        strategy_name,
                    )
                    return True
                else:
                    log.debug(
                        "Strategy %s resulted in permissions %s",
                        strategy_name,
                        actual_perms,
                    )

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

    def _strategy_stat_constants(
        self, key_path: Path, key_content: str
    ) -> None:
        """Strategy: use stat constants for permission setting."""
        import os
        import stat

        with open(key_path, "w", encoding="utf-8") as f:
            f.write(key_content)

        # Try multiple permission setting approaches
        mode = stat.S_IRUSR | stat.S_IWUSR
        os.chmod(str(key_path), mode)
        key_path.chmod(mode)

    def _create_key_in_memory_fs(
        self, key_path: Path, key_content: str
    ) -> bool:
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
                        mode="w",
                        dir=memory_dir,
                        prefix="g2g_key_",
                        suffix=".tmp",
                        delete=False,
                    ) as tmp_file:
                        tmp_file.write(key_content)
                        tmp_path = Path(tmp_file.name)

                    # Try to set permissions
                    tmp_path.chmod(0o600)
                    actual_perms = oct(tmp_path.stat().st_mode)[-3:]

                    if actual_perms == "600":
                        # Move to final location
                        shutil.move(str(tmp_path), str(key_path))
                        log.debug(
                            "Successfully created SSH key using memory "
                            "filesystem: %s",
                            memory_dir,
                        )
                        return True
                    else:
                        tmp_path.unlink()

                except Exception as exc:
                    log.debug(
                        "Memory filesystem strategy failed for %s: %s",
                        memory_dir,
                        exc,
                    )
                    try:
                        if tmp_path is not None and tmp_path.exists():
                            tmp_path.unlink()
                    except Exception as cleanup_exc:
                        log.debug(
                            "Failed to cleanup temporary key file: %s",
                            cleanup_exc,
                        )

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

        log.debug("Building SSH environment for git operations")
        env = build_non_interactive_ssh_env()

        # Set GIT_SSH_COMMAND based on available configuration
        cmd = self._build_git_ssh_command
        if cmd:
            log.debug("Using custom GIT_SSH_COMMAND: %s", cmd)
            env["GIT_SSH_COMMAND"] = cmd
        else:
            # Fallback to basic non-interactive SSH command
            from .ssh_common import build_git_ssh_command

            fallback_cmd = build_git_ssh_command()
            log.debug("Using fallback GIT_SSH_COMMAND: %s", fallback_cmd)
            env["GIT_SSH_COMMAND"] = fallback_cmd

        # Override SSH agent settings if using SSH agent
        if self._use_ssh_agent and self._ssh_agent_manager:
            log.debug("Applying SSH agent environment variables")
            agent_env = self._ssh_agent_manager.get_ssh_env()
            log.debug(
                "SSH agent environment: %s",
                {k: v for k, v in agent_env.items() if "SSH" in k},
            )
            env.update(agent_env)
        else:
            log.debug(
                "Not using SSH agent (use_ssh_agent=%s, manager=%s)",
                self._use_ssh_agent,
                self._ssh_agent_manager is not None,
            )

        log.debug("Final SSH environment contains %d variables", len(env))
        return env

    def _ensure_workspace_prepared(self, branch: str) -> None:
        """Ensure workspace is prepared with latest remote state.

        Performs a git fetch to get the latest branch state. Does NOT
        proactively unshallow to avoid performance impact on large repos.
        If checkout later fails due to missing commits in shallow clone,
        _checkout_with_unshallow_fallback() will handle it reactively.

        Args:
            branch: The branch to fetch from origin
        """
        if self._workspace_prepared and self._prepared_branch == branch:
            log.debug("Workspace already prepared for branch: %s", branch)
            return

        log.debug(
            "Preparing workspace: fetching latest state for branch %s", branch
        )
        try:
            run_cmd(
                ["git", "fetch", "origin", branch],
                cwd=self.workspace,
                env=self._ssh_env(),
            )
            self._workspace_prepared = True
            self._prepared_branch = branch
            log.debug("Workspace preparation completed for branch: %s", branch)
        except CommandError as exc:
            log.warning("Failed to fetch from origin: %s", exc)
            # Don't mark as prepared if fetch failed
            raise

    def _is_shallow_clone(self) -> bool:
        """Check if the current workspace is a shallow clone.

        Returns:
            True if the repository is a shallow clone, False otherwise.
        """
        shallow_file = self.workspace / ".git" / "shallow"
        if shallow_file.exists():
            return True
        # Also check via git command for edge cases (e.g., worktrees)
        try:
            result = run_cmd(
                ["git", "rev-parse", "--is-shallow-repository"],
                cwd=self.workspace,
                check=False,
            )
            return result.stdout.strip().lower() == "true"
        except Exception:
            return False

    def _unshallow_repository(self) -> bool:
        """Unshallow the repository to get full history.

        Returns:
            True if unshallowing succeeded or repo is already full,
            False if unshallowing failed.
        """
        if not self._is_shallow_clone():
            log.debug("Repository is not shallow, no unshallow needed")
            return True

        log.info("Unshallowing repository to fetch full history...")
        try:
            run_cmd(
                ["git", "fetch", "--unshallow", "origin"],
                cwd=self.workspace,
                env=self._ssh_env(),
            )
        except CommandError as exc:
            log.warning("Failed to unshallow repository: %s", exc)
            return False
        else:
            log.debug("Repository unshallowed successfully")
            return True

    def _deepen_repository(self, depth: int = 100) -> bool:
        """Deepen the repository history by fetching more commits.

        This is a lighter alternative to full unshallow, fetching only
        the specified number of additional commits.

        Args:
            depth: Number of additional commits to fetch (default: 100)

        Returns:
            True if deepening succeeded, False otherwise.
        """
        if not self._is_shallow_clone():
            log.debug("Repository is not shallow, no deepening needed")
            return True

        log.debug("Deepening repository by %d commits...", depth)
        try:
            run_cmd(
                ["git", "fetch", f"--deepen={depth}", "origin"],
                cwd=self.workspace,
                env=self._ssh_env(),
            )
        except CommandError as exc:
            log.debug("Failed to deepen repository: %s", exc)
            return False
        else:
            log.debug("Repository deepened by %d commits", depth)
            return True

    def _checkout_with_unshallow_fallback(
        self,
        branch_name: str,
        start_point: str,
        create_branch: bool = True,
    ) -> None:
        """Checkout a branch with graduated deepening fallback.

        If the initial checkout fails because the start_point SHA is not
        available (common in shallow clones), this method will:
        1. First try to deepen the repository (fetch 100 more commits)
        2. If that fails, fully unshallow the repository
        3. Retry the checkout after each attempt

        This graduated approach minimizes performance impact for most cases
        while still handling edge cases where full history is needed.

        Args:
            branch_name: Name of the branch to checkout or create
            start_point: The SHA or ref to start the branch from
            create_branch: If True, create a new branch (-b flag)

        Raises:
            CommandError: If checkout fails even after unshallowing
        """
        cmd = ["git", "checkout"]
        if create_branch:
            cmd.extend(["-b", branch_name, start_point])
        else:
            cmd.append(branch_name)

        checkout_exc: CommandError | None = None
        try:
            run_cmd(cmd, cwd=self.workspace)
        except CommandError as exc:
            checkout_exc = exc

        if checkout_exc is None:
            return  # Success on first attempt

        # Analyze the failure
        error_msg = str(checkout_exc).lower()
        # Check if failure is due to missing commit (shallow clone issue)
        is_missing_commit = (
            "not a commit" in error_msg
            or "cannot be created from" in error_msg
            or "bad revision" in error_msg
            or "unknown revision" in error_msg
            or "invalid reference" in error_msg
        )

        if not is_missing_commit:
            # Not a shallow clone issue, re-raise immediately
            raise checkout_exc

        log.warning(
            "Checkout failed, SHA %s not available in shallow clone. "
            "Attempting graduated deepening...",
            start_point,
        )

        # Step 1: Try deepening first (cheaper than full unshallow)
        if self._deepen_repository(depth=100):
            log.debug("Retrying checkout after deepening...")
            try:
                run_cmd(cmd, cwd=self.workspace)
            except CommandError as deepen_exc:
                log.debug(
                    "Checkout still failed after deepening: %s", deepen_exc
                )
            else:
                log.info("Checkout succeeded after deepening repository")
                return

        # Step 2: Full unshallow as last resort
        log.info("Deepening insufficient, performing full unshallow...")
        if not self._unshallow_repository():
            log.error(
                "Failed to unshallow repository. Cannot checkout SHA: %s",
                start_point,
            )
            raise checkout_exc

        # Retry the checkout after full unshallow
        log.debug("Retrying checkout after full unshallow...")
        run_cmd(cmd, cwd=self.workspace)
        log.info("Checkout succeeded after full unshallow")

    def _merge_squash_with_unshallow_fallback(self, head_sha: str) -> None:
        """Perform git merge --squash with graduated deepening fallback.

        If the initial merge fails because the shallow clone lacks a common
        ancestor between the base and head (causing "refusing to merge
        unrelated histories"), this method will:
        1. First try to deepen the repository (fetch 100 more commits)
        2. If that fails, fully unshallow the repository
        3. Retry the merge after each attempt

        This mirrors the graduated approach used by
        ``_checkout_with_unshallow_fallback`` but for merge operations,
        which are equally susceptible to shallow clone limitations.

        Args:
            head_sha: The SHA to merge (PR head commit)

        Raises:
            CommandError: If merge fails even after unshallowing, or if
                the failure is not related to shallow clone history
        """
        merge_cmd = ["git", "merge", "--squash", head_sha]

        merge_exc: CommandError | None = None
        try:
            run_cmd(merge_cmd, cwd=self.workspace)
        except CommandError as exc:
            merge_exc = exc

        if merge_exc is None:
            return  # Success on first attempt

        # Check if the failure is due to unrelated histories in a shallow clone
        error_str = str(merge_exc).lower()
        stderr_str = (merge_exc.stderr or "").lower()
        combined = f"{error_str} {stderr_str}"

        is_unrelated_histories = (
            "refusing to merge unrelated histories" in combined
            or "unrelated histories" in combined
            or "no common ancestor" in combined
        )

        if not is_unrelated_histories or not self._is_shallow_clone():
            # Not a shallow clone issue — let the caller handle the error
            raise merge_exc

        log.warning(
            "Merge --squash failed due to unrelated histories in shallow "
            "clone. Attempting graduated deepening to recover..."
        )

        # Step 1: Try deepening first (cheaper than full unshallow)
        if self._deepen_repository(depth=100):
            log.debug("Retrying merge --squash after deepening...")
            # Reset the failed merge state before retrying
            run_cmd(
                ["git", "merge", "--abort"],
                cwd=self.workspace,
                check=False,
            )
            try:
                run_cmd(merge_cmd, cwd=self.workspace)
            except CommandError as deepen_exc:
                log.debug(
                    "Merge --squash still failed after deepening: %s",
                    deepen_exc,
                )
                # Re-check whether this is still a shallow-history problem;
                # if the error has changed (e.g. real merge conflict), an
                # expensive full unshallow cannot help — propagate immediately.
                deepen_combined = (
                    f"{deepen_exc} {deepen_exc.stderr or ''}".lower()
                )
                deepen_is_unrelated = (
                    "refusing to merge unrelated histories" in deepen_combined
                    or "unrelated histories" in deepen_combined
                    or "no common ancestor" in deepen_combined
                )
                if not deepen_is_unrelated:
                    raise
            else:
                log.info("Merge --squash succeeded after deepening repository")
                return

        # Step 2: Full unshallow as last resort
        log.info(
            "Deepening insufficient, performing full unshallow for merge..."
        )
        # Reset the failed merge state before retrying
        run_cmd(
            ["git", "merge", "--abort"],
            cwd=self.workspace,
            check=False,
        )
        if not self._unshallow_repository():
            log.error(
                "Failed to unshallow repository. Cannot merge SHA: %s",
                head_sha,
            )
            raise merge_exc

        # Retry the merge after full unshallow
        log.debug("Retrying merge --squash after full unshallow...")
        run_cmd(merge_cmd, cwd=self.workspace)
        log.info("Merge --squash succeeded after full unshallow")

    def _cleanup_ssh(self) -> None:
        """Clean up temporary SSH files created by this tool.

        Securely removes the separate SSH temporary directory and all contents.
        This ensures no temporary files or credentials are left behind.
        """
        log.debug("Cleaning up temporary SSH configuration files")

        try:
            # Clean up SSH agent if we used it
            if self._ssh_agent_manager:
                self._ssh_agent_manager.cleanup()
                self._ssh_agent_manager = None
                self._use_ssh_agent = False

            # Securely remove separate SSH temporary directory and all contents
            if self._ssh_temp_dir and self._ssh_temp_dir.exists():
                import os
                import shutil

                # First, overwrite any key files to prevent recovery
                try:
                    for root, _dirs, files in os.walk(self._ssh_temp_dir):
                        for file in files:
                            file_path = Path(root) / file
                            if file_path.exists() and file_path.is_file():
                                # Overwrite file with random data
                                try:
                                    size = file_path.stat().st_size
                                    if size > 0:
                                        import secrets

                                        with open(file_path, "wb") as f:
                                            f.write(secrets.token_bytes(size))
                                            # Sync to ensure write completes
                                            os.fsync(f.fileno())
                                except Exception as overwrite_exc:
                                    log.debug(
                                        "Failed to overwrite %s: %s",
                                        file_path,
                                        overwrite_exc,
                                    )
                except Exception as walk_exc:
                    log.debug(
                        "Failed to walk SSH temp directory for secure "
                        "cleanup: %s",
                        walk_exc,
                    )

                # Remove the directory tree
                shutil.rmtree(self._ssh_temp_dir)
                log.debug(
                    "Securely cleaned up temporary SSH directory: %s",
                    self._ssh_temp_dir,
                )
                self._ssh_temp_dir = None
        except Exception as exc:
            log.warning("Failed to clean up temporary SSH files: %s", exc)

    def _configure_git(
        self,
        gerrit: GerritInfo,
        inputs: Inputs,
    ) -> None:
        """Set git global config and initialize git-review if needed."""
        log.debug("Configuring git and git-review for %s", gerrit.host)

        # Git user identity is now configured earlier before merge operations
        # Prefer repo-local config; fallback to global if needed
        try:
            git_config(
                "gitreview.username",
                inputs.gerrit_ssh_user_g2g,
                global_=False,
                cwd=self.workspace,
            )
        except GitError:
            git_config(
                "gitreview.username", inputs.gerrit_ssh_user_g2g, global_=True
            )
        # Git user identity is configured by _ensure_git_user_identity
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
            remote_url = (
                f"ssh://{ssh_user}@{gerrit.host}:{gerrit.port}/{gerrit.project}"
            )
            log.debug("Adding 'gerrit' remote: %s", remote_url)
            # Use our specific SSH configuration for adding remote
            env = self._ssh_env()
            run_cmd(
                ["git", "remote", "add", "gerrit", remote_url],
                check=False,
                cwd=self.workspace,
                env=env,
            )

        # Workaround for submodules commit-msg hook
        hooks_path = run_cmd(
            ["git", "rev-parse", "--show-toplevel"], cwd=self.workspace
        ).stdout.strip()
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
        # Initialize git-review (copies commit-msg hook) - only once per
        # execution
        if not self._git_review_initialized:
            try:
                # Use our specific SSH configuration for git-review setup
                log.debug("Initializing git-review (one-time setup)")
                env = self._ssh_env()
                run_cmd(
                    ["git", "review", "-s", "-v"], cwd=self.workspace, env=env
                )
                self._git_review_initialized = True
                log.debug("Git-review initialization completed")
            except CommandError as exc:
                msg = f"Failed to initialize git-review: {exc}"
                raise OrchestratorError(msg) from exc
        else:
            log.debug("Git-review already initialized, skipping setup")

    def _prepare_single_commits(
        self,
        inputs: Inputs,
        gh: GitHubContext,
        gerrit: GerritInfo,
        reuse_change_ids: list[str] | None = None,
    ) -> PreparedChange:
        """Cherry-pick commits one-by-one and ensure Change-Id is present."""
        log.info("Preparing single-commit submission for PR #%s", gh.pr_number)
        branch = self._resolve_target_branch()
        # Determine commit range: commits in HEAD not in base branch
        base_ref = f"origin/{branch}"
        # Use our SSH command for git operations that might need SSH

        # Ensure workspace is prepared (consolidated git fetch)
        self._ensure_workspace_prepared(branch)
        revs = run_cmd(
            ["git", "rev-list", "--reverse", f"{base_ref}..HEAD"],
            cwd=self.workspace,
        ).stdout
        commit_list = [c for c in revs.splitlines() if c.strip()]
        if not commit_list:
            log.info("No commits to submit; returning empty PreparedChange")
            return PreparedChange(change_ids=[], commit_shas=[])
        # Create temp branch from base sha; export for downstream
        base_sha = run_cmd(
            ["git", "rev-parse", base_ref], cwd=self.workspace
        ).stdout.strip()
        tmp_branch = f"g2g_tmp_{gh.pr_number or 'pr'!s}_{os.getpid()}"
        os.environ["G2G_TMP_BRANCH"] = tmp_branch
        self._checkout_with_unshallow_fallback(
            branch_name=tmp_branch,
            start_point=base_sha,
            create_branch=True,
        )
        change_ids: list[str] = []
        for idx, csha in enumerate(commit_list):
            run_cmd(["git", "checkout", tmp_branch], cwd=self.workspace)
            git_cherry_pick(csha, cwd=self.workspace)
            # Preserve author of the original commit
            author = run_cmd(
                ["git", "show", "-s", "--pretty=format:%an <%ae>", csha],
                cwd=self.workspace,
            ).stdout.strip()
            git_commit_amend(
                author=author, no_edit=True, signoff=True, cwd=self.workspace
            )

            # Get current commit message
            cur_msg = run_cmd(
                ["git", "show", "-s", "--pretty=format:%B", "HEAD"],
                cwd=self.workspace,
            ).stdout
            # Clean ellipses from commit message
            cur_msg = _clean_ellipses_from_message(cur_msg)

            # Determine Change-ID to use (reuse if provided)
            desired_change_id = None
            if reuse_change_ids and idx < len(reuse_change_ids):
                desired_change_id = reuse_change_ids[idx]

            # Build topic for metadata
            if "/" in gh.repository:
                repo_name = gh.repository.split("/")[-1]
            else:
                repo_name = gh.repository
            if gh.pr_number:
                topic = f"GH-{gh.repository_owner}-{repo_name}-{gh.pr_number}"
            else:
                topic = f"GH-{gh.repository_owner}-{repo_name}"

            # Use centralized function to build complete message
            # with all trailers
            new_msg = self._build_commit_message_with_trailers(
                base_message=cur_msg,
                inputs=inputs,
                gh=gh,
                change_id=desired_change_id,
                preserve_existing=True,
                include_g2g_metadata=True,
                g2g_mode="multi-commit",
                g2g_topic=topic,
                g2g_change_ids=reuse_change_ids if reuse_change_ids else None,
            )

            # Only amend if message changed
            if new_msg.strip() != cur_msg.strip():
                git_commit_amend(
                    message=new_msg,
                    no_edit=False,
                    signoff=False,
                    author=author,
                    cwd=self.workspace,
                )
            # Extract newly added Change-Id from last commit trailers
            trailers = git_last_commit_trailers(
                keys=["Change-Id"], cwd=self.workspace
            )
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
                "No Change-IDs collected during preparation for PR #%s "
                "(will be ensured via commit-msg hook)",
                gh.pr_number,
            )
        return PreparedChange(change_ids=uniq_ids, commit_shas=[])

    def _prepare_squashed_commit(
        self,
        inputs: Inputs,
        gh: GitHubContext,
        gerrit: GerritInfo,
        reuse_change_ids: list[str] | None = None,
    ) -> PreparedChange:
        """Squash PR commits into a single commit and handle Change-Id."""
        log.debug("Preparing squashed commit for PR #%s", gh.pr_number)
        branch = self._resolve_target_branch()

        # Ensure workspace is prepared (consolidated git fetch)
        self._ensure_workspace_prepared(branch)
        base_ref = f"origin/{branch}"
        base_sha = run_cmd(
            ["git", "rev-parse", base_ref], cwd=self.workspace
        ).stdout.strip()
        head_sha = run_cmd(
            ["git", "rev-parse", "HEAD"], cwd=self.workspace
        ).stdout.strip()

        # Create temp branch from base and merge-squash PR head
        tmp_branch = f"g2g_tmp_{gh.pr_number or 'pr'!s}_{os.getpid()}"
        os.environ["G2G_TMP_BRANCH"] = tmp_branch

        log.debug(
            "Git merge preparation: base_sha=%s, head_sha=%s, tmp_branch=%s",
            base_sha,
            head_sha,
            tmp_branch,
        )

        # Check if we have any commits to merge
        try:
            merge_base = run_cmd(
                ["git", "merge-base", base_sha, head_sha], cwd=self.workspace
            ).stdout.strip()
            log.debug("Merge base: %s", merge_base)

            # Check if there are any commits between base and head
            commits_to_merge = run_cmd(
                ["git", "rev-list", f"{base_sha}..{head_sha}"],
                cwd=self.workspace,
            ).stdout.strip()
            if not commits_to_merge:
                log.warning(
                    "No commits found between base (%s) and head (%s)",
                    base_sha,
                    head_sha,
                )
            else:
                commit_count = len(commits_to_merge.splitlines())
                log.debug("Found %d commits to merge", commit_count)

        except Exception as debug_exc:
            log.warning("Failed to analyze merge situation: %s", debug_exc)
            # Proactively deepen if merge-base fails in a shallow clone,
            # as this strongly indicates the shallow history is insufficient
            # for the upcoming merge --squash operation.
            if self._is_shallow_clone():
                log.info(
                    "merge-base failed in shallow clone — proactively "
                    "deepening repository to improve merge success chances"
                )
                self._deepen_repository(depth=100)

        self._checkout_with_unshallow_fallback(
            branch_name=tmp_branch,
            start_point=base_sha,
            create_branch=True,
        )

        # Show git status before attempting merge
        try:
            status_output = run_cmd(
                ["git", "status", "--porcelain"], cwd=self.workspace
            ).stdout
            if status_output.strip():
                log.debug(
                    "Git status before merge (modified files detected):\n%s",
                    status_output,
                )
            else:
                log.debug("Git status before merge: working directory clean")

            # Show current branch
            current_branch = run_cmd(
                ["git", "branch", "--show-current"], cwd=self.workspace
            ).stdout.strip()
            log.debug("Current branch before merge: %s", current_branch)

        except Exception as status_exc:
            log.warning("Failed to get git status before merge: %s", status_exc)

        log.debug("About to run: git merge --squash %s", head_sha)
        try:
            self._merge_squash_with_unshallow_fallback(head_sha)
        except CommandError as merge_exc:
            # Enhanced error handling for git merge failures
            error_details = self._analyze_merge_failure(
                merge_exc, base_sha, head_sha
            )

            # Try to provide recovery suggestions
            recovery_msg = self._suggest_merge_recovery(
                merge_exc, base_sha, head_sha
            )

            # Log error at debug level - user-friendly message comes later
            log.debug("Git merge --squash failed: %s", error_details)
            if recovery_msg:
                log.debug("Suggested recovery: %s", recovery_msg)

            # Enhanced debugging if verbose mode is enabled
            from .utils import is_verbose_mode

            if is_verbose_mode():
                self._debug_merge_failure_context(base_sha, head_sha)

            # Re-raise with user-friendly message
            error_msg = "Failed to merge PR commits"
            if "refusing to merge unrelated histories" in str(merge_exc):
                error_msg = (
                    "Cannot merge PR: branches have unrelated histories. "
                    "The PR branch may not be based on the correct target "
                    "branch."
                )
            elif recovery_msg and "conflict" in recovery_msg.lower():
                error_msg = (
                    "Cannot merge PR: merge conflicts detected. "
                    "Please resolve conflicts in the PR."
                )
            elif recovery_msg:
                error_msg = f"Failed to merge PR commits. {recovery_msg}"

            raise OrchestratorError(error_msg) from merge_exc

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
                if ln.strip() in ("---", "```") or ln.startswith(
                    "updated-dependencies:"
                ):
                    in_metadata_section = True
                    continue
                if in_metadata_section:
                    if ln.startswith(("- dependency-", "  dependency-")):
                        continue
                    if (
                        not ln.startswith(("  ", "-", "dependency-"))
                        and ln.strip()
                    ):
                        in_metadata_section = False
                # Skip Change-Id lines from body - they should only be in footer
                if ln.startswith("Change-Id:"):
                    log.debug(
                        "Skipping Change-Id from commit body: %s", ln.strip()
                    )
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
                        title_line = title_line[
                            : title_line.index(bp) + len(bp.strip())
                        ]
                        break
                else:
                    words = title_line[:100].split()
                    title_line = (
                        " ".join(words[:-1])
                        if len(words) > 1
                        else title_line[:100].rstrip()
                    )

            # Apply conventional commit normalization if enabled
            if inputs.normalise_commit and gh.pr_number:
                try:
                    # Get PR author for normalization context
                    client = build_client()
                    repo = get_repo_from_env(client)
                    pr_obj = get_pull(repo, int(gh.pr_number))
                    author = getattr(pr_obj, "user", {})
                    author_login = (
                        getattr(author, "login", "") if author else ""
                    )
                    title_line = normalize_commit_title(
                        title_line, author_login, self.workspace
                    )
                except Exception as e:
                    log.debug(
                        "Failed to apply commit normalization in squash "
                        "mode: %s",
                        e,
                    )

            return title_line

        def _build_clean_message_lines(message_lines: list[str]) -> list[str]:
            if not message_lines:
                return []
            title_line = _clean_title_line(message_lines[0].strip())
            out: list[str] = [title_line]
            if len(message_lines) > 1:
                body_start = 1
                while (
                    body_start < len(message_lines)
                    and not message_lines[body_start].strip()
                ):
                    body_start += 1
                if body_start < len(message_lines):
                    out.append("")
                    # Clean up ellipses from body lines
                    body_content = "\n".join(message_lines[body_start:])
                    cleaned_body_content = _clean_ellipses_from_message(
                        body_content
                    )
                    if cleaned_body_content.strip():
                        out.extend(cleaned_body_content.splitlines())
            return out

        def _maybe_reuse_change_id(pr_str: str) -> str:
            reuse = ""
            sync_all_prs = (
                os.getenv("SYNC_ALL_OPEN_PRS", "false").lower() == "true"
            )
            if (
                not sync_all_prs
                and gh.event_name in ("pull_request", "pull_request_target")
                and gh.event_action in ("reopened", "synchronize")
            ):
                try:
                    client = build_client()
                    repo = get_repo_from_env(client)
                    pr_obj = get_pull(repo, int(pr_str))
                    cand = get_recent_change_ids_from_comments(
                        pr_obj, max_comments=50
                    )
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

        def _compose_base_message(
            lines_in: list[str],
            signed_off: list[str],
        ) -> str:
            """
            Compose base message with Signed-off-by for centralized
            builder.
            """
            msg = "\n".join(lines_in).strip()
            # Add Signed-off-by to message body so centralized function
            # can parse it
            if signed_off:
                msg += "\n\n" + "\n".join(signed_off)
            return msg

        # Build message parts
        raw_lines = _collect_log_lines()
        message_lines, signed_off, _existing_cids = _parse_message_parts(
            raw_lines
        )
        clean_lines = _build_clean_message_lines(message_lines)
        pr_str = str(gh.pr_number or "").strip()
        reuse_cid = _maybe_reuse_change_id(pr_str)
        # Phase 3: if external reuse list provided, override with first
        # Change-Id
        if reuse_change_ids:
            cand = reuse_change_ids[0]
            if cand:
                reuse_cid = cand
        # Build base message with Signed-off-by
        base_msg = _compose_base_message(clean_lines, signed_off)

        # Use centralized function to build complete message with all trailers
        commit_msg = self._build_commit_message_with_trailers(
            base_message=base_msg,
            inputs=inputs,
            gh=gh,
            change_id=reuse_cid,
            preserve_existing=True,
        )

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
            msg_after = run_cmd(
                ["git", "show", "-s", "--pretty=format:%B", "HEAD"],
                cwd=self.workspace,
            ).stdout

            found = [
                m.strip()
                for m in re.findall(
                    r"(?mi)^Change-Id:\s*([A-Za-z0-9._-]+)\s*$", msg_after
                )
            ]
            if found:
                log.debug(
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
        operation_mode: str | None = None,
    ) -> None:
        """Optionally replace commit message with PR title/body.

        This function ONLY replaces the subject and body of the commit,
        preserving ALL existing trailers (Issue-ID, Change-Id, GitHub-PR,
        etc.) that were already added by _prepare_squashed_commit or
        _prepare_single_commits.

        For UPDATE operations, this will skip the override to respect
        manual commit amendments made by the user.
        """
        if not inputs.use_pr_as_commit:
            log.debug("USE_PR_AS_COMMIT disabled; skipping")
            return

        # For UPDATE operations, skip PR title/body override to respect
        # manual commit message amendments
        if operation_mode == "update":
            log.debug(
                "UPDATE operation detected; skipping PR title/body override "
                "to respect manual commit amendments"
            )
            return

        log.debug("Applying PR title/body to commit for PR #%s", gh.pr_number)
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
                title = normalize_commit_title(
                    title, author_login, self.workspace
                )

        # Get current commit message with all trailers
        current_body = git_show("HEAD", fmt="%B", cwd=self.workspace)

        # Split into message body and trailer block
        # Trailers are the lines at the end that match "Key: Value" format
        lines = current_body.split("\n")

        # Find where trailers start (working backwards from the end)
        trailer_start_idx = len(lines)
        trailer_keywords = [
            "Issue-ID:",
            "Signed-off-by:",
            "Change-Id:",
            "GitHub-PR:",
            "GitHub-Hash:",
            "Co-authored-by:",
        ]

        # Find the first trailer line from the end
        found_trailer_block = False
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            if line and any(line.startswith(kw) for kw in trailer_keywords):
                trailer_start_idx = i
                found_trailer_block = True
            elif found_trailer_block and line:
                # Found non-trailer after trailers, stop
                break

        # Extract existing trailers
        existing_trailers = []
        if found_trailer_block:
            existing_trailers = lines[trailer_start_idx:]

        # Build new message: title + body + preserved trailers
        new_message_parts = []
        if title:
            new_message_parts.append(title)
        if body:
            if title:
                new_message_parts.append(
                    ""
                )  # Blank line between title and body
            new_message_parts.append(body)

        # Add preserved trailers
        if existing_trailers:
            # Ensure blank line before trailers
            new_message_parts.append("")
            new_message_parts.extend(existing_trailers)

        commit_message = "\n".join(new_message_parts).strip()

        # Get current author
        author = run_cmd(
            ["git", "show", "-s", "--pretty=format:%an <%ae>", "HEAD"],
            cwd=self.workspace,
            env=self._ssh_env(),
        ).stdout.strip()

        # Check if Signed-off-by exists
        has_signoff = any(
            line.strip().startswith("Signed-off-by:")
            for line in existing_trailers
        )

        # Amend commit with new message, preserving trailers
        git_commit_amend(
            cwd=self.workspace,
            no_edit=False,
            signoff=not has_signoff,  # Only add signoff if not already present
            author=author,
            message=commit_message,
        )

        # Collect Change-Id trailers for later comment emission
        try:
            trailers_after = git_last_commit_trailers(
                keys=["Change-Id"], cwd=self.workspace
            )
            self._latest_apply_pr_change_ids = trailers_after.get(
                "Change-Id", []
            )
        except Exception as exc:
            log.debug(
                "Failed to collect Change-Ids after apply_pr_title: %s", exc
            )

    @staticmethod
    def _append_missing_trailers(
        message: str, trailers: list[str], *, ensure_final_newline: bool = True
    ) -> str:
        """Append missing trailers to a commit message with proper formatting.

        Args:
            message: The base commit message
            trailers: List of trailer lines to potentially append
            ensure_final_newline: Whether to ensure the message ends with a
                newline

        Returns:
            The message with missing trailers appended, properly formatted
        """
        needed = [trailer for trailer in trailers if trailer not in message]
        if not needed:
            return message

        result = message.rstrip() + "\n\n" + "\n".join(needed)

        if ensure_final_newline:
            result = result.rstrip("\n") + "\n"

        return result

    def _push_to_gerrit(
        self,
        *,
        gerrit: GerritInfo,
        repo: RepoNames,
        branch: str,
        reviewers: str,
        single_commits: bool,
        prepared: PreparedChange | None = None,
    ) -> None:
        """Push prepared commit(s) to Gerrit using git-review."""
        log.debug(
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
        topic = (
            f"{prefix}-{repo.project_github}-{pr_num}"
            if pr_num
            else f"{prefix}-{repo.project_github}"
        )

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
            log.debug("Building git review command with topic: %s", topic)
            collected_change_ids: list[str] = []
            if prepared:
                collected_change_ids.extend(prepared.all_change_ids())
            # Add any Change-Ids captured from apply_pr path (squash amend)
            extra_ids = getattr(self, "_latest_apply_pr_change_ids", [])
            for cid in extra_ids:
                if cid and cid not in collected_change_ids:
                    collected_change_ids.append(cid)
            revs = [
                r.strip()
                for r in (reviewers or "").split(",")
                if r.strip() and "@" in r and r.strip() != branch
            ]
            for r in revs:
                args.extend(["--reviewer", r])
            # Don't pass branch as positional argument to git-review
            # Instead, infer the target branch from the git configuration

            if env_bool("CI_TESTING", False):
                log.debug(
                    "CI_TESTING enabled: using synthetic orphan commit "
                    "push path"
                )
                self._create_orphan_commit_and_push(
                    gerrit, repo, branch, reviewers, topic, env
                )
                return

            log.debug("Final git review command: %s", " ".join(args))
            log.debug(
                "Git review environment variables: %s",
                {k: v for k, v in env.items() if "SSH" in k or "GIT" in k},
            )
            log.debug("Working directory: %s", self.workspace)

            # Execute the git review command
            run_cmd(args, cwd=self.workspace, env=env)
            log.debug("Successfully pushed changes to Gerrit")
        except CommandError as exc:
            # Check if this is a "no common ancestry" error in CI_TESTING mode
            if self._should_handle_unrelated_history(exc):
                log.debug(
                    "Detected unrelated repository history. Creating orphan "
                    "commit for CI testing..."
                )
                self._create_orphan_commit_and_push(
                    gerrit, repo, branch, reviewers, topic, env
                )
                return

            # Check for account not found error and try with case-normalized
            # emails
            account_not_found_emails = self._extract_account_not_found_emails(
                exc
            )
            if account_not_found_emails:
                normalized_reviewers = self._normalize_reviewer_emails(
                    reviewers, account_not_found_emails
                )
                if normalized_reviewers != reviewers:
                    log.debug(
                        "Retrying with case-normalized email addresses..."
                    )
                    try:
                        # Rebuild args with normalized reviewers
                        retry_args = args[:-1]  # Remove branch (last arg)
                        # Clear previous reviewer args and add normalized ones
                        retry_args = [
                            arg for arg in retry_args if arg != "--reviewer"
                        ]
                        retry_args = [
                            retry_args[i]
                            for i in range(len(retry_args))
                            if i == 0 or retry_args[i - 1] != "--reviewer"
                        ]

                        norm_revs = [
                            r.strip()
                            for r in (normalized_reviewers or "").split(",")
                            if r.strip() and "@" in r and r.strip() != branch
                        ]
                        for r in norm_revs:
                            retry_args.extend(["--reviewer", r])
                        retry_args.append(branch)

                        log.debug(
                            "Retrying git review command with normalized "
                            "emails: %s",
                            " ".join(retry_args),
                        )
                        run_cmd(retry_args, cwd=self.workspace, env=env)
                        log.debug(
                            "Successfully pushed changes to Gerrit with "
                            "normalized email addresses"
                        )

                        # Update configuration file with normalized email
                        # addresses
                        self._update_config_with_normalized_emails(
                            account_not_found_emails
                        )
                    except CommandError as retry_exc:
                        log.warning(
                            "Retry with normalized emails also failed: %s",
                            self._analyze_gerrit_push_failure(retry_exc),
                        )
                        # Continue with original error handling
                    else:
                        # On success, emit mapping comment before return
                        try:
                            gh_context = getattr(
                                self, "_gh_context_for_push", None
                            )
                            replace_existing = getattr(
                                self, "_inputs", None
                            ) and getattr(
                                self._inputs,
                                "persist_single_mapping_comment",
                                True,
                            )
                            self._emit_change_id_map_comment(
                                gh_context=gh_context,
                                change_ids=collected_change_ids,
                                multi=single_commits,
                                topic=topic,
                                replace_existing=bool(replace_existing),
                            )
                        except Exception as cexc:
                            log.debug(
                                "Failed to emit Change-Id map comment "
                                "(retry path): %s",
                                cexc,
                            )
                        return

            # Analyze the specific failure reason from git review output
            error_details = self._analyze_gerrit_push_failure(exc)

            # Always log the error details, even if not in verbose mode
            log.exception("Gerrit push failed: %s", error_details)

            # In debug mode, also show the raw command output
            if is_verbose_mode():
                log.debug("Git review command: %s", " ".join(exc.cmd or []))
                log.debug("Return code: %s", exc.returncode)
                if exc.stdout:
                    log.debug("Command stdout:\n%s", exc.stdout)
                if exc.stderr:
                    log.debug("Command stderr:\n%s", exc.stderr)

            # Include raw output in error message if analysis didn't provide
            # useful info
            has_raw_output = exc.stdout or exc.stderr
            if error_details.startswith("Unknown error") and has_raw_output:
                raw_output = ""
                if exc.stdout:
                    raw_output += f"stdout: {exc.stdout.strip()}\n"
                if exc.stderr:
                    raw_output += f"stderr: {exc.stderr.strip()}"
                if raw_output:
                    error_details = (
                        f"{error_details}\nRaw output:\n{raw_output}"
                    )

            msg = (
                f"Failed to push changes to Gerrit with git-review: "
                f"{error_details}"
            )
            raise OrchestratorError(msg) from exc
        # Cleanup temporary branch used during preparation
        else:
            # Successful push: emit mapping comment (Phase 2)
            try:
                gh_context = getattr(self, "_gh_context_for_push", None)
                replace_existing = getattr(self, "_inputs", None) and getattr(
                    self._inputs, "persist_single_mapping_comment", True
                )
                self._emit_change_id_map_comment(
                    gh_context=gh_context,
                    change_ids=collected_change_ids,
                    multi=single_commits,
                    topic=topic,
                    replace_existing=bool(replace_existing),
                )
            except Exception as exc_emit:
                log.debug(
                    "Failed to emit Change-Id map comment (success path): %s",
                    exc_emit,
                )
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

    def _extract_account_not_found_emails(self, exc: CommandError) -> list[str]:
        """Extract email addresses from 'Account not found' errors.

        Args:
            exc: The CommandError from git review failure

        Returns:
            List of email addresses that were not found in Gerrit
        """
        combined_output = f"{exc.stdout}\n{exc.stderr}"
        import re

        # Pattern to match: Account 'email@domain.com' not found
        pattern = r"Account\s+'([^']+)'\s+not\s+found"
        matches = re.findall(pattern, combined_output, re.IGNORECASE)

        # Filter to only include valid email addresses
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        valid_emails = [
            email for email in matches if re.match(email_pattern, email)
        ]

        if valid_emails:
            log.debug("Found 'Account not found' emails: %s", valid_emails)

        return valid_emails

    def _normalize_reviewer_emails(
        self, reviewers: str, failed_emails: list[str]
    ) -> str:
        """Normalize reviewer email addresses to lowercase.

        Args:
            reviewers: Comma-separated string of reviewer emails
            failed_emails: List of emails that failed account lookup

        Returns:
            Comma-separated string with failed emails converted to lowercase
        """
        if not reviewers or not failed_emails:
            return reviewers

        reviewer_list = [r.strip() for r in reviewers.split(",") if r.strip()]
        normalized_list = []

        for reviewer in reviewer_list:
            if reviewer in failed_emails:
                normalized = reviewer.lower()
                if normalized != reviewer:
                    log.info(
                        "Normalizing email case: %s -> %s", reviewer, normalized
                    )
                normalized_list.append(normalized)
            else:
                normalized_list.append(reviewer)

        return ",".join(normalized_list)

    def _update_config_with_normalized_emails(
        self, original_emails: list[str]
    ) -> None:
        """Update configuration file with normalized email addresses.

        Args:
            original_emails: List of original emails that were normalized
        """
        # Skip config updates in dry run mode
        from .utils import env_bool

        if env_bool("DRY_RUN"):
            log.debug("Skipping config file update in dry run mode")
            return

        try:
            # Get current organization for config lookup
            org = os.getenv("ORGANIZATION") or os.getenv(
                "GITHUB_REPOSITORY_OWNER"
            )
            if not org:
                log.debug("No organization found, skipping config file update")
                return

            config_path = os.getenv("G2G_CONFIG_PATH", "").strip()
            if not config_path:
                config_path = "~/.config/github2gerrit/configuration.txt"

            config_path_obj = Path(config_path).expanduser()
            if not config_path_obj.exists():
                log.debug(
                    "Config file does not exist, skipping update: %s",
                    config_path_obj,
                )
                return

            # Read current config content
            content = config_path_obj.read_text(encoding="utf-8")
            original_content = content

            # Look for email addresses in the content and normalize them
            for original_email in original_emails:
                normalized_email = original_email.lower()
                if normalized_email != original_email:
                    # Replace the original email with normalized version
                    # This handles both quoted and unquoted email addresses
                    patterns = [
                        f'"{original_email}"',  # Quoted
                        f"'{original_email}'",  # Single quoted
                        original_email,  # Unquoted
                    ]

                    for pattern in patterns:
                        if pattern in content:
                            replacement = pattern.replace(
                                original_email, normalized_email
                            )
                            content = content.replace(pattern, replacement)
                            log.info(
                                "Updated config file: %s -> %s",
                                pattern,
                                replacement,
                            )

            # Write back if changes were made
            if content != original_content:
                config_path_obj.write_text(content, encoding="utf-8")
                log.info(
                    "Configuration file updated with normalized email "
                    "addresses: %s",
                    config_path_obj,
                )
            else:
                log.debug(
                    "No email addresses found in config file to normalize"
                )

        except Exception as exc:
            log.warning(
                "Failed to update configuration file with normalized "
                "emails: %s",
                exc,
            )

    def _save_discovered_ssh_keys_to_config(self) -> None:
        """Save discovered SSH keys to configuration file.

        This method saves SSH keys that were discovered during host key
        auto-discovery to the configuration file for future use.
        """
        # Skip config updates in dry run mode
        from .utils import env_bool

        if env_bool("DRY_RUN"):
            log.debug("Skipping SSH key config file update in dry run mode")
            return

        # Check if we have discovered keys and organization to save
        if (
            not self._discovered_ssh_keys
            or not self._ssh_discovery_organization
        ):
            log.debug(
                "No discovered SSH keys or organization to save to config"
            )
            return

        try:
            # Get config path
            config_path = os.getenv("G2G_CONFIG_PATH", "").strip()
            if not config_path:
                config_path = "~/.config/github2gerrit/configuration.txt"

            config_path_obj = Path(config_path).expanduser()
            if not config_path_obj.exists():
                log.debug(
                    "Config file does not exist, skipping SSH key save: %s",
                    config_path_obj,
                )
                return

            # Read current config content
            content = config_path_obj.read_text(encoding="utf-8")
            original_content = content

            # Add discovered SSH keys to the configuration
            # This is a simplified implementation - in a real scenario,
            # you might want more sophisticated config file parsing
            section_header = f"[{self._ssh_discovery_organization}]"
            if section_header not in content:
                content += f"\n{section_header}\n"

            # Add the discovered keys (this is a basic implementation)
            ssh_keys_line = (
                f'GERRIT_KNOWN_HOSTS_DISCOVERED = "{self._discovered_ssh_keys}"'
            )
            if "GERRIT_KNOWN_HOSTS_DISCOVERED" not in content:
                content += f"{ssh_keys_line}\n"

            # Write back if changes were made
            if content != original_content:
                config_path_obj.write_text(content, encoding="utf-8")
                log.info(
                    "Configuration file updated with discovered SSH keys: %s",
                    config_path_obj,
                )
            else:
                log.debug("No SSH key changes needed in config file")

        except Exception as exc:
            log.warning(
                "Failed to update configuration file with discovered "
                "SSH keys: %s",
                exc,
            )

    def _should_handle_unrelated_history(self, exc: CommandError) -> bool:
        """Check if we should handle unrelated repository history in CI
        testing mode."""
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
        self,
        gerrit: GerritInfo,
        repo: RepoNames,
        branch: str,
        reviewers: str,
        topic: str,
        env: dict[str, str],
    ) -> None:
        """Create a synthetic commit on top of the remote base with the PR
        tree (CI testing mode)."""
        log.debug(
            "CI_TESTING: Creating synthetic commit on top of remote base "
            "for unrelated repository"
        )

        try:
            # Capture the current PR commit message and tree
            commit_msg = run_cmd(
                ["git", "log", "--format=%B", "-n", "1", "HEAD"],
                cwd=self.workspace,
            ).stdout.strip()
            pr_tree = run_cmd(
                ["git", "show", "--quiet", "--format=%T", "HEAD"],
                cwd=self.workspace,
            ).stdout.strip()

            # Create/update a synthetic branch based on the remote base branch
            synth_branch = f"synth-{topic}"
            # Ensure remote ref exists locally (best-effort)
            run_cmd(
                ["git", "fetch", "gerrit", branch],
                cwd=self.workspace,
                env=env,
                check=False,
            )
            run_cmd(
                [
                    "git",
                    "checkout",
                    "-B",
                    synth_branch,
                    f"remotes/gerrit/{branch}",
                ],
                cwd=self.workspace,
                env=env,
            )

            # Replace working tree contents with the PR tree
            # 1) Remove current tracked files (ignore errors if none)
            run_cmd(
                ["git", "rm", "-r", "--quiet", "."],
                cwd=self.workspace,
                env=env,
                check=False,
            )
            # 2) Clean untracked files/dirs (SSH files are now outside
            # workspace)
            run_cmd(
                ["git", "clean", "-fdx"],
                cwd=self.workspace,
                env=env,
                check=False,
            )
            # 3) Checkout the PR tree into working directory
            run_cmd(
                ["git", "checkout", pr_tree, "--", "."],
                cwd=self.workspace,
                env=env,
            )
            run_cmd(["git", "add", "-A"], cwd=self.workspace, env=env)

            # Commit synthetic change with the same message (should already
            # include Change-Id)
            import tempfile as _tempfile
            from pathlib import Path as _Path

            with _tempfile.NamedTemporaryFile(
                "w", delete=False, encoding="utf-8"
            ) as _tf:
                # Ensure Signed-off-by for current committer (uploader) is
                # present in the footer
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
                    sob_line = (
                        f"Signed-off-by: {committer_name} <{committer_email}>"
                    )
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
                run_cmd(
                    ["git", "commit", "-F", str(_tmp_msg_path)],
                    cwd=self.workspace,
                    env=env,
                )
            finally:
                from contextlib import suppress

                with suppress(Exception):
                    _tmp_msg_path.unlink(missing_ok=True)

            # Push directly to refs/for/<branch> with topic and reviewers to
            # avoid rebase behavior
            push_ref = f"refs/for/{branch}%topic={topic}"
            revs = [
                r.strip()
                for r in (reviewers or "").split(",")
                if r.strip() and "@" in r and r.strip() != branch
            ]
            for r in revs:
                push_ref += f",r={r}"
            run_cmd(
                [
                    "git",
                    "push",
                    "--no-follow-tags",
                    "gerrit",
                    f"HEAD:{push_ref}",
                ],
                cwd=self.workspace,
                env=env,
            )
            log.debug("Successfully pushed synthetic commit to Gerrit")

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

        # Remove extra whitespace and normalize line breaks for better pattern
        # matching
        normalized_output = " ".join(combined_lower.split())

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
        elif (
            "authenticity of host" in combined_lower
            and "can't be established" in combined_lower
        ):
            return (
                "SSH host key unknown. The GERRIT_KNOWN_HOSTS value does not "
                "contain the host key for the Gerrit server. "
                "The tool will attempt "
                "to auto-discover host keys on the next run, or you can "
                "manually run "
                "'ssh-keyscan -p 29418 <gerrit-host>' to get the host keys."
            )
        # Check for specific SSH key issues before general permission denied
        elif (
            "key_load_public" in combined_lower
            and "invalid format" in combined_lower
        ):
            return (
                "SSH key format is invalid. Check that the SSH private key "
                "is properly formatted."
            )
        elif "no matching host key type found" in normalized_output:
            return (
                "SSH key type not supported by server. The server may not "
                "accept this SSH key algorithm."
            )
        elif "authentication failed" in combined_lower:
            return (
                "SSH authentication failed - check SSH key, username, and "
                "server configuration"
            )
        # Check for connection timeout/refused before "could not read" check
        elif (
            "connection timed out" in combined_lower
            or "connection refused" in combined_lower
        ):
            return (
                "Connection failed - check network connectivity and Gerrit "
                "server availability"
            )
        # Check for specific SSH publickey-only authentication failures
        elif "permission denied (publickey)" in combined_lower and not any(
            auth_method in combined_lower
            for auth_method in ["gssapi", "password", "keyboard"]
        ):
            return (
                "SSH public key authentication failed. The SSH key may be "
                "invalid, not authorized for this user, or the wrong key type."
            )
        # Check for general SSH permission issues
        elif "permission denied" in combined_lower:
            return "SSH permission denied - check SSH key and user permissions"
        elif "could not read from remote repository" in combined_lower:
            return (
                "Could not read from remote repository - check SSH "
                "authentication and repository access permissions"
            )
        # Check for Gerrit-specific issues
        elif "missing issue-id" in combined_lower:
            return "Missing Issue-ID in commit message."
        elif "commit not associated to any issue" in combined_lower:
            return "Commit not associated to any issue."
        elif (
            "remote rejected" in combined_lower
            and "refs/for/" in combined_lower
        ):
            # Extract specific rejection reason from output
            # Handle multiline rejection messages by looking in normalized
            # output
            import re

            # Look for the rejection pattern in the normalized output
            rejection_match = re.search(
                r"!\s*\[remote rejected\].*?\((.*?)\)", normalized_output
            )
            if rejection_match:
                reason = rejection_match.group(1).strip()
                return f"Gerrit rejected the push: {reason}"

            # Fallback: look line by line
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
            # For unknown errors, include more context
            context_parts = []
            if exc.returncode is not None:
                context_parts.append(f"exit code {exc.returncode}")
            if exc.cmd:
                context_parts.append(f"command: {' '.join(exc.cmd)}")

            context = f" ({', '.join(context_parts)})" if context_parts else ""
            return f"Unknown error{context}: {exc}"

    def _query_gerrit_for_results(
        self,
        *,
        gerrit: GerritInfo,
        repo: RepoNames,
        change_ids: Sequence[str],
    ) -> SubmissionResult:
        """Query Gerrit for change URL/number and patchset sha via REST."""
        log.debug("Querying Gerrit for submitted change(s) via REST")

        # pygerrit2 netrc filter is already applied in execute() unless
        # verbose mode

        # Create centralized URL builder (auto-discovers base path)
        url_builder = create_gerrit_url_builder(gerrit.host)

        # Query changes using centralized REST client
        # Credential priority: CLI args > .netrc > environment variables.
        # This call uses .netrc > environment (no CLI args passed).
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
            )
            try:
                log.debug("Gerrit API base URL (discovered): %s", api_base_url)
                changes = client.get(path)
            except Exception as exc:
                log.warning(
                    "Failed to query change via REST for %s: %s", cid, exc
                )
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
                change_url = url_builder.change_url(
                    repo.project_gerrit, int(num)
                )
                urls.append(change_url)
                nums.append(num)
            if current_rev:
                shas.append(current_rev)

        return SubmissionResult(
            change_urls=urls, change_numbers=nums, commit_shas=shas
        )

    def _prepare_workspace_checkout(
        self, *, inputs: Inputs, gh: GitHubContext
    ) -> None:
        """Initialize and set up git workspace using battle-tested CLI logic."""
        from .gitutils import run_cmd
        from .ssh_common import build_git_ssh_command
        from .ssh_common import build_non_interactive_ssh_env
        from .utils import env_bool

        # Use CLI's proven checkout logic with SSH support
        repo_full = gh.repository.strip() if gh.repository else ""
        server_url = gh.server_url or "https://github.com"
        server_url = server_url.rstrip("/")
        base_ref = gh.base_ref or ""
        pr_num_str: str = str(gh.pr_number) if gh.pr_number else "0"

        # Initialize git repository (no hardcoded branch assumption)
        run_cmd(["git", "init"], cwd=self.workspace)

        # Determine repository URL and setup authentication like CLI does
        repo_https_url = f"{server_url}/{repo_full}.git"
        repo_ssh_url = f"git@{server_url.split('//')[-1]}:{repo_full}.git"

        # Check for SSH preference (matching CLI behavior)
        use_ssh = env_bool("G2G_RESPECT_USER_SSH", False)

        if use_ssh:
            repo_url = repo_ssh_url
            # Set up SSH environment for private repos
            env = {
                "GIT_SSH_COMMAND": build_git_ssh_command(),
                **build_non_interactive_ssh_env(),
            }
            log.debug("Using SSH URL for GitHub repo: %s", repo_url)
        else:
            repo_url = repo_https_url
            env = {}
            log.debug("Using HTTPS URL: %s", repo_url)

        run_cmd(
            ["git", "remote", "add", "origin", repo_url], cwd=self.workspace
        )

        # Fetch base branch and PR head with CLI's fallback logic
        fetch_success = False

        if base_ref:
            try:
                branch_ref = (
                    f"refs/heads/{base_ref}:refs/remotes/origin/{base_ref}"
                )
                run_cmd(
                    [
                        "git",
                        "fetch",
                        f"--depth={inputs.fetch_depth}",
                        "origin",
                        branch_ref,
                    ],
                    cwd=self.workspace,
                    env=env,
                )
            except Exception as exc:
                log.debug("Base branch fetch failed for %s: %s", base_ref, exc)

        if gh.pr_number:
            try:
                pr_ref = (
                    f"refs/pull/{pr_num_str}/head:"
                    f"refs/remotes/origin/pr/{pr_num_str}/head"
                )
                run_cmd(
                    [
                        "git",
                        "fetch",
                        f"--depth={inputs.fetch_depth}",
                        "origin",
                        pr_ref,
                    ],
                    cwd=self.workspace,
                    env=env,
                )
                # Checkout PR head
                run_cmd(
                    [
                        "git",
                        "checkout",
                        "-B",
                        "g2g_pr_head",
                        f"refs/remotes/origin/pr/{pr_num_str}/head",
                    ],
                    cwd=self.workspace,
                )
                fetch_success = True
            except Exception as exc:
                log.debug("PR fetch failed, will try API fallback: %s", exc)

        # Fallback to GitHub API archive if git fetch failed (CLI's resilience)
        if not fetch_success and pr_num_str and pr_num_str != "0":
            log.info("Git fetch failed, falling back to GitHub API archive")
            self._fallback_to_api_archive(self.workspace, gh, inputs)

    def _validate_and_get_api_base_url(self, server_url: str) -> str:
        """Validate server URL and return appropriate API base URL.

        Prevents SSRF attacks by validating GitHub URLs.

        Args:
            server_url: The GitHub server URL to validate

        Returns:
            Validated API base URL

        Raises:
            OrchestratorError: If URL validation fails
        """
        try:
            parsed = urllib.parse.urlparse(server_url)

            # Validate scheme
            if parsed.scheme not in ("http", "https"):
                raise OrchestratorError(f"Invalid URL scheme: {parsed.scheme}")  # noqa: TRY003, TRY301

            # Validate hostname exists
            hostname = parsed.hostname
            if not hostname:
                raise OrchestratorError("Invalid URL: missing hostname")  # noqa: TRY003, TRY301

            # Prevent access to private/local addresses with SSRF protection
            self._validate_hostname_against_ssrf(hostname)

            # Determine API base URL
            if hostname == "github.com" or hostname.endswith(".github.com"):
                return "https://api.github.com"
            else:
                # GitHub Enterprise - validate it looks like a reasonable URL
                if not parsed.netloc or "." not in parsed.netloc:
                    raise OrchestratorError(  # noqa: TRY003, TRY301
                        f"Invalid GitHub Enterprise URL: {server_url}"
                    )
                return f"{parsed.scheme}://{parsed.netloc}/api/v3"

        except Exception as e:
            if isinstance(e, OrchestratorError):
                raise
            raise OrchestratorError(f"URL validation failed: {e}") from e  # noqa: TRY003

    def _validate_hostname_against_ssrf(self, hostname: str) -> None:
        """Validate hostname against SSRF attacks with comprehensive protection.

        This implements multiple layers of protection:
        1. Allowlist known safe GitHub domains
        2. Resolve ALL IP addresses (IPv4 and IPv6)
        3. Block private, loopback, reserved, and multicast ranges
        4. Prevent DNS rebinding attacks by validating all resolved IPs

        Args:
            hostname: The hostname to validate

        Raises:
            OrchestratorError: If hostname fails SSRF validation
        """
        # Allowlist for known safe GitHub domains
        safe_github_domains = {
            "github.com",
            "api.github.com",
            "raw.githubusercontent.com",
            "objects.githubusercontent.com",
            "codeload.github.com",
        }

        # Check if hostname is in our allowlist (exact match or subdomain)
        hostname_lower = hostname.lower()
        for safe_domain in safe_github_domains:
            if hostname_lower == safe_domain or hostname_lower.endswith(
                f".{safe_domain}"
            ):
                return  # Allow known safe domains

        # For GitHub Enterprise or other domains, perform IP validation
        try:
            # Get ALL IP addresses for the hostname (both IPv4 and IPv6)
            addr_infos = socket.getaddrinfo(
                hostname,
                None,
                family=socket.AF_UNSPEC,  # Both IPv4 and IPv6
                type=socket.SOCK_STREAM,
            )

            if not addr_infos:
                msg = f"No IP addresses found for hostname: {hostname}"
                raise OrchestratorError(msg)

            # Extract and validate all unique IP addresses
            ip_addresses = set()
            for addr_info in addr_infos:
                ip_str = addr_info[4][
                    0
                ]  # Extract IP from (family, type, proto, canonname, sockaddr)
                ip_addresses.add(ip_str)

            blocked_ips = []
            for ip_str in ip_addresses:
                try:
                    ip_obj = ipaddress.ip_address(ip_str)

                    # Block private, loopback, reserved, multicast addresses
                    if (
                        ip_obj.is_private
                        or ip_obj.is_loopback
                        or ip_obj.is_reserved
                        or ip_obj.is_multicast
                        or ip_obj.is_link_local
                        or ip_obj.is_unspecified
                    ):
                        blocked_ips.append(ip_str)

                    # Additional IPv4 specific checks
                    if isinstance(ip_obj, ipaddress.IPv4Address):
                        # Block additional ranges not caught by is_private
                        if (
                            ip_obj
                            in ipaddress.IPv4Network(
                                "0.0.0.0/8"
                            )  # "This" network
                            or ip_obj
                            in ipaddress.IPv4Network(
                                "100.64.0.0/10"
                            )  # Carrier-grade NAT
                            or ip_obj
                            in ipaddress.IPv4Network(
                                "169.254.0.0/16"
                            )  # Link-local
                            or ip_obj
                            in ipaddress.IPv4Network(
                                "192.0.0.0/24"
                            )  # IETF Protocol Assignments
                            or ip_obj
                            in ipaddress.IPv4Network(
                                "192.0.2.0/24"
                            )  # Documentation
                            or ip_obj
                            in ipaddress.IPv4Network(
                                "198.18.0.0/15"
                            )  # Benchmarking
                            or ip_obj
                            in ipaddress.IPv4Network(
                                "198.51.100.0/24"
                            )  # Documentation
                            or ip_obj
                            in ipaddress.IPv4Network(
                                "203.0.113.0/24"
                            )  # Documentation
                        ):
                            blocked_ips.append(ip_str)

                    # Additional IPv6 specific checks
                    elif isinstance(ip_obj, ipaddress.IPv6Address) and (
                        ip_obj in ipaddress.IPv6Network("::1/128")  # Loopback
                        or ip_obj
                        in ipaddress.IPv6Network("fe80::/10")  # Link-local
                        or ip_obj
                        in ipaddress.IPv6Network("fc00::/7")  # Unique local
                        or ip_obj
                        in ipaddress.IPv6Network(
                            "2001:db8::/32"
                        )  # Documentation
                    ):
                        blocked_ips.append(ip_str)

                except (ipaddress.AddressValueError, ValueError):
                    # If we can't parse the IP, block it for safety
                    blocked_ips.append(ip_str)

            # If ANY IP address is blocked, reject the entire hostname
            if blocked_ips:
                all_ips = ", ".join(sorted(str(ip) for ip in ip_addresses))
                blocked_list = ", ".join(sorted(str(ip) for ip in blocked_ips))
                msg = (
                    f"Access to private/local addresses not allowed: "
                    f"{hostname} "
                    f"resolves to [{all_ips}], blocked IPs: [{blocked_list}]"
                )
                raise OrchestratorError(msg)

        except socket.gaierror:
            # If hostname doesn't resolve, it's likely invalid
            msg = f"Cannot resolve hostname: {hostname}"
            raise OrchestratorError(msg) from None

    def _fallback_to_api_archive(
        self, workspace: Path, gh: GitHubContext, inputs: Inputs
    ) -> None:
        """Fallback to GitHub API archive download (ported from CLI)."""
        import json
        import zipfile

        repo_full = gh.repository.strip() if gh.repository else ""
        pr_num_str = str(gh.pr_number) if gh.pr_number else "0"

        # Get GitHub token
        token = os.getenv("GITHUB_TOKEN", "").strip()
        if not token:
            msg = "No GITHUB_TOKEN available for API fallback"
            raise OrchestratorError(msg)

        # Determine API base URL with validation to prevent SSRF
        server_url = gh.server_url or "https://github.com"
        api_base = self._validate_and_get_api_base_url(server_url)
        # Get PR details to find head SHA
        pr_api_url = f"{api_base}/repos/{repo_full}/pulls/{pr_num_str}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        try:
            req = Request(pr_api_url, headers=headers)  # noqa: S310
            with urlopen(req, timeout=30) as response:  # noqa: S310
                pr_data = json.loads(response.read().decode())
        except Exception:
            log.exception("Failed to fetch PR data from GitHub API")
            raise

        head_sha = pr_data["head"]["sha"]

        # Download archive
        archive_url = f"{api_base}/repos/{repo_full}/zipball/{head_sha}"

        try:
            req = Request(archive_url, headers=headers)  # noqa: S310
            with urlopen(req, timeout=120) as response:  # noqa: S310
                archive_data = response.read()
        except Exception:
            log.exception("Failed to download archive from GitHub API")
            raise

        # Initialize git if needed
        if not (workspace / ".git").exists():
            run_cmd(["git", "init"], cwd=workspace)

        # Extract archive
        archive_path = workspace / "archive.zip"
        archive_path.write_bytes(archive_data)

        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            zip_ref.extractall(workspace)

        # Find extracted directory (GitHub creates repo-sha format)
        extracted_dirs = [
            d for d in workspace.iterdir() if d.is_dir() and d.name != ".git"
        ]
        if not extracted_dirs:
            msg = "No content found in GitHub archive"
            raise OrchestratorError(msg)

        source_dir = extracted_dirs[0]

        # Move contents to workspace root
        for item in source_dir.iterdir():
            if item.name != ".git":
                import shutil

                target = workspace / item.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                shutil.move(str(item), str(target))

        # Clean up
        source_dir.rmdir()
        archive_path.unlink()

        # Create expected branch
        run_cmd(["git", "checkout", "-B", "g2g_pr_head"], cwd=workspace)

        log.info("Successfully set up workspace using GitHub API archive")

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

            _MSG_HOOK_SIZE_BOUNDS = (
                "commit-msg hook size outside expected bounds"
            )
            _MSG_HOOK_READ_FAILED = "failed reading commit-msg hook"
            _MSG_HOOK_NO_SHEBANG = "commit-msg hook missing shebang"
            _MSG_HOOK_BAD_CONTENT = (
                "commit-msg hook content lacks expected markers"
            )

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
                "curl fetch of commit-msg: url=%s http_status=%s size=%dB "
                "rc=%s",
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
            if not any(
                m in text_head
                for m in ("Change-Id", "Gerrit Code Review", "add_change_id")
            ):
                _raise_orch(_MSG_HOOK_BAD_CONTENT)

            # Make hook executable (single chmod)
            hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)
            log.debug(
                "Successfully installed commit-msg hook from %s", hook_url
            )

        except Exception as exc:
            log.warning(
                "Failed to install commit-msg hook via centralized curl: %s",
                exc,
            )
            msg = f"Could not install commit-msg hook: {exc}"
            raise OrchestratorError(msg) from exc

    def _ensure_change_id_present(
        self, gerrit: GerritInfo, author: str
    ) -> list[str]:
        """Ensure the last commit has a Change-Id.

        Installs the commit-msg hook and amends the commit if needed.
        """
        trailers = git_last_commit_trailers(
            keys=["Change-Id"], cwd=self.workspace
        )
        existing_change_ids = trailers.get("Change-Id", [])

        if existing_change_ids:
            log.debug(
                "Found existing Change-Id(s) in footer: %s", existing_change_ids
            )
            # Clean up any duplicate Change-IDs in the message body
            self._clean_change_ids_from_body(author)
            return [c for c in existing_change_ids if c]

        log.debug(
            "No Change-Id found; attempting to install commit-msg hook and "
            "amend commit"
        )
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
                "Commit-msg hook installation failed, falling back to direct "
                "Change-Id injection: %s",
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

            change_id = (
                "I" + _hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]
            )

            # Clean message and ensure proper footer placement
            cleaned_msg = self._clean_commit_message_for_change_id(current_msg)
            new_msg = (
                cleaned_msg.rstrip() + "\n\n" + f"Change-Id: {change_id}\n"
            )
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
        trailers = git_last_commit_trailers(
            keys=["Change-Id"], cwd=self.workspace
        )
        return [c for c in trailers.get("Change-Id", []) if c]

    def _clean_change_ids_from_body(self, author: str) -> None:
        """Remove any Change-Id lines from the commit message body, keeping
        only footer trailers."""
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
        """Remove Change-Id lines from message body while preserving footer
        trailers."""
        lines = message.splitlines()

        # Parse proper trailers using the fixed trailer parser
        trailers = _parse_trailers(message)
        change_id_trailers = trailers.get("Change-Id", [])
        signed_off_trailers = trailers.get("Signed-off-by", [])
        other_trailers = {
            k: v
            for k, v in trailers.items()
            if k not in ["Change-Id", "Signed-off-by"]
        }

        # Find trailer section by working backwards to find continuous
        # trailer block
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
                if not (
                    k and v and not k.startswith(" ") and not k.startswith("\t")
                ):
                    # Invalid trailer format - trailers start after this
                    trailer_start = i + 1
                    break

        # Process body lines (before trailers) and remove any Change-Id
        # references
        body_lines = []
        for i in range(trailer_start):
            line = lines[i]
            # Remove any Change-Id references from body lines
            if "Change-Id:" in line:
                # If line starts with Change-Id:, skip it entirely
                if line.strip().startswith("Change-Id:"):
                    log.debug(
                        "Removing Change-Id line from body: %s", line.strip()
                    )
                    continue
                else:
                    # If Change-Id is mentioned within the line, remove that
                    # part
                    original_line = line
                    # Remove Change-Id: followed by the ID value

                    # Pattern to match "Change-Id: <value>" where value can
                    # contain word chars, hyphens, etc.
                    line = re.sub(r"Change-Id:\s*[A-Za-z0-9._-]+\b", "", line)
                    # Clean up extra whitespace
                    line = re.sub(r"\s+", " ", line).strip()
                    if line != original_line:
                        log.debug(
                            "Cleaned Change-Id reference from body line: "
                            "%s -> %s",
                            original_line.strip(),
                            line,
                        )
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
        log.debug(
            "_add_backref_comment_in_gerrit called with %d commit SHAs",
            len(commit_shas),
        )

        if not commit_shas:
            log.warning("No commit shas to comment on in Gerrit")
            return

        # Check if back-reference comments are disabled
        skip_comments_env = os.getenv("G2G_SKIP_GERRIT_COMMENTS", "")
        log.debug(
            "G2G_SKIP_GERRIT_COMMENTS environment variable: '%s'",
            skip_comments_env,
        )

        if skip_comments_env.lower() in ("true", "1", "yes"):
            log.info(
                "Skipping back-reference comments "
                "(G2G_SKIP_GERRIT_COMMENTS=true)"
            )
            return

        log.debug("Adding back-reference comment in Gerrit")
        user = os.getenv("GERRIT_SSH_USER_G2G", "")
        server = gerrit.host
        pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"
        run_url = (
            f"{gh.server_url}/{gh.repository}/actions/runs/{gh.run_id}"
            if gh.run_id
            else "N/A"
        )
        message = f"GHPR: {pr_url} | Action-Run: {run_url}"
        log.debug("Adding back-reference comment: %s", message)
        # Idempotence override: allow forcing duplicate comments (debug/testing)
        force_dup = os.getenv("G2G_FORCE_BACKREF_DUPLICATE", "").lower() in (
            "1",
            "true",
            "yes",
        )

        def _has_existing_backref(commit_sha: str) -> bool:
            if force_dup:
                return False
            try:
                from .gerrit_rest import build_client_for_host

                client = build_client_for_host(
                    gerrit.host, timeout=8.0, max_attempts=3
                )
                # Query change messages for this commit
                path = f"/changes/?q=commit:{commit_sha}&o=MESSAGES"
                data = client.get(path)
                if not isinstance(data, list):
                    return False
                for entry in data:
                    msgs = entry.get("messages") or []
                    for msg in msgs:
                        txt = (msg.get("message") or "").strip()
                        if "GHPR:" in txt and pr_url in txt:
                            log.debug(
                                "Skipping back-reference for %s "
                                "(already present)",
                                commit_sha,
                            )
                            return True
            except Exception as exc:
                log.debug(
                    "Backref idempotence check failed for %s: %s",
                    commit_sha,
                    exc,
                )
            return False

        log.debug(
            "Processing %d commit SHAs for back-reference comments",
            len(commit_shas),
        )

        for csha in commit_shas:
            log.debug("Processing commit SHA: %s", csha)
            if _has_existing_backref(csha):
                log.debug(
                    "Commit %s already has back-reference, skipping", csha
                )
                continue
            if not csha:
                log.debug("Empty commit SHA, skipping")
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
                elif (
                    self._use_ssh_agent
                    and self._ssh_agent_manager
                    and self._ssh_agent_manager.known_hosts_path
                ):
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
                log.debug(
                    "Successfully added back-reference comment for %s: %s",
                    csha,
                    message,
                )
            except CommandError as exc:
                log.warning(
                    "Failed to add back-reference comment for %s "
                    "(non-fatal): %s",
                    csha,
                    exc,
                )
                if exc.stderr:
                    log.debug("SSH stderr: %s", exc.stderr)
                if exc.stdout:
                    log.debug("SSH stdout: %s", exc.stdout)
                log.debug(
                    "SSH command that failed: %s",
                    " ".join(ssh_cmd) if "ssh_cmd" in locals() else "unknown",
                )
                log.debug(
                    "Back-reference comment failed but change was successfully "
                    "submitted. You can set G2G_SKIP_GERRIT_COMMENTS=true to "
                    "disable these comments."
                )
            except Exception as exc:
                log.warning(
                    "Failed to add back-reference comment for %s "
                    "(non-fatal): %s",
                    csha,
                    exc,
                )
                log.debug(
                    "Back-reference comment failure details:", exc_info=True
                )
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
            log.debug(
                "Source/origin pull request will NOT be updated with Gerrit "
                "change when CI_TESTING set true"
            )
            return
        log.debug("Adding reference comment on PR #%s", gh.pr_number)
        if not gh.pr_number:
            return
        urls = result.change_urls or []

        # Determine operation type for comment
        operation_mode = os.getenv("G2G_OPERATION_MODE", "unknown")
        operation_verb = "raised"
        if operation_mode == "update":
            operation_verb = "updated"
        elif operation_mode == "edit":
            operation_verb = "synchronized"

        try:
            client = build_client()
            repo = get_repo_from_env(client)
            # At this point, gh.pr_number is non-None due to earlier guard.
            pr_obj = get_pull(repo, int(gh.pr_number))
            # Post a concise one-line comment for each Gerrit change URL
            for u in urls:
                create_pr_comment(
                    pr_obj,
                    f"Change {operation_verb} in Gerrit by GitHub2Gerrit: {u}",
                )
        except Exception as exc:
            log.warning("Failed to add PR comment: %s", exc)

    def _close_pull_request_if_required(
        self,
        gh: GitHubContext,
    ) -> None:
        """Close the PR if policy requires.

        Supports both ``pull_request`` and ``pull_request_target`` triggers.
        When PRESERVE_GITHUB_PRS is true, skip closing PRs (useful for testing).
        """
        # Respect PRESERVE_GITHUB_PRS to avoid closing PRs during tests
        preserve = os.getenv("PRESERVE_GITHUB_PRS", "").strip().lower()
        if preserve in ("1", "true", "yes"):
            log.debug(
                "PRESERVE_GITHUB_PRS is enabled; skipping PR close for #%s",
                gh.pr_number,
            )
            return
        # Close PRs on pull_request or pull_request_target events.
        if gh.event_name not in ("pull_request", "pull_request_target"):
            log.debug("Event is not a pull_request event; not closing PR")
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

        log.debug("Dry-run: starting preflight checks")
        if os.getenv("G2G_DRYRUN_DISABLE_NETWORK", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            log.debug(
                "Dry-run: network checks disabled (G2G_DRYRUN_DISABLE_NETWORK)"
            )
            log.debug(
                "Dry-run targets: Gerrit project=%s branch=%s "
                "topic_prefix=GH-%s",
                repo.project_gerrit,
                self._resolve_target_branch(),
                repo.project_github,
            )
            if inputs.reviewers_email:
                log.debug(
                    "Reviewers (from inputs/config): %s", inputs.reviewers_email
                )
            elif os.getenv("REVIEWERS_EMAIL"):
                log.debug(
                    "Reviewers (from environment): %s",
                    os.getenv("REVIEWERS_EMAIL"),
                )
            return

        # DNS resolution for Gerrit host
        try:
            socket.getaddrinfo(gerrit.host, None)
            log.debug(
                "DNS resolution for Gerrit host '%s' succeeded", gerrit.host
            )
        except Exception as exc:
            msg = "DNS resolution failed"
            raise OrchestratorError(msg) from exc

        # SSH (TCP) reachability on Gerrit port
        try:
            with socket.create_connection(
                (gerrit.host, gerrit.port), timeout=5
            ):
                pass
            log.debug(
                "SSH TCP connectivity to %s:%s verified",
                gerrit.host,
                gerrit.port,
            )
        except Exception as exc:
            msg = "SSH TCP connectivity failed"
            raise OrchestratorError(msg) from exc

        # Gerrit REST reachability and optional auth check
        # Credential priority: CLI args > .netrc > environment variables.
        base_path = os.getenv("GERRIT_HTTP_BASE_PATH", "").strip().strip("/")
        self._verify_gerrit_rest(gerrit.host, base_path)

        # GitHub token and metadata checks
        try:
            client = build_client()
            repo_obj = get_repo_from_env(client)
            if gh.pr_number is not None:
                pr_obj = get_pull(repo_obj, gh.pr_number)
                log.debug(
                    "GitHub PR #%s metadata loaded successfully", gh.pr_number
                )
                try:
                    title, _ = get_pr_title_body(pr_obj)
                    log.debug("GitHub PR title: %s", title)
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
        log.debug(
            "Dry-run targets: Gerrit project=%s branch=%s topic_prefix=GH-%s",
            repo.project_gerrit,
            self._resolve_target_branch(),
            repo.project_github,
        )
        if inputs.reviewers_email:
            log.debug(
                "Reviewers (from inputs/config): %s", inputs.reviewers_email
            )
        elif os.getenv("REVIEWERS_EMAIL"):
            log.info(
                "Reviewers (from environment): %s", os.getenv("REVIEWERS_EMAIL")
            )

    def _verify_gerrit_rest(
        self,
        host: str,
        base_path: str,
    ) -> None:
        """Probe Gerrit REST endpoint with optional auth.

        Uses the centralized gerrit_rest client to ensure proper base path
        handling and consistent API interactions.

        Credential priority: CLI args > .netrc > environment variables.
        """
        from .gerrit_rest import build_client_for_host

        try:
            # Use centralized client builder for base path and credentials
            client = build_client_for_host(
                host,
                timeout=8.0,
                max_attempts=3,
            )

            # Test connectivity with appropriate endpoint
            if client.is_authenticated:
                _ = client.get("/accounts/self")
                log.debug("Gerrit REST authenticated access verified")
            else:
                _ = client.get("/dashboard/self")
                log.debug("Gerrit REST endpoint reachable (unauthenticated)")

        except Exception as exc:
            # Use centralized URL builder for consistent error reporting
            url_builder = create_gerrit_url_builder(host, base_path)
            api_url = url_builder.api_url()
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

    def _validate_committed_files(
        self, gh: GitHubContext, result: SubmissionResult
    ) -> None:
        """Validate that only expected files from the GitHub PR were
        committed to Gerrit.

        This is a safety check to ensure no tool artifacts (like SSH keys) were
        accidentally included in the Gerrit change.
        """
        if not gh.pr_number or not result.commit_shas:
            log.debug("Skipping file validation - no PR number or commit SHAs")
            return

        try:
            # Get files changed in the GitHub PR
            from .github_api import build_client
            from .github_api import get_pull
            from .github_api import get_repo_from_env

            client = build_client()
            repo = get_repo_from_env(client)
            pr_obj = get_pull(repo, int(gh.pr_number))

            # Get list of files changed in the PR
            github_files = set()
            for file in pr_obj.get_files():  # type: ignore[attr-defined]
                github_files.add(file.filename)

            log.debug(
                "GitHub PR files (%d): %s",
                len(github_files),
                sorted(github_files),
            )

            # Check files in each commit SHA that was pushed to Gerrit
            for commit_sha in result.commit_shas:
                try:
                    # Get files changed in the Gerrit commit
                    from .gitutils import run_cmd

                    files_output = run_cmd(
                        [
                            "git",
                            "show",
                            "--name-only",
                            "--pretty=format:",
                            commit_sha,
                        ],
                        cwd=self.workspace,
                    ).stdout.strip()

                    if not files_output:
                        continue

                    gerrit_files = {
                        f.strip() for f in files_output.split("\n") if f.strip()
                    }
                    log.debug(
                        "Gerrit commit %s files (%d): %s",
                        commit_sha[:8],
                        len(gerrit_files),
                        sorted(gerrit_files),
                    )

                    # Check for unexpected files
                    unexpected_files = gerrit_files - github_files
                    if unexpected_files:
                        # Filter out known safe files that might legitimately
                        # differ
                        suspicious_files = []
                        for f in unexpected_files:
                            # Skip files that are legitimately different
                            if f in [".gitreview", ".gitignore"]:
                                continue
                            # Flag SSH artifacts and other suspicious files
                            if (
                                ".ssh" in f
                                or "known_hosts" in f
                                or f.startswith("gerrit_key")
                            ):
                                suspicious_files.append(f)
                            else:
                                # Other unexpected files - log but don't error
                                log.warning(
                                    "Unexpected file in Gerrit commit: %s", f
                                )

                        if suspicious_files:
                            log.error(
                                "❌ CRITICAL: SSH artifacts detected in Gerrit "
                                "commit %s: %s",
                                commit_sha[:8],
                                suspicious_files,
                            )
                            log.error(
                                "This indicates a serious bug where tool "
                                "artifacts were committed. The Gerrit change "
                                "may need manual cleanup."
                            )
                            # Don't fail the pipeline, but log prominently for
                            # monitoring

                    # Also check if we're missing expected files
                    missing_files = github_files - gerrit_files
                    if missing_files:
                        log.warning(
                            "Files in GitHub PR but not in Gerrit commit "
                            "%s: %s",
                            commit_sha[:8],
                            sorted(missing_files),
                        )

                except Exception as commit_exc:
                    log.debug(
                        "Failed to validate files for commit %s: %s",
                        commit_sha[:8],
                        commit_exc,
                    )

        except Exception as exc:
            log.debug("File validation failed (non-critical): %s", exc)

    def _analyze_merge_failure(
        self, merge_exc: CommandError, base_sha: str, head_sha: str
    ) -> str:
        """Analyze git merge failure and provide detailed error information."""
        error_parts = []

        # Include basic command info
        if merge_exc.cmd:
            error_parts.append(f"Command: {' '.join(merge_exc.cmd)}")
        if merge_exc.returncode is not None:
            error_parts.append(f"Exit code: {merge_exc.returncode}")

        # Analyze stderr for common patterns
        stderr = merge_exc.stderr or ""
        if "conflict" in stderr.lower():
            error_parts.append("Merge conflicts detected")
        if "abort" in stderr.lower():
            error_parts.append("Merge was aborted")
        if "fatal" in stderr.lower():
            error_parts.append("Fatal git error occurred")

        # Include actual git output
        if merge_exc.stdout and merge_exc.stdout.strip():
            error_parts.append(f"Git output: {merge_exc.stdout.strip()}")
        if stderr and stderr.strip():
            error_parts.append(f"Git error: {stderr.strip()}")

        return (
            "; ".join(error_parts) if error_parts else "Unknown merge failure"
        )

    def _suggest_merge_recovery(
        self, merge_exc: CommandError, base_sha: str, head_sha: str
    ) -> str:
        """Suggest recovery actions based on merge failure analysis."""
        stderr = (merge_exc.stderr or "").lower()

        if (
            "committer identity unknown" in stderr
            or "empty ident name" in stderr
        ):
            return (
                "Git user identity not configured - this should be handled "
                "automatically by the tool. Please report this as a bug."
            )
        elif "conflict" in stderr:
            return "Check for merge conflicts in the PR files and resolve them"
        elif "fatal: refusing to merge unrelated histories" in stderr:
            return (
                "The branches have unrelated histories - check if the PR "
                "branch is based on the correct target"
            )
        elif "nothing to commit" in stderr:
            return (
                "No changes to merge - the PR may already be merged or have "
                "no differences"
            )
        elif "abort" in stderr:
            return (
                "Previous merge operation may have been interrupted - check "
                "repository state"
            )

        # Try to provide generic guidance
        try:
            # Check if commits exist between base and head
            commits_cmd = ["git", "rev-list", f"{base_sha}..{head_sha}"]
            commits_result = run_cmd(
                commits_cmd, cwd=self.workspace, check=False
            )
            if (
                commits_result.returncode == 0
                and not commits_result.stdout.strip()
            ):
                return (
                    "No commits found between base and head - PR may be empty "
                    "or already merged"
                )
        except Exception as e:
            log.debug(
                "Failed to check commit range for recovery suggestion: %s", e
            )

        return (
            "Review git repository state and ensure PR branch is properly "
            "synchronized with target"
        )

    def _debug_merge_failure_context(
        self, base_sha: str, head_sha: str
    ) -> None:
        """Provide extensive debugging context for merge failures when verbose mode is enabled."""  # noqa: E501
        log.error("=== VERBOSE MODE: Extended merge failure analysis ===")

        try:
            # Show detailed git log between base and head
            log_result = run_cmd(
                [
                    "git",
                    "log",
                    "--oneline",
                    "--graph",
                    f"{base_sha}..{head_sha}",
                ],
                cwd=self.workspace,
                check=False,
            )
            if log_result.returncode == 0:
                log.error("Commits to be merged:\n%s", log_result.stdout)
            else:
                log.error("Failed to get commit log: %s", log_result.stderr)

            # Show file differences
            diff_result = run_cmd(
                ["git", "diff", "--name-status", base_sha, head_sha],
                cwd=self.workspace,
                check=False,
            )
            if diff_result.returncode == 0:
                log.error(
                    "Files changed between base and head:\n%s",
                    diff_result.stdout,
                )

            # Show merge-base information
            merge_base_result = run_cmd(
                ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
                cwd=self.workspace,
                check=False,
            )
            if merge_base_result.returncode == 0:
                log.error(
                    "Base SHA %s is an ancestor of head SHA %s",
                    base_sha[:8],
                    head_sha[:8],
                )
            else:
                log.error(
                    "Base SHA %s is NOT an ancestor of head SHA %s",
                    base_sha[:8],
                    head_sha[:8],
                )

            # Show current repository state
            status_result = run_cmd(
                ["git", "status", "--porcelain"],
                cwd=self.workspace,
                check=False,
            )
            if status_result.stdout.strip():
                log.error(
                    "Repository has uncommitted changes:\n%s",
                    status_result.stdout,
                )

            # Show current branch and HEAD
            branch_result = run_cmd(
                ["git", "branch", "--show-current"],
                cwd=self.workspace,
                check=False,
            )
            if branch_result.returncode == 0:
                log.error("Current branch: %s", branch_result.stdout.strip())

            head_result = run_cmd(
                ["git", "rev-parse", "HEAD"], cwd=self.workspace, check=False
            )
            if head_result.returncode == 0:
                log.error("Current HEAD: %s", head_result.stdout.strip())

        except Exception:
            log.exception("Failed to gather debug context")

        log.error("=== End verbose merge failure analysis ===")

    def _ensure_git_user_identity(self, inputs: Inputs) -> None:
        """Ensure git user identity is configured for merge operations."""
        log.debug("Ensuring git user identity is configured")

        # Check if user.name and user.email are already configured
        try:
            name_result = run_cmd(
                ["git", "config", "user.name"], cwd=self.workspace, check=False
            )
            email_result = run_cmd(
                ["git", "config", "user.email"], cwd=self.workspace, check=False
            )

            if (
                name_result.returncode == 0
                and name_result.stdout.strip()
                and email_result.returncode == 0
                and email_result.stdout.strip()
            ):
                log.debug(
                    "Git user identity already configured: %s <%s>",
                    name_result.stdout.strip(),
                    email_result.stdout.strip(),
                )
                return

        except Exception as e:
            log.debug("Failed to check existing git identity: %s", e)

        # Configure git identity using Gerrit credentials
        user_name = inputs.gerrit_ssh_user_g2g or "github2gerrit-bot"
        user_email = (
            inputs.gerrit_ssh_user_g2g_email or "github2gerrit@example.com"
        )

        log.debug("Configuring git identity: %s <%s>", user_name, user_email)

        try:
            # Set local repository identity
            run_cmd(
                ["git", "config", "user.name", user_name], cwd=self.workspace
            )
            run_cmd(
                ["git", "config", "user.email", user_email], cwd=self.workspace
            )
            log.debug("Successfully configured git user identity")

        except CommandError as e:
            # Fallback to global config if local fails
            log.warning(
                "Failed to set local git identity, trying global: %s", e
            )
            try:
                run_cmd(["git", "config", "--global", "user.name", user_name])
                run_cmd(["git", "config", "--global", "user.email", user_email])
                log.debug("Successfully configured global git user identity")
            except CommandError as global_e:
                log.exception("Failed to configure git user identity")
                msg = "Cannot configure git user identity"
                raise OrchestratorError(msg) from global_e


# ---------------------
# Utility functions
# ---------------------
