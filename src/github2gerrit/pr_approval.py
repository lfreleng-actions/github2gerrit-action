# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Maintainer approval for fork pull requests.

A pull request raised from a fork is written by someone without write
access to the base repository. Transferring it to Gerrit pushes that
content under the tool's own SSH identity, where Gerrit CI will then
execute it. This module decides whether a maintainer has authorised
that.

Why a review rather than a comment directive
────────────────────────────────────────────
On a Gerrit mirror the only usable trust signal is organisation
membership (see :mod:`github2gerrit.trust`), and the pull request author
frequently holds it. Under a comment scheme they could therefore
authorise their own change. GitHub structurally forbids approving your
own pull request, so a review carries a guarantee that a comment cannot.

The evaluation is deliberately conservative:

* the approval must come from a **trusted** association,
* it must be bound to the **current head SHA**, because GitHub only
  dismisses stale approvals when branch protection says so — without
  this an approve-then-force-push would slip through,
* a later ``CHANGES_REQUESTED`` from any trusted reviewer blocks, and
* the pull request author is excluded regardless, so a mirror
  configured to accept self-review still cannot be used that way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from .approvers import describe_additional_sources
from .github_api import get_pull_request_reviews
from .trust import describe_trust_policy
from .trust import is_trusted_association


__all__ = [
    "APPROVAL_MARKER",
    "ApprovalStatus",
    "describe_approver_policy",
    "evaluate_fork_approval",
    "render_blocked_comment",
    "render_cleared_comment",
]

log = logging.getLogger("github2gerrit.pr_approval")

APPROVAL_MARKER = "<!-- github2gerrit:fork-approval v1 -->"
"""Sentinel identifying the gate's explanatory comment, so repeated
runs edit one comment rather than adding a new one each time."""

_STATE_APPROVED = "APPROVED"
_STATE_CHANGES_REQUESTED = "CHANGES_REQUESTED"
_STATE_DISMISSED = "DISMISSED"

_DECISIVE_STATES = frozenset(
    {_STATE_APPROVED, _STATE_CHANGES_REQUESTED, _STATE_DISMISSED}
)
"""States that express, revoke or withhold authorisation.

``COMMENTED`` and ``PENDING`` deliberately do not: a reviewer who
leaves remarks without approving has not changed their position, and
must not displace their own earlier approval.

``DISMISSED`` *is* included, and must be. It is how an approval is
revoked, so leaving it out would let a dismissed approval keep
authorising the transfer.
"""


@dataclass(frozen=True)
class ApprovalStatus:
    """Outcome of evaluating a fork pull request's reviews.

    Attributes:
        approved: Whether the transfer is authorised.
        reason: Short human-readable explanation, used in logs and in
            the pull request comment.
        approvers: Logins whose current approval covers the head SHA.
        stale_approvers: Logins who approved an earlier commit. Kept
            separate so the comment can tell someone their approval
            went stale rather than implying they never gave one.
        blockers: Trusted logins currently requesting changes.
    """

    approved: bool
    reason: str
    approvers: list[str] = field(default_factory=list)
    stale_approvers: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


def _latest_decisive_reviews(
    reviews: list[Any],
    *,
    exclude_login: str,
) -> dict[str, Any]:
    """Reduce a review history to each author's current position.

    The reviews endpoint returns history oldest-first, so later entries
    overwrite earlier ones.  Only decisive states participate, so a
    trailing ``COMMENTED`` does not erase an approval.
    """
    latest: dict[str, Any] = {}
    excluded = exclude_login.strip().lower()

    for review in reviews:
        state = str(getattr(review, "state", "") or "").strip().upper()
        if state not in _DECISIVE_STATES:
            continue

        login = str(
            getattr(getattr(review, "user", None), "login", "") or ""
        ).strip()
        if not login:
            continue

        if excluded and login.lower() == excluded:
            # GitHub rejects self-approval, but the guarantee is worth
            # holding locally too: it is the only structural check
            # backing an otherwise weak trust signal.
            log.debug("Ignoring self-review by PR author %s", login)
            continue

        latest[login] = review

    return latest


