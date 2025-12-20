# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Shared data models for github2gerrit.

This module exists to avoid circular imports between the CLI and the
core orchestrator by providing the common dataclasses used across both.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


__all__ = ["GitHubContext", "Inputs", "PROperationMode"]


class PROperationMode(Enum):
    """Represents the type of operation being performed on a PR."""

    CREATE = "create"  # New PR (opened event)
    UPDATE = "update"  # PR updated (synchronize event - rebase, new commits)
    EDIT = "edit"  # PR metadata edited (edited event - title/description)
    REOPEN = "reopen"  # PR reopened (reopened event)
    CLOSE = "close"  # PR closed (closed event)
    UNKNOWN = "unknown"  # Unknown or not applicable


@dataclass(frozen=True)
class Inputs:
    """
    Effective inputs used by the orchestration pipeline.

    These consolidate:
    - CLI flags
    - Environment variables
    - Configuration file values
    """

    # Primary behavior flags
    submit_single_commits: bool
    use_pr_as_commit: bool
    fetch_depth: int

    # Required SSH/Git identity inputs
    gerrit_known_hosts: str
    gerrit_ssh_privkey_g2g: str
    gerrit_ssh_user_g2g: str
    gerrit_ssh_user_g2g_email: str

    # GitHub API access
    github_token: str

    # Metadata and reviewers
    organization: str
    reviewers_email: str

    # Behavior toggles
    preserve_github_prs: bool
    dry_run: bool
    normalise_commit: bool

    # Optional (reusable workflow compatibility / overrides)
    gerrit_server: str
    gerrit_server_port: int
    gerrit_project: str
    issue_id: str
    issue_id_lookup_json: str
    allow_duplicates: bool
    ci_testing: bool
    duplicates_filter: str = "open"

    # Reconciliation configuration options
    reuse_strategy: str = "topic+comment"  # topic, comment, topic+comment, none
    similarity_subject: float = 0.7  # Subject token Jaccard threshold
    similarity_update_factor: float = (
        0.75  # Multiplier for UPDATE operations (0.0-1.0)
    )
    similarity_files: bool = False  # File signature match requirement
    allow_orphan_changes: bool = (
        False  # Keep unmatched Gerrit changes without warning
    )
    persist_single_mapping_comment: bool = (
        True  # Replace vs append mapping comments
    )
    log_reconcile_json: bool = (
        True  # Emit structured JSON reconciliation summary
    )


@dataclass(frozen=True)
class GitHubContext:
    """
    Minimal GitHub event context used by the orchestrator.

    This captures only the fields the flow depends on, regardless of
    whether the tool is triggered inside GitHub Actions or invoked
    directly with a URL (in which case many of these may be empty).
    """

    event_name: str
    event_action: str
    event_path: Path | None

    repository: str
    repository_owner: str
    server_url: str

    run_id: str
    sha: str

    base_ref: str
    head_ref: str

    def get_operation_mode(self) -> PROperationMode:
        """Determine the operation mode based on event type and action.

        Returns:
            PROperationMode enum indicating the type of operation
        """
        if self.event_name != "pull_request_target":
            return PROperationMode.UNKNOWN

        action = self.event_action.lower() if self.event_action else ""

        action_map = {
            "opened": PROperationMode.CREATE,
            "synchronize": PROperationMode.UPDATE,
            "edited": PROperationMode.EDIT,
            "reopened": PROperationMode.REOPEN,
            "closed": PROperationMode.CLOSE,
        }
        return action_map.get(action, PROperationMode.UNKNOWN)

    pr_number: int | None
