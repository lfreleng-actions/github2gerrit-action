# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Phase 2: Reconciliation plan + orphan policy integration.

Enhancements over Phase 1:
- Introduces a first-class `ReconciliationPlan` data model
- Computes deterministic digest (sha256 first 12 hex chars)
- Applies a lightweight orphan policy (comment / abandon / ignore - stub)
- Emits enriched JSON summary including digest
- Maintains backward compatibility (still returns list[str] to caller)

Current scope (Phase 2):
- Strategy handling (topic / comment / topic+comment)
- Multi-pass matching via `ReconciliationMatcher`
- Mapping reuse (legacy comment path) with extension
- Plan construction (reused / new / orphan / digest)
- Orphan policy logging (no side-effect network abandon yet)

Deferred (later phases):
- Real abandon/comment REST side-effects
- Verification phase digest comparison
- Idempotent backref retrieval via Gerrit REST
- Full pipeline state machine integration

Design notes:
- Silent fallback on missing optional inputs (robustness)
- All new helpers keep line length within 80 characters
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from github2gerrit.core import GerritInfo
from github2gerrit.gerrit_query import GerritChange
from github2gerrit.gerrit_query import query_changes_by_topic
from github2gerrit.mapping_comment import parse_mapping_comments
from github2gerrit.mapping_comment import validate_mapping_consistency
from github2gerrit.reconcile_matcher import LocalCommit
from github2gerrit.reconcile_matcher import ReconciliationMatcher


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type hints (imported lazily to avoid circulars)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - typing only
    from github2gerrit.models import GitHubContext
    from github2gerrit.models import Inputs
