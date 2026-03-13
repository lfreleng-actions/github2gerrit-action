# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Regression tests for GitHub issue #157.

Covers three fixes:
1. REST API hostname derived from .gitreview instead of gerrit.{org}.org
2. Cleanup REST failures are non-fatal (don't mark the job as failed)
3. UPDATE error message mentions CREATE_MISSING as a remedy

See: https://github.com/lfreleng-actions/github2gerrit-action/issues/157
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.config import _read_gitreview_host
from github2gerrit.config import derive_gerrit_parameters
from github2gerrit.gerrit_pr_closer import cleanup_closed_github_prs
from github2gerrit.gerrit_rest import GerritRestError


# ---------------------------------------------------------------------------
# 1. _read_gitreview_host — local file
# ---------------------------------------------------------------------------


class TestReadGitreviewHostLocal:
    """Tests for reading host from a local .gitreview file."""

    def test_reads_host_from_local_gitreview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Host is returned when a valid .gitreview exists in cwd."""
        gitreview = tmp_path / ".gitreview"
        gitreview.write_text(
            "[gerrit]\nhost=git.opendaylight.org\nport=29418\n"
            "project=l2switch.git\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        result = _read_gitreview_host()
        assert result == "git.opendaylight.org"

    def test_returns_none_when_no_local_gitreview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None when .gitreview does not exist and no repository given."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        result = _read_gitreview_host()
        assert result is None

    def test_returns_none_for_empty_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None when .gitreview exists but host= line is empty."""
        gitreview = tmp_path / ".gitreview"
        gitreview.write_text(
            "[gerrit]\nhost=\nport=29418\nproject=test.git\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        result = _read_gitreview_host()
        assert result is None

    def test_returns_none_for_missing_host_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None when .gitreview exists but has no host= line at all."""
        gitreview = tmp_path / ".gitreview"
        gitreview.write_text(
            "[gerrit]\nport=29418\nproject=test.git\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        result = _read_gitreview_host()
        assert result is None

    def test_strips_whitespace_from_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leading/trailing whitespace on the host value is stripped."""
        gitreview = tmp_path / ".gitreview"
        gitreview.write_text(
            "[gerrit]\nhost=  gerrit.example.org  \nport=29418\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        result = _read_gitreview_host()
        assert result == "gerrit.example.org"


# ---------------------------------------------------------------------------
# 1b. _read_gitreview_host — remote fallback
# ---------------------------------------------------------------------------


class TestReadGitreviewHostRemote:
    """Tests for fetching .gitreview from raw.githubusercontent.com."""

    @patch("github2gerrit.config.urllib.request.urlopen")
    def test_fetches_from_master_branch(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falls back to remote when no local .gitreview exists."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        mock_response = MagicMock()
        mock_response.read.return_value = (
            b"[gerrit]\nhost=git.opendaylight.org\n"
            b"port=29418\nproject=aaa.git\n"
        )
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = _read_gitreview_host("opendaylight/aaa")
        assert result == "git.opendaylight.org"

    @patch("github2gerrit.config.urllib.request.urlopen")
    def test_returns_none_when_remote_unavailable(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """None when both local and remote .gitreview are unavailable."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        mock_urlopen.side_effect = OSError("Network unreachable")

        result = _read_gitreview_host("opendaylight/aaa")
        assert result is None

    def test_returns_none_when_no_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None when no local file and no repository is provided."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        result = _read_gitreview_host(None)
        assert result is None

    def test_returns_none_for_bare_repository_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None when repository has no slash (not owner/repo format)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        result = _read_gitreview_host("noslash")
        assert result is None


# ---------------------------------------------------------------------------
# 2. derive_gerrit_parameters uses .gitreview host
# ---------------------------------------------------------------------------


class TestDeriveGerritParametersGitreview:
    """Verify the server derivation priority chain in derive_gerrit_parameters.

    Priority: config file > .gitreview > heuristic gerrit.{org}.org
    """

    @patch("github2gerrit.config._read_gitreview_host")
    @patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_gitreview_host_used_when_no_config(
        self,
        mock_derive_creds: MagicMock,
        mock_read_gitreview: MagicMock,
    ) -> None:
        """When no config file entry, .gitreview host wins over heuristic."""
        mock_derive_creds.return_value = (None, None)
        mock_read_gitreview.return_value = "git.opendaylight.org"

        derived = derive_gerrit_parameters(
            "opendaylight", repository="opendaylight/l2switch"
        )

        assert derived["GERRIT_SERVER"] == "git.opendaylight.org"
        # SSH credentials should use the .gitreview host for lookup
        mock_derive_creds.assert_called_once_with(
            "git.opendaylight.org", "opendaylight"
        )

    @patch("github2gerrit.config._read_gitreview_host")
    @patch("github2gerrit.config.load_org_config")
    @patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_config_file_beats_gitreview(
        self,
        mock_derive_creds: MagicMock,
        mock_load_org_config: MagicMock,
        mock_read_gitreview: MagicMock,
    ) -> None:
        """Config file GERRIT_SERVER takes precedence over .gitreview."""
        mock_derive_creds.return_value = (None, None)
        mock_read_gitreview.return_value = "git.opendaylight.org"
        mock_load_org_config.return_value = {
            "GERRIT_SERVER": "custom.gerrit.example.org",
        }

        derived = derive_gerrit_parameters(
            "opendaylight", repository="opendaylight/l2switch"
        )

        assert derived["GERRIT_SERVER"] == "custom.gerrit.example.org"

    @patch("github2gerrit.config._read_gitreview_host")
    @patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_heuristic_fallback_when_no_gitreview(
        self,
        mock_derive_creds: MagicMock,
        mock_read_gitreview: MagicMock,
    ) -> None:
        """Falls back to gerrit.{org}.org when .gitreview is unavailable."""
        mock_derive_creds.return_value = (None, None)
        mock_read_gitreview.return_value = None

        derived = derive_gerrit_parameters("onap")

        assert derived["GERRIT_SERVER"] == "gerrit.onap.org"

    @patch("github2gerrit.config._read_gitreview_host")
    @patch("github2gerrit.ssh_config_parser.derive_gerrit_credentials")
    def test_project_derived_from_repository(
        self,
        mock_derive_creds: MagicMock,
        mock_read_gitreview: MagicMock,
    ) -> None:
        """GERRIT_PROJECT is derived from repository owner/repo format."""
        mock_derive_creds.return_value = (None, None)
        mock_read_gitreview.return_value = "git.opendaylight.org"

        derived = derive_gerrit_parameters(
            "opendaylight", repository="opendaylight/l2switch"
        )

        assert derived["GERRIT_PROJECT"] == "l2switch"


# ---------------------------------------------------------------------------
# 3. cleanup_closed_github_prs — REST errors are non-fatal
# ---------------------------------------------------------------------------


class TestCleanupNonFatal:
    """Verify that cleanup REST failures don't raise fatal errors."""

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    def test_gerrit_rest_error_returns_zero(
        self, mock_build_client: MagicMock
    ) -> None:
        """GerritRestError during cleanup logs a warning and returns 0."""
        mock_client = MagicMock()
        mock_client.get.side_effect = GerritRestError(
            "Failed to resolve 'gerrit.opendaylight.org'"
        )
        mock_build_client.return_value = mock_client

        # Must NOT raise; should return 0
        result = cleanup_closed_github_prs(
            gerrit_server="gerrit.opendaylight.org",
            gerrit_project="l2switch",
            dry_run=True,
        )
        assert result == 0

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    def test_connection_error_returns_zero(
        self, mock_build_client: MagicMock
    ) -> None:
        """Generic connection errors during cleanup return 0."""
        mock_build_client.side_effect = ConnectionError(
            "Name resolution failed"
        )

        result = cleanup_closed_github_prs(
            gerrit_server="gerrit.opendaylight.org",
            gerrit_project="l2switch",
            dry_run=True,
        )
        assert result == 0

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    def test_timeout_error_returns_zero(
        self, mock_build_client: MagicMock
    ) -> None:
        """Timeout errors during cleanup return 0."""
        mock_client = MagicMock()
        mock_client.get.side_effect = GerritRestError("timed out")
        mock_build_client.return_value = mock_client

        result = cleanup_closed_github_prs(
            gerrit_server="unreachable.example.org",
            gerrit_project="project",
            dry_run=True,
        )
        assert result == 0

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    def test_successful_cleanup_returns_count(
        self, mock_build_client: MagicMock
    ) -> None:
        """Normal operation returns 0 when no changes need abandoning."""
        mock_client = MagicMock()
        mock_client.get.return_value = []
        mock_build_client.return_value = mock_client

        result = cleanup_closed_github_prs(
            gerrit_server="gerrit.example.org",
            gerrit_project="test-project",
            dry_run=True,
        )
        assert result == 0


# ---------------------------------------------------------------------------
# 4. UPDATE error message mentions CREATE_MISSING
# ---------------------------------------------------------------------------


class TestUpdateErrorMentionsCreateMissing:
    """Verify the UPDATE-no-existing-change error message includes guidance."""

    def test_error_message_contains_create_missing(self) -> None:
        """The OrchestratorError message should mention CREATE_MISSING."""
        from github2gerrit.core import GerritInfo
        from github2gerrit.core import Orchestrator
        from github2gerrit.core import OrchestratorError
        from github2gerrit.models import GitHubContext

        gh = GitHubContext(
            event_name="pull_request",
            event_action="synchronize",
            event_path=None,
            repository="opendaylight/aaa",
            repository_owner="opendaylight",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="master",
            head_ref="feature",
            pr_number=3,
        )

        gerrit = GerritInfo(
            host="git.opendaylight.org",
            port=29418,
            project="aaa",
        )

        # Create a minimal orchestrator (workspace doesn't matter here)
        orch = Orchestrator(workspace=Path("/tmp/fake"))  # noqa: S108

        # Mock the internal change lookup to return empty (no existing change)
        with (
            patch.object(orch, "_find_existing_change_for_pr", return_value=[]),
            pytest.raises(OrchestratorError, match="CREATE_MISSING"),
        ):
            orch._enforce_existing_change_for_update(gh, gerrit)

    def test_error_message_contains_comment_command(self) -> None:
        """The error message should mention the PR comment command."""
        from github2gerrit.core import GerritInfo
        from github2gerrit.core import Orchestrator
        from github2gerrit.core import OrchestratorError
        from github2gerrit.models import GitHubContext

        gh = GitHubContext(
            event_name="pull_request",
            event_action="reopened",
            event_path=None,
            repository="opendaylight/aaa",
            repository_owner="opendaylight",
            server_url="https://github.com",
            run_id="456",
            sha="def456",
            base_ref="master",
            head_ref="feature",
            pr_number=7,
        )

        gerrit = GerritInfo(
            host="git.opendaylight.org",
            port=29418,
            project="aaa",
        )

        orch = Orchestrator(workspace=Path("/tmp/fake"))  # noqa: S108

        with (
            patch.object(orch, "_find_existing_change_for_pr", return_value=[]),
            pytest.raises(
                OrchestratorError,
                match="@github2gerrit create missing change",
            ),
        ):
            orch._enforce_existing_change_for_update(gh, gerrit)
