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


__all__ = [
    "RECHECK_EVENTS",
    "GitHubContext",
    "Inputs",
    "PROperationMode",
    "head_repo_is_trusted",
]


RECHECK_EVENTS: frozenset[str] = frozenset(
    {
        "issue_comment",
        "pull_request_review",
    }
)
"""Events that ask the tool to look at a pull request again.

Neither changes the pull request's code, so each maps to UPDATE: an
existing Gerrit change should gain a patchset rather than a sibling.

They exist because the fork approval gate needs a **privileged** run to
notice that a maintainer has approved.  GitHub withholds secrets from
``pull_request_review`` on a fork pull request, so the approval itself
cannot be the run that acts on it (see
:func:`github2gerrit.cli._skip_unprivileged_fork_run`).

The important property is that these are *triggers only*.  They decide
when to re-evaluate, never whether to proceed: authorisation is always
re-read from the API by
:func:`github2gerrit.pr_approval.evaluate_fork_approval`.  So which
event fired, and who fired it, grants nothing — which is what lets the
comment trigger accept a comment from anybody without weakening the
gate.

That argument holds only where a gate exists.  A same-repository pull
request never reaches one, so a re-check on it is short-circuited
entirely; see
:func:`github2gerrit.cli._recheck_has_nothing_to_unblock`.
"""


def head_repo_is_trusted(repository: str, head_repo: str) -> bool:
    """Return ``True`` only when a PR head is known to be in-repo.

    The single definition of the trust rule, shared by
    :attr:`GitHubContext.head_is_trusted` and by the configuration
    layer, which resolves provenance from the environment before any
    :class:`GitHubContext` exists.

    Unresolved provenance answers ``False``: a missing signal must never
    grant a fork the standing of a same-repository branch.
    """
    if not repository.strip() or not head_repo.strip():
        return False
    return head_repo.strip().lower() == repository.strip().lower()


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
    commit_rules_json: str
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

    # Fallback behaviour: create Gerrit change on UPDATE when none exists
    create_missing: bool = False  # --create-missing CLI flag


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

        Supports both ``pull_request`` and ``pull_request_target``
        triggers.  Using ``pull_request`` is preferred for security
        (avoids granting secrets to untrusted fork code), while
        ``pull_request_target`` is accepted for backward compatibility.

        Every event in :data:`RECHECK_EVENTS` maps to UPDATE.  None of
        them changes code, so an existing Gerrit change should gain a
        patchset rather than a sibling.  When such an event is instead
        the one that first unblocks a fork pull request, no change
        exists yet and the create-missing fallback covers it — see
        ``Orchestrator._should_create_missing``.  Choosing CREATE here
        instead would raise a duplicate for the far commoner case of a
        re-check after a push.

        Returns:
            PROperationMode enum indicating the type of operation
        """
        if self.event_name in RECHECK_EVENTS:
            return PROperationMode.UPDATE

        if self.event_name not in ("pull_request", "pull_request_target"):
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

    head_repo: str = ""
    """Full ``owner/repo`` name of the pull request head repository.

    Empty when unknown (for example outside a pull request context).
    """

    @property
    def is_fork_pr(self) -> bool:
        """Return ``True`` when the PR head lives in a different repository.

        Fork pull requests are untrusted: their tree is authored by
        someone without write access to the base repository.  Content
        read out of that tree must never influence where the resulting
        change is pushed.

        Returns ``False`` when either repository name is unknown, so an
        absent signal never *weakens* handling of a same-repo PR.  The
        caller is responsible for treating unknown provenance
        conservatively where that matters.
        """
        if not self.head_repo or not self.repository:
            return False
        return self.head_repo.strip().lower() != self.repository.strip().lower()

    @property
    def head_is_trusted(self) -> bool:
        """Return ``True`` only when the PR head is known to be in-repo.

        This is the *trust* question, deliberately distinct from
        :attr:`is_fork_pr`, which is the factual one.  Unresolved
        provenance answers ``False`` here so that a missing signal never
        grants a fork the standing of a same-repository branch.

        Content taken from the pull request tree, and any ref name the
        head supplies, must be gated on this rather than on
        ``not is_fork_pr``.
        """
        return head_repo_is_trusted(self.repository, self.head_repo)
