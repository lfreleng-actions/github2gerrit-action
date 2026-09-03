# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Maintainer approval gate for fork pull requests.

Transferring a fork pull request pushes someone else's code into Gerrit
under the tool's SSH identity, where Gerrit CI executes it. These tests
pin the conditions under which that is allowed.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.cli import _recheck_has_nothing_to_unblock
from github2gerrit.cli import _recover_pr_metadata
from github2gerrit.cli import _skip_unrequested_comment_run
from github2gerrit.models import RECHECK_EVENTS
from github2gerrit.models import GitHubContext
from github2gerrit.models import PROperationMode
from github2gerrit.pr_approval import APPROVAL_MARKER
from github2gerrit.pr_approval import ApprovalStatus
from github2gerrit.pr_approval import evaluate_fork_approval
from github2gerrit.pr_approval import render_blocked_comment
from github2gerrit.pr_approval import render_cleared_comment
from github2gerrit.pr_commands import CMD_CHECK
from github2gerrit.pr_commands import CMD_CREATE_MISSING
from github2gerrit.pr_commands import COMMAND_REGISTRY
from github2gerrit.pr_commands import find_open_command


BASE_REPO = "opendaylight/mdsal"
FORK_REPO = "contributor/mdsal"
HEAD_SHA = "0b2abdcf7bb2fb5ed6620f214968ae2b3c5e70e6"
OLD_SHA = "1111111111111111111111111111111111111111"


def _review(
    state: str,
    login: str,
    association: str = "MEMBER",
    commit_id: str = HEAD_SHA,
) -> Any:
    review = MagicMock()
    review.state = state
    review.commit_id = commit_id
    review.author_association = association
    review.user = MagicMock()
    review.user.login = login
    return review


def _pr(reviews: list[Any], author: str = "contributor") -> Any:
    pr = MagicMock()
    pr.get_reviews.return_value = reviews
    pr.user = MagicMock()
    pr.user.login = author
    pr.head = MagicMock()
    pr.head.sha = HEAD_SHA
    return pr


def _evaluate(reviews: list[Any], author: str = "contributor"):
    return evaluate_fork_approval(
        _pr(reviews, author), head_sha=HEAD_SHA, author_login=author
    )


class TestApprovalEvaluation:
    """Which reviews authorise a transfer."""

    def test_no_reviews_blocks(self) -> None:
        status = _evaluate([])
        assert status.approved is False
        assert "no maintainer has approved" in status.reason

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_trusted_approval_passes(self, association: str) -> None:
        status = _evaluate([_review("APPROVED", "maintainer", association)])
        assert status.approved is True
        assert status.approvers == ["maintainer"]

    @pytest.mark.parametrize(
        "association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"]
    )
    def test_untrusted_approval_ignored(self, association: str) -> None:
        status = _evaluate([_review("APPROVED", "outsider", association)])
        assert status.approved is False

    def test_missing_association_ignored(self) -> None:
        status = _evaluate([_review("APPROVED", "ghost", "")])
        assert status.approved is False


class TestApprovalBindsToHeadSha:
    """The approve-then-force-push bypass."""

    def test_approval_of_older_commit_does_not_count(self) -> None:
        """GitHub keeps approvals across pushes unless told otherwise."""
        status = _evaluate(
            [_review("APPROVED", "maintainer", commit_id=OLD_SHA)]
        )
        assert status.approved is False
        assert status.stale_approvers == ["maintainer"]
        assert "does not cover the current commit" in status.reason

    def test_approval_without_commit_id_does_not_count(self) -> None:
        """Absent SHA metadata cannot establish what was reviewed."""
        status = _evaluate([_review("APPROVED", "maintainer", commit_id="")])
        assert status.approved is False
        assert status.stale_approvers == ["maintainer"]

    def test_unknown_head_sha_blocks(self) -> None:
        pr = _pr([_review("APPROVED", "maintainer")])
        status = evaluate_fork_approval(
            pr, head_sha="", author_login="contributor"
        )
        assert status.approved is False

    def test_reapproval_after_push_counts(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "maintainer", commit_id=OLD_SHA),
                _review("APPROVED", "maintainer", commit_id=HEAD_SHA),
            ]
        )
        assert status.approved is True

    def test_sha_comparison_is_case_insensitive(self) -> None:
        status = _evaluate(
            [_review("APPROVED", "maintainer", commit_id=HEAD_SHA.upper())]
        )
        assert status.approved is True


