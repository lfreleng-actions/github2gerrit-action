# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Bulk dispatch fans out to one leg per pull request (#422).

A bulk ``workflow_dispatch`` used to process every open pull request in
one job, in the group ``g2g-<repo>-workflow_dispatch``, while events for
those pull requests ran in ``g2g-<repo>-<number>``. GitHub does not
serialise different groups, so both could find no Gerrit change and
create one each.

The reusable workflow now lists the open pull requests and runs a leg
per pull request in that pull request's own group. These tests hold it
to that by evaluating the workflow's own expressions, not by matching
strings, and run the enumeration script straight out of the workflow.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import typer
from workflow_harness import REPO
from workflow_harness import Page
from workflow_harness import evaluate as _evaluate
from workflow_harness import load_jobs
from workflow_harness import matrix_of
from workflow_harness import render as _render
from workflow_harness import request_of
from workflow_harness import run_enumeration
from workflow_harness import truthy as _truthy

from github2gerrit.cli import _check_automation_only
from github2gerrit.cli import _check_single_pr_duplicates
from github2gerrit.cli import _handle_bulk_mode
from github2gerrit.cli import _handle_single_pr
from github2gerrit.cli import _stop_for_pr_state
from github2gerrit.core import SubmissionResult
from github2gerrit.duplicate_detection import DuplicateChangeError
from github2gerrit.models import GitHubContext
from github2gerrit.models import PROperationMode
from github2gerrit.pr_approval import render_transferred_comment


HEAD_SHA = "0b2abdcf7bb2fb5ed6620f214968ae2b3c5e70e6"
ACTION_STEP = "Run github2gerrit composite action"
LEG_ONLY = {
    ("with", "PR_NUMBER"),
    ("env", "G2G_SWEEP_LEG"),
}
"""The action-step keys that make a sweep leg, and all that may differ."""


class TestTheEvaluatorItself:
    """Enough confidence in the evaluator for the tests built on it."""

    def test_or_returns_the_first_truthy_operand(self) -> None:
        assert _evaluate("a || b || 'c'", {"a": 0, "b": ""}) == "c"
        assert _evaluate("a || b", {"a": 29, "b": "x"}) == 29

    def test_and_returns_the_deciding_operand(self) -> None:
        assert _evaluate("a && 'x'", {"a": "y"}) == "x"
        assert _evaluate("a && 'x'", {"a": ""}) == ""

    def test_string_comparison_ignores_case(self) -> None:
        assert _evaluate("a == 'TRUE'", {"a": "true"}) is True

    def test_missing_names_are_null(self) -> None:
        assert _evaluate("a.b != ''", {}) is False

    def test_functions(self) -> None:
        assert _evaluate("contains(a, 'check')", {"a": "@x CHECK"}) is True
        assert _evaluate("fromJSON(a)", {"a": '{"pr": [1]}'}) == {"pr": [1]}


# ---------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    return load_jobs()


def _dispatch(pr_number: str, change_url: str = "") -> dict[str, Any]:
    return {
        "github.event_name": "workflow_dispatch",
        "inputs.PR_NUMBER": pr_number,
        "inputs.GERRIT_CHANGE_URL": change_url,
    }


_DISPATCHES = [
    pytest.param(_dispatch("0"), True, id="bulk"),
    pytest.param(_dispatch(""), True, id="bulk-unset"),
    pytest.param(_dispatch("29"), False, id="single"),
    pytest.param(
        _dispatch("0", "https://gerrit.example.org/c/p/+/1"),
        False,
        id="gerrit-event",
    ),
    pytest.param(
        _dispatch("29", "https://gerrit.example.org/c/p/+/1"),
        False,
        id="gerrit-event-with-pr",
    ),
]


