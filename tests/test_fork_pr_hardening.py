# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Fork pull request hardening.

A fork pull request's tree is authored by someone without write access
to the base repository.  ``.gitreview`` read out of that tree must never
influence where the resulting change is pushed, or a fork could redirect
the push to an arbitrary Gerrit project.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import patch

import pytest

from github2gerrit import config
from github2gerrit.cli import _augment_pr_refs_if_needed
from github2gerrit.cli import _build_bulk_pr_tasks
from github2gerrit.cli import _head_repo_for_pr
from github2gerrit.cli import _read_head_repo
from github2gerrit.cli import _ref_for_pr
from github2gerrit.cli import _skip_unprivileged_fork_run
from github2gerrit.core import Orchestrator
from github2gerrit.duplicate_detection import DuplicateDetector
from github2gerrit.gitreview import GitReviewInfo
from github2gerrit.models import GitHubContext
from github2gerrit.models import Inputs


sys.path.append(str(Path(__file__).parent))
from fixtures.make_repo import init_repo
from fixtures.make_repo import write_gitreview


BASE_REPO = "opendaylight/mdsal"
FORK_REPO = "attacker/mdsal"


def _gh_ctx(
    *,
    repository: str = BASE_REPO,
    head_repo: str = "",
    base_ref: str = "master",
    head_ref: str = "feature/evil",
    pr_number: int | None = 29,
    event_name: str = "pull_request_target",
) -> GitHubContext:
    return GitHubContext(
        event_name=event_name,
        event_action="opened",
        event_path=None,
        repository=repository,
        repository_owner=repository.split("/")[0],
        server_url="https://github.com",
        run_id="1",
        sha="deadbeef",
        base_ref=base_ref,
        head_ref=head_ref,
        pr_number=pr_number,
        head_repo=head_repo,
    )


def _inputs(*, privkey: str = "", dry_run: bool = False) -> Inputs:
    """A stand-in carrying only the fields the skip decision reads.

    ``Inputs`` has some thirty required fields; supplying all of them
    here would suggest the rest matter to this decision, which they do
    not. The cast records the partiality deliberately.
    """
    return cast(
        Inputs,
        SimpleNamespace(gerrit_ssh_privkey_g2g=privkey, dry_run=dry_run),
    )


class TestIsForkPr:
    """Provenance detection on GitHubContext."""

    def test_fork_head_detected(self) -> None:
        assert _gh_ctx(head_repo=FORK_REPO).is_fork_pr is True

    def test_same_repo_not_a_fork(self) -> None:
        assert _gh_ctx(head_repo=BASE_REPO).is_fork_pr is False

    def test_comparison_is_case_insensitive(self) -> None:
        assert _gh_ctx(head_repo="OpenDaylight/MdSal").is_fork_pr is False

    def test_unknown_head_repo_is_not_a_fork(self) -> None:
        # An absent signal must not silently change same-repo handling.
        assert _gh_ctx(head_repo="").is_fork_pr is False

    def test_unknown_base_repo_is_not_a_fork(self) -> None:
        ctx = _gh_ctx(repository="", head_repo=FORK_REPO)
        assert ctx.is_fork_pr is False