class TestSelfApproval:
    """GitHub forbids it; the tool does not rely on that alone."""

    def test_author_cannot_approve_own_pr(self) -> None:
        """Org membership is weak, so the author is excluded outright."""
        status = _evaluate(
            [_review("APPROVED", "contributor", "MEMBER")],
            author="contributor",
        )
        assert status.approved is False

    def test_author_exclusion_is_case_insensitive(self) -> None:
        status = _evaluate(
            [_review("APPROVED", "Contributor", "MEMBER")],
            author="contributor",
        )
        assert status.approved is False

    def test_other_member_still_counts(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "contributor", "MEMBER"),
                _review("APPROVED", "maintainer", "MEMBER"),
            ],
            author="contributor",
        )
        assert status.approved is True
        assert status.approvers == ["maintainer"]


class TestReviewHistoryReduction:
    """The endpoint returns history, not current state."""

    def test_changes_requested_blocks(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "one"),
                _review("CHANGES_REQUESTED", "two"),
            ]
        )
        assert status.approved is False
        assert status.blockers == ["two"]

    def test_later_approval_supersedes_changes_requested(self) -> None:
        status = _evaluate(
            [
                _review("CHANGES_REQUESTED", "maintainer"),
                _review("APPROVED", "maintainer"),
            ]
        )
        assert status.approved is True

    def test_later_changes_requested_supersedes_approval(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "maintainer"),
                _review("CHANGES_REQUESTED", "maintainer"),
            ]
        )
        assert status.approved is False

    def test_trailing_comment_does_not_erase_approval(self) -> None:
        """COMMENTED expresses no position and must not displace one."""
        status = _evaluate(
            [
                _review("APPROVED", "maintainer"),
                _review("COMMENTED", "maintainer"),
            ]
        )
        assert status.approved is True

    def test_dismissed_approval_does_not_count(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "maintainer"),
                _review("DISMISSED", "maintainer"),
            ]
        )
        assert status.approved is False

    def test_pending_review_does_not_count(self) -> None:
        status = _evaluate([_review("PENDING", "maintainer")])
        assert status.approved is False


class TestEvaluationFailsClosed:
    """An unreadable review list is not an approval."""

    def test_api_failure_blocks(self) -> None:
        pr = MagicMock()
        pr.get_reviews.side_effect = RuntimeError("403")

        status = evaluate_fork_approval(
            pr, head_sha=HEAD_SHA, author_login="contributor"
        )

        assert status.approved is False
        assert "could not read" in status.reason


class TestBlockedComment:
    """What the contributor is told."""

    def test_carries_marker_for_idempotent_updates(self) -> None:
        body = render_blocked_comment(
            ApprovalStatus(approved=False, reason="no approval"),
            head_sha=HEAD_SHA,
        )
        assert body.startswith(APPROVAL_MARKER)

    def test_explains_stale_approval_distinctly(self) -> None:
        """Being told 'no approval' when you approved is confusing."""
        body = render_blocked_comment(
            ApprovalStatus(
                approved=False,
                reason="the approval from maintainer does not cover it",
                stale_approvers=["maintainer"],
            ),
            head_sha=HEAD_SHA,
        )
        assert "Re-approve" in body
        assert HEAD_SHA[:7] in body

    def test_names_the_trust_policy(self) -> None:
        body = render_blocked_comment(
            ApprovalStatus(approved=False, reason="no approval"),
            head_sha=HEAD_SHA,
        )
        assert "MEMBER" in body
        assert "cannot approve their own" in body


def _ctx(
    *,
    head_repo: str = FORK_REPO,
    event_name: str = "pull_request_target",
    pr_number: int | None = 29,
) -> GitHubContext:
    return GitHubContext(
        event_name=event_name,
        event_action="opened",
        event_path=None,
        repository=BASE_REPO,
        repository_owner="opendaylight",
        server_url="https://github.com",
        run_id="1",
        sha=HEAD_SHA,
        base_ref="master",
        head_ref="topic/fix",
        pr_number=pr_number,
        head_repo=head_repo,
    )


