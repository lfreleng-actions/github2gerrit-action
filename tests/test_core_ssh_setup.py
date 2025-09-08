# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for the SSH setup functionality in core.py.

These tests verify that the SSH setup is non-invasive and doesn't
modify user SSH configuration while still providing secure access.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import TypeVar
from unittest.mock import patch

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.models import Inputs


def snapshot_dir_state(directory: Path) -> dict[str, str]:
    """Capture the state of a directory including file contents and permissions.

    Returns a dict mapping file paths to their content hashes and permissions.
    """
    if not directory.exists():
        return {}

    state = {}
    for item in directory.rglob("*"):
        if item.is_file():
            try:
                # Create a hash of file content and permissions
                content = item.read_bytes()
                permissions = oct(item.stat().st_mode)
                content_hash = hashlib.sha256(content).hexdigest()
                state[str(item.relative_to(directory))] = f"{content_hash}:{permissions}"
            except (OSError, PermissionError):
                # Skip files we can't read
                continue
    return state


if TYPE_CHECKING:
    F = TypeVar("F", bound=Callable[..., object])

    def fixture(*args: object, **kwargs: object) -> Callable[[F], F]: ...
    def parametrize(*args: object, **kwargs: object) -> Callable[[F], F]: ...
else:
    from pytest import fixture as _fixture
    from pytest import mark as _mark

    fixture = _fixture
    parametrize = _mark.parametrize


@fixture
def minimal_inputs() -> Inputs:
    return Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="gerrit.example.org ssh-rsa AAAAB3NzaC1yc2E...",
        gerrit_ssh_privkey_g2g="-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "fake_key_content\n"
        "-----END OPENSSH PRIVATE KEY-----",
        gerrit_ssh_user_g2g="gerrit-bot",
        gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
        organization="example",
        reviewers_email="",
        preserve_github_prs=False,
        dry_run=False,
        normalise_commit=True,
        gerrit_server="",
        gerrit_server_port="29418",
        gerrit_project="",
        issue_id="",
        allow_duplicates=False,
        ci_testing=False,
    )


def test_ssh_setup_creates_workspace_specific_files(tmp_path: Path, minimal_inputs: Inputs) -> None:
    """SSH setup should create files in workspace, not user SSH directory."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create fake user SSH directory to ensure it's not modified
    user_ssh_dir = Path.home() / ".ssh"
    original_user_ssh_exists = user_ssh_dir.exists()
    original_user_ssh_state = snapshot_dir_state(user_ssh_dir)

    orch = Orchestrator(workspace=workspace)

    # Act
    gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")
    orch._setup_ssh(minimal_inputs, gerrit_info)

    # Assert: tool-specific SSH directory is created
    tool_ssh_dir = workspace / ".ssh-g2g"
    assert tool_ssh_dir.exists()
    assert tool_ssh_dir.is_dir()
    assert oct(tool_ssh_dir.stat().st_mode)[-3:] == "700"

    # Assert: SSH key is created in tool directory
    key_path = tool_ssh_dir / "gerrit_key"
    assert key_path.exists()
    assert key_path.is_file()
    assert oct(key_path.stat().st_mode)[-3:] == "600"

    # Assert: known hosts is created in tool directory
    known_hosts_path = tool_ssh_dir / "known_hosts"
    assert known_hosts_path.exists()
    assert known_hosts_path.is_file()

    # Assert: user SSH directory is not modified
    user_ssh_modified = user_ssh_dir.exists() != original_user_ssh_exists
    assert not user_ssh_modified, "User SSH directory should not be modified"

    # Assert: user SSH directory contents are not modified
    new_user_ssh_state = snapshot_dir_state(user_ssh_dir)
    assert original_user_ssh_state == new_user_ssh_state, "User SSH directory contents should not be modified"

    # Assert: user's id_rsa is not touched
    user_id_rsa = user_ssh_dir / "id_rsa"
    if user_id_rsa.exists():
        # If user has id_rsa, check it wasn't modified
        content = user_id_rsa.read_text()
        assert "fake_key_content" not in content


def test_ssh_setup_skips_when_credentials_missing(tmp_path: Path) -> None:
    """SSH setup should skip when SSH credentials are not provided."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orch = Orchestrator(workspace=workspace)

    inputs_no_key = Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="",
        gerrit_ssh_privkey_g2g="",  # Missing private key
        gerrit_ssh_user_g2g="gerrit-bot",
        gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
        organization="example",
        reviewers_email="",
        preserve_github_prs=False,
        dry_run=False,
        normalise_commit=True,
        gerrit_server="gerrit.example.org",
        gerrit_server_port="29418",
        gerrit_project="example/project",
        issue_id="",
        allow_duplicates=False,
        ci_testing=False,
    )

    # Act
    gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")
    orch._setup_ssh(inputs_no_key, gerrit_info)

    # Assert: no SSH directory is created
    tool_ssh_dir = workspace / ".ssh-g2g"
    assert not tool_ssh_dir.exists()

    # Assert: SSH command is None
    assert orch._build_git_ssh_command is None


