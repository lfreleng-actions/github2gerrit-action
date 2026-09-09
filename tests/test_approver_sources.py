# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Optional sources of approval authority.

These widen who counts as a maintainer, so the tests are mostly about
what they must *not* do: default to on, admit an entry the file does
not name, or let any of the gate's other guarantees slip.
"""

from __future__ import annotations

import textwrap
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.approvers import APPROVER_LOGINS_ENV
from github2gerrit.approvers import INFO_YAML_LFID_ENV
from github2gerrit.approvers import INFO_YAML_PATH
from github2gerrit.approvers import USE_INFO_YAML_ENV
from github2gerrit.approvers import describe_additional_sources
from github2gerrit.approvers import explicit_approvers
from github2gerrit.approvers import parse_info_yaml
from github2gerrit.approvers import resolve_additional_approvers
from github2gerrit.cli import _check_fork_approval
from github2gerrit.models import GitHubContext
from github2gerrit.pr_approval import evaluate_fork_approval


HEAD_SHA = "0b2abdcf7bb2fb5ed6620f214968ae2b3c5e70e6"

# The real file from opendaylight/mdsal: an anchored project_lead
# aliased into both primary_contact and the committer roster, and no
# github_id anywhere.
MDSAL_INFO_YAML = textwrap.dedent("""
    ---
    project: "mdsal"
    project_lead: &odl_mdsal_ptl
      name: "Robert Varga"
      email: "nite@hq.sk"
      id: "rovarga"
    primary_contact: *odl_mdsal_ptl
    committers:
      - <<: *odl_mdsal_ptl
      - name: "Tom Pantelis"
        email: "tompantelis@gmail.com"
        id: "tpantelis"
      - name: "Jie Han"
        email: "han.jie@zte.com.cn"
        id: "JieHan2017"
""").strip()

WITH_GITHUB_IDS = textwrap.dedent("""
    ---
    project: "example"
    project_lead:
      name: "Lead"
      id: "lead-lfid"
      github_id: "Lead-GitHub"
    committers:
      - name: "Committer"
        id: "committer-lfid"
        github_id: "committer-github"
""").strip()


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No source is enabled unless a test asks for it."""
    for var in (APPROVER_LOGINS_ENV, USE_INFO_YAML_ENV, INFO_YAML_LFID_ENV):
        monkeypatch.delenv(var, raising=False)


def _repo_returning(text: str) -> Any:
    repo = MagicMock()
    repo.get_contents.return_value = MagicMock(
        decoded_content=text.encode("utf-8")
    )
    return repo


class TestDefaultsAreOff:
    """Widening a trust decision must never happen by accident."""

    def test_nothing_configured_names_nobody(self) -> None:
        assert resolve_additional_approvers() == frozenset()

    def test_info_yaml_is_not_read_unless_enabled(self) -> None:
        repo = _repo_returning(MDSAL_INFO_YAML)
        assert (
            resolve_additional_approvers(base_repo=repo, base_ref="master")
            == frozenset()
        )
        repo.get_contents.assert_not_called()

    def test_no_sources_described(self) -> None:
        assert describe_additional_sources() == ""