class TestForkApprovalGate:
    """The gate's decision at the CLI boundary."""

    def _gate(self, ctx: GitHubContext, pr: Any) -> bool:
        from github2gerrit.cli import _check_fork_approval

        with (
            patch("github2gerrit.cli._post_fork_approval_notice"),
            patch(
                "github2gerrit.cli._is_github_actions_context",
                return_value=True,
            ),
        ):
            return _check_fork_approval(pr, ctx)[0]

    def test_direct_cli_invocation_is_not_gated(self) -> None:
        """The operator running the CLI is already the authority.

        The gate exists for the unattended path, where the tool acts on
        a shared identity with nobody watching.
        """
        from github2gerrit.cli import _check_fork_approval

        with (
            patch("github2gerrit.cli._post_fork_approval_notice"),
            patch(
                "github2gerrit.cli._is_github_actions_context",
                return_value=False,
            ),
        ):
            assert _check_fork_approval(_pr([]), _ctx())[0] is True

    def test_same_repo_pr_is_never_gated(self) -> None:
        """A same-repo head branch already implies write access."""
        pr = _pr([])
        assert self._gate(_ctx(head_repo=BASE_REPO), pr) is True

    def test_fork_without_approval_blocked(self) -> None:
        assert self._gate(_ctx(), _pr([])) is False

    def test_fork_with_approval_allowed(self) -> None:
        pr = _pr([_review("APPROVED", "maintainer")])
        assert self._gate(_ctx(), pr) is True

    def test_unresolvable_pr_blocks(self) -> None:
        """Fail closed: unknown provenance is not permission."""
        assert self._gate(_ctx(), None) is False

    def test_unresolvable_pr_on_same_repo_still_passes(self) -> None:
        assert self._gate(_ctx(head_repo=BASE_REPO), None) is True

    def test_unknown_provenance_is_gated(self) -> None:
        """An absent signal is not an answer to a question of authority.

        ``is_fork_pr`` reports ``False`` when provenance is unknown
        because it states a fact. The gate uses ``head_is_trusted``
        instead, which reports ``False`` in the same case, so a pull
        request whose head could not be resolved is gated rather than
        waved through.
        """
        assert self._gate(_ctx(head_repo=""), _pr([])) is False

    def test_unknown_provenance_with_approval_passes(self) -> None:
        pr = _pr([_review("APPROVED", "maintainer")])
        assert self._gate(_ctx(head_repo=""), pr) is True


class TestApprovalNoticeOwnership:
    """The marker is not proof of authorship.

    Anyone may paste it into a comment. Ownership is established by
    attempting the edit, which the API refuses on another user's
    comment.
    """

    def _post(self, comments: list[Any]) -> tuple[bool, list[Any]]:
        from github2gerrit.cli import _post_fork_approval_notice

        issue = MagicMock()
        issue.get_comments.return_value = comments
        pr = MagicMock()
        pr.as_issue.return_value = issue

        with (
            patch("github2gerrit.cli.env_bool", return_value=False),
            patch("github2gerrit.cli.create_pr_comment") as created,
        ):
            _post_fork_approval_notice(
                pr,
                ApprovalStatus(approved=False, reason="no approval"),
                HEAD_SHA,
            )
        return created.called, comments

    def _marker_comment(self, *, editable: bool) -> Any:
        comment = MagicMock()
        comment.body = f"{APPROVAL_MARKER}\nolder text"
        if not editable:
            comment.edit.side_effect = RuntimeError("403 Forbidden")
        return comment

    def test_own_notice_is_edited_not_duplicated(self) -> None:
        own = self._marker_comment(editable=True)

        created, _ = self._post([own])

        own.edit.assert_called_once()
        assert created is False

    def test_planted_marker_does_not_suppress_the_notice(self) -> None:
        """A comment we cannot edit is not ours; post our own."""
        planted = self._marker_comment(editable=False)

        created, _ = self._post([planted])

        assert created is True

    def test_planted_marker_alongside_our_own(self) -> None:
        """Newest first, so our own notice is found and edited."""
        planted = self._marker_comment(editable=False)
        own = self._marker_comment(editable=True)

        created, _ = self._post([planted, own])

        own.edit.assert_called_once()
        assert created is False

    def test_no_prior_notice_creates_one(self) -> None:
        other = MagicMock()
        other.body = "unrelated chatter"

        created, _ = self._post([other])

        assert created is True

    def test_comment_failure_does_not_raise(self) -> None:
        """A block must never become a crash."""
        pr = MagicMock()
        pr.as_issue.side_effect = RuntimeError("boom")

        from github2gerrit.cli import _post_fork_approval_notice

        with (
            patch("github2gerrit.cli.env_bool", return_value=False),
            patch(
                "github2gerrit.cli.create_pr_comment",
                side_effect=RuntimeError("boom"),
            ),
        ):
            _post_fork_approval_notice(
                pr,
                ApprovalStatus(approved=False, reason="no approval"),
                HEAD_SHA,
            )


