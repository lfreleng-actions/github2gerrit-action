# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for PR operation mode detection and update handling.

Tests verify:
- PR operation mode detection from event types
- Proper routing for CREATE vs UPDATE operations
- Change-ID recovery for UPDATE operations
- Metadata sync functionality
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import OrchestratorError
from github2gerrit.models import GitHubContext
from github2gerrit.models import PROperationMode


class TestPROperationModeDetection:
    """Tests for PROperationMode detection in GitHubContext."""

    def test_opened_event_detected_as_create(self) -> None:
        """Verify 'opened' action is detected as CREATE mode."""
        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        assert gh.get_operation_mode() == PROperationMode.CREATE

    def test_synchronize_event_detected_as_update(self) -> None:
        """Verify 'synchronize' action is detected as UPDATE mode."""
        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="synchronize",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        assert gh.get_operation_mode() == PROperationMode.UPDATE

    def test_edited_event_detected_as_edit(self) -> None:
        """Verify 'edited' action is detected as EDIT mode."""
        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="edited",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        assert gh.get_operation_mode() == PROperationMode.EDIT

    def test_reopened_event_detected_as_reopen(self) -> None:
        """Verify 'reopened' action is detected as REOPEN mode."""
        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="reopened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        assert gh.get_operation_mode() == PROperationMode.REOPEN

    def test_closed_event_detected_as_close(self) -> None:
        """Verify 'closed' action is detected as CLOSE mode."""
        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="closed",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        assert gh.get_operation_mode() == PROperationMode.CLOSE

    def test_non_pr_event_returns_unknown(self) -> None:
        """Verify non-PR events return UNKNOWN mode."""
        gh = GitHubContext(
            event_name="push",
            event_action="",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="",
            head_ref="",
            pr_number=None,
        )

        assert gh.get_operation_mode() == PROperationMode.UNKNOWN

    def test_unknown_action_returns_unknown(self) -> None:
        """Verify unknown PR action returns UNKNOWN mode."""
        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="some_new_action",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        assert gh.get_operation_mode() == PROperationMode.UNKNOWN


class TestFindExistingChange:
    """Tests for _find_existing_change_for_pr method."""

    def test_find_existing_change_by_topic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify finding existing change by topic query."""
        orch = Orchestrator(workspace=tmp_path)

        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="synchronize",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        # Mock the query_changes_by_topic function
        mock_change = Mock()
        mock_change.change_id = "I1234567890abcdef"

        def mock_query_changes_by_topic(client, topic, statuses=None):
            if topic == "GH-owner-repo-42":
                return [mock_change]
            return []

        monkeypatch.setattr(
            "github2gerrit.gerrit_query.query_changes_by_topic",
            mock_query_changes_by_topic,
        )

        # Mock GerritRestClient
        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.GerritRestClient",
            Mock,
        )

        # Execute
        change_ids = orch._find_existing_change_for_pr(gh, gerrit)

        # Verify
        assert change_ids == ["I1234567890abcdef"]

    def test_find_existing_change_returns_empty_when_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify empty list returned when no change found."""
        orch = Orchestrator(workspace=tmp_path)

        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="synchronize",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=99,
        )

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        # Mock to return empty results
        monkeypatch.setattr(
            "github2gerrit.gerrit_query.query_changes_by_topic",
            lambda client, topic, statuses=None: [],
        )

        # Mock GerritRestClient
        mock_client = Mock()
        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.GerritRestClient",
            Mock(return_value=mock_client),
        )

        # Mock build_client to prevent actual API calls
        monkeypatch.setattr(
            "github2gerrit.core.build_client",
            Mock(side_effect=Exception("No API access")),
        )

        # Execute
        change_ids = orch._find_existing_change_for_pr(gh, gerrit)

        # Verify
        assert change_ids == []


