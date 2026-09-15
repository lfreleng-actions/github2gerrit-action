# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for duplicate change detection."""

import os
import re
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import unquote
from urllib.parse import urlsplit

import pytest
import responses

from github2gerrit.config import apply_config_to_env
from github2gerrit.config import apply_parameter_derivation
from github2gerrit.config import mark_derived_keys
from github2gerrit.duplicate_detection import ChangeFingerprint
from github2gerrit.duplicate_detection import DuplicateChangeError
from github2gerrit.duplicate_detection import DuplicateDetector
from github2gerrit.duplicate_detection import check_for_duplicates
from github2gerrit.models import GitHubContext


def _gitreview_urlopen(content: bytes | None) -> Any:
    """Build a ``urllib.request.urlopen`` side effect for tests.

    Serves *content* only for an ``https://raw.githubusercontent.com``
    origin and refuses everything else, so an unexpected network call
    fails loudly rather than resolving through the real network.  The
    origin is compared after parsing rather than by substring, since a
    substring test would also accept
    ``https://raw.githubusercontent.com.example.invalid/...`` or a URL
    carrying the name only in its path or query string.  Pass ``None``
    to make the ``.gitreview`` fetch itself unresolvable.
    """

    def _open(url: Any, *_args: Any, **_kwargs: Any) -> Any:
        target = url if isinstance(url, str) else getattr(url, "full_url", "")
        parsed = urlsplit(target)
        from_raw_github = (
            parsed.scheme == "https"
            and parsed.netloc == "raw.githubusercontent.com"
        )
        if content is not None and from_raw_github:
            response = Mock()
            response.read.return_value = content
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)
            return response
        raise URLError(f"blocked in test: {target}")

    return _open


@pytest.mark.parametrize(
    "url",
    [
        "https://raw.githubusercontent.com.example.invalid/o/r/b/.gitreview",
        "https://example.invalid/?raw.githubusercontent.com",
        "https://example.invalid/raw.githubusercontent.com/.gitreview",
        "http://raw.githubusercontent.com/o/r/b/.gitreview",
    ],
)
def test_gitreview_urlopen_rejects_lookalike_origins(url: str) -> None:
    """The test guard matches the origin, not merely a substring.

    A substring check would serve mocked content to a lookalike host, a
    path segment or a query string, so a malformed or redirected fetch
    URL could pass these tests silently.
    """
    opener = _gitreview_urlopen(b"[gerrit]\nhost=gerrit.example.org\n")

    with pytest.raises(URLError):
        opener(url)


def test_gitreview_urlopen_accepts_expected_origin() -> None:
    """The guard still serves the origin the fetcher actually uses."""
    content = b"[gerrit]\nhost=gerrit.example.org\n"
    opener = _gitreview_urlopen(content)

    response = opener("https://raw.githubusercontent.com/o/r/main/.gitreview")

    assert response.read() == content