def test_git_ssh_command_prevents_agent_scanning(tmp_path: Path, minimal_inputs: Inputs) -> None:
    """Generated SSH command should prevent SSH agent scanning."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orch = Orchestrator(workspace=workspace)

    # Act
    gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")
    orch._setup_ssh(minimal_inputs, gerrit_info)
    ssh_cmd = orch._build_git_ssh_command

    # Assert: SSH command is generated
    assert ssh_cmd is not None
    assert ssh_cmd.startswith("ssh ")

    # Assert: critical options are present
    assert "-o IdentitiesOnly=yes" in ssh_cmd
    assert "-o StrictHostKeyChecking=yes" in ssh_cmd
    assert "-o PasswordAuthentication=no" in ssh_cmd

    # Assert: tool-specific files are referenced
    assert str(workspace / ".ssh-g2g" / "gerrit_key") in ssh_cmd
    assert str(workspace / ".ssh-g2g" / "known_hosts") in ssh_cmd


def test_ssh_cleanup_removes_temporary_files(tmp_path: Path, minimal_inputs: Inputs) -> None:
    """SSH cleanup should remove all temporary files."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orch = Orchestrator(workspace=workspace)

    # Setup SSH files
    gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")
    orch._setup_ssh(minimal_inputs, gerrit_info)
    tool_ssh_dir = workspace / ".ssh-g2g"
    assert tool_ssh_dir.exists()

    # Act
    orch._cleanup_ssh()

    # Assert: temporary directory is removed
    assert not tool_ssh_dir.exists()


def test_ssh_cleanup_handles_missing_files_gracefully(tmp_path: Path) -> None:
    """SSH cleanup should handle missing files without error."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orch = Orchestrator(workspace=workspace)

    # Act: cleanup without setup (no files to clean)
    # Should not raise an exception
    orch._cleanup_ssh()

    # Assert: no errors occurred (test passes if no exception raised)
    assert True


def test_ssh_setup_preserves_existing_user_config(tmp_path: Path, minimal_inputs: Inputs) -> None:
    """SSH setup should not modify existing user SSH configuration."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create a fake user SSH config to protect
    fake_user_ssh = tmp_path / "fake_user_ssh"
    fake_user_ssh.mkdir(mode=0o700)
    fake_config = fake_user_ssh / "config"
    fake_config.write_text("Host example.com\n    User testuser\n")
    original_config = fake_config.read_text()

    orch = Orchestrator(workspace=workspace)

    # Mock Path.home() to point to our fake directory
    with patch("pathlib.Path.home", return_value=tmp_path):
        # Act
        gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")
        orch._setup_ssh(minimal_inputs, gerrit_info)

        # Assert: user SSH config is unchanged
        if fake_config.exists():
            current_config = fake_config.read_text()
            assert current_config == original_config


def test_ssh_auto_discovery_integration(tmp_path: Path) -> None:
    """Test SSH auto-discovery integration with core orchestrator."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orch = Orchestrator(workspace=workspace)

    inputs_without_known_hosts = Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="",  # Empty - should trigger auto-discovery
        gerrit_ssh_privkey_g2g="-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "fake_key_content\n"
        "-----END OPENSSH PRIVATE KEY-----",
        gerrit_ssh_user_g2g="gerrit-bot",
        gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
        organization="testorg",
        reviewers_email="",
        preserve_github_prs=False,
        dry_run=False,
        normalise_commit=True,
        gerrit_server="",
        gerrit_server_port="29418",
        gerrit_project="",
        issue_id="",
        allow_duplicates=False,
        ci_testing=False,
    )

    gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")

    # Mock the auto-discovery function
    with patch("github2gerrit.core.auto_discover_gerrit_host_keys") as mock_autodiscover:
        mock_autodiscover.return_value = "gerrit.example.org ssh-rsa AAAAB3NzaC1yc2EAUTO_DISCOVERED_KEY"

        # Act
        orch._setup_ssh(inputs_without_known_hosts, gerrit_info)

        # Assert: auto-discovery was called
        mock_autodiscover.assert_called_once_with(
            gerrit_hostname="gerrit.example.org",
            gerrit_port=29418,
            organization="testorg",
            save_to_config=True,
        )

        # Assert: SSH setup completed with auto-discovered keys
        tool_ssh_dir = workspace / ".ssh-g2g"
        assert tool_ssh_dir.exists()

        known_hosts_path = tool_ssh_dir / "known_hosts"
        assert known_hosts_path.exists()

        # Verify auto-discovered keys were used
        known_hosts_content = known_hosts_path.read_text()
        assert "AUTO_DISCOVERED_KEY" in known_hosts_content


def test_ssh_auto_discovery_fallback_when_discovery_fails(
    tmp_path: Path,
) -> None:
    """Test SSH setup continues gracefully when auto-discovery fails."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orch = Orchestrator(workspace=workspace)

    inputs_without_known_hosts = Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="",  # Empty - should trigger auto-discovery
        gerrit_ssh_privkey_g2g="-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "fake_key_content\n"
        "-----END OPENSSH PRIVATE KEY-----",
        gerrit_ssh_user_g2g="gerrit-bot",
        gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
        organization="testorg",
        reviewers_email="",
        preserve_github_prs=False,
        dry_run=False,
        normalise_commit=True,
        gerrit_server="",
        gerrit_server_port="29418",
        gerrit_project="",
        issue_id="",
        allow_duplicates=False,
        ci_testing=False,
    )

    gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")

    # Mock auto-discovery to fail
    with patch("github2gerrit.core.auto_discover_gerrit_host_keys") as mock_autodiscover:
        mock_autodiscover.return_value = None  # Discovery failed

        # Act
        orch._setup_ssh(inputs_without_known_hosts, gerrit_info)

        # Assert: auto-discovery was attempted
        mock_autodiscover.assert_called_once()

        # Assert: SSH setup was skipped due to no host keys
        tool_ssh_dir = workspace / ".ssh-g2g"
        assert not tool_ssh_dir.exists()

        # Assert: SSH command is None
        assert orch._build_git_ssh_command is None