except Exception:  # pragma: no cover
    GitHubContext = object  # type: ignore[misc,assignment]
    Inputs = object  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def perform_reconciliation(
    inputs: Inputs,
    gh: GitHubContext,
    gerrit: GerritInfo | None,
    local_commits: list[LocalCommit],
    *,
    expected_pr_url: str | None = None,
    expected_github_hash: str | None = None,
) -> list[str]:
    """
    Build and apply a reconciliation plan (Phase 2).

    Still returns only the ordered Change-Id list for backward
    compatibility with the legacy orchestrator. A richer plan object
    is constructed internally (digest, orphan ids, classification).

    Args:
      inputs: Configuration / feature switches.
      gh: GitHub context information.
      gerrit: Gerrit connection info.
      local_commits: Ordered local commits (topological).
      expected_pr_url: Optional authoritative PR URL.
      expected_github_hash: Optional expected GitHub-Hash trailer.

    Returns:
      Ordered list of Change-Ids (plan.mapping_order).
    """
    if not local_commits:
        return []

    strategy = (
        getattr(inputs, "reuse_strategy", "topic+comment") or ""
    ).lower()
    if strategy == "none":
        log.info("Reconciliation disabled (reuse_strategy=none)")
        return []

    if expected_pr_url is None:
        expected_pr_url = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"

    log.debug(
        "Recon strategy=%s commits=%d pr=%s",
        strategy,
        len(local_commits),
        expected_pr_url,
    )

    # 1. Topic discovery
    gerrit_changes: list[GerritChange] = []
    if "topic" in strategy:
        gerrit_changes = _query_and_validate_topic_changes(
            gerrit=gerrit,
            gh=gh,
            allow_orphans=getattr(inputs, "allow_orphan_changes", False),
            expected_pr_url=expected_pr_url,
            expected_github_hash=expected_github_hash,
        )

    # 2. Comment fallback (only if topic yielded nothing)
    if "comment" in strategy and not gerrit_changes:
        mapped_ids = _attempt_comment_based_reuse(
            gh=gh,
            expected_pr_url=expected_pr_url,
            expected_github_hash=expected_github_hash,
        )
        if mapped_ids:
            ordered = _extend_or_generate(
                mapped_ids, len(local_commits), local_commits
            )
            plan = ReconciliationPlan(
                change_ids=ordered,
                reused_ids=mapped_ids[: len(local_commits)],
                new_ids=ordered[len(mapped_ids) :],
                orphan_change_ids=[],
                digest=_compute_plan_digest(ordered),
                strategy=strategy,
            )
            _maybe_emit_summary(
                plan, log_json=getattr(inputs, "log_reconcile_json", False)
            )
            return plan.change_ids

    # 3. Matcher path
    if gerrit_changes:
        matcher = ReconciliationMatcher(
            similarity_threshold=getattr(inputs, "similarity_subject", 0.7),
            allow_duplicate_subjects=True,
        )
        result = matcher.reconcile(local_commits, gerrit_changes)
        ordered = result.change_ids
        reused_ids = ordered[: result.reused_count]
        new_ids = ordered[result.reused_count :]
        orphan_ids = [c.change_id for c in result.orphaned_changes]
        plan = ReconciliationPlan(
            change_ids=ordered,
            reused_ids=reused_ids,
            new_ids=new_ids,
            orphan_change_ids=orphan_ids,
            digest=_compute_plan_digest(ordered),
            strategy=strategy,
        )
        # Apply orphan policy (with REST side-effects)
        orphan_policy = getattr(inputs, "orphan_policy", "comment")
        actions = _apply_orphan_policy(orphan_ids, orphan_policy, gerrit=gerrit)
        if actions.has_actions():
            log.info(
                "ORPHAN_ACTIONS json=%s",
                json.dumps(actions.as_dict(), separators=(",", ":")),
            )
        _maybe_emit_summary(
            plan, log_json=getattr(inputs, "log_reconcile_json", False)
        )
        return plan.change_ids

    # 4. All new path
    new_ids = [_generate_change_id(c.sha) for c in local_commits]
    plan = ReconciliationPlan(
        change_ids=new_ids,
        reused_ids=[],
        new_ids=new_ids,
        orphan_change_ids=[],
        digest=_compute_plan_digest(new_ids),
        strategy=strategy,
    )
    _maybe_emit_summary(
        plan, log_json=getattr(inputs, "log_reconcile_json", False)
    )
    return plan.change_ids


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _query_and_validate_topic_changes(
    *,
    gerrit: GerritInfo | None,
    gh: GitHubContext,
    allow_orphans: bool,
    expected_pr_url: str,
    expected_github_hash: str | None,
) -> list[GerritChange]:
    """Query and filter Gerrit changes by topic with metadata validation."""
    topic = (
        f"GH-{gh.repository_owner}-{gh.repository.split('/')[-1]}-"
        f"{gh.pr_number}"
    )
    try:
        from github2gerrit.gerrit_rest import (
            build_client_for_host,  # lazy import
        )

        if gerrit is None:
            log.debug("No Gerrit info provided, returning empty changes list")
            return []

        client = build_client_for_host(
            gerrit.host,
            timeout=8.0,
            max_attempts=3,
        )
        statuses = ["NEW", "MERGED"] if not allow_orphans else ["NEW"]
        changes = query_changes_by_topic(client, topic, statuses=statuses)
        if not changes:
            log.debug(
                "Topic query returned 0 Gerrit changes for topic=%s", topic
            )
            return []

        validated = _filter_changes_by_pr_metadata(
            changes,
            expected_pr_url=expected_pr_url,
            expected_github_hash=expected_github_hash,
        )
        log.info(
            "Validated %d/%d Gerrit changes via topic metadata match",
            len(validated),
            len(changes),
        )
    except Exception as exc:
        log.debug("Topic-based discovery failed: %s", exc)
    else:
        return validated
    return []


def _filter_changes_by_pr_metadata(
    changes: Sequence[GerritChange],
    *,
    expected_pr_url: str,
    expected_github_hash: str | None,
) -> list[GerritChange]:
    """
    Filter changes whose commit messages reference the expected PR
    (and GitHub-Hash when provided).
    """
    filtered: list[GerritChange] = []
    for ch in changes:
        msg = ch.commit_message or ""
        if expected_pr_url not in msg:
            continue
        if (
            expected_github_hash
            and f"GitHub-Hash: {expected_github_hash}" not in msg
        ):
            continue
        filtered.append(ch)
    return filtered