class TestChangeFingerprint:
    """Test ChangeFingerprint functionality."""

    def test_normalize_title_basic(self) -> None:
        """Test basic title normalization."""
        fp = ChangeFingerprint("Fix authentication issue")
        assert fp._normalized_title == "fix authentication issue"

    def test_normalize_title_removes_conventional_commits(self) -> None:
        """Test that conventional commit prefixes are removed."""
        cases = [
            ("feat: Add new feature", "add new feature"),
            ("fix(auth): Fix authentication", "fix authentication"),
            ("docs: Update README", "update readme"),
            ("chore: Update dependencies", "update dependencies"),
        ]

        for input_title, expected in cases:
            fp = ChangeFingerprint(input_title)
            assert fp._normalized_title == expected

    def test_normalize_title_removes_versions(self) -> None:
        """Test that version numbers are normalized."""
        cases = [
            (
                "Bump library from 1.2.3 to 2.0.0",
                "bump library from x.y.z to x.y.z",
            ),
            ("Update v1.0 to v2.1.5", "update vx.y.z to vx.y.z"),
            ("Upgrade package 0.6 to 0.8", "upgrade package x.y.z to x.y.z"),
        ]

        for input_title, expected in cases:
            fp = ChangeFingerprint(input_title)
            assert fp._normalized_title == expected

    def test_normalize_title_removes_commit_hashes(self) -> None:
        """Test that commit hashes are normalized."""
        fp = ChangeFingerprint("Revert commit abc1234567890def")
        assert fp._normalized_title == "revert commit commit_hash"

    def test_identical_fingerprints_are_similar(self) -> None:
        """Test that identical fingerprints are detected as similar."""
        fp1 = ChangeFingerprint("Fix authentication issue")
        fp2 = ChangeFingerprint("Fix authentication issue")
        assert fp1.is_similar_to(fp2)

    def test_version_bumps_are_similar(self) -> None:
        """Test that version bumps are detected as similar."""
        fp1 = ChangeFingerprint("Bump library from 1.0 to 1.1")
        fp2 = ChangeFingerprint("Bump library from 1.1 to 1.2")
        assert fp1.is_similar_to(fp2)

    def test_different_libraries_not_similar(self) -> None:
        """Test that different libraries are not similar."""
        fp1 = ChangeFingerprint("Bump library-a from 1.0 to 1.1")
        fp2 = ChangeFingerprint("Bump library-b from 1.0 to 1.1")
        assert not fp1.is_similar_to(fp2)

    def test_similar_files_and_titles(self) -> None:
        """Test similarity detection with file changes."""
        fp1 = ChangeFingerprint(
            "Update requirements",
            files_changed=["requirements.txt", "pyproject.toml"],
        )
        fp2 = ChangeFingerprint(
            "Update requirements file",
            files_changed=["requirements.txt", "setup.py"],
        )
        assert fp1.is_similar_to(fp2)

    def test_content_hash_similarity(self) -> None:
        """Test content hash-based similarity."""
        fp1 = ChangeFingerprint("Fix issue", "This fixes a bug")
        fp2 = ChangeFingerprint("Fix issue", "This fixes a bug")
        assert fp1.is_similar_to(fp2)
        assert fp1._content_hash == fp2._content_hash


class TestDuplicateDetector:
    """Test DuplicateDetector functionality."""

    def _create_mock_pr(
        self,
        number: int,
        title: str,
        state: str = "open",
        updated_at: datetime | None = None,
        body: str = "",
    ) -> Any:
        """Create a mock PR object."""
        if updated_at is None:
            updated_at = datetime.now(UTC)

        pr = Mock()
        pr.number = number
        pr.title = title
        pr.body = body
        pr.state = state
        pr.updated_at = updated_at
        pr.get_files.return_value = []  # Empty files by default
        return pr

    def _create_mock_repo(self, prs: list[Any]) -> Any:
        """Create a mock repository with given PRs."""
        repo = Mock()

        def get_pulls_with_state(state: str = "all") -> list[Any]:
            if state == "open":
                return [pr for pr in prs if pr.state == "open"]
            elif state == "closed":
                return [pr for pr in prs if pr.state in ("closed", "merged")]
            else:  # state == "all"
                return prs

        repo.get_pulls.side_effect = get_pulls_with_state
        repo.get_pull.side_effect = lambda num: next(
            pr for pr in prs if pr.number == num
        )
        return repo

    def test_get_recent_prs_filters_by_date(self) -> None:
        """Test that get_recent_prs filters by lookback period."""
        now = datetime.now(UTC)
        old_date = now - timedelta(days=10)
        recent_date = now - timedelta(days=2)

        prs = [
            self._create_mock_pr(1, "Recent PR", updated_at=recent_date),
            self._create_mock_pr(2, "Old PR", updated_at=old_date),
        ]

        repo = self._create_mock_repo(prs)
        detector = DuplicateDetector(repo, lookback_days=7)

        # Test that detector was initialized properly
        assert detector.repo == repo
        assert detector.lookback_days == 7

    def test_detector_basic_functionality(self) -> None:
        """Test basic detector functionality."""
        repo = Mock()
        detector = DuplicateDetector(repo)

        assert detector.repo == repo
        assert detector.lookback_days == 7

    def test_check_for_duplicates_no_gerrit_config(self) -> None:
        """Test that check_for_duplicates works without Gerrit config."""
        pr = self._create_mock_pr(1, "Fix authentication")
        detector = DuplicateDetector(Mock())

        # Should not raise error when no Gerrit config is available
        detector.check_for_duplicates(pr, allow_duplicates=False)

    def test_check_for_duplicates_allows_with_flag(self) -> None:
        """Test that check_for_duplicates allows duplicates with flag."""
        pr = self._create_mock_pr(1, "Fix authentication")
        detector = DuplicateDetector(Mock())

        # Should not raise error with allow_duplicates=True
        detector.check_for_duplicates(pr, allow_duplicates=True)