class TestSkipUnprivilegedForkRun:
    """A run GitHub denied secrets to must stop, and say why.

    The skip has to be narrow. Swallowing a genuinely unset secret is
    the worse failure of the two, because a silent success reads as a
    working configuration.
    """

    @pytest.fixture(autouse=True)
    def _in_actions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)

    @pytest.mark.parametrize(
        "event_name",
        ["pull_request", "pull_request_review", "pull_request_review_comment"],
    )
    def test_fork_run_without_key_is_skipped(self, event_name: str) -> None:
        ctx = _gh_ctx(head_repo=FORK_REPO, event_name=event_name)
        assert _skip_unprivileged_fork_run(_inputs(), ctx) is True

    @pytest.mark.parametrize(
        "event_name",
        ["pull_request_target", "issue_comment", "schedule", "workflow_run"],
    )
    def test_privileged_trigger_still_reports_a_missing_key(
        self, event_name: str
    ) -> None:
        # These run from the default branch and do receive secrets, so
        # an absent key there is a real misconfiguration.
        ctx = _gh_ctx(head_repo=FORK_REPO, event_name=event_name)
        assert _skip_unprivileged_fork_run(_inputs(), ctx) is False

    def test_same_repo_run_still_reports_a_missing_key(self) -> None:
        ctx = _gh_ctx(head_repo=BASE_REPO, event_name="pull_request")
        assert _skip_unprivileged_fork_run(_inputs(), ctx) is False

    def test_unresolved_provenance_still_reports_a_missing_key(self) -> None:
        # Deliberately keyed on is_fork_pr, not head_is_trusted: an
        # unknown head is not known to be a fork, and treating it as
        # one would hide a same-repository misconfiguration.
        ctx = _gh_ctx(head_repo="", event_name="pull_request")
        assert ctx.head_is_trusted is False
        assert _skip_unprivileged_fork_run(_inputs(), ctx) is False

    def test_present_key_is_never_skipped(self) -> None:
        ctx = _gh_ctx(head_repo=FORK_REPO, event_name="pull_request_review")
        assert _skip_unprivileged_fork_run(_inputs(privkey="KEY"), ctx) is False

    def test_direct_cli_invocation_is_unaffected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Outside Actions there is no trigger to blame; the operator is
        # running the tool themselves and wants the real error.
        monkeypatch.setenv("GITHUB_ACTIONS", "false")
        monkeypatch.setenv("GITHUB_EVENT_NAME", "")
        ctx = _gh_ctx(head_repo=FORK_REPO, event_name="pull_request")
        assert _skip_unprivileged_fork_run(_inputs(), ctx) is False

    def test_no_gerrit_mode_still_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # G2G_NO_GERRIT is keyless by design: it exercises the whole
        # pipeline with the Gerrit network operations stubbed out. The
        # absent key is the point, not a symptom of a denied secret,
        # so skipping would defeat the mode entirely.
        monkeypatch.setenv("G2G_NO_GERRIT", "true")
        ctx = _gh_ctx(head_repo=FORK_REPO, event_name="pull_request")
        assert _skip_unprivileged_fork_run(_inputs(), ctx) is False

    def test_dry_run_still_runs(self) -> None:
        # A dry run returns from Orchestrator.execute before SSH setup,
        # so it is keyless for the same reason.
        ctx = _gh_ctx(head_repo=FORK_REPO, event_name="pull_request")
        assert _skip_unprivileged_fork_run(_inputs(dry_run=True), ctx) is False


class TestHeadIsTrusted:
    """Trust is stricter than the factual fork question."""

    def test_same_repo_head_is_trusted(self) -> None:
        assert _gh_ctx(head_repo=BASE_REPO).head_is_trusted is True

    def test_fork_head_is_not_trusted(self) -> None:
        assert _gh_ctx(head_repo=FORK_REPO).head_is_trusted is False

    def test_unknown_provenance_is_not_trusted(self) -> None:
        # is_fork_pr says False, but that must not imply trust.
        ctx = _gh_ctx(head_repo="")
        assert ctx.is_fork_pr is False
        assert ctx.head_is_trusted is False