class TestApprovedHeadPinning:
    """Closing the window between the check and the fetch.

    ``refs/pull/<N>/head`` is mutable, so a contributor can push
    between the gate reading the head SHA and the workspace fetch
    reading the ref. The fetch compares what it got against what was
    approved.
    """

    def _enforce(self, approved: str, fetched: str) -> None:
        from github2gerrit.core import Orchestrator

        orch = Orchestrator(
            workspace=Path("/nonexistent"), approved_sha=approved
        )

        result = MagicMock()
        result.stdout = fetched

        with patch("github2gerrit.gitutils.run_cmd", return_value=result):
            orch._enforce_approved_head(MagicMock())

    def test_matching_head_passes(self) -> None:
        self._enforce(HEAD_SHA, HEAD_SHA)

    def test_comparison_is_case_insensitive(self) -> None:
        self._enforce(HEAD_SHA, HEAD_SHA.upper())

    def test_moved_head_refuses(self) -> None:
        from github2gerrit.core import OrchestratorError

        with pytest.raises(OrchestratorError, match="moved after approval"):
            self._enforce(HEAD_SHA, OLD_SHA)

    def test_no_recorded_approval_imposes_no_constraint(self) -> None:
        """Same-repo PRs and CLI runs record nothing and are unaffected."""
        self._enforce("", OLD_SHA)

    def test_archive_fallback_is_also_checked(self) -> None:
        """The archive path re-reads the PR's current head.

        It must be checked before download: unlike the git path there
        is no commit object left to compare afterwards.
        """
        from github2gerrit.core import Orchestrator
        from github2gerrit.core import OrchestratorError

        orch = Orchestrator(
            workspace=Path("/nonexistent"), approved_sha=HEAD_SHA
        )

        orch._assert_archive_sha_approved(HEAD_SHA)
        with pytest.raises(OrchestratorError, match="moved after approval"):
            orch._assert_archive_sha_approved(OLD_SHA)

    def test_archive_fallback_unconstrained_when_no_gate(self) -> None:
        from github2gerrit.core import Orchestrator

        orch = Orchestrator(workspace=Path("/nonexistent"))
        orch._assert_archive_sha_approved(OLD_SHA)


class TestApprovedShaIsPerPullRequest:
    """Bulk runs process several pull requests concurrently.

    The approved commit travels with the pull request rather than
    through shared state, so one worker cannot clear or overwrite
    another's constraint.
    """

    def _gate(self, ctx: GitHubContext, pr: Any) -> tuple[bool, str]:
        from github2gerrit.cli import _check_fork_approval

        with (
            patch("github2gerrit.cli._post_fork_approval_notice"),
            patch(
                "github2gerrit.cli._is_github_actions_context",
                return_value=True,
            ),
        ):
            return _check_fork_approval(pr, ctx)

    def test_approval_returns_the_head(self) -> None:
        pr = _pr([_review("APPROVED", "maintainer")])
        assert self._gate(_ctx(), pr) == (True, HEAD_SHA)

    def test_block_returns_no_constraint(self) -> None:
        assert self._gate(_ctx(), _pr([])) == (False, "")

    def test_trusted_head_returns_no_constraint(self) -> None:
        pr = _pr([])
        assert self._gate(_ctx(head_repo=BASE_REPO), pr) == (True, "")

    def test_nothing_is_written_to_the_environment(self) -> None:
        """Shared state is what made concurrent runs unsafe."""
        before = dict(os.environ)
        self._gate(_ctx(), _pr([_review("APPROVED", "maintainer")]))
        assert os.environ == before