class TestEnforceExistingChange:
    """Tests for _enforce_existing_change_for_update method."""

    def test_enforce_raises_error_when_no_change_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify error raised when UPDATE requires existing change but none found."""
        orch = Orchestrator(workspace=tmp_path)

        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="synchronize",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        # Mock _find_existing_change_for_pr to return empty
        monkeypatch.setattr(
            orch,
            "_find_existing_change_for_pr",
            lambda gh, gerrit: [],
        )

        # Execute and verify exception
        with pytest.raises(OrchestratorError) as exc_info:
            orch._enforce_existing_change_for_update(gh, gerrit)

        error_msg = str(exc_info.value)
        assert "UPDATE operation requires existing Gerrit change" in error_msg
        assert "GH-owner-repo-42" in error_msg
        assert "PR #42" in error_msg

    def test_enforce_returns_change_ids_when_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify Change-IDs returned when existing change found."""
        orch = Orchestrator(workspace=tmp_path)

        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="synchronize",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        expected_ids = ["I1234567890abcdef", "Ifedcba0987654321"]

        # Mock _find_existing_change_for_pr to return change IDs
        monkeypatch.setattr(
            orch,
            "_find_existing_change_for_pr",
            lambda gh, gerrit: expected_ids,
        )

        # Execute
        change_ids = orch._enforce_existing_change_for_update(gh, gerrit)

        # Verify
        assert change_ids == expected_ids


class TestVerifyPatchsetCreation:
    """Tests for _verify_patchset_creation method."""

    def test_verify_patchset_logs_success_for_updated_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify successful logging when patchset > 1 (update)."""
        orch = Orchestrator(workspace=tmp_path)

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        change_ids = ["I1234567890abcdef"]

        # Mock REST client to return change with patchset 2
        mock_client = Mock()
        mock_client.get.return_value = {
            "_number": "12345",
            "change_id": "I1234567890abcdef",
            "subject": "Update dependencies",
            "status": "NEW",
            "current_revision": "rev123",
            "revisions": {
                "rev123": {
                    "_number": 2,
                }
            },
        }

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.GerritRestClient",
            Mock(return_value=mock_client),
        )

        # Execute (should not raise)
        orch._verify_patchset_creation(gerrit, change_ids, "update")

        # Verify results were stored
        assert hasattr(orch, "_verification_results")
        results = orch._verification_results
        assert len(results) == 1
        assert results[0]["patchset"] == 2
        assert results[0]["verified"] is True

    def test_verify_patchset_warns_for_patchset_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify warning logged when patchset = 1 (may be new change)."""
        orch = Orchestrator(workspace=tmp_path)

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        change_ids = ["I1234567890abcdef"]

        # Mock REST client to return change with patchset 1
        mock_client = Mock()
        mock_client.get.return_value = {
            "_number": "12345",
            "change_id": "I1234567890abcdef",
            "subject": "New change",
            "status": "NEW",
            "current_revision": "rev123",
            "revisions": {
                "rev123": {
                    "_number": 1,
                }
            },
        }

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.GerritRestClient",
            Mock(return_value=mock_client),
        )

        # Execute (should not raise but will log warning)
        orch._verify_patchset_creation(gerrit, change_ids, "update")

        # Verify results show patchset 1
        results = orch._verification_results
        assert results[0]["patchset"] == 1


