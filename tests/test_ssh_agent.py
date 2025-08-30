# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for SSH agent-based authentication."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from github2gerrit.ssh_agent_setup import SSHAgentError
from github2gerrit.ssh_agent_setup import SSHAgentManager
from github2gerrit.ssh_agent_setup import setup_ssh_agent_auth


# Test constants
TEST_SSH_SOCK = "/var/folders/test/ssh-agent.sock"
TEST_SSH_PID = 12345


class TestSSHAgentManager:
    """Test SSH agent manager functionality."""

    def test_ssh_agent_manager_init(self, tmp_path: Path) -> None:
        """Test SSH agent manager initialization."""
        manager = SSHAgentManager(tmp_path)
        assert manager.workspace == tmp_path
        assert manager.agent_pid is None
        assert manager.auth_sock is None
        assert manager.known_hosts_path is None

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_start_agent_success(self, mock_run_cmd: Mock, tmp_path: Path) -> None:
        """Test successful SSH agent startup."""
        mock_run_cmd.return_value = Mock(stdout=f"SSH_AUTH_SOCK={TEST_SSH_SOCK};\nSSH_AGENT_PID={TEST_SSH_PID};\n")

        manager = SSHAgentManager(tmp_path)
        manager.start_agent()

        assert manager.auth_sock == TEST_SSH_SOCK
        assert manager.agent_pid == TEST_SSH_PID
        assert os.environ.get("SSH_AUTH_SOCK") == TEST_SSH_SOCK
        assert os.environ.get("SSH_AGENT_PID") == str(TEST_SSH_PID)

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_start_agent_parse_failure(self, mock_run_cmd: Mock, tmp_path: Path) -> None:
        """Test SSH agent startup with parsing failure."""
        mock_run_cmd.return_value = Mock(stdout="invalid output")

        manager = SSHAgentManager(tmp_path)

        with pytest.raises(SSHAgentError, match="Failed to parse ssh-agent output"):
            manager.start_agent()

    @patch("subprocess.Popen")
    def test_add_key_success(self, mock_popen: Mock, tmp_path: Path) -> None:
        """Test successful key addition to SSH agent."""
        # Setup manager with agent running
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        # Mock successful ssh-add
        mock_process = Mock()
        mock_process.communicate.return_value = ("Identity added\n", "")
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        test_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest_key_content\n-----END OPENSSH PRIVATE KEY-----"

        manager.add_key(test_key)

        # Verify ssh-add was called correctly
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["/usr/bin/ssh-add", "-"]
        assert call_args[1]["stdin"] == subprocess.PIPE

    def test_add_key_no_agent(self, tmp_path: Path) -> None:
        """Test adding key when no agent is running."""
        manager = SSHAgentManager(tmp_path)

        with pytest.raises(SSHAgentError, match="SSH agent not started"):
            manager.add_key("test_key")

    @patch("subprocess.Popen")
    def test_add_key_failure(self, mock_popen: Mock, tmp_path: Path) -> None:
        """Test key addition failure."""
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        # Mock failed ssh-add
        mock_process = Mock()
        mock_process.communicate.return_value = ("", "Error loading key")
        mock_process.returncode = 1
        mock_popen.return_value = mock_process

        with pytest.raises(SSHAgentError, match="ssh-add failed"):
            manager.add_key("test_key")

    def test_setup_known_hosts(self, tmp_path: Path) -> None:
        """Test known hosts setup."""
        manager = SSHAgentManager(tmp_path)
        known_hosts_content = "gerrit.example.com ssh-rsa AAAAB3NzaC1yc2E..."

        manager.setup_known_hosts(known_hosts_content)

        expected_path = tmp_path / ".ssh-g2g" / "known_hosts"
        assert expected_path.exists()
        assert expected_path.read_text() == known_hosts_content.strip() + "\n"
        assert manager.known_hosts_path == expected_path

    def test_get_git_ssh_command(self, tmp_path: Path) -> None:
        """Test SSH command generation."""
        manager = SSHAgentManager(tmp_path)

        # Setup known hosts
        known_hosts_path = tmp_path / ".ssh-g2g" / "known_hosts"
        known_hosts_path.parent.mkdir(parents=True)
        known_hosts_path.touch()
        manager.known_hosts_path = known_hosts_path

        command = manager.get_git_ssh_command()

        assert "ssh" in command
        assert f"-o UserKnownHostsFile={known_hosts_path}" in command
        assert "-o IdentitiesOnly=no" in command  # Allow SSH agent
        assert "-o BatchMode=yes" in command

    def test_get_git_ssh_command_no_known_hosts(self, tmp_path: Path) -> None:
        """Test SSH command generation without known hosts."""
        manager = SSHAgentManager(tmp_path)

        with pytest.raises(SSHAgentError, match="Known hosts not configured"):
            manager.get_git_ssh_command()

    def test_get_ssh_env(self, tmp_path: Path) -> None:
        """Test SSH environment variables."""
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        env = manager.get_ssh_env()

        assert env["SSH_AUTH_SOCK"] == TEST_SSH_SOCK
        assert env["SSH_AGENT_PID"] == str(TEST_SSH_PID)

    def test_get_ssh_env_no_agent(self, tmp_path: Path) -> None:
        """Test SSH environment when no agent is running."""
        manager = SSHAgentManager(tmp_path)

        with pytest.raises(SSHAgentError, match="SSH agent not started"):
            manager.get_ssh_env()

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_list_keys_success(self, mock_run_cmd: Mock, tmp_path: Path) -> None:
        """Test listing keys in agent."""
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        mock_run_cmd.return_value = Mock(stdout="2048 SHA256:abc123 test@example.com (RSA)")

        result = manager.list_keys()

        assert "2048 SHA256:abc123" in result

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_list_keys_no_keys(self, mock_run_cmd: Mock, tmp_path: Path) -> None:
        """Test listing keys when none are loaded."""
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        from github2gerrit.gitutils import CommandError

        mock_run_cmd.side_effect = CommandError(
            "The agent has no identities.",
            cmd=["ssh-add", "-l"],
            returncode=1,
            stdout="",
            stderr="The agent has no identities.",
        )

        result = manager.list_keys()

        assert result == "No keys loaded"

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_cleanup(self, mock_run_cmd: Mock, tmp_path: Path) -> None:
        """Test cleanup of SSH agent and files."""
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        # Create temporary SSH directory
        tool_ssh_dir = tmp_path / ".ssh-g2g"
        tool_ssh_dir.mkdir()
        (tool_ssh_dir / "known_hosts").touch()

        # Store original env
        original_auth_sock = os.environ.get("SSH_AUTH_SOCK")
        os.environ["SSH_AUTH_SOCK"] = TEST_SSH_SOCK
        os.environ["SSH_AGENT_PID"] = str(TEST_SSH_PID)
        manager._original_env = {"SSH_AUTH_SOCK": original_auth_sock or "", "SSH_AGENT_PID": ""}

        manager.cleanup()

        # Verify agent was killed
        mock_run_cmd.assert_called_with(["/bin/kill", str(TEST_SSH_PID)], timeout=5)

        # Verify temporary files were removed
        assert not tool_ssh_dir.exists()

        # Verify state was reset
        assert manager.agent_pid is None
        assert manager.auth_sock is None  # type: ignore[unreachable]
        assert manager.known_hosts_path is None


