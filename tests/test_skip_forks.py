# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""``SKIP_FORKS``: a repository that takes no fork pull requests.

The approval gate lets a trusted maintainer transfer a fork pull
request. A repository that accepts no outside contribution through
GitHub has no use for that path, and every piece of it — resolving the
pull request, reading its reviews, holding the Gerrit key on a runner
for it — is surface that policy can simply remove. These tests pin
that removal at both places it happens: the tool, and the reusable
workflow's job condition.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from workflow_harness import REPO
from workflow_harness import evaluate as _evaluate
from workflow_harness import load_jobs
from workflow_harness import truthy as _truthy

from github2gerrit.cli import _check_fork_approval
from github2gerrit.models import GitHubContext
from github2gerrit.pr_approval import APPROVAL_MARKER
from github2gerrit.pr_approval import render_forks_skipped_comment


BASE_REPO = "opendaylight/mdsal"
FORK_REPO = "contributor/mdsal"
HEAD_SHA = "0b2abdcf7bb2fb5ed6620f214968ae2b3c5e70e6"


def _ctx(*, head_repo: str = FORK_REPO) -> GitHubContext:
    return GitHubContext(
        event_name="pull_request_target",
        event_action="opened",
        event_path=None,
        repository=BASE_REPO,
        repository_owner="opendaylight",
        server_url="https://github.com",
        run_id="1",
        sha=HEAD_SHA,
        base_ref="master",
        head_ref="topic/fix",
        pr_number=29,
        head_repo=head_repo,
    )


def _pr() -> Any:
    pr = MagicMock()
    pr.user.login = "contributor"
    pr.head.sha = HEAD_SHA
    return pr


class TestTheToolTurnsForksAway:
    """The gate's decision once SKIP_FORKS is set."""

    @pytest.fixture(autouse=True)
    def _policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SKIP_FORKS", "true")
        monkeypatch.delenv("CI_TESTING", raising=False)

    def _gate(
        self, ctx: GitHubContext, pr: Any, *, in_actions: bool = True
    ) -> tuple[tuple[bool, str], MagicMock, MagicMock]:
        with (
            patch("github2gerrit.cli.create_pr_comment") as comment,
            patch(
                "github2gerrit.cli._edit_owned_marker_comment",
                return_value=False,
            ),
            patch("github2gerrit.cli.evaluate_fork_approval") as approval,
            patch(
                "github2gerrit.cli._is_github_actions_context",
                return_value=in_actions,
            ),
        ):
            return _check_fork_approval(pr, ctx), comment, approval

    def test_a_fork_head_is_refused_before_any_review_is_read(self) -> None:
        result, comment, approval = self._gate(_ctx(), _pr())
        assert result == (False, "")
        # No review can lift it, so none is consulted.
        approval.assert_not_called()
        comment.assert_called_once()

    def test_unknown_provenance_is_refused_too(self) -> None:
        # An absent signal is not an answer, here as at the gate.
        result, _comment, approval = self._gate(_ctx(head_repo=""), _pr())
        assert result == (False, "")
        approval.assert_not_called()

    def test_a_same_repository_head_is_unaffected(self) -> None:
        result, comment, _approval = self._gate(
            _ctx(head_repo=BASE_REPO), _pr()
        )
        assert result == (True, "")
        comment.assert_not_called()

    def test_it_applies_to_a_direct_cli_run_as_well(self) -> None:
        # The approval gate exempts an operator at the keyboard; a
        # policy the operator stated does not exempt them.
        result, _comment, approval = self._gate(_ctx(), _pr(), in_actions=False)
        assert result == (False, "")
        approval.assert_not_called()

    def test_an_unresolved_pull_request_is_refused_without_a_notice(
        self,
    ) -> None:
        result, comment, _approval = self._gate(_ctx(), None)
        assert result == (False, "")
        comment.assert_not_called()

    def test_the_notice_shares_the_gates_comment(self) -> None:
        # One comment per pull request, edited in place, whichever of
        # the two wrote it first.
        body = render_forks_skipped_comment()
        assert body.startswith(APPROVAL_MARKER)
        assert "SKIP_FORKS" in body
        assert "Gerrit" in body

    def test_the_notice_is_edited_rather_than_repeated(self) -> None:
        with (
            patch("github2gerrit.cli.create_pr_comment") as comment,
            patch(
                "github2gerrit.cli._edit_owned_marker_comment",
                return_value=True,
            ) as edit,
            patch(
                "github2gerrit.cli._is_github_actions_context",
                return_value=True,
            ),
        ):
            assert _check_fork_approval(_pr(), _ctx()) == (False, "")
        edit.assert_called_once()
        comment.assert_not_called()