class TestMetadataSync:
    """Tests for Gerrit change metadata sync functionality."""

    def test_sync_updates_title_when_different(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify metadata sync updates Gerrit when PR title differs."""
        orch = Orchestrator(workspace=tmp_path)

        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="edited",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        change_ids = ["I1234567890abcdef"]

        # Mock GitHub PR
        mock_pr = Mock()
        mock_pr.title = "Updated PR Title"
        mock_pr.body = "PR description"

        # Mock git_show to return a commit message that matches PR title
        # (so it uses PR title/body for sync)
        mock_commit_msg = (
            "Updated PR Title\n\nPR description\n\nChange-Id: I1234567890abcdef"
        )
        monkeypatch.setattr(
            "github2gerrit.core.git_show",
            lambda *args, **kwargs: mock_commit_msg,
        )

        # Mock GitHub API calls
        monkeypatch.setattr(
            "github2gerrit.core.build_client",
            Mock,
        )
        monkeypatch.setattr(
            "github2gerrit.core.get_repo_from_env",
            Mock(return_value=Mock()),
        )
        monkeypatch.setattr(
            "github2gerrit.core.get_pull",
            lambda repo, num: mock_pr,
        )
        monkeypatch.setattr(
            "github2gerrit.core.get_pr_title_body",
            lambda pr: ("Updated PR Title", "PR description"),
        )

        # Mock Gerrit REST client
        mock_client = Mock()
        mock_client.get.return_value = {
            "subject": "Old Gerrit Title",
            "status": "NEW",
            "current_revision": "rev1",
            "revisions": {
                "rev1": {
                    "commit": {
                        "message": "Old Gerrit Title\n\nOld body\n\nChange-Id: I1234567890abcdef"
                    }
                }
            },
        }

        update_called = []

        def mock_put(path, data):
            update_called.append((path, data))
            return {"ok": True}

        mock_client.put = mock_put

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.GerritRestClient",
            Mock(return_value=mock_client),
        )

        # Mock environment variables for credentials
        monkeypatch.setenv("GERRIT_HTTP_USER", "testuser")
        monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "testpass")

        # Execute
        orch._sync_gerrit_change_metadata(gh, gerrit, change_ids)

        # Verify update was attempted
        assert len(update_called) > 0
        assert "message" in update_called[0][1]
        assert "Updated PR Title" in update_called[0][1]["message"]

    def test_sync_skips_when_titles_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify no update when Gerrit subject matches PR title."""
        orch = Orchestrator(workspace=tmp_path)

        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="edited",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        change_ids = ["I1234567890abcdef"]

        # Mock GitHub PR with same title
        mock_pr = Mock()
        mock_pr.title = "Same Title"
        mock_pr.body = "Description"

        # Mock git_show to return a commit message that matches PR title
        mock_commit_msg = (
            "Same Title\n\nDescription\n\nChange-Id: I1234567890abcdef"
        )
        monkeypatch.setattr(
            "github2gerrit.core.git_show",
            lambda *args, **kwargs: mock_commit_msg,
        )

        # Mock GitHub with matching subject
        monkeypatch.setattr(
            "github2gerrit.core.build_client",
            Mock,
        )
        monkeypatch.setattr(
            "github2gerrit.core.get_repo_from_env",
            Mock(return_value=Mock()),
        )
        monkeypatch.setattr(
            "github2gerrit.core.get_pull",
            lambda repo, num: mock_pr,
        )
        monkeypatch.setattr(
            "github2gerrit.core.get_pr_title_body",
            lambda pr: ("Same Title", "Description"),
        )

        # Mock Gerrit REST client
        mock_client = Mock()
        mock_client.get.return_value = {
            "subject": "Same Title",
            "status": "NEW",
            "current_revision": "rev1",
            "revisions": {
                "rev1": {
                    "commit": {
                        "message": "Same Title\n\nDescription\n\nChange-Id: I1234567890abcdef"
                    }
                }
            },
        }

        update_called = []
        mock_client.put = lambda path, data: update_called.append(True)

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.GerritRestClient",
            Mock(return_value=mock_client),
        )

        # Execute
        orch._sync_gerrit_change_metadata(gh, gerrit, change_ids)

        # Verify no update was called
        assert len(update_called) == 0