class TestCheckForDuplicatesFunction:
    """Test the convenience check_for_duplicates function."""

    def _create_mock_github_context(
        self, pr_number: int | None = 123
    ) -> GitHubContext:
        """Create a mock GitHub context."""
        return GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=Path("event.json"),
            repository="org/repo",
            repository_owner="org",
            server_url="https://github.com",
            run_id="123456",
            sha="abc123",
            base_ref="main",
            head_ref="feature-branch",
            pr_number=pr_number,
        )

    @patch("github2gerrit.duplicate_detection.build_client")
    @patch("github2gerrit.duplicate_detection.get_repo_from_env")
    def test_check_for_duplicates_success(
        self, mock_get_repo: Any, mock_build_client: Any
    ) -> None:
        """Test successful duplicate check."""
        # Mock the GitHub API
        mock_repo = Mock()
        mock_pr = Mock()
        mock_pr.title = "Fix authentication"
        mock_pr.body = "This fixes auth issues"

        mock_repo.get_pull.return_value = mock_pr
        mock_get_repo.return_value = mock_repo

        gh = self._create_mock_github_context()

        # Should not raise any exception
        check_for_duplicates(gh, allow_duplicates=False)

    @patch("github2gerrit.duplicate_detection.build_client")
    @patch("github2gerrit.duplicate_detection.get_repo_from_env")
    def test_check_for_duplicates_no_pr_number(
        self, mock_get_repo: Any, mock_build_client: Any
    ) -> None:
        """Test that function handles missing PR number gracefully."""
        gh = self._create_mock_github_context(pr_number=None)

        # Should not raise any exception or make API calls
        check_for_duplicates(gh, allow_duplicates=False)

        mock_build_client.assert_not_called()
        mock_get_repo.assert_not_called()

    @patch("github2gerrit.duplicate_detection.build_client")
    @patch("github2gerrit.duplicate_detection.get_repo_from_env")
    def test_check_for_duplicates_api_failure_doesnt_crash(
        self, mock_get_repo: Any, mock_build_client: Any
    ) -> None:
        """Test that API failures don't crash the process."""
        # Mock API failure
        mock_build_client.side_effect = Exception("API Error")

        gh = self._create_mock_github_context()

        # Should not raise exception, just log warning
        check_for_duplicates(gh, allow_duplicates=False)