class TestTheDefaultIsOff:
    """Without the setting, the approval gate decides as before."""

    def test_a_fork_head_reaches_the_approval_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SKIP_FORKS", raising=False)
        status = MagicMock(approved=True, reason="approved")
        with (
            patch("github2gerrit.cli._clear_fork_approval_notice"),
            patch(
                "github2gerrit.cli.evaluate_fork_approval",
                return_value=status,
            ) as approval,
            patch(
                "github2gerrit.cli._is_github_actions_context",
                return_value=True,
            ),
        ):
            assert _check_fork_approval(_pr(), _ctx()) == (True, HEAD_SHA)
        approval.assert_called_once()


# ---------------------------------------------------------------------
# The reusable workflow
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    return load_jobs()


def _target(head_repo: str | None, skip_forks: bool) -> dict[str, Any]:
    context: dict[str, Any] = {
        "github.event_name": "pull_request_target",
        "github.event.pull_request.number": 29,
        "inputs.SKIP_FORKS": skip_forks,
    }
    if head_repo is not None:
        context["github.event.pull_request.head.repo.full_name"] = head_repo
    return context


class TestTheJobDoesNotStartForAFork:
    """With SKIP_FORKS the credentials never reach a runner for one."""

    def _runs(self, jobs: dict[str, Any], context: dict[str, Any]) -> bool:
        return _truthy(_evaluate(jobs["github2gerrit"]["if"], context))

    def test_a_fork_head_is_skipped(self, jobs: dict[str, Any]) -> None:
        assert not self._runs(jobs, _target("someone/" + REPO, True))

    def test_a_deleted_fork_is_skipped(self, jobs: dict[str, Any]) -> None:
        # GitHub reports no head repository for a deleted fork; the
        # tool would treat that as untrusted, and so does the job.
        assert not self._runs(jobs, _target(None, True))

    def test_a_same_repository_head_still_runs(
        self, jobs: dict[str, Any]
    ) -> None:
        assert self._runs(jobs, _target(REPO, True))

    def test_without_the_setting_a_fork_still_runs(
        self, jobs: dict[str, Any]
    ) -> None:
        # pull_request_target on a fork head is the trigger that
        # applies the approval gate; the default keeps it.
        assert self._runs(jobs, _target("someone/" + REPO, False))

    def test_a_comment_re_check_still_reaches_the_tool(
        self, jobs: dict[str, Any]
    ) -> None:
        # The payload names no head repository, so the job cannot
        # decide; the tool does, and leaves a notice.
        context = {
            "github.event_name": "issue_comment",
            "github.event.issue.number": 29,
            "github.event.issue.pull_request": {"url": "x"},
            "github.event.issue.state": "open",
            "github.event.comment.body": "@github2gerrit check",
            "inputs.SKIP_FORKS": True,
        }
        assert self._runs(jobs, context)

    def test_the_setting_reaches_both_jobs(self, jobs: dict[str, Any]) -> None:
        for job in ("github2gerrit", "github2gerrit-sweep"):
            step = next(
                s
                for s in jobs[job]["steps"]
                if s.get("name") == "Run github2gerrit composite action"
            )
            assert step["with"]["SKIP_FORKS"] == "${{ inputs.SKIP_FORKS }}"

    def test_nothing_else_about_the_condition_moved(
        self, jobs: dict[str, Any]
    ) -> None:
        # The clause is a pure restriction: every context that ran
        # before still runs with the setting off.
        for context in (
            {"github.event_name": "push"},
            _target(REPO, False),
            _target("someone/" + REPO, False),
        ):
            assert self._runs(jobs, copy.deepcopy(context))