def test_ssh_setup_augments_provided_known_hosts_with_autodiscovery(
    tmp_path: Path,
) -> None:
    """Test that provided known_hosts are augmented with auto-discovery when needed."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orch = Orchestrator(workspace=workspace)

    inputs_with_known_hosts = Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="gerrit.example.org ssh-rsa MANUALLY_PROVIDED_KEY",
        gerrit_ssh_privkey_g2g="-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "fake_key_content\n"
        "-----END OPENSSH PRIVATE KEY-----",
        gerrit_ssh_user_g2g="gerrit-bot",
        gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
        organization="testorg",
        reviewers_email="",
        preserve_github_prs=False,
        dry_run=False,
        normalise_commit=True,
        gerrit_server="",
        gerrit_server_port="29418",
        gerrit_project="",
        issue_id="",
        allow_duplicates=False,
        ci_testing=False,
    )

    gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")

    # Mock auto-discovery to return additional keys for augmentation
    with patch("github2gerrit.core.auto_discover_gerrit_host_keys") as mock_autodiscover:
        mock_autodiscover.return_value = "[gerrit.example.org]:29418 ssh-ed25519 AUTO_DISCOVERED_KEY"

        # Act
        orch._setup_ssh(inputs_with_known_hosts, gerrit_info)

        # Assert: auto-discovery was called for augmentation (missing [host]:port format)
        mock_autodiscover.assert_called_once_with(
            gerrit_hostname="gerrit.example.org", gerrit_port=29418, organization="testorg", save_to_config=True
        )

        # Assert: both provided and discovered keys were used
        tool_ssh_dir = workspace / ".ssh-g2g"
        assert tool_ssh_dir.exists()

        known_hosts_path = tool_ssh_dir / "known_hosts"
        assert known_hosts_path.exists()

        known_hosts_content = known_hosts_path.read_text()
        assert "MANUALLY_PROVIDED_KEY" in known_hosts_content
        assert "AUTO_DISCOVERED_KEY" in known_hosts_content


def test_ssh_command_isolation_from_environment(tmp_path: Path, minimal_inputs: Inputs) -> None:
    """SSH command should be isolated from SSH agent environment."""
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orch = Orchestrator(workspace=workspace)

    # Setup with SSH agent environment variables
    original_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    original_agent_pid = os.environ.get("SSH_AGENT_PID")

    try:
        os.environ["SSH_AUTH_SOCK"] = str(tmp_path / "fake_ssh_agent")
        os.environ["SSH_AGENT_PID"] = "12345"

        # Act
        gerrit_info = GerritInfo(host="gerrit.example.org", port=29418, project="test/repo")
        orch._setup_ssh(minimal_inputs, gerrit_info)
        ssh_cmd = orch._build_git_ssh_command

        # Assert: IdentitiesOnly prevents agent usage regardless of env
        assert ssh_cmd is not None
        assert "-o IdentitiesOnly=yes" in ssh_cmd

    finally:
        # Cleanup environment
        if original_auth_sock is not None:
            os.environ["SSH_AUTH_SOCK"] = original_auth_sock
        elif "SSH_AUTH_SOCK" in os.environ:
            del os.environ["SSH_AUTH_SOCK"]

        if original_agent_pid is not None:
            os.environ["SSH_AGENT_PID"] = original_agent_pid
        elif "SSH_AGENT_PID" in os.environ:
            del os.environ["SSH_AGENT_PID"]