class TestEveryDispatchIsHandledOnce:
    """A dispatch runs the single job or the sweep, never both or neither."""

    @pytest.mark.parametrize(("context", "bulk"), _DISPATCHES)
    def test_bulk_dispatches_fan_out_and_nothing_else_does(
        self, jobs: dict[str, Any], context: dict[str, Any], bulk: bool
    ) -> None:
        enumerate_runs = _truthy(_evaluate(jobs["enumerate"]["if"], context))
        single_runs = _truthy(_evaluate(jobs["github2gerrit"]["if"], context))
        assert enumerate_runs is bulk
        assert single_runs is not bulk

    @pytest.mark.parametrize(
        "context",
        [
            {
                "github.event_name": "pull_request_target",
                "github.event.pull_request.number": 29,
                "github.event.pull_request.head.repo.full_name": REPO,
            },
            {"github.event_name": "push"},
        ],
        ids=["pull_request_target", "push"],
    )
    def test_events_never_start_a_sweep(
        self, jobs: dict[str, Any], context: dict[str, Any]
    ) -> None:
        assert not _truthy(_evaluate(jobs["enumerate"]["if"], context))
        assert _truthy(_evaluate(jobs["github2gerrit"]["if"], context))

    def test_the_sweep_needs_the_enumeration(
        self, jobs: dict[str, Any]
    ) -> None:
        sweep = jobs["github2gerrit-sweep"]
        assert sweep["needs"] == "enumerate"
        # An empty matrix is the disabled case: start no legs at all.
        assert not _truthy(
            _evaluate(sweep["if"], {"needs.enumerate.outputs.matrix": ""})
        )


def _event_contexts(number: int) -> dict[str, dict[str, Any]]:
    """Every route by which an event-driven run reaches pull request N."""
    return {
        "pull_request_target": {
            "github.event_name": "pull_request_target",
            "github.event.pull_request.number": number,
        },
        "issue_comment": {
            "github.event_name": "issue_comment",
            "github.event.issue.number": number,
        },
        "single dispatch": _dispatch(str(number)),
    }


def _leg_context(number: int) -> dict[str, Any]:
    return {**_dispatch("0"), "matrix.pr": number}


def _group(job: dict[str, Any], context: dict[str, Any]) -> str:
    return _render(job["concurrency"]["group"], context)


class TestALegTakesItsPullRequestsLock:
    """The acceptance criterion: a leg and an event run cannot overlap."""

    @pytest.mark.parametrize(
        "route", ["pull_request_target", "issue_comment", "single dispatch"]
    )
    def test_a_leg_queues_behind_every_event_route(
        self, jobs: dict[str, Any], route: str
    ) -> None:
        event = _group(jobs["github2gerrit"], _event_contexts(29)[route])
        leg = _group(jobs["github2gerrit-sweep"], _leg_context(29))
        assert leg == event == f"g2g-{REPO}-29"

    def test_unrelated_pull_requests_do_not_share_a_lock(
        self, jobs: dict[str, Any]
    ) -> None:
        sweep = jobs["github2gerrit-sweep"]
        legs = {_group(sweep, _leg_context(n)) for n in (29, 30, 31)}
        assert len(legs) == 3
        assert _group(sweep, _leg_context(29)) != _group(
            jobs["github2gerrit"], _event_contexts(30)["pull_request_target"]
        )

    def test_legs_run_in_parallel(self, jobs: dict[str, Any]) -> None:
        strategy = jobs["github2gerrit-sweep"]["strategy"]
        assert strategy["max-parallel"] > 1
        # One pull request failing must not cancel the others.
        assert strategy["fail-fast"] is False

    def test_the_cleanup_leg_keeps_the_repository_wide_group(
        self, jobs: dict[str, Any]
    ) -> None:
        # The group the single bulk job used, which Gerrit-event
        # dispatches share.
        cleanup = _group(jobs["github2gerrit-sweep"], _leg_context(0))
        gerrit_event = _group(
            jobs["github2gerrit"],
            _dispatch("0", "https://gerrit.example.org/c/p/+/1"),
        )
        assert cleanup == gerrit_event == f"g2g-{REPO}-workflow_dispatch"

    def test_in_flight_legs_are_queued_not_cancelled(
        self, jobs: dict[str, Any]
    ) -> None:
        concurrency = jobs["github2gerrit-sweep"]["concurrency"]
        assert concurrency["cancel-in-progress"] is False


def _action_step(job: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in job["steps"] if s.get("name") == ACTION_STEP)