class TestDuplicateDetectionBranchHints:
    """Duplicate detection runs before the pipeline resolver."""

    def _resolve(self, gh: GitHubContext) -> dict[str, Any]:
        detector = DuplicateDetector(repo=cast("Any", object()))
        captured: dict[str, Any] = {}

        def _fake_fetch(repo_full: str, **kwargs: Any) -> None:
            captured.update(kwargs)
            return None

        with patch(
            "github2gerrit.gitreview.fetch_gitreview_raw",
            side_effect=_fake_fetch,
        ):
            detector._resolve_gerrit_info_from_env_or_gitreview(gh)
        return captured

    def test_fork_head_ref_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fork may name its branch after one in the base repository."""
        monkeypatch.delenv("GERRIT_SERVER", raising=False)
        monkeypatch.delenv("GERRIT_PROJECT", raising=False)

        kwargs = self._resolve(
            _gh_ctx(head_repo=FORK_REPO, head_ref="stable/scandium")
        )

        assert kwargs["branches"] == ["master"]
        # Defaults would query the default branch's Gerrit project.
        assert kwargs["default_branches"] == ()

    def test_trusted_head_ref_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GERRIT_SERVER", raising=False)
        monkeypatch.delenv("GERRIT_PROJECT", raising=False)

        kwargs = self._resolve(
            _gh_ctx(head_repo=BASE_REPO, head_ref="topic/fix")
        )

        assert kwargs["branches"] == ["topic/fix", "master"]
        assert kwargs["default_branches"] == ("master", "main")


class TestReadGitreviewForkIsolation:
    """``_read_gitreview`` must not trust a fork-supplied file."""

    def test_fork_gitreview_in_workspace_is_ignored(
        self, tmp_path: Path
    ) -> None:
        """A malicious .gitreview in the PR tree must not be used."""
        repo = init_repo(tmp_path / "forked", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.opendaylight.org",
            port=29418,
            project="attacker/exfiltration-target",
        )
        orch = Orchestrator(workspace=repo.path)

        trusted = GitReviewInfo(
            host="gerrit.opendaylight.org",
            port=29418,
            project="mdsal",
        )
        with patch(
            "github2gerrit.core.fetch_gitreview", return_value=trusted
        ) as mock_fetch:
            info = orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=FORK_REPO),
            )

        assert info is not None
        assert info.project == "mdsal", (
            "fork-supplied .gitreview must not reach Gerrit resolution"
        )
        assert mock_fetch.call_count == 1
        assert mock_fetch.call_args.kwargs["skip_local"] is True

    def test_fork_resolution_pinned_to_base_ref(self, tmp_path: Path) -> None:
        """Fork branch names must not steer base-repository lookups."""
        repo = init_repo(tmp_path / "forked2", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.example.org",
            port=29418,
            project="attacker/target",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch(
            "github2gerrit.core.fetch_gitreview", return_value=None
        ) as mock_fetch:
            orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=FORK_REPO, head_ref="evil-branch"),
            )

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["api_ref"] == "master"
        assert kwargs["branches"] == ["master"]
        assert "evil-branch" not in kwargs["branches"]
        assert kwargs["include_env_refs"] is False
        assert kwargs["repo_full"] == BASE_REPO
        # master/main defaults would answer for the default branch,
        # which may map to a different Gerrit project than the base.
        assert kwargs["default_branches"] == ()

    def test_fork_without_base_ref_skips_api_lookup(
        self, tmp_path: Path
    ) -> None:
        """``get_contents(ref=None)`` would read the default branch."""
        repo = init_repo(tmp_path / "norefs", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.evil.example",
            port=29418,
            project="attacker/target",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch(
            "github2gerrit.core.fetch_gitreview", return_value=None
        ) as mock_fetch:
            orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=FORK_REPO, base_ref=""),
            )

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["api_ref"] is None
        assert kwargs["repo_obj"] is None
        assert kwargs["default_branches"] == ()

    def test_same_repo_pr_still_reads_local_file(self, tmp_path: Path) -> None:
        """Non-fork behaviour is unchanged."""
        repo = init_repo(tmp_path / "trusted", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.opendaylight.org",
            port=29418,
            project="mdsal",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch("github2gerrit.core.fetch_gitreview") as mock_fetch:
            info = orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=BASE_REPO),
            )

        assert info is not None
        assert info.project == "mdsal"
        mock_fetch.assert_not_called()

    def test_no_context_still_reads_local_file(self, tmp_path: Path) -> None:
        """Direct CLI use without an event context is unaffected."""
        repo = init_repo(tmp_path / "local", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.example.org",
            port=29418,
            project="some/project",
        )
        orch = Orchestrator(workspace=repo.path)

        info = orch._read_gitreview(repo.path / ".gitreview")

        assert info is not None
        assert info.project == "some/project"

    def test_unknown_provenance_is_untrusted(self, tmp_path: Path) -> None:
        """A PR whose head repository is unknown must not be trusted.

        Specific-PR ``workflow_dispatch`` runs and direct URL invocations
        carry no pull request payload, and GitHub reports a null head
        repo for deleted forks.  Trusting the tree in those cases would
        let a fork pick another Gerrit project.
        """
        repo = init_repo(tmp_path / "unknown", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.evil.example",
            port=29418,
            project="attacker/target",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch(
            "github2gerrit.core.fetch_gitreview", return_value=None
        ) as mock_fetch:
            info = orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=""),
            )

        assert info is None
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args.kwargs["skip_local"] is True

    def test_context_without_pr_number_reads_local_file(
        self, tmp_path: Path
    ) -> None:
        """Push runs have a context but no PR, so the tree is the repo's."""
        repo = init_repo(tmp_path / "pushed", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.opendaylight.org",
            port=29418,
            project="mdsal",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch("github2gerrit.core.fetch_gitreview") as mock_fetch:
            info = orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo="", pr_number=None),
            )

        assert info is not None
        assert info.project == "mdsal"
        mock_fetch.assert_not_called()

    def test_fork_falls_through_when_base_lookup_fails(
        self, tmp_path: Path
    ) -> None:
        """Returning None lets inputs/derivation take over."""
        repo = init_repo(tmp_path / "forked3", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.evil.example",
            port=29418,
            project="attacker/target",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch("github2gerrit.core.fetch_gitreview", return_value=None):
            info = orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=FORK_REPO),
            )

        assert info is None