class TestSSHAgentSetup:
    """Test high-level SSH agent setup function."""

    @patch("github2gerrit.ssh_agent_setup.SSHAgentManager")
    def test_setup_ssh_agent_auth_success(self, mock_manager_class: Mock, tmp_path: Path) -> None:
        """Test successful SSH agent authentication setup."""
        mock_manager = Mock()
        mock_manager.list_keys.return_value = "2048 SHA256:abc123 test@example.com (RSA)"
        mock_manager_class.return_value = mock_manager

        private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----"
        known_hosts = "gerrit.example.com ssh-rsa AAAAB3NzaC1yc2E..."

        result = setup_ssh_agent_auth(tmp_path, private_key, known_hosts)

        assert result == mock_manager
        mock_manager.start_agent.assert_called_once()
        mock_manager.add_key.assert_called_once_with(private_key)
        mock_manager.setup_known_hosts.assert_called_once_with(known_hosts)

    @patch("github2gerrit.ssh_agent_setup.SSHAgentManager")
    def test_setup_ssh_agent_auth_no_keys_loaded(self, mock_manager_class: Mock, tmp_path: Path) -> None:
        """Test setup failure when no keys are loaded."""
        mock_manager = Mock()
        mock_manager.list_keys.return_value = "No keys loaded"
        mock_manager_class.return_value = mock_manager

        private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----"
        known_hosts = "gerrit.example.com ssh-rsa AAAAB3NzaC1yc2E..."

        with pytest.raises(SSHAgentError, match="No keys were loaded into SSH agent"):
            setup_ssh_agent_auth(tmp_path, private_key, known_hosts)

        # Verify cleanup was called on failure
        mock_manager.cleanup.assert_called_once()

    @patch("github2gerrit.ssh_agent_setup.SSHAgentManager")
    def test_setup_ssh_agent_auth_failure_cleanup(self, mock_manager_class: Mock, tmp_path: Path) -> None:
        """Test cleanup on setup failure."""
        mock_manager = Mock()
        mock_manager.start_agent.side_effect = SSHAgentError("Test failure")
        mock_manager_class.return_value = mock_manager

        private_key = "test_key"
        known_hosts = "test_hosts"

        with pytest.raises(SSHAgentError):
            setup_ssh_agent_auth(tmp_path, private_key, known_hosts)

        # Verify cleanup was called on failure
        mock_manager.cleanup.assert_called_once()