def evaluate_fork_approval(
    pr: Any,
    *,
    head_sha: str,
    author_login: str = "",
    extra_approvers: frozenset[str] = frozenset(),
) -> ApprovalStatus:
    """Decide whether a fork pull request may transfer to Gerrit.

    Args:
        pr: Pull request object.
        head_sha: Current head commit of the pull request. Approvals
            recorded against any other commit do not count.
        author_login: Pull request author, excluded from reviewing.
        extra_approvers: Lower-cased logins admitted by an opt-in
            source (see :mod:`github2gerrit.approvers`). These widen
            *who counts as a maintainer*, and nothing else: the head
            binding, the ``CHANGES_REQUESTED`` block and the author
            exclusion all still apply.

    Returns:
        An :class:`ApprovalStatus`. Any failure to read reviews yields
        an unapproved result rather than an exception, so the gate
        fails closed.
    """
    try:
        reviews = get_pull_request_reviews(pr)
    except Exception as exc:
        log.warning("Could not read reviews; treating as unapproved: %s", exc)
        return ApprovalStatus(
            approved=False,
            reason="the tool could not read this pull request's reviews",
        )

    latest = _latest_decisive_reviews(reviews, exclude_login=author_login)

    approvers: list[str] = []
    stale_approvers: list[str] = []
    blockers: list[str] = []
    target = head_sha.strip().lower()

    for login, review in sorted(latest.items()):
        association = str(getattr(review, "author_association", "") or "")
        named = login.strip().lower() in extra_approvers
        if not (named or is_trusted_association(association)):
            log.debug(
                "Ignoring review by %s (%s): not a trusted association, "
                "and not named by any configured approver source",
                login,
                association or "unknown",
            )
            continue

        state = str(getattr(review, "state", "") or "").strip().upper()
        if state == _STATE_CHANGES_REQUESTED:
            blockers.append(login)
            continue
        if state != _STATE_APPROVED:
            # DISMISSED, or anything unrecognised. Only an explicit
            # approval authorises; everything else withholds.
            continue

        commit_id = str(getattr(review, "commit_id", "") or "").strip().lower()
        if not target or not commit_id or commit_id != target:
            # The guarantee is that the approval covers exactly the
            # commit about to be transferred. Absent SHA metadata
            # cannot establish that, so it withholds approval rather
            # than being waved through.
            stale_approvers.append(login)
            continue

        approvers.append(login)

    if blockers:
        return ApprovalStatus(
            approved=False,
            reason=(
                "a maintainer has requested changes: " + ", ".join(blockers)
            ),
            approvers=approvers,
            stale_approvers=stale_approvers,
            blockers=blockers,
        )

    if approvers:
        return ApprovalStatus(
            approved=True,
            reason="approved by " + ", ".join(approvers),
            approvers=approvers,
            stale_approvers=stale_approvers,
        )

    if stale_approvers:
        return ApprovalStatus(
            approved=False,
            reason=(
                "the approval from "
                + ", ".join(stale_approvers)
                + " does not cover the current commit"
            ),
            stale_approvers=stale_approvers,
        )

    return ApprovalStatus(
        approved=False,
        reason="no maintainer has approved this pull request",
    )


def render_cleared_comment(
    status: ApprovalStatus,
    *,
    head_sha: str,
) -> str:
    """Build the replacement for a notice whose block has lifted.

    Posted by editing the earlier notice rather than adding a second
    comment, so the pull request does not keep telling a contributor
    they are waiting for something that already happened.
    """
    short_sha = head_sha[:7] if head_sha else "unknown"

    return "\n".join(
        [
            APPROVAL_MARKER,
            "### Approved",
            "",
            f"This pull request is {status.reason}, for commit "
            f"`{short_sha}`, and transfers to Gerrit.",
            "",
            "Pushing further commits requires a fresh approval, because "
            "an approval covers the commit it was given for.",
        ]
    )


def render_blocked_comment(
    status: ApprovalStatus,
    *,
    head_sha: str,
) -> str:
    """Build the explanatory comment posted when the gate blocks."""
    short_sha = head_sha[:7] if head_sha else "unknown"

    lines = [
        APPROVAL_MARKER,
        "### Awaiting maintainer approval",
        "",
        "This pull request does not transfer to Gerrit until a maintainer "
        "approves it. That applies to pull requests raised from a fork, "
        "and to any whose origin the tool could not establish.",
        "",
        f"**Status:** {status.reason}.",
        "",
    ]

    if status.stale_approvers:
        lines += [
            "An earlier approval exists but covers a different commit. "
            "GitHub keeps approvals across pushes unless branch protection "
            "dismisses them, so this tool checks the approval against the "
            f"commit it will transfer (`{short_sha}`). Re-approve to "
            "refresh it.",
            "",
        ]

    lines += [
        "**To proceed:** submit an approving review, then comment "
        "`@github2gerrit check` to transfer the change. Anyone may post "
        "that comment; it grants nothing on its own, because the tool "
        "re-reads the reviews to decide. The review alone cannot do it: "
        "GitHub withholds this repository's credentials from runs "
        "triggered by a review on a pull request from a fork.",
        "",
        f"Reviews count from: {describe_approver_policy()}. The pull "
        "request author cannot approve their own pull request.",
    ]

    return "\n".join(lines)


def describe_approver_policy() -> str:
    """Describe who may approve, including any opt-in source.

    Shared by the rendered notice and the blocking log line, so a
    project that has widened its approver set is not told in either
    place that only associations count.

    The extra clause appears only when a project enabled a source, so
    nobody is told about machinery that is not in use.
    """
    policy = describe_trust_policy()
    extra = describe_additional_sources()
    return f"{policy}, and {extra}" if extra else policy