class TestExplicitAllowlist:
    def test_logins_are_parsed_and_lowercased(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(APPROVER_LOGINS_ENV, "Alice, BOB ,")
        assert explicit_approvers() == frozenset({"alice", "bob"})

    def test_blank_value_names_nobody(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(APPROVER_LOGINS_ENV, "   ,  ,")
        assert explicit_approvers() == frozenset()


class TestInfoYamlParsing:
    def test_lfids_are_ignored_by_default(self) -> None:
        # The default must not treat an LFID as a GitHub login: whoever
        # registers that username on GitHub would inherit authority.
        assert parse_info_yaml(MDSAL_INFO_YAML) == frozenset()

    def test_lfids_are_used_when_opted_in(self) -> None:
        assert parse_info_yaml(MDSAL_INFO_YAML, match_lfid=True) == frozenset(
            {"rovarga", "tpantelis", "jiehan2017"}
        )

    def test_github_id_is_preferred_over_lfid(self) -> None:
        # Even with LFID matching on, an explicit github_id settles the
        # question for that person; their LFID must not also be added.
        logins = parse_info_yaml(WITH_GITHUB_IDS, match_lfid=True)
        assert logins == frozenset({"lead-github", "committer-github"})

    def test_github_ids_need_no_opt_in(self) -> None:
        assert parse_info_yaml(WITH_GITHUB_IDS) == frozenset(
            {"lead-github", "committer-github"}
        )

    def test_primary_contact_carries_no_authority(self) -> None:
        # It names whoever should be contacted about the project, which
        # is not the same as who may authorise a transfer.
        contact_only = textwrap.dedent("""
            ---
            project: "example"
            primary_contact:
              name: "Ops"
              github_id: "ops-account"
            committers:
              - name: "Committer"
                github_id: "committer-github"
        """).strip()
        assert parse_info_yaml(contact_only) == frozenset({"committer-github"})

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not: [a, mapping",
            "- a\n- list",
            "just a string",
            # PyYAML raises RecursionError, not YAMLError, on deeply
            # nested input. Letting it escape would turn a malformed
            # base file into a failed workflow.
            "[" * 4000 + "]" * 4000,
        ],
    )
    def test_unparsable_content_names_nobody(self, text: str) -> None:
        # A malformed file must neither widen the set nor raise into
        # the gate.
        assert parse_info_yaml(text, match_lfid=True) == frozenset()


class TestInfoYamlProvenance:
    """The file must come from the base side of the pull request."""

    def test_read_from_the_base_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        monkeypatch.setenv(INFO_YAML_LFID_ENV, "true")
        repo = _repo_returning(MDSAL_INFO_YAML)

        logins = resolve_additional_approvers(base_repo=repo, base_ref="master")

        repo.get_contents.assert_called_once_with(INFO_YAML_PATH, ref="master")
        assert "rovarga" in logins

    def test_unknown_base_ref_declines_the_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Falling back to the default branch would authorise from a
        # roster the pull request does not target. .gitreview fails
        # closed in the same situation.
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        monkeypatch.setenv(INFO_YAML_LFID_ENV, "true")
        repo = _repo_returning(MDSAL_INFO_YAML)

        logins = resolve_additional_approvers(base_repo=repo, base_ref="  ")

        assert logins == frozenset()
        repo.get_contents.assert_not_called()

    def test_explicit_list_survives_a_declined_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sources are independent; one failing closed must not
        # silently discard the other.
        monkeypatch.setenv(APPROVER_LOGINS_ENV, "named-reviewer")
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        assert resolve_additional_approvers(base_ref="") == frozenset(
            {"named-reviewer"}
        )

    def test_missing_file_names_nobody(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        repo = MagicMock()
        repo.get_contents.side_effect = RuntimeError("404")
        assert (
            resolve_additional_approvers(base_repo=repo, base_ref="master")
            == frozenset()
        )

    def test_sources_combine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(APPROVER_LOGINS_ENV, "external-reviewer")
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        monkeypatch.setenv(INFO_YAML_LFID_ENV, "true")
        logins = resolve_additional_approvers(
            base_repo=_repo_returning(MDSAL_INFO_YAML), base_ref="master"
        )
        assert {"external-reviewer", "tpantelis"} <= logins


class TestGateGuaranteesSurviveWidening:
    """A named approver gains standing, and nothing more."""

    def _review(
        self,
        login: str,
        *,
        state: str = "APPROVED",
        association: str = "NONE",
        commit_id: str = HEAD_SHA,
    ) -> Any:
        review = MagicMock()
        review.state = state
        review.user.login = login
        review.author_association = association
        review.commit_id = commit_id
        return review

    def _evaluate(
        self,
        reviews: list[Any],
        *,
        author: str = "contributor",
        named: frozenset[str] = frozenset(),
    ):
        pr = MagicMock()
        pr.get_reviews.return_value = reviews
        return evaluate_fork_approval(
            pr,
            head_sha=HEAD_SHA,
            author_login=author,
            extra_approvers=named,
        )

    def test_untrusted_association_alone_is_refused(self) -> None:
        status = self._evaluate([self._review("gerrit-reviewer")])
        assert status.approved is False

    def test_named_login_with_no_association_is_admitted(self) -> None:
        # The whole point: a Gerrit reviewer with no GitHub standing.
        status = self._evaluate(
            [self._review("gerrit-reviewer")],
            named=frozenset({"gerrit-reviewer"}),
        )
        assert status.approved is True

    def test_naming_is_case_insensitive(self) -> None:
        status = self._evaluate(
            [self._review("Gerrit-Reviewer")],
            named=frozenset({"gerrit-reviewer"}),
        )
        assert status.approved is True

    def test_named_author_still_cannot_self_approve(self) -> None:
        # The one guarantee GitHub enforces for us. No widening may
        # cost us it.
        status = self._evaluate(
            [self._review("contributor")],
            author="contributor",
            named=frozenset({"contributor"}),
        )
        assert status.approved is False

    def test_named_approval_still_binds_to_the_head(self) -> None:
        status = self._evaluate(
            [self._review("gerrit-reviewer", commit_id="0" * 40)],
            named=frozenset({"gerrit-reviewer"}),
        )
        assert status.approved is False
        assert status.stale_approvers == ["gerrit-reviewer"]

    def test_named_changes_requested_still_blocks(self) -> None:
        status = self._evaluate(
            [
                self._review("approver", association="MEMBER"),
                self._review("gerrit-reviewer", state="CHANGES_REQUESTED"),
            ],
            named=frozenset({"gerrit-reviewer"}),
        )
        assert status.approved is False
        assert status.blockers == ["gerrit-reviewer"]


class TestAuthorityComesFromTheBaseRepository:
    """The gate must read `INFO.yaml` from the base side only.

    The resolver tests above prove only that it reads whichever object
    it is handed. This covers the wiring that chooses which object —
    the part that would let a fork nominate its own approvers if it
    picked `pr.head.repo`.
    """

    FORK_INFO_YAML = textwrap.dedent("""
        ---
        project: "mdsal"
        project_lead:
          name: "Attacker"
          github_id: "attacker"
        committers:
          - name: "Attacker"
            github_id: "attacker"
    """).strip()

    def _pull_request(self) -> Any:
        """A PR whose fork tampers with `INFO.yaml` to name itself."""
        pr = MagicMock()
        pr.user.login = "contributor"
        pr.head.sha = HEAD_SHA
        pr.head.ref = "topic"
        pr.head.repo = _repo_returning(self.FORK_INFO_YAML)
        pr.head.repo.full_name = "contributor/mdsal"
        pr.base.ref = "master"
        pr.base.repo = _repo_returning(MDSAL_INFO_YAML)
        pr.base.repo.full_name = "opendaylight/mdsal"

        approval = MagicMock()
        approval.state = "APPROVED"
        approval.user.login = "attacker"
        approval.author_association = "NONE"
        approval.commit_id = HEAD_SHA
        pr.get_reviews.return_value = [approval]
        return pr

    def _gate(self, pr: Any) -> tuple[bool, str]:
        gh = GitHubContext(
            event_name="issue_comment",
            event_action="created",
            event_path=None,
            repository="opendaylight/mdsal",
            repository_owner="opendaylight",
            server_url="https://github.com",
            run_id="1",
            sha=HEAD_SHA,
            base_ref="master",
            head_ref="topic",
            pr_number=29,
            head_repo="contributor/mdsal",
        )
        with patch(
            "github2gerrit.cli._is_github_actions_context", return_value=True
        ):
            return _check_fork_approval(pr, gh)

    def test_tampered_fork_copy_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        monkeypatch.setenv(INFO_YAML_LFID_ENV, "true")
        monkeypatch.setenv("CI_TESTING", "true")
        pr = self._pull_request()

        allowed, _sha = self._gate(pr)

        # The fork's copy names "attacker" as lead and committer. Were
        # it consulted, the approval above would clear the gate.
        assert allowed is False
        pr.head.repo.get_contents.assert_not_called()

    def test_base_copy_supplies_authority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        monkeypatch.setenv(INFO_YAML_LFID_ENV, "true")
        monkeypatch.setenv("CI_TESTING", "true")
        pr = self._pull_request()
        # tpantelis is a committer in the base repository's copy.
        pr.get_reviews.return_value[0].user.login = "tpantelis"

        allowed, sha = self._gate(pr)

        assert allowed is True
        assert sha == HEAD_SHA
        pr.base.repo.get_contents.assert_called_once_with(
            INFO_YAML_PATH, ref="master"
        )


class TestPolicyDescription:
    def test_allowlist_is_described(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(APPROVER_LOGINS_ENV, "alice")
        assert "approver list" in describe_additional_sources()

    def test_lfid_matching_is_disclosed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A project matching on LFIDs has accepted an impersonation
        # risk; the notice should say so rather than imply otherwise.
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        monkeypatch.setenv(INFO_YAML_LFID_ENV, "true")
        assert "`id`" in describe_additional_sources()

    def test_github_id_only_is_described_as_such(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(USE_INFO_YAML_ENV, "true")
        described = describe_additional_sources()
        assert "`github_id`" in described
        assert "or `id`" not in described