def _attempt_comment_based_reuse(
    *,
    gh: GitHubContext,
    expected_pr_url: str,
    expected_github_hash: str | None,
) -> list[str] | None:
    """
    Attempt to recover mapping from prior PR comment (legacy path).
    Returns ordered list of mapped Change-Ids or None.
    """
    try:
        from github2gerrit.github_api import build_client
        from github2gerrit.github_api import get_pull
        from github2gerrit.github_api import get_repo_from_env

        gh_client = build_client()
        repo = get_repo_from_env(gh_client)
        pr_obj = get_pull(repo, int(gh.pr_number or 0))
        issue = pr_obj.as_issue()
        comments = list(issue.get_comments())
        bodies = [getattr(c, "body", "") or "" for c in comments]

        mapping = parse_mapping_comments(bodies)
        if not mapping:
            return None

        if expected_github_hash is None:
            # Skip strict validation if hash is unknown (best-effort reuse).
            if mapping.pr_url != expected_pr_url:
                log.warning(
                    "Skipping mapping reuse: PR URL mismatch (%s != %s)",
                    mapping.pr_url,
                    expected_pr_url,
                )
                return None
            return mapping.change_ids

        if validate_mapping_consistency(
            mapping, expected_pr_url, expected_github_hash
        ):
            log.debug(
                "Using comment-based mapping reuse (%d Change-Ids).",
                len(mapping.change_ids),
            )
            return mapping.change_ids
        else:
            return None
    except Exception as exc:
        log.debug("Comment-based reconciliation failed: %s", exc)
        return None


def _extend_or_generate(
    existing_ids: list[str],
    total_commits: int,
    local_commits: list[LocalCommit],
) -> list[str]:
    """
    Extend an existing ordered mapping with new Change-Ids for additional
    commits not present in the prior mapping list.
    """
    result = list(existing_ids)
    for idx in range(len(existing_ids), total_commits):
        # Use commit SHA for stable seed component (still add entropy).
        result.append(_generate_change_id(local_commits[idx].sha))
    return result


def _generate_change_id(seed: str) -> str:
    """
    Generate a Gerrit-style Change-Id using a seed and time component.
    """
    content = f"{time.time()}_{seed}"
    return "I" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:40]


def _emit_summary_json(
    *,
    total_local: int,
    reused: int,
    new: int,
    orphaned: int,
    strategy: str,
    digest: str,
) -> None:
    """
    Emit a structured one-line JSON summary for downstream parsing.
    """
    summary = {
        "total_local": total_local,
        "reused": reused,
        "new": new,
        "orphaned": orphaned,
        "strategy": strategy,
        "digest": digest,
    }
    log.info(
        "RECONCILE_SUMMARY json=%s",
        json.dumps(summary),
    )


# ---------------------------------------------------------------------------
# Minimal dataclass for future expansion (placeholder for Phase 2)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReconciliationPlan:
    """
    Concrete reconciliation plan (Phase 2).

    Fields:
      change_ids: Ordered Change-Ids aligned to local commits
      reused_ids: Subset reused from existing Gerrit changes
      new_ids: Newly generated Change-Ids
      orphan_change_ids: Gerrit changes not matched by local commits
      digest: Deterministic digest of ordered Change-Ids
      strategy: Strategy string used
    """

    change_ids: list[str]
    reused_ids: list[str]
    new_ids: list[str]
    orphan_change_ids: list[str]
    digest: str
    strategy: str


@dataclass(slots=True)
class OrphanActionLog:
    """
    Captures logged orphan handling outcomes (Phase 2 stub).
    """

    abandoned: list[str]
    commented: list[str]
    ignored: list[str]

    def has_actions(self) -> bool:
        return bool(self.abandoned or self.commented)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "abandoned": self.abandoned,
            "commented": self.commented,
            "ignored": self.ignored,
        }