class TestApprovalNoticeRetraction:
    """A lifted block must stop saying it is blocking."""

    def _clear(self, comments: list[Any]) -> None:
        from github2gerrit.cli import _clear_fork_approval_notice

        issue = MagicMock()
        issue.get_comments.return_value = comments
        pr = MagicMock()
        pr.as_issue.return_value = issue

        with patch("github2gerrit.cli.env_bool", return_value=False):
            _clear_fork_approval_notice(
                pr,
                ApprovalStatus(approved=True, reason="approved by maintainer"),
                HEAD_SHA,
            )

    def test_existing_notice_is_retracted(self) -> None:
        notice = MagicMock()
        notice.body = f"{APPROVAL_MARKER}\nAwaiting maintainer approval"

        self._clear([notice])

        notice.edit.assert_called_once()
        assert "Approved" in notice.edit.call_args[0][0]

    def test_no_notice_creates_nothing(self) -> None:
        """A PR that was never blocked has nothing to retract."""
        other = MagicMock()
        other.body = "unrelated chatter"

        with patch("github2gerrit.cli.create_pr_comment") as created:
            self._clear([other])

        assert created.called is False

    def test_planted_marker_is_not_edited(self) -> None:
        planted = MagicMock()
        planted.body = APPROVAL_MARKER
        planted.edit.side_effect = RuntimeError("403 Forbidden")

        self._clear([planted])

    def test_cleared_body_warns_that_pushes_reset_approval(self) -> None:
        body = render_cleared_comment(
            ApprovalStatus(approved=True, reason="approved by maintainer"),
            head_sha=HEAD_SHA,
        )
        assert body.startswith(APPROVAL_MARKER)
        assert "fresh approval" in body


class TestRecheckEventOperationMode:
    """A re-check must not create a sibling change."""

    @pytest.mark.parametrize("event_name", sorted(RECHECK_EVENTS))
    def test_recheck_events_map_to_update(self, event_name: str) -> None:
        ctx = _ctx(event_name=event_name)
        assert ctx.get_operation_mode() is PROperationMode.UPDATE

    def test_pull_request_events_unchanged(self) -> None:
        ctx = _ctx(event_name="pull_request_target")
        assert ctx.get_operation_mode() is PROperationMode.CREATE