class TestDependabotScenarios:
    """Test specific Dependabot-style scenarios."""

    def test_identical_dependabot_prs(self) -> None:
        """Test detection of identical Dependabot PRs."""
        fp1 = ChangeFingerprint(
            "Bump lfit/gerrit-review-action from 0.6 to 0.8"
        )
        fp2 = ChangeFingerprint(
            "Bump lfit/gerrit-review-action from 0.6 to 0.8"
        )
        fp3 = ChangeFingerprint(
            "Bump lfit/gerrit-review-action from 0.6 to 0.8"
        )

        assert fp1.is_similar_to(fp2)
        assert fp1.is_similar_to(fp3)
        assert fp2.is_similar_to(fp3)

    def test_different_dependabot_versions(self) -> None:
        """Test that different version bumps are still similar."""
        fp1 = ChangeFingerprint(
            "Bump lfit/gerrit-review-action from 0.6 to 0.7"
        )
        fp2 = ChangeFingerprint(
            "Bump lfit/gerrit-review-action from 0.6 to 0.8"
        )
        fp3 = ChangeFingerprint(
            "Bump lfit/gerrit-review-action from 0.7 to 0.8"
        )

        assert fp1.is_similar_to(fp2)
        assert fp1.is_similar_to(fp3)
        assert fp2.is_similar_to(fp3)