def _compute_plan_digest(change_ids: list[str]) -> str:
    content = "\n".join(change_ids)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _apply_orphan_policy(
    orphan_ids: list[str],
    policy: str,
    *,
    gerrit: GerritInfo | None = None,
) -> OrphanActionLog:
    """
    Apply orphan policy with REST side-effects.

    Policies:
      - abandon: invoke Gerrit REST abandon with reason
      - comment: add explanatory comment via Gerrit REST
      - ignore: no action taken
    """
    policy_lc = (policy or "comment").lower()
    abandoned: list[str] = []
    commented: list[str] = []
    ignored: list[str] = []

    if not orphan_ids:
        return OrphanActionLog(abandoned=[], commented=[], ignored=[])

    if policy_lc == "abandon":
        abandoned = _abandon_orphan_changes(orphan_ids, gerrit)
        log.info(
            "Orphan policy 'abandon' completed for %d changes", len(abandoned)
        )
    elif policy_lc == "comment":
        commented = _comment_orphan_changes(orphan_ids, gerrit)
        log.info(
            "Orphan policy 'comment' completed for %d changes", len(commented)
        )
    else:
        ignored = orphan_ids[:]
        log.info("Orphan policy 'ignore' selected - no action taken")

    return OrphanActionLog(
        abandoned=abandoned,
        commented=commented,
        ignored=ignored,
    )


def _abandon_orphan_changes(
    orphan_ids: list[str], gerrit: GerritInfo | None
) -> list[str]:
    """
    Abandon orphan changes via Gerrit REST API.

    Returns list of successfully abandoned change IDs.
    """
    if not orphan_ids or gerrit is None:
        return []

    from github2gerrit.gerrit_rest import GerritRestError
    from github2gerrit.gerrit_rest import build_client_for_host

    abandoned = []
    try:
        client = build_client_for_host(
            gerrit.host, timeout=10.0, max_attempts=3
        )

        for change_id in orphan_ids:
            try:
                abandon_message = (
                    "Abandoned due to GitHub PR update (orphaned change)"
                )
                path = f"/changes/{change_id}/abandon"
                data = {"message": abandon_message}

                client.post(path, data=data)
                abandoned.append(change_id)
                log.debug("Successfully abandoned change %s", change_id)

            except GerritRestError as exc:
                log.warning("Failed to abandon change %s: %s", change_id, exc)
            except Exception as exc:
                log.warning(
                    "Unexpected error abandoning change %s: %s", change_id, exc
                )

    except Exception as exc:
        log.warning(
            "Failed to create Gerrit REST client for abandon operations: %s",
            exc,
        )

    return abandoned


def _comment_orphan_changes(
    orphan_ids: list[str], gerrit: GerritInfo | None
) -> list[str]:
    """
    Add comments to orphan changes via Gerrit REST API.

    Returns list of successfully commented change IDs.
    """
    if not orphan_ids or gerrit is None:
        return []

    from github2gerrit.gerrit_rest import GerritRestError
    from github2gerrit.gerrit_rest import build_client_for_host

    commented = []
    try:
        client = build_client_for_host(
            gerrit.host, timeout=10.0, max_attempts=3
        )

        for change_id in orphan_ids:
            try:
                comment_message = (
                    "This change has been orphaned by a GitHub PR update. "
                    "It is no longer part of the current PR commit set."
                )
                path = f"/changes/{change_id}/revisions/current/review"
                data = {"message": comment_message}

                client.post(path, data=data)
                commented.append(change_id)
                log.debug("Successfully commented on change %s", change_id)

            except GerritRestError as exc:
                log.warning(
                    "Failed to comment on change %s: %s", change_id, exc
                )
            except Exception as exc:
                log.warning(
                    "Unexpected error commenting on change %s: %s",
                    change_id,
                    exc,
                )

    except Exception as exc:
        log.warning(
            "Failed to create Gerrit REST client for comment operations: %s",
            exc,
        )

    return commented


def _maybe_emit_summary(plan: ReconciliationPlan, *, log_json: bool) -> None:
    if not log_json:
        return
    _emit_summary_json(
        total_local=len(plan.change_ids),
        reused=len(plan.reused_ids),
        new=len(plan.new_ids),
        orphaned=len(plan.orphan_change_ids),
        strategy=plan.strategy,
        digest=plan.digest,
    )