class TestCommentDoorbell:
    """A comment decides *when* to look, never *whether* to proceed.

    The directive is a noise filter: without it, subscribing to
    ``issue_comment`` would run the whole pipeline on every remark.
    Authorisation is re-read from the reviews afterwards, so the
    comment's author is deliberately irrelevant.
    """

    def _ctx_with_comment(
        self, tmp_path: Path, body: str, *, event_name: str = "issue_comment"
    ) -> GitHubContext:
        payload = tmp_path / "event.json"
        payload.write_text(
            json.dumps({"comment": {"body": body}}), encoding="utf-8"
        )
        return dataclasses.replace(
            _ctx(event_name=event_name), event_path=payload
        )

    def test_directive_lets_the_run_continue(self, tmp_path: Path) -> None:
        ctx = self._ctx_with_comment(tmp_path, "@github2gerrit check")
        assert _skip_unrequested_comment_run(ctx) is False

    @pytest.mark.parametrize("alias", ["check", "recheck", "retry"])
    def test_aliases_are_accepted(self, tmp_path: Path, alias: str) -> None:
        ctx = self._ctx_with_comment(tmp_path, f"@github2gerrit {alias}")
        assert _skip_unrequested_comment_run(ctx) is False

    def test_ordinary_comment_is_skipped(self, tmp_path: Path) -> None:
        ctx = self._ctx_with_comment(tmp_path, "Looks good to me, thanks!")
        assert _skip_unrequested_comment_run(ctx) is True

    def test_bare_mention_is_not_a_directive(self, tmp_path: Path) -> None:
        ctx = self._ctx_with_comment(tmp_path, "cc @github2gerrit")
        assert _skip_unrequested_comment_run(ctx) is True

    def test_payload_without_a_comment_is_skipped(self, tmp_path: Path) -> None:
        payload = tmp_path / "event.json"
        payload.write_text(json.dumps({}), encoding="utf-8")
        ctx = dataclasses.replace(
            _ctx(event_name="issue_comment"), event_path=payload
        )
        assert _skip_unrequested_comment_run(ctx) is True

    def test_comment_on_an_ordinary_issue_is_skipped(
        self, tmp_path: Path
    ) -> None:
        # issue_comment fires for issues too. Without this the issue
        # number would be taken for a pull request number. Checked in
        # the CLI because a composite-action caller writes their own
        # `on:` block and so has no job-level guard.
        payload = tmp_path / "event.json"
        payload.write_text(
            json.dumps(
                {
                    "issue": {"number": 29},
                    "comment": {"body": "@github2gerrit check"},
                }
            ),
            encoding="utf-8",
        )
        ctx = dataclasses.replace(
            _ctx(event_name="issue_comment"), event_path=payload
        )
        assert _skip_unrequested_comment_run(ctx) is True

    def test_comment_on_a_pull_request_is_accepted(
        self, tmp_path: Path
    ) -> None:
        payload = tmp_path / "event.json"
        payload.write_text(
            json.dumps(
                {
                    "issue": {
                        "number": 29,
                        "state": "open",
                        "pull_request": {"url": "https://api/pulls/29"},
                    },
                    "comment": {"body": "@github2gerrit check"},
                }
            ),
            encoding="utf-8",
        )
        ctx = dataclasses.replace(
            _ctx(event_name="issue_comment"), event_path=payload
        )
        assert _skip_unrequested_comment_run(ctx) is False

    @pytest.mark.parametrize("state", ["closed", "CLOSED"])
    def test_comment_on_a_closed_pull_request_is_skipped(
        self, tmp_path: Path, state: str
    ) -> None:
        # A closed pull request has no gate left to lift. Continuing
        # would reach _exit_for_pr_state_error and put a failing check
        # on it, which any commenter could then do at will.
        payload = tmp_path / "event.json"
        payload.write_text(
            json.dumps(
                {
                    "issue": {
                        "number": 29,
                        "state": state,
                        "pull_request": {"url": "https://api/pulls/29"},
                    },
                    "comment": {"body": "@github2gerrit check"},
                }
            ),
            encoding="utf-8",
        )
        ctx = dataclasses.replace(
            _ctx(event_name="issue_comment"), event_path=payload
        )
        assert _skip_unrequested_comment_run(ctx) is True

    @pytest.mark.parametrize(
        "event_name",
        ["pull_request_target", "pull_request_review", "push"],
    )
    def test_other_events_are_never_filtered(
        self, tmp_path: Path, event_name: str
    ) -> None:
        # The filter exists only to stop comment runs multiplying; it
        # must not silently swallow any other trigger.
        ctx = self._ctx_with_comment(
            tmp_path, "no directive here", event_name=event_name
        )
        assert _skip_unrequested_comment_run(ctx) is False


class TestOpenCommandRefusesPrivilegedCommands:
    """The authorship bypass must stay confined to safe commands.

    ``find_open_command`` is the one sanctioned way around the trust
    filter that issue #382 introduced.  It has to refuse anything that
    grants something, or the defect returns by the back door.
    """

    def test_check_is_servable_without_an_author(self) -> None:
        match = find_open_command("@github2gerrit check", CMD_CHECK.name)
        assert match is not None

    def test_privileged_command_is_refused(self) -> None:
        assert CMD_CREATE_MISSING.requires_trust is True
        with pytest.raises(ValueError, match="requires a trusted author"):
            find_open_command(
                "@github2gerrit create missing change",
                CMD_CREATE_MISSING.name,
            )

    @pytest.mark.parametrize(
        "body",
        [
            "@github2gerrit checkout this branch",
            "@github2gerrit checker",
            "@github2gerrit check_status",
            "@github2gerrit rechecking the logs",
            "@github2gerrit retry_later",
            "@github2gerrit retryable",
        ],
    )
    def test_longer_words_do_not_ring_the_doorbell(self, body: str) -> None:
        # The matcher tolerates trailing text after a command, which
        # without a word boundary makes every word starting with the
        # command name a match. `check` is a short, common English
        # stem, and matching it starts the transfer pipeline.
        assert find_open_command(body, CMD_CHECK.name) is None

    @pytest.mark.parametrize(
        "body",
        [
            "@github2gerrit check",
            "@github2gerrit check.",
            "@github2gerrit check please, it is approved",
            "@github2gerrit recheck!",
        ],
    )
    def test_trailing_text_is_still_tolerated(self, body: str) -> None:
        assert find_open_command(body, CMD_CHECK.name) is not None

    def test_unregistered_command_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unregistered command"):
            find_open_command("@github2gerrit nope", "nope")

    def test_commands_require_trust_unless_they_opt_out(self) -> None:
        # The default must stay restrictive, so a command added later
        # is confined to trusted authors unless its author opted out.
        opted_out = [c.name for c in COMMAND_REGISTRY if not c.requires_trust]
        assert opted_out == [CMD_CHECK.name]