@pytest.mark.integration
class TestSSHAgentIntegration:
    """Integration tests for SSH agent functionality."""

    def test_ssh_agent_real_workflow(self, tmp_path: Path) -> None:
        """Test real SSH agent workflow (requires ssh-agent)."""
        pytest.importorskip("subprocess")

        # Skip if ssh-agent is not available
        try:
            import shutil

            if not shutil.which("ssh-agent"):
                pytest.skip("ssh-agent not available")
        except Exception:
            pytest.skip("ssh-agent not available")

        manager = SSHAgentManager(tmp_path)

        try:
            # This would normally fail without a real key, but we test the workflow
            manager.start_agent()
            assert manager.auth_sock is not None
            assert manager.agent_pid is not None

            # Test known hosts setup
            known_hosts = "gerrit.example.com ssh-rsa AAAAB3NzaC1yc2E..."
            manager.setup_known_hosts(known_hosts)
            assert manager.known_hosts_path is not None
            assert manager.known_hosts_path.exists()

            # Test command generation
            command = manager.get_git_ssh_command()
            assert "ssh" in command

        finally:
            manager.cleanup()


class TestSSHPathDiscovery:
    """Test SSH executable path discovery functionality."""

    @patch("shutil.which")
    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_start_agent_with_path_discovery(self, mock_run_cmd: Mock, mock_which: Mock, tmp_path: Path) -> None:
        """Test SSH agent startup with path discovery."""
        # Mock shutil.which to return a custom path
        mock_which.return_value = "/custom/path/ssh-agent"
        mock_run_cmd.return_value = Mock(stdout=f"SSH_AUTH_SOCK={TEST_SSH_SOCK};\nSSH_AGENT_PID={TEST_SSH_PID};\n")

        manager = SSHAgentManager(tmp_path)
        manager.start_agent()

        # Verify shutil.which was called
        mock_which.assert_called_once_with("ssh-agent")
        # Verify run_cmd was called with the discovered path
        mock_run_cmd.assert_called_once_with(["/custom/path/ssh-agent", "-s"], timeout=10)

    @patch("shutil.which")
    def test_start_agent_ssh_agent_not_found(self, mock_which: Mock, tmp_path: Path) -> None:
        """Test SSH agent startup when ssh-agent is not found."""
        # Mock shutil.which to return None (not found)
        mock_which.return_value = None

        manager = SSHAgentManager(tmp_path)

        with pytest.raises(SSHAgentError, match="ssh-agent not found in PATH"):
            manager.start_agent()

        mock_which.assert_called_once_with("ssh-agent")

    @patch("shutil.which")
    @patch("subprocess.Popen")
    def test_add_key_with_path_discovery(self, mock_popen: Mock, mock_which: Mock, tmp_path: Path) -> None:
        """Test key addition with ssh-add path discovery."""
        # Setup manager with agent running
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        # Mock shutil.which to return a custom path
        mock_which.return_value = "/custom/path/ssh-add"

        # Mock successful ssh-add
        mock_process = Mock()
        mock_process.communicate.return_value = ("Identity added\n", "")
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        manager.add_key("test_key")

        # Verify shutil.which was called
        mock_which.assert_called_once_with("ssh-add")
        # Verify Popen was called with the discovered path
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["/custom/path/ssh-add", "-"]

    @patch("shutil.which")
    def test_add_key_ssh_add_not_found(self, mock_which: Mock, tmp_path: Path) -> None:
        """Test key addition when ssh-add is not found."""
        # Setup manager with agent running
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        # Mock shutil.which to return None (not found)
        mock_which.return_value = None

        with pytest.raises(SSHAgentError, match="ssh-add not found in PATH"):
            manager.add_key("test_key")

        mock_which.assert_called_once_with("ssh-add")

    @patch("shutil.which")
    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_list_keys_with_path_discovery(self, mock_run_cmd: Mock, mock_which: Mock, tmp_path: Path) -> None:
        """Test listing keys with ssh-add path discovery."""
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        # Mock shutil.which to return a custom path
        mock_which.return_value = "/custom/path/ssh-add"
        mock_run_cmd.return_value = Mock(stdout="2048 SHA256:abc123 test@example.com (RSA)")

        result = manager.list_keys()

        # Verify shutil.which was called
        mock_which.assert_called_once_with("ssh-add")
        # Verify run_cmd was called with the discovered path
        mock_run_cmd.assert_called_once()
        call_args = mock_run_cmd.call_args
        assert call_args[0][0] == ["/custom/path/ssh-add", "-l"]
        assert "2048 SHA256:abc123" in result

    @patch("shutil.which")
    def test_list_keys_ssh_add_not_found(self, mock_which: Mock, tmp_path: Path) -> None:
        """Test listing keys when ssh-add is not found."""
        manager = SSHAgentManager(tmp_path)
        manager.auth_sock = TEST_SSH_SOCK
        manager.agent_pid = TEST_SSH_PID

        # Mock shutil.which to return None (not found)
        mock_which.return_value = None

        with pytest.raises(SSHAgentError, match="ssh-add not found in PATH"):
            manager.list_keys()

        mock_which.assert_called_once_with("ssh-add")