class TestG2GMetadataBlock:
    """Tests for GitHub2Gerrit metadata block in commit messages."""

    def test_metadata_block_included_in_squash_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify G2G metadata block is included in squash commit messages."""
        orch = Orchestrator(workspace=tmp_path)

        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        from github2gerrit.models import Inputs

        inputs = Inputs(
            submit_single_commits=False,
            use_pr_as_commit=False,
            fetch_depth=10,
            gerrit_known_hosts="",
            gerrit_ssh_privkey_g2g="",
            gerrit_ssh_user_g2g="testuser",
            gerrit_ssh_user_g2g_email="test@example.com",
            github_token="token",  # noqa: S106
            organization="owner",
            reviewers_email="",
            preserve_github_prs=True,
            dry_run=False,
            normalise_commit=False,
            gerrit_server="gerrit.example.org",
            gerrit_server_port=29418,
            gerrit_project="test/project",
            issue_id="",
            issue_id_lookup_json="[]",
            allow_duplicates=False,
            ci_testing=False,
        )

        # Build a commit message with metadata
        result = orch._build_commit_message_with_trailers(
            base_message="Update dependencies\n\nThis updates all deps.",
            inputs=inputs,
            gh=gh,
            change_id="I1234567890abcdef",
            preserve_existing=True,
            include_g2g_metadata=True,
            g2g_mode="squash",
            g2g_topic="GH-owner-repo-42",
            g2g_change_ids=["I1234567890abcdef"],
        )

        # Verify metadata block is present
        assert "GitHub2Gerrit Metadata:" in result
        assert "Mode: squash" in result
        assert "Topic: GH-owner-repo-42" in result

        # Verify it comes before trailers
        assert result.index("GitHub2Gerrit Metadata:") < result.index(
            "Change-Id:"
        )

    def test_metadata_block_included_in_multi_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify G2G metadata block is included in multi-commit messages."""
        orch = Orchestrator(workspace=tmp_path)

        gh = GitHubContext(
            event_name="pull_request_target",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        from github2gerrit.models import Inputs

        inputs = Inputs(
            submit_single_commits=True,
            use_pr_as_commit=False,
            fetch_depth=10,
            gerrit_known_hosts="",
            gerrit_ssh_privkey_g2g="",
            gerrit_ssh_user_g2g="testuser",
            gerrit_ssh_user_g2g_email="test@example.com",
            github_token="token",  # noqa: S106
            organization="owner",
            reviewers_email="",
            preserve_github_prs=True,
            dry_run=False,
            normalise_commit=False,
            gerrit_server="gerrit.example.org",
            gerrit_server_port=29418,
            gerrit_project="test/project",
            issue_id="",
            issue_id_lookup_json="[]",
            allow_duplicates=False,
            ci_testing=False,
        )

        # Build a commit message with multi-commit metadata
        result = orch._build_commit_message_with_trailers(
            base_message="Fix bug in module",
            inputs=inputs,
            gh=gh,
            change_id="I1234567890abcdef",
            preserve_existing=True,
            include_g2g_metadata=True,
            g2g_mode="multi-commit",
            g2g_topic="GH-owner-repo-42",
            g2g_change_ids=["I1234567890abcdef", "Ifedcba0987654321"],
        )

        # Verify metadata block is present
        assert "GitHub2Gerrit Metadata:" in result
        assert "Mode: multi-commit" in result
        assert "Topic: GH-owner-repo-42" in result
        assert "Change-Ids: I1234567890abcdef, Ifedcba0987654321" in result

    def test_metadata_sync_preserves_g2g_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify metadata sync preserves G2G metadata block."""
        orch = Orchestrator(workspace=tmp_path)

        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="test/project",
        )

        # Mock current change with G2G metadata block
        mock_client = Mock()
        mock_client.get.return_value = {
            "subject": "Old Title",
            "status": "NEW",
            "current_revision": "rev123",
            "revisions": {
                "rev123": {
                    "commit": {
                        "message": """Old Title

Old description here.

GitHub2Gerrit Metadata:
Mode: squash
Topic: GH-owner-repo-42
Digest: abc123def456

Change-Id: I1234567890abcdef
GitHub-PR: https://github.com/owner/repo/pull/42
GitHub-Hash: e24c5d88ac357ccc"""
                    }
                }
            },
        }

        update_result = None

        def mock_put(path, data):
            nonlocal update_result
            update_result = data
            return {"ok": True}

        mock_client.put = mock_put

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.GerritRestClient",
            Mock(return_value=mock_client),
        )

        # Mock environment variables
        monkeypatch.setenv("GERRIT_HTTP_USER", "testuser")
        monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "testpass")

        # Execute update with new title
        success = orch._update_gerrit_change_metadata(
            gerrit=gerrit,
            change_id="I1234567890abcdef",
            title="New Title",
            description="New description here.",
        )

        # Verify update succeeded
        assert success is True
        assert update_result is not None

        # Verify G2G metadata block was preserved
        new_message = update_result["message"]
        assert "GitHub2Gerrit Metadata:" in new_message
        assert "Mode: squash" in new_message
        assert "Topic: GH-owner-repo-42" in new_message
        assert "Digest: abc123def456" in new_message

        # Verify trailers were preserved
        assert "Change-Id: I1234567890abcdef" in new_message
        assert "GitHub-PR: https://github.com/owner/repo/pull/42" in new_message
        assert "GitHub-Hash: e24c5d88ac357ccc" in new_message

        # Verify new title and description are present
        assert "New Title" in new_message
        assert "New description here." in new_message