class TestRecheckNeedsAGateToLift:
    """A re-check on a same-repository head must do nothing.

    The comment doorbell accepts any author, on the grounds that asking
    the tool to look again grants nothing. That holds only because the
    gate answers the question, and a same-repository pull request never
    reaches the gate — so without this any commenter could drive an
    unchanged pull request through the submission pipeline at will.
    """

    @pytest.mark.parametrize("event_name", sorted(RECHECK_EVENTS))
    def test_trusted_head_has_nothing_to_unblock(self, event_name: str) -> None:
        ctx = _ctx(event_name=event_name, head_repo=BASE_REPO)
        assert _recheck_has_nothing_to_unblock(ctx) is True

    @pytest.mark.parametrize("event_name", sorted(RECHECK_EVENTS))
    def test_fork_head_proceeds(self, event_name: str) -> None:
        ctx = _ctx(event_name=event_name, head_repo=FORK_REPO)
        assert _recheck_has_nothing_to_unblock(ctx) is False

    @pytest.mark.parametrize("event_name", sorted(RECHECK_EVENTS))
    def test_unresolved_provenance_proceeds(self, event_name: str) -> None:
        # The gate applies to an unresolved head, so short-circuiting
        # here would leave it permanently unable to transfer.
        ctx = _ctx(event_name=event_name, head_repo="")
        assert _recheck_has_nothing_to_unblock(ctx) is False

    @pytest.mark.parametrize(
        "event_name", ["pull_request_target", "pull_request", "push"]
    )
    def test_other_events_are_unaffected(self, event_name: str) -> None:
        ctx = _ctx(event_name=event_name, head_repo=BASE_REPO)
        assert _recheck_has_nothing_to_unblock(ctx) is False


class TestPullRequestMetadataRecovery:
    """A second chance at metadata the payload did not carry.

    An ``issue_comment`` payload has none of it, so the first attempt
    depends on an API call that swallows its own failures. The pull
    request fetched moments later is a separate call, and one transient
    failure must not leave a same-repository pull request looking gated
    — nor leave the base ref empty for a transfer that follows.
    """

    def _pr(
        self,
        full_name: str | None,
        *,
        base_ref: str = "stable/scandium",
        head_ref: str = "topic",
        head_sha: str = HEAD_SHA,
    ) -> Any:
        pr = MagicMock()
        pr.head.repo.full_name = full_name
        pr.base.ref = base_ref
        pr.head.ref = head_ref
        pr.head.sha = head_sha
        return pr

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "PR_HEAD_REPO",
            "GITHUB_BASE_REF",
            "GITHUB_HEAD_REF",
            "GITHUB_SHA",
        ):
            monkeypatch.setenv(var, "")
        monkeypatch.setenv("GITHUB_REPOSITORY", BASE_REPO)
        monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
        monkeypatch.setenv("PR_NUMBER", "29")

    def _recover(self, head_repo: str, pr: Any) -> Any:
        ctx = _ctx(event_name="issue_comment", head_repo=head_repo)
        ctx = dataclasses.replace(ctx, base_ref="", head_ref="")
        return _recover_pr_metadata(ctx, pr)

    def test_provenance_is_recovered(self) -> None:
        recovered = self._recover("", self._pr(BASE_REPO))
        assert recovered.head_repo == BASE_REPO
        assert recovered.head_is_trusted is True
        assert _recheck_has_nothing_to_unblock(recovered) is True

    def test_refs_are_recovered_alongside_provenance(self) -> None:
        # Recovering only the head repository would answer whether to
        # proceed while leaving where to push unresolved, and an empty
        # base ref lets target resolution fall back to a default
        # branch.
        recovered = self._recover("", self._pr(FORK_REPO))
        assert recovered.base_ref == "stable/scandium"
        assert recovered.head_ref == "topic"
        assert recovered.sha == HEAD_SHA

    def test_a_fork_head_still_proceeds(self) -> None:
        recovered = self._recover("", self._pr(FORK_REPO))
        assert recovered.head_repo == FORK_REPO
        assert _recheck_has_nothing_to_unblock(recovered) is False

    def test_known_provenance_is_never_overwritten(self) -> None:
        # A head already resolved came from the event payload or an
        # earlier API call, and must not be replaced by a value taken
        # from an object the caller supplied.
        recovered = self._recover(FORK_REPO, self._pr(BASE_REPO))
        assert recovered.head_repo == FORK_REPO

    def test_a_pull_request_that_cannot_answer_changes_nothing(self) -> None:
        pr = MagicMock()
        pr.head.repo.full_name = ""
        pr.base.ref = ""
        pr.head.ref = ""
        pr.head.sha = ""
        recovered = self._recover("", pr)
        assert recovered.head_repo == ""
        assert recovered.base_ref == ""

    def test_refs_recover_without_provenance(self) -> None:
        # A deleted fork answers null for head.repo while still
        # exposing its refs. Tying the two together would leave
        # base_ref empty exactly there, so workspace setup would skip
        # the target branch and resolution could fall back to a
        # default one.
        recovered = self._recover("", self._pr(None))
        assert recovered.base_ref == "stable/scandium"
        assert recovered.head_ref == "topic"
        # Provenance stays unresolved, so the pull request stays gated.
        assert recovered.head_repo == ""
        assert recovered.head_is_trusted is False

    def test_no_pull_request_changes_nothing(self) -> None:
        assert self._recover("", None).head_repo == ""


