# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""``SKIP_FORKS``: a repository that takes no fork pull requests.

The approval gate lets a trusted maintainer transfer a fork pull
request. A repository that accepts no outside contribution through
GitHub has no use for that path, and every piece of it — resolving the
pull request, reading its reviews, holding the Gerrit key on a runner
for it — is surface that policy can simply remove. These tests pin
that removal in the tool, and pin that the reusable workflow still
starts the job for a fork pull request with the policy set: the
refusal is where the contributor gets an answer, and the job is how
the tool gets to give one.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from workflow_harness import REPO
from workflow_harness import evaluate as _evaluate
from workflow_harness import load_jobs
from workflow_harness import truthy as _truthy

from github2gerrit.cli import _check_fork_approval
from github2gerrit.cli import _export_bool_if_explicit
from github2gerrit.config import apply_config_to_env
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


def _ctx_with_source(source_name: str) -> Any:
    """A typer context whose one parameter came from *source_name*."""
    source = MagicMock()
    source.name = source_name
    ctx = MagicMock()
    ctx.get_parameter_source.return_value = source
    return ctx


class TestAConfigurationFileCanStateThePolicy:
    """A direct CLI run honours SKIP_FORKS from the per-org config.

    The configuration file is applied to the environment after the
    options are read, for keys not already set. Exporting the option's
    default beforehand would therefore shadow the file, and a fork
    would pass on a run that supplied neither the flag nor the
    variable.
    """

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        # The tests below write to os.environ directly, through the
        # helpers under test. delenv on an absent name records no undo,
        # so the variable is removed again explicitly on the way out.
        monkeypatch.delenv("SKIP_FORKS", raising=False)
        yield
        os.environ.pop("SKIP_FORKS", None)

    def test_the_default_is_not_exported(self) -> None:
        _export_bool_if_explicit(
            _ctx_with_source("DEFAULT"), "skip_forks", "SKIP_FORKS", False
        )
        assert "SKIP_FORKS" not in os.environ

    def test_an_explicit_flag_is_exported(self) -> None:
        _export_bool_if_explicit(
            _ctx_with_source("COMMANDLINE"), "skip_forks", "SKIP_FORKS", True
        )
        assert os.environ["SKIP_FORKS"] == "true"

    def test_an_explicit_flag_outranks_the_file(self) -> None:
        _export_bool_if_explicit(
            _ctx_with_source("COMMANDLINE"), "skip_forks", "SKIP_FORKS", False
        )
        apply_config_to_env({"SKIP_FORKS": "true"})
        assert os.environ["SKIP_FORKS"] == "false"

    def test_a_variable_already_set_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GitHub Actions passes the action input as the variable; the
        # consumer parses that string itself.
        monkeypatch.setenv("SKIP_FORKS", "false")
        _export_bool_if_explicit(
            _ctx_with_source("ENVIRONMENT"), "skip_forks", "SKIP_FORKS", False
        )
        assert os.environ["SKIP_FORKS"] == "false"

    def test_the_file_alone_refuses_a_fork(self) -> None:
        _export_bool_if_explicit(
            _ctx_with_source("DEFAULT"), "skip_forks", "SKIP_FORKS", False
        )
        apply_config_to_env({"SKIP_FORKS": "true"})
        with (
            patch("github2gerrit.cli._post_forks_skipped_notice"),
            patch("github2gerrit.cli.evaluate_fork_approval") as approval,
            patch(
                "github2gerrit.cli._is_github_actions_context",
                return_value=False,
            ),
        ):
            assert _check_fork_approval(_pr(), _ctx()) == (False, "")
        approval.assert_not_called()


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


class TestTheJobStillStartsForAFork:
    """The setting reaches the tool; it does not stop the job.

    pull_request_target on a fork head is the one trigger a
    contributor's own pull request fires, so it is the run that has to
    answer them: the author gate closes a human-authored pull request
    with a comment, and the fork policy leaves its notice. Skipping the
    job would keep the key off the runner for that run and leave the
    pull request open in silence; the tool refuses before any fetch
    and before the key is materialised instead, as on every route.
    """

    def _runs(self, jobs: dict[str, Any], context: dict[str, Any]) -> bool:
        return _truthy(_evaluate(jobs["github2gerrit"]["if"], context))

    def test_a_fork_head_runs_with_the_setting(
        self, jobs: dict[str, Any]
    ) -> None:
        assert self._runs(jobs, _target("someone/" + REPO, True))

    def test_a_deleted_fork_runs_with_the_setting(
        self, jobs: dict[str, Any]
    ) -> None:
        # GitHub reports no head repository for a deleted fork; the
        # tool treats that as untrusted and refuses it with the notice.
        assert self._runs(jobs, _target(None, True))

    def test_a_comment_re_check_still_reaches_the_tool(
        self, jobs: dict[str, Any]
    ) -> None:
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

    def test_the_condition_does_not_read_the_setting(
        self, jobs: dict[str, Any]
    ) -> None:
        # Every context runs the same with the setting on or off: the
        # decision is the tool's, where the notice can be left.
        assert "SKIP_FORKS" not in jobs["github2gerrit"]["if"]
        for context in (
            {"github.event_name": "push"},
            _target(REPO, False),
            _target("someone/" + REPO, False),
            _target(None, False),
        ):
            with_setting: dict[str, Any] = {
                **context,
                "inputs.SKIP_FORKS": True,
            }
            assert self._runs(jobs, copy.deepcopy(context))
            assert self._runs(jobs, with_setting)