class TestGerritDuplicateDetection:
    """Test Gerrit duplicate detection functionality."""

    def _create_mock_github_context(
        self, pr_number: int = 123, repository: str = "org/repo"
    ) -> GitHubContext:
        """Create a mock GitHub context."""
        return GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=Path("event.json"),
            repository=repository,
            repository_owner="org",
            server_url="https://github.com",
            run_id="123456",
            sha="abc123",
            base_ref="main",
            head_ref="feature-branch",
            pr_number=pr_number,
        )

    def _create_mock_repo(self, prs: list[Any]) -> Any:
        """Create a mock GitHub repository with the given PRs."""
        mock_repo = Mock()
        mock_repo.get_pulls.return_value = prs
        return mock_repo

    def test_resolve_gerrit_info_from_env(self) -> None:
        """Test resolving Gerrit info from environment variables."""
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        with patch.dict(
            "os.environ",
            {
                "GERRIT_SERVER": "gerrit.example.org",
                "GERRIT_PROJECT": "test/project",
            },
        ):
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)
            assert result == ("gerrit.example.org", "test/project")

    def test_resolve_gerrit_info_missing_env(self) -> None:
        """Test that missing environment variables return None."""
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        with patch.dict("os.environ", {}, clear=True):
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)
            assert result is None

    def test_resolve_gerrit_info_skips_local_gitreview(self) -> None:
        """Test that local .gitreview reading is skipped in composite action context."""
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        # Even if a local .gitreview exists, it should be skipped
        # and fall back to remote fetching or return None
        with patch.dict("os.environ", {}, clear=True):
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)
            assert (
                result is None
            )  # Should skip local file and return None for remote fallback

    @patch("urllib.request.urlopen")
    @patch("pathlib.Path.exists")
    def test_resolve_gerrit_info_from_remote_gitreview(
        self, mock_exists: Any, mock_urlopen: Any
    ) -> None:
        """Test resolving Gerrit info from remote .gitreview file."""
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        mock_exists.return_value = False

        # Mock the HTTP response
        mock_response = Mock()
        mock_response.read.return_value = b"""[gerrit]
host=gerrit.example.org
port=29418
project=test/project.git
"""
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=None)
        mock_urlopen.return_value = mock_response

        with patch.dict("os.environ", {}, clear=True):
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)
            assert result == ("gerrit.example.org", "test/project")

    @patch("urllib.request.urlopen")
    def test_resolve_gerrit_info_explicit_env_skips_gitreview(
        self, mock_urlopen: Any
    ) -> None:
        """Explicit configuration short-circuits before any fetch.

        Values the operator set deliberately outrank the pull request's
        ``.gitreview``, and resolving them must cost no network I/O.
        """
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        mock_urlopen.side_effect = _gitreview_urlopen(
            b"[gerrit]\nhost=gerrit.wrong.org\nproject=wrong/project\n"
        )

        with patch.dict(
            "os.environ",
            {
                "GERRIT_SERVER": "gerrit.example.org",
                "GERRIT_PROJECT": "test/project",
            },
        ):
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)

        assert result == ("gerrit.example.org", "test/project")
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_resolve_gerrit_info_gitreview_outranks_derived_env(
        self, mock_urlopen: Any
    ) -> None:
        """A derived environment pair loses to a resolvable .gitreview.

        Derivation guesses the project from the GitHub repository name,
        so the per-pull-request ``.gitreview`` is the better answer.
        """
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        mock_urlopen.side_effect = _gitreview_urlopen(
            b"[gerrit]\nhost=gerrit.real.org\nport=29418\n"
            b"project=real/project.git\n"
        )

        with patch.dict(
            "os.environ",
            {
                "GERRIT_SERVER": "gerrit.derived.org",
                "GERRIT_PROJECT": "derived-project",
            },
        ):
            mark_derived_keys(["GERRIT_SERVER", "GERRIT_PROJECT"])
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)

        assert result == ("gerrit.real.org", "real/project")

    @patch("urllib.request.urlopen")
    def test_resolve_gerrit_info_keeps_explicit_host_takes_project(
        self, mock_urlopen: Any
    ) -> None:
        """A derived project must not drag an explicit host down with it.

        Provenance is per field.  Treating the pair as a unit made one
        derived key demote both, so ``.gitreview`` replaced a host the
        operator had configured deliberately.
        """
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        mock_urlopen.side_effect = _gitreview_urlopen(
            b"[gerrit]\nhost=gerrit.gitreview.org\nport=29418\n"
            b"project=gitreview/project.git\n"
        )

        with patch.dict(
            "os.environ",
            {
                "GERRIT_SERVER": "gerrit.explicit.org",
                "GERRIT_PROJECT": "derived-project",
            },
        ):
            mark_derived_keys(["GERRIT_PROJECT"])
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)

        assert result == ("gerrit.explicit.org", "gitreview/project")

    @patch("github2gerrit.config._read_gitreview_host")
    @patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    @patch("urllib.request.urlopen")
    def test_legacy_config_file_project_loses_to_gitreview(
        self,
        mock_urlopen: Any,
        mock_derive_creds: Any,
        mock_config_gitreview_host: Any,
    ) -> None:
        """The same outcome, reached through the configuration file.

        The case above marks the project derived directly.  This one
        earns the mark the way a real local CLI run does: releases that
        auto-saved derived values put GERRIT_PROJECT in the
        per-organization section, so load_org_config returns an old
        repository-name guess, derivation finds the key already filled,
        and without provenance duplicate detection would short-circuit
        on it and query the wrong project.
        """
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context(
            repository="onap/integration-distribution"
        )

        # Keep derivation off the SSH config and off the network.
        mock_derive_creds.return_value = (None, None)
        mock_config_gitreview_host.return_value = None

        mock_urlopen.side_effect = _gitreview_urlopen(
            b"[gerrit]\nhost=gerrit.gitreview.org\nport=29418\n"
            b"project=integration/distribution.git\n"
        )

        with patch.dict(
            "os.environ",
            {
                # Set by the operator for this run.
                "GERRIT_SERVER": "gerrit.explicit.org",
                # Unset, as on a local CLI run that leans on the
                # per-organization configuration file.
                "GERRIT_PROJECT": "",
            },
        ):
            cfg = apply_parameter_derivation(
                # As load_org_config returns it from the stored section.
                {"GERRIT_PROJECT": "integration-distribution"},
                "onap",
                repository=gh.repository,
                save_to_config=False,
            )
            apply_config_to_env(cfg)

            # The stale name is in play, alongside the explicit host.
            assert os.environ["GERRIT_SERVER"] == "gerrit.explicit.org"
            assert os.environ["GERRIT_PROJECT"] == "integration-distribution"

            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)

        assert result == ("gerrit.explicit.org", "integration/distribution")

    @patch("urllib.request.urlopen")
    def test_resolve_gerrit_info_keeps_explicit_project_takes_host(
        self, mock_urlopen: Any
    ) -> None:
        """The mirror case: a derived host must not discard an explicit
        project.

        Only the field lacking operator intent may come from
        ``.gitreview``.
        """
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        mock_urlopen.side_effect = _gitreview_urlopen(
            b"[gerrit]\nhost=gerrit.gitreview.org\nport=29418\n"
            b"project=gitreview/project.git\n"
        )

        with patch.dict(
            "os.environ",
            {
                "GERRIT_SERVER": "gerrit.derived.org",
                "GERRIT_PROJECT": "explicit/project",
            },
        ):
            mark_derived_keys(["GERRIT_SERVER"])
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)

        assert result == ("gerrit.gitreview.org", "explicit/project")

    @patch("urllib.request.urlopen")
    def test_host_only_gitreview_keeps_derived_project_in_query(
        self, mock_urlopen: Any
    ) -> None:
        """A ``.gitreview`` without ``project=`` must not blank the
        project.

        ``parse_gitreview`` needs only ``host=``, so a host-only file
        parses successfully and leaves ``info.project`` empty.  Taking
        that empty string over the derived environment value drops the
        ``project:`` qualifier from the Gerrit query, which then
        searches every project on the server: an unrelated change that
        happens to share a title would block a legitimate submission.
        A derived project is a guess, but a scoped guess beats an
        unscoped search.
        """
        mock_urlopen.side_effect = _gitreview_urlopen(
            b"[gerrit]\nhost=gerrit.gitreview.org\nport=29418\n"
        )

        responses.start()
        try:
            detector = DuplicateDetector(Mock())
            gh = self._create_mock_github_context(
                repository="org/derived-project"
            )

            target_pr = Mock()
            target_pr.number = 123
            target_pr.title = "Fix authentication"
            target_pr.body = ""
            target_pr.get_files.return_value = []

            responses.add(
                responses.GET,
                re.compile(r"https://gerrit\.gitreview\.org/.*"),
                json=[],
                status=200,
            )

            with patch.dict(
                "os.environ",
                {
                    # As parameter derivation would leave them.
                    "GERRIT_SERVER": "gerrit.derived.org",
                    "GERRIT_PROJECT": "derived-project",
                    # Pin the base path so no discovery probe runs.
                    "GERRIT_HTTP_BASE_PATH": "r",
                },
            ):
                mark_derived_keys(["GERRIT_SERVER", "GERRIT_PROJECT"])

                resolved = detector._resolve_gerrit_info_from_env_or_gitreview(
                    gh
                )
                assert resolved == (
                    "gerrit.gitreview.org",
                    "derived-project",
                )

                detector.check_for_duplicates(
                    target_pr, allow_duplicates=False, gh=gh
                )

            queried = [
                unquote(call.request.url or "") for call in responses.calls
            ]
            assert queried, "expected at least one Gerrit query"
            assert all("project:derived-project " in url for url in queried), (
                f"Gerrit query was not scoped to a project: {queried}"
            )
        finally:
            responses.stop()
            responses.reset()

    @patch("urllib.request.urlopen")
    def test_resolve_gerrit_info_falls_back_to_derived_env(
        self, mock_urlopen: Any
    ) -> None:
        """An unresolvable .gitreview leaves the derived pair in play.

        Returning None instead would turn a possibly-wrong query into no
        duplicate detection at all, which is strictly worse.
        """
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context()

        mock_urlopen.side_effect = _gitreview_urlopen(None)

        with patch.dict(
            "os.environ",
            {
                "GERRIT_SERVER": "gerrit.derived.org",
                "GERRIT_PROJECT": "derived-project",
            },
        ):
            mark_derived_keys(["GERRIT_SERVER", "GERRIT_PROJECT"])
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)

        assert result == ("gerrit.derived.org", "derived-project")

    @patch("urllib.request.urlopen")
    def test_resolve_gerrit_info_falls_back_without_repository(
        self, mock_urlopen: Any
    ) -> None:
        """With no repository to fetch from, the derived pair still wins
        over returning nothing."""
        detector = DuplicateDetector(Mock())
        gh = self._create_mock_github_context(repository="")

        mock_urlopen.side_effect = _gitreview_urlopen(None)

        with patch.dict(
            "os.environ",
            {
                "GERRIT_SERVER": "gerrit.derived.org",
                "GERRIT_PROJECT": "derived-project",
            },
        ):
            mark_derived_keys(["GERRIT_SERVER", "GERRIT_PROJECT"])
            result = detector._resolve_gerrit_info_from_env_or_gitreview(gh)

        assert result == ("gerrit.derived.org", "derived-project")
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_duplicate_query_uses_gitreview_path_not_derived_name(
        self, mock_urlopen: Any
    ) -> None:
        """Acceptance criterion for a Gerrit project with a path separator.

        ``opendaylight/integration-distribution`` derives the project
        ``integration-distribution``, but Gerrit hosts it at
        ``integration/distribution``.  Querying the derived name asks
        about a project that does not exist, so every duplicate is
        silently missed.  The ``.gitreview`` must win.
        """
        mock_urlopen.side_effect = _gitreview_urlopen(
            b"[gerrit]\nhost=gerrit.opendaylight.org\nport=29418\n"
            b"project=integration/distribution.git\n"
        )

        responses.start()
        try:
            detector = DuplicateDetector(Mock())
            gh = self._create_mock_github_context(
                repository="opendaylight/integration-distribution"
            )

            target_pr = Mock()
            target_pr.number = 123
            target_pr.title = "Fix authentication"
            target_pr.body = ""
            target_pr.get_files.return_value = []

            # Answer any query, so the only thing that can distinguish
            # the two projects is the query Gerrit was actually asked.
            responses.add(
                responses.GET,
                re.compile(r"https://gerrit\.opendaylight\.org/.*"),
                json=[
                    {
                        "_number": 12345,
                        "subject": "Fix authentication",
                        "project": "integration/distribution",
                        "current_revision": "abc123",
                        "revisions": {
                            "abc123": {
                                "commit": {"message": "Fix authentication"},
                                "files": {},
                            }
                        },
                    }
                ],
                status=200,
            )

            with patch.dict(
                "os.environ",
                {
                    # As parameter derivation would leave them.
                    "GERRIT_SERVER": "gerrit.opendaylight.org",
                    "GERRIT_PROJECT": "integration-distribution",
                    # Pin the base path so no discovery probe runs.
                    "GERRIT_HTTP_BASE_PATH": "r",
                },
            ):
                mark_derived_keys(["GERRIT_SERVER", "GERRIT_PROJECT"])

                with pytest.raises(DuplicateChangeError):
                    detector.check_for_duplicates(
                        target_pr, allow_duplicates=False, gh=gh
                    )

            queried = [
                unquote(call.request.url or "") for call in responses.calls
            ]
            assert queried, "expected at least one Gerrit query"
            assert any(
                "project:integration/distribution" in url for url in queried
            ), f"Gerrit was not queried for the .gitreview project: {queried}"
            assert not any(
                "project:integration-distribution" in url for url in queried
            ), f"Gerrit was queried for the derived project: {queried}"
        finally:
            responses.stop()
            responses.reset()

    def test_check_for_duplicates_with_gerrit_duplicate(
        self,
    ) -> None:
        """
        Test that Gerrit duplicates are detected and prevent new
        submissions.
        """
        # Start responses for this test
        responses.start()

        try:
            detector = DuplicateDetector(Mock())
            gh = self._create_mock_github_context()

            # Mock target PR
            target_pr = Mock()
            target_pr.number = 123
            target_pr.title = "Fix authentication"
            target_pr.body = ""
            target_pr.get_files.return_value = []

            # Mock Gerrit REST API response with matching subject
            gerrit_response = [
                {
                    "_number": 12345,
                    "subject": "Fix authentication",
                    "project": "test/project",
                    "current_revision": "abc123",
                    "revisions": {
                        "abc123": {
                            "commit": {
                                "message": "Fix authentication\n\nSome details"
                            },
                            "files": {},
                        }
                    },
                }
            ]

            # Mock the Gerrit REST API using responses - match the actual query
            import re

            responses.add(
                responses.GET,
                re.compile(
                    r"https://gerrit\.example\.org"
                    r"(/[a-z]+)?/a?/?changes/\?.*"
                ),
                json=gerrit_response,
                status=200,
            )

            with patch.dict(
                "os.environ",
                {
                    "GERRIT_SERVER": "gerrit.example.org",
                    "GERRIT_PROJECT": "test/project",
                    "GERRIT_HTTP_BASE_PATH": "r",
                },
            ):
                with pytest.raises(DuplicateChangeError) as exc_info:
                    detector.check_for_duplicates(
                        target_pr, allow_duplicates=False, gh=gh
                    )

                assert "subject matches existing Gerrit change(s)" in str(
                    exc_info.value
                )
        finally:
            responses.stop()

    def test_check_for_duplicates_with_gerrit_duplicate_allowed(
        self,
    ) -> None:
        """Test that Gerrit duplicates are allowed with the flag."""
        # Start responses for this test
        responses.start()

        try:
            detector = DuplicateDetector(Mock())
            gh = self._create_mock_github_context()

            # Mock target PR
            target_pr = Mock()
            target_pr.number = 123
            target_pr.title = "Fix authentication"
            target_pr.body = ""
            target_pr.get_files.return_value = []

            # Mock Gerrit REST API response with matching subject
            gerrit_response = [
                {
                    "_number": 12345,
                    "subject": "Fix authentication",
                    "project": "test/project",
                    "current_revision": "abc123",
                    "revisions": {
                        "abc123": {
                            "commit": {
                                "message": "Fix authentication\n\nSome details"
                            },
                            "files": {},
                        }
                    },
                }
            ]

            # Mock the Gerrit REST API using responses - match the actual query
            import re

            responses.add(
                responses.GET,
                re.compile(
                    r"https://gerrit\.example\.org"
                    r"(/[a-z]+)?/a?/?changes/\?.*"
                ),
                json=gerrit_response,
                status=200,
            )

            with patch.dict(
                "os.environ",
                {
                    "GERRIT_SERVER": "gerrit.example.org",
                    "GERRIT_PROJECT": "test/project",
                    "GERRIT_HTTP_BASE_PATH": "r",
                },
            ):
                # Should NOT raise when allow_duplicates=True
                detector.check_for_duplicates(
                    target_pr, allow_duplicates=True, gh=gh
                )
        finally:
            responses.stop()

    def test_different_dependabot_packages(self) -> None:
        """Test that different packages are not similar."""
        fp1 = ChangeFingerprint(
            "Bump lfit/gerrit-review-action from 0.6 to 0.8"
        )
        fp2 = ChangeFingerprint("Bump actions/checkout from 3 to 4")

        assert not fp1.is_similar_to(fp2)

    def test_mixed_case_and_formatting(self) -> None:
        """Test that formatting differences don't affect detection."""
        fp1 = ChangeFingerprint(
            "Bump lfit/gerrit-review-action from 0.6 to 0.8"
        )
        fp2 = ChangeFingerprint(
            "bump lfit/gerrit-review-action from 0.6 to 0.8"
        )
        fp3 = ChangeFingerprint(
            "Bump `lfit/gerrit-review-action` from 0.6 to 0.8"
        )

        assert fp1.is_similar_to(fp2)
        assert fp1.is_similar_to(fp3)
        assert fp2.is_similar_to(fp3)