def _without_leg_keys(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps = copy.deepcopy(steps)
    step = next(s for s in steps if s.get("name") == ACTION_STEP)
    for block, key in LEG_ONLY:
        step.get(block, {}).pop(key, None)
    return steps


class TestTheLegIsTheSingleJob:
    """The sweep must not drift from the job it replaces for bulk runs.

    Its steps are a copy, since GitHub offers no way to share a job
    body across two jobs that works on every server. Anything that
    reaches the action one way must reach it the other.
    """

    def test_the_steps_match_apart_from_the_leg_keys(
        self, jobs: dict[str, Any]
    ) -> None:
        single = [
            step
            for step in jobs["github2gerrit"]["steps"]
            if "github.event_name == 'push'" not in str(step.get("if", ""))
        ]
        assert _without_leg_keys(
            jobs["github2gerrit-sweep"]["steps"]
        ) == _without_leg_keys(single)

    @pytest.mark.parametrize(
        "key", ["runs-on", "timeout-minutes", "permissions"]
    )
    def test_the_job_settings_match(
        self, jobs: dict[str, Any], key: str
    ) -> None:
        assert jobs["github2gerrit-sweep"][key] == jobs["github2gerrit"][key]

    def test_a_leg_names_its_pull_request_and_says_it_is_a_leg(
        self, jobs: dict[str, Any]
    ) -> None:
        step = _action_step(jobs["github2gerrit-sweep"])
        assert step["with"]["PR_NUMBER"] == "${{ matrix.pr }}"
        assert step["env"]["G2G_SWEEP_LEG"] == "true"

    def test_a_leg_does_not_choose_where_the_tool_comes_from(
        self, jobs: dict[str, Any]
    ) -> None:
        # A leg depends on G2G_SWEEP_LEG, which a tool older than the
        # workflow ignores. Legs once forced the tool to install from
        # the workflow's own commit for that reason; every run now
        # does, so a leg has nothing to say about it.
        step = _action_step(jobs["github2gerrit-sweep"])
        assert "USE_LOCAL_ACTION" not in step.get("with", {})

    def test_the_single_job_is_never_a_leg(self, jobs: dict[str, Any]) -> None:
        step = _action_step(jobs["github2gerrit"])
        assert "G2G_SWEEP_LEG" not in step.get("env", {})


# ---------------------------------------------------------------------
# The enumeration script, executed out of the workflow
# ---------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
class TestTheEnumeration:
    """The step that builds the sweep's matrix."""

    def _run(
        self,
        jobs: dict[str, Any],
        tmp_path: Path,
        pages: Sequence[Page],
        **env: str,
    ) -> tuple[subprocess.CompletedProcess[str], str, list[list[str]]]:
        return run_enumeration(jobs, tmp_path, pages, **env)

    _matrix = staticmethod(matrix_of)
    _request = staticmethod(request_of)

    def test_a_leg_per_pull_request_and_one_cleanup(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        result, output, _ = self._run(jobs, tmp_path, [[31, 29, 30]])
        assert result.returncode == 0, result.stderr
        assert json.loads(self._matrix(output)) == {"pr": [29, 30, 31, 0]}

    def test_the_matrix_drives_the_legs_end_to_end(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # From the script's output through the sweep's own matrix
        # expression to each leg's lock and PR_NUMBER.
        _result, output, _ = self._run(jobs, tmp_path, [[29, 30]])
        sweep = jobs["github2gerrit-sweep"]
        matrix = _evaluate(
            sweep["strategy"]["matrix"],
            {"needs.enumerate.outputs.matrix": self._matrix(output)},
        )
        for number in matrix["pr"]:
            leg = _leg_context(number)
            pr_input = _render(_action_step(sweep)["with"]["PR_NUMBER"], leg)
            assert pr_input == str(number)
            if number:
                event = _event_contexts(number)["pull_request_target"]
                assert _group(sweep, leg) == _group(
                    jobs["github2gerrit"], event
                )

    def test_every_page_is_read_by_following_the_cursor(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # Offsets shift when a pull request on a page already read
        # closes, skipping one at the boundary; a cursor does not.
        pages = [list(range(1, 101)), list(range(101, 201)), [201, 202]]
        result, output, calls = self._run(jobs, tmp_path, pages)
        assert result.returncode == 0, result.stderr
        assert len(json.loads(self._matrix(output))["pr"]) == 203
        cursors = [self._request(c)["variables"]["after"] for c in calls]
        assert cursors == [None, "1", "2"]

    def test_the_listing_stops_when_github_says_it_is_done(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # A full page is not a sign of more; hasNextPage is.
        result, output, calls = self._run(jobs, tmp_path, [list(range(1, 101))])
        assert result.returncode == 0, result.stderr
        assert len(json.loads(self._matrix(output))["pr"]) == 101
        assert len(calls) == 1

    def test_the_request_asks_for_open_pull_requests_oldest_first(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        _result, _output, calls = self._run(jobs, tmp_path, [[29]])
        call = calls[0]
        assert "Authorization: Bearer t0ken" in call
        assert call[-1] == "https://api.github.example/graphql"
        request = self._request(call)
        owner, name = REPO.split("/")
        assert request["variables"]["owner"] == owner
        assert request["variables"]["name"] == name
        query = " ".join(request["query"].split())
        assert "states: OPEN" in query
        # Oldest first, so one opened meanwhile lands after the cursor
        assert "orderBy: {field: CREATED_AT, direction: ASC}" in query

    def test_a_pull_request_listed_twice_gets_one_leg(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # Defensive: a second leg would queue behind the first and
        # submit the same head again.
        pages = [list(range(1, 101)), [100, 101]]
        result, output, _ = self._run(jobs, tmp_path, pages)
        assert result.returncode == 0, result.stderr
        legs = json.loads(self._matrix(output))["pr"]
        assert legs == [*range(1, 102), 0]

    def test_the_limit_counts_pull_requests_not_listings(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        pages = [
            list(range(1, 101)),
            list(range(101, 201)),
            [*range(201, 256), 1],
        ]
        result, output, _ = self._run(jobs, tmp_path, pages)
        assert result.returncode == 0, result.stderr
        assert len(json.loads(self._matrix(output))["pr"]) == 256

    def test_no_open_pull_requests_still_cleans_up(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # The single bulk job ran the cleanup even with nothing to
        # process.
        result, output, _ = self._run(jobs, tmp_path, [[]])
        assert result.returncode == 0, result.stderr
        assert json.loads(self._matrix(output)) == {"pr": [0]}

    def test_the_largest_matrix_github_allows(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        pages = [
            list(range(1, 101)),
            list(range(101, 201)),
            list(range(201, 256)),
        ]
        result, output, _ = self._run(jobs, tmp_path, pages)
        assert result.returncode == 0, result.stderr
        assert len(json.loads(self._matrix(output))["pr"]) == 256

    def test_more_than_github_allows_fails_rather_than_truncating(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # Processing a subset would pick the same subset on every
        # dispatch and never reach the rest.
        pages = [
            list(range(1, 101)),
            list(range(101, 201)),
            list(range(201, 257)),
        ]
        result, output, _ = self._run(jobs, tmp_path, pages)
        assert result.returncode == 1
        assert "256 open pull requests exceed" in result.stdout
        assert "matrix=" not in output

    @pytest.mark.parametrize("value", ["true", "True", " yes ", "1", "ON"])
    def test_a_disabled_repository_starts_no_legs(
        self, jobs: dict[str, Any], tmp_path: Path, value: str
    ) -> None:
        result, output, calls = self._run(
            jobs, tmp_path, [[29]], G2G_DISABLED=value
        )
        assert result.returncode == 0, result.stderr
        assert self._matrix(output) == ""
        assert calls == []

    def test_an_http_failure_fails_the_sweep(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        result, output, _ = self._run(jobs, tmp_path, [[29]], CURL_FAIL="1")
        assert result.returncode != 0
        assert "matrix=" not in output

    def test_a_graphql_error_fails_the_sweep(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # GraphQL reports most failures in a 200 response, which
        # --fail-with-body alone would take for an empty listing.
        result, output, _ = self._run(
            jobs, tmp_path, [[29]], GRAPHQL_ERRORS="1"
        )
        assert result.returncode == 1
        assert "Resource not accessible" in result.stdout
        assert "matrix=" not in output


# ---------------------------------------------------------------------
# The tool's side of a leg
# ---------------------------------------------------------------------


def _ctx(*, head_repo: str = "contributor/mdsal") -> GitHubContext:
    return GitHubContext(
        event_name="workflow_dispatch",
        event_action="",
        event_path=None,
        repository=REPO,
        repository_owner="opendaylight",
        server_url="https://github.com",
        run_id="1",
        sha=HEAD_SHA,
        base_ref="master",
        head_ref="topic/fix",
        pr_number=29,
        head_repo=head_repo,
    )


class TestAClosedPullRequestEndsALegCleanly:
    """Open when listed, closed before its leg ran: nothing to do."""

    def test_a_leg_stops_without_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("G2G_SWEEP_LEG", "true")
        with pytest.raises(typer.Exit) as exc:
            _stop_for_pr_state(_ctx(), "closed")
        assert exc.value.exit_code == 0

    def test_a_dispatch_naming_it_still_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("G2G_SWEEP_LEG", raising=False)
        with pytest.raises(typer.Exit) as exc:
            _stop_for_pr_state(_ctx(), "closed")
        assert exc.value.exit_code != 0


class TestAnAutomationOnlyRejectionIsASkip:
    """``AUTOMATION_ONLY`` closing a pull request must not fail a sweep.

    It defaults to true, so an ordinary human-authored pull request in
    a mirror is closed and the run ends. The single bulk job counted
    that pull request as skipped; a leg must too, or one such pull
    request marks the whole sweep failed.
    """

    def _reject(self, monkeypatch: pytest.MonkeyPatch, *, leg: bool) -> Any:
        monkeypatch.setenv("AUTOMATION_ONLY", "true")
        monkeypatch.setenv("G2G_SWEEP_LEG", "true" if leg else "false")
        pr = MagicMock()
        pr.user.login = "a-human"
        with (
            patch("github2gerrit.github_api.close_pr") as close,
            pytest.raises(SystemExit) as exc,
        ):
            _check_automation_only(pr, _ctx())
        close.assert_called_once()
        return exc.value.code

    def test_a_leg_closes_it_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._reject(monkeypatch, leg=True) == 0

    def test_a_run_naming_it_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._reject(monkeypatch, leg=False) == 1


class TestABlockedDuplicateIsASkip:
    """``ALLOW_DUPLICATES=false`` blocking a pull request is no failure.

    The single bulk job skipped a duplicate and carried on. A leg takes
    the single-PR path, which fails on one, as a run naming the pull
    request should; a leg must keep the bulk behaviour instead.
    """

    def _check(self, monkeypatch: pytest.MonkeyPatch, *, leg: bool) -> Any:
        monkeypatch.setenv("G2G_SWEEP_LEG", "true" if leg else "false")
        data = MagicMock()
        data.allow_duplicates = False
        data.duplicates_filter = ""
        tracker = MagicMock()
        with (
            patch(
                "github2gerrit.cli.check_for_duplicates",
                side_effect=DuplicateChangeError("duplicate of #28", [28]),
            ),
            patch(
                "github2gerrit.cli.DuplicateDetector._generate_github_change_hash",
                return_value="hash",
            ),
            pytest.raises((SystemExit, typer.Exit)) as exc,
        ):
            _check_single_pr_duplicates(
                data, _ctx(), PROperationMode.UNKNOWN, tracker
            )
        return exc.value, tracker

    def test_a_leg_skips_it_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop, tracker = self._check(monkeypatch, leg=True)
        assert isinstance(stop, SystemExit)
        assert stop.code == 0
        tracker.duplicate_skipped.assert_called_once()
        # A clean stop must release the progress display too.
        tracker.stop.assert_called_once()

    def test_a_run_naming_it_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop, _tracker = self._check(monkeypatch, leg=False)
        assert isinstance(stop, typer.Exit)
        assert stop.exit_code != 0


class TestTheCleanupLeg:
    """The pr 0 leg runs the repository-wide part and nothing else."""

    def _handle(self, monkeypatch: pytest.MonkeyPatch, *, leg: bool) -> Any:
        monkeypatch.setenv("SYNC_ALL_OPEN_PRS", "true")
        monkeypatch.setenv("G2G_SWEEP_LEG", "true" if leg else "false")
        with (
            patch("github2gerrit.cli._process_bulk", return_value=True) as bulk,
            patch("github2gerrit.cli._run_gerrit_cleanup_tasks") as cleanup,
            patch("github2gerrit.cli.log_api_metrics_summary"),
        ):
            assert _handle_bulk_mode(MagicMock(), _ctx(), no_gerrit=False)
        return bulk, cleanup

    def test_it_processes_no_pull_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bulk, cleanup = self._handle(monkeypatch, leg=True)
        bulk.assert_not_called()
        cleanup.assert_called_once()

    def test_the_single_bulk_job_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The composite action cannot fan out, so a caller using it
        # directly still sweeps in one job.
        bulk, cleanup = self._handle(monkeypatch, leg=False)
        bulk.assert_called_once()
        cleanup.assert_called_once()


def _pushed() -> SubmissionResult:
    return SubmissionResult(
        change_urls=[], change_numbers=[], commit_shas=[], pushed=True
    )


def _notice_pr(body: str) -> Any:
    comment = MagicMock()
    comment.body = body
    issue = MagicMock()
    issue.get_comments.return_value = [comment]
    pr = MagicMock()
    pr.as_issue.return_value = issue
    pr.head = MagicMock()
    pr.head.sha = HEAD_SHA
    return pr


class TestAPullRequestLeg:
    """A leg is a sweep of one pull request, not a request for it."""

    def _run(
        self, pr: Any, monkeypatch: pytest.MonkeyPatch, *, leg: bool
    ) -> dict[str, Any]:
        monkeypatch.setenv("G2G_SHOW_PROGRESS", "false")
        monkeypatch.setenv("G2G_SWEEP_LEG", "true" if leg else "false")
        monkeypatch.setenv("CI_TESTING", "true")
        ctx = _ctx()
        data = MagicMock()
        data.dry_run = False
        with (
            patch(
                "github2gerrit.cli._augment_pr_refs_if_needed", return_value=ctx
            ),
            patch(
                "github2gerrit.cli._extract_and_display_pr_info",
                return_value=pr,
            ),
            patch("github2gerrit.cli._recover_pr_metadata", return_value=ctx),
            patch(
                "github2gerrit.cli._check_fork_approval",
                return_value=(True, HEAD_SHA),
            ) as gate,
            patch("github2gerrit.cli._check_single_pr_duplicates"),
            patch(
                "github2gerrit.cli._process_single",
                return_value=(True, _pushed()),
            ) as pipeline,
            patch("github2gerrit.cli._run_gerrit_cleanup_tasks") as cleanup,
            patch("github2gerrit.cli.log_api_metrics_summary"),
        ):
            try:
                _handle_single_pr(
                    data, ctx, PROperationMode.UNKNOWN, no_gerrit=False
                )
                exit_code = None
            except SystemExit as exc:
                exit_code = exc.code
        return {
            "gate": gate,
            "pipeline": pipeline,
            "cleanup": cleanup,
            "exit": exit_code,
        }

    def test_a_transferred_head_is_passed_over(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pr = _notice_pr(render_transferred_comment(head_sha=HEAD_SHA))
        calls = self._run(pr, monkeypatch, leg=True)
        assert calls["exit"] == 0
        calls["gate"].assert_not_called()
        calls["pipeline"].assert_not_called()

    def test_an_untransferred_head_is_processed_without_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The cleanup is the cleanup leg's; a copy per leg would race.
        calls = self._run(_notice_pr("unrelated"), monkeypatch, leg=True)
        calls["pipeline"].assert_called_once()
        calls["cleanup"].assert_not_called()

    def test_a_run_that_is_not_a_leg_still_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._run(_notice_pr("unrelated"), monkeypatch, leg=False)
        calls["pipeline"].assert_called_once()
        calls["cleanup"].assert_called_once()

    def test_a_run_that_is_not_a_leg_ignores_the_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pr = _notice_pr(render_transferred_comment(head_sha=HEAD_SHA))
        calls = self._run(pr, monkeypatch, leg=False)
        calls["gate"].assert_called_once()
        calls["pipeline"].assert_called_once()