class TestBlockedCommentWording:
    """The notice must not assert more than the tool established."""

    def test_does_not_claim_the_pr_is_from_a_fork(self) -> None:
        """Unknown provenance is gated too, and is not known to be a fork."""
        body = render_blocked_comment(
            ApprovalStatus(approved=False, reason="no approval"),
            head_sha=HEAD_SHA,
        )
        assert "comes from a fork" not in body
        assert "could not establish" in body


class TestRecheckTriggersCreateMissing:
    """First authorisation of a fork PR has no change to update."""

    def _should_create(
        self, event_name: str, head_repo: str = FORK_REPO
    ) -> bool:
        from github2gerrit.core import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        inputs = MagicMock()
        inputs.create_missing = False
        gh = _ctx(event_name=event_name, head_repo=head_repo)

        with patch("github2gerrit.core.build_client", side_effect=OSError):
            return orch._should_create_missing(inputs, gh)[0]

    @pytest.mark.parametrize("event_name", sorted(RECHECK_EVENTS))
    def test_recheck_event_authorises_create(self, event_name: str) -> None:
        # Whichever trigger notices the approval may be the first run
        # permitted to reach Gerrit, so all of them need the fallback.
        assert self._should_create(event_name) is True

    @pytest.mark.parametrize("event_name", sorted(RECHECK_EVENTS))
    def test_same_repo_head_is_not_authorised(self, event_name: str) -> None:
        # A same-repository PR was never gated, so a re-check on one
        # must not override CREATE_MISSING=false.
        assert self._should_create(event_name, head_repo=BASE_REPO) is False

    def test_reason_names_the_review(self) -> None:
        """The notice must not claim a comment or flag triggered it."""
        from github2gerrit.core import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        inputs = MagicMock()
        inputs.create_missing = False

        with patch("github2gerrit.core.build_client", side_effect=OSError):
            _ok, reason = orch._should_create_missing(
                inputs, _ctx(event_name="pull_request_review")
            )

        assert "approving review" in reason
        assert "--create-missing" not in reason

    def test_flag_reason_names_the_flag(self) -> None:
        from github2gerrit.core import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        inputs = MagicMock()
        inputs.create_missing = True

        _ok, reason = orch._should_create_missing(inputs, _ctx())

        assert "--create-missing" in reason

    def test_synchronize_event_does_not(self) -> None:
        assert self._should_create("pull_request_target") is False

    def test_same_repo_review_does_not_override_policy(self) -> None:
        """A same-repo PR was never gated, so a review is not consent.

        Otherwise any review on any pull request would quietly defeat
        ``CREATE_MISSING=false``.
        """
        assert (
            self._should_create("pull_request_review", head_repo=BASE_REPO)
            is False
        )