class TestConfigGitreviewProvenance:
    """Config derivation runs before the PR context is read.

    With no provenance available, or a fork head, the lookup must not
    consult ``GITHUB_HEAD_REF``: a fork picks its own branch name, and
    one matching a real base-repository branch would select that
    branch's host.  A head known to live in the base repository keeps
    its precedence, matching what ``_read_gitreview`` does later.
    """

    def _host(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def _fake(repository: str | None = None, **kwargs: Any) -> None:
            captured.update(kwargs)
            return None

        monkeypatch.setattr(
            "github2gerrit.gitreview.read_gitreview_host", _fake
        )
        config._read_gitreview_host(BASE_REPO)
        return captured

    def test_fork_head_ref_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PR_HEAD_REPO", FORK_REPO)
        monkeypatch.setenv("GITHUB_HEAD_REF", "stable/scandium")
        monkeypatch.setenv("GITHUB_BASE_REF", "master")

        kwargs = self._host(monkeypatch)

        assert kwargs["include_env_refs"] is False
        assert kwargs["branches"] == ("master",)

    def test_unknown_provenance_ignores_head_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An absent signal must not earn the head ref any trust."""
        monkeypatch.setenv("PR_HEAD_REPO", "")
        monkeypatch.setenv("GITHUB_HEAD_REF", "stable/scandium")
        monkeypatch.setenv("GITHUB_BASE_REF", "master")

        assert self._host(monkeypatch)["branches"] == ("master",)

    def test_trusted_head_ref_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same-repo PRs keep head-first order.

        ``_read_gitreview`` reads the head tree for a trusted head, so
        dropping the head ref here would let derivation record one host
        while the pipeline pushes to another.
        """
        monkeypatch.setenv("PR_HEAD_REPO", BASE_REPO)
        monkeypatch.setenv("GITHUB_HEAD_REF", "topic/fix")
        monkeypatch.setenv("GITHUB_BASE_REF", "master")

        assert self._host(monkeypatch)["branches"] == ("topic/fix", "master")

    def test_no_refs_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Push runs have no PR refs; master/main still apply."""
        monkeypatch.setenv("PR_HEAD_REPO", "")
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)

        kwargs = self._host(monkeypatch)

        assert kwargs["branches"] == ()
        assert kwargs["include_env_refs"] is False


class TestHeadRepoResolution:
    """Provenance plumbing from the event and the API."""

    def test_env_var_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PR_HEAD_REPO", FORK_REPO)
        payload = {"pull_request": {"head": {"repo": {"full_name": "x/y"}}}}
        assert _read_head_repo(payload) == FORK_REPO

    def test_payload_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PR_HEAD_REPO", raising=False)
        payload = {"pull_request": {"head": {"repo": {"full_name": FORK_REPO}}}}
        assert _read_head_repo(payload) == FORK_REPO

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"pull_request": {}},
            {"pull_request": {"head": {}}},
            # A deleted fork reports a null repo.
            {"pull_request": {"head": {"repo": None}}},
            {"pull_request": {"head": {"repo": {}}}},
        ],
    )
    def test_missing_payload_fields_yield_empty(
        self, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PR_HEAD_REPO", raising=False)
        assert _read_head_repo(payload) == ""

    def test_head_repo_from_pull_request_object(self) -> None:
        class _Repo:
            full_name = FORK_REPO

        class _Head:
            repo = _Repo()

        class _Pr:
            head = _Head()

        assert _head_repo_for_pr(_Pr()) == FORK_REPO

    def test_head_repo_from_object_missing_repo(self) -> None:
        class _Pr:
            head = None

        assert _head_repo_for_pr(_Pr()) == ""

    def test_refs_read_from_pull_request_object(self) -> None:
        class _End:
            def __init__(self, ref: str) -> None:
                self.ref = ref

        class _Pr:
            base = _End("stable/scandium")
            head = _End("topic/fix")

        assert _ref_for_pr(_Pr(), "base") == "stable/scandium"
        assert _ref_for_pr(_Pr(), "head") == "topic/fix"

    def test_refs_missing_yield_empty(self) -> None:
        class _Pr:
            base = None

        assert _ref_for_pr(_Pr(), "base") == ""
        assert _ref_for_pr(_Pr(), "head") == ""


class TestBulkContextProvenance:
    """Bulk runs must take refs from each PR, not the outer context."""

    def test_per_pr_refs_used(self) -> None:
        class _End:
            def __init__(self, ref: str, full_name: str | None = None) -> None:
                self.ref = ref
                if full_name is not None:
                    self.repo = type("_R", (), {"full_name": full_name})()

        class _Pr:
            number = 29
            base = _End("stable/scandium")
            head = _End("topic/fix", FORK_REPO)

        # Bulk runs are dispatched, so the outer context has no PR refs.
        outer = _gh_ctx(base_ref="", head_ref="", pr_number=None)

        tasks = _build_bulk_pr_tasks(outer, [_Pr()])

        assert len(tasks) == 1
        _pr, ctx = tasks[0]
        assert ctx.base_ref == "stable/scandium"
        assert ctx.head_ref == "topic/fix"
        assert ctx.head_repo == FORK_REPO
        assert ctx.is_fork_pr is True


class TestAugmentPrRefs:
    """Provenance must be resolved whatever the triggering event."""

    @pytest.mark.parametrize(
        "event_name",
        ["issue_comment", "workflow_dispatch", "pull_request_review", ""],
    )
    def test_missing_metadata_queried_for_any_event(
        self, event_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An issue_comment payload has a PR number but no refs or head."""
        for var in ("G2G_TARGET_URL",):
            monkeypatch.delenv(var, raising=False)
        # Set rather than delete so monkeypatch restores it: the code
        # under test writes PR_HEAD_REPO through os.environ directly,
        # which teardown would otherwise leak into later tests.
        monkeypatch.setenv("PR_HEAD_REPO", "")
        monkeypatch.setenv("GITHUB_REPOSITORY", BASE_REPO)
        monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)

        class _End:
            def __init__(self, ref: str, full_name: str = "") -> None:
                self.ref = ref
                self.sha = "cafebabe"
                if full_name:
                    self.repo = type("_R", (), {"full_name": full_name})()

        class _Pr:
            base = _End("stable/scandium")
            head = _End("topic/fix", FORK_REPO)

        ctx = _gh_ctx(base_ref="", head_ref="", head_repo="", pr_number=29)
        ctx = dataclasses.replace(ctx, event_name=event_name)

        with (
            patch("github2gerrit.cli.build_client"),
            patch("github2gerrit.cli.get_repo_from_env"),
            patch("github2gerrit.cli.get_pull", return_value=_Pr()),
        ):
            result = _augment_pr_refs_if_needed(ctx)

        assert result.head_repo == FORK_REPO
        assert result.base_ref == "stable/scandium"
        assert result.is_fork_pr is True

    def test_complete_context_makes_no_api_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pull request payloads already carry everything needed."""
        monkeypatch.delenv("G2G_TARGET_URL", raising=False)

        ctx = _gh_ctx(head_repo=FORK_REPO, base_ref="master", head_ref="topic")

        with patch("github2gerrit.cli.get_pull") as mock_get_pull:
            result = _augment_pr_refs_if_needed(ctx)

        mock_get_pull.assert_not_called()
        assert result is ctx
