# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for SSH host key auto-discovery functionality."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from github2gerrit.ssh_discovery import SSHDiscoveryError
from github2gerrit.ssh_discovery import auto_discover_gerrit_host_keys
from github2gerrit.ssh_discovery import extract_gerrit_info_from_gitreview
from github2gerrit.ssh_discovery import fetch_ssh_host_keys
from github2gerrit.ssh_discovery import is_host_reachable
from github2gerrit.ssh_discovery import save_host_keys_to_config


class TestSSHDiscovery:
    """Test SSH host key discovery functionality."""

    def test_is_host_reachable_success(self) -> None:
        """Test successful host reachability check."""
        with patch("socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = Mock()
            mock_conn.return_value.__exit__ = Mock()

            result = is_host_reachable("example.com", 22, timeout=1)
            assert result is True
            mock_conn.assert_called_once_with(("example.com", 22), timeout=1)

    def test_is_host_reachable_failure(self) -> None:
        """Test host reachability check failure."""
        with patch("socket.create_connection") as mock_conn:
            mock_conn.side_effect = OSError("Connection refused")

            result = is_host_reachable("nonexistent.example.com", 22, timeout=1)
            assert result is False

    @patch("github2gerrit.ssh_discovery.is_host_reachable")
    @patch("github2gerrit.ssh_discovery.run_cmd")
    def test_fetch_ssh_host_keys_success(self, mock_run_cmd: Mock, mock_reachable: Mock) -> None:
        """Test successful SSH host key fetching."""
        mock_reachable.return_value = True
        mock_run_cmd.return_value = Mock(
            stdout=(
                "example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ...\n"
                "example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI..."
            ),
            stderr="",
            returncode=0,
        )

        result = fetch_ssh_host_keys("example.com", 29418, timeout=5)

        assert "ssh-rsa" in result
        assert "ssh-ed25519" in result
        assert "example.com" in result
        mock_reachable.assert_called_once_with("example.com", 29418, timeout=5)
        mock_run_cmd.assert_called_once()

    @patch("github2gerrit.ssh_discovery.is_host_reachable")
    def test_fetch_ssh_host_keys_unreachable(self, mock_reachable: Mock) -> None:
        """Test SSH host key fetching when host is unreachable."""
        mock_reachable.return_value = False

        with pytest.raises(SSHDiscoveryError) as exc_info:
            fetch_ssh_host_keys("unreachable.example.com", 29418)

        assert "not reachable" in str(exc_info.value)

    @patch("github2gerrit.ssh_discovery.is_host_reachable")
    @patch("github2gerrit.ssh_discovery.run_cmd")
    def test_fetch_ssh_host_keys_empty_output(self, mock_run_cmd: Mock, mock_reachable: Mock) -> None:
        """Test SSH host key fetching with empty output."""
        mock_reachable.return_value = True
        mock_run_cmd.return_value = Mock(stdout="", stderr="", returncode=0)

        with pytest.raises(SSHDiscoveryError) as exc_info:
            fetch_ssh_host_keys("example.com", 29418)

        assert "No SSH host keys found" in str(exc_info.value)

    @patch("github2gerrit.ssh_discovery.is_host_reachable")
    @patch("github2gerrit.ssh_discovery.run_cmd")
    def test_fetch_ssh_host_keys_command_error(self, mock_run_cmd: Mock, mock_reachable: Mock) -> None:
        """Test SSH host key fetching with command error."""
        from github2gerrit.gitutils import CommandError

        mock_reachable.return_value = True
        mock_run_cmd.side_effect = CommandError(
            "ssh-keyscan failed",
            cmd=["ssh-keyscan"],
            returncode=1,
            stdout="",
            stderr="Connection refused",
        )

        with pytest.raises(SSHDiscoveryError) as exc_info:
            fetch_ssh_host_keys("example.com", 29418)

        assert "Failed to connect" in str(exc_info.value)

    def test_extract_gerrit_info_from_gitreview_success(self) -> None:
        """Test successful extraction of Gerrit info from .gitreview."""
        gitreview_content = """
[gerrit]
host = gerrit.example.org
port = 29418
project = test/project
"""

        result = extract_gerrit_info_from_gitreview(gitreview_content)
        assert result == ("gerrit.example.org", 29418)

    def test_extract_gerrit_info_from_gitreview_default_port(self) -> None:
        """Test extraction with default port."""
        gitreview_content = """
[gerrit]
host = gerrit.example.org
project = test/project
"""

        result = extract_gerrit_info_from_gitreview(gitreview_content)
        assert result == ("gerrit.example.org", 29418)

    def test_extract_gerrit_info_from_gitreview_no_host(self) -> None:
        """Test extraction when no host is specified."""
        gitreview_content = """
[gerrit]
port = 29418
project = test/project
"""

        result = extract_gerrit_info_from_gitreview(gitreview_content)
        assert result is None

    def test_extract_gerrit_info_from_gitreview_invalid_port(self) -> None:
        """Test extraction with invalid port."""
        gitreview_content = """
[gerrit]
host = gerrit.example.org
port = invalid
project = test/project
"""

        result = extract_gerrit_info_from_gitreview(gitreview_content)
        assert result == ("gerrit.example.org", 29418)  # Falls back to default

    def test_save_host_keys_to_config_new_file(self) -> None:
        """Test saving host keys to a new configuration file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.txt"
            host_keys = (
                "example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ...\n"
                "example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI..."
            )

            save_host_keys_to_config(host_keys, "testorg", str(config_path))

            content = config_path.read_text()
            assert "[testorg]" in content
            assert "GERRIT_KNOWN_HOSTS" in content
            assert "ssh-rsa" in content
            assert "\\n" in content  # Should be escaped for INI format

    def test_save_host_keys_to_config_existing_file(self) -> None:
        """Test saving host keys to an existing configuration file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.txt"

            # Create existing config
            existing_content = """
[default]
SOME_KEY = "some_value"

[testorg]
OTHER_KEY = "other_value"

[otherorg]
ANOTHER_KEY = "another_value"
"""
            config_path.write_text(existing_content)

            host_keys = "example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."
            save_host_keys_to_config(host_keys, "testorg", str(config_path))

            content = config_path.read_text()
            assert "[testorg]" in content
            assert "GERRIT_KNOWN_HOSTS" in content
            assert "OTHER_KEY" in content  # Existing keys preserved
            assert "[otherorg]" in content  # Other sections preserved

    def test_save_host_keys_to_config_update_existing(self) -> None:
        """Test updating existing GERRIT_KNOWN_HOSTS in configuration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.txt"

            # Create existing config with old host keys
            existing_content = """
[testorg]
GERRIT_KNOWN_HOSTS = "old.example.com ssh-rsa OLD_KEY"
OTHER_KEY = "other_value"
"""
            config_path.write_text(existing_content)

            new_host_keys = "new.example.com ssh-rsa NEW_KEY"
            save_host_keys_to_config(new_host_keys, "testorg", str(config_path))

            content = config_path.read_text()
            assert "NEW_KEY" in content
            assert "OLD_KEY" not in content
            assert "OTHER_KEY" in content  # Other keys preserved

    @patch("github2gerrit.ssh_discovery.fetch_ssh_host_keys")
    @patch("github2gerrit.ssh_discovery.save_host_keys_to_config")
    def test_auto_discover_success(self, mock_save: Mock, mock_fetch: Mock) -> None:
        """Test successful auto-discovery."""
        mock_fetch.return_value = "example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."

        result = auto_discover_gerrit_host_keys(
            gerrit_hostname="example.com",
            gerrit_port=29418,
            organization="testorg",
            save_to_config=True,
        )

        assert result is not None
        assert "ssh-rsa" in result
        mock_fetch.assert_called_once_with("example.com", 29418)
        mock_save.assert_called_once()

    @patch("github2gerrit.ssh_discovery.fetch_ssh_host_keys")
    def test_auto_discover_no_save(self, mock_fetch: Mock) -> None:
        """Test auto-discovery without saving to config."""
        mock_fetch.return_value = "example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."

        with patch("github2gerrit.ssh_discovery.save_host_keys_to_config") as mock_save:
            result = auto_discover_gerrit_host_keys(
                gerrit_hostname="example.com",
                gerrit_port=29418,
                organization="testorg",
                save_to_config=False,
            )

            assert result is not None
            mock_save.assert_not_called()

    def test_auto_discover_no_hostname(self) -> None:
        """Test auto-discovery without hostname."""
        result = auto_discover_gerrit_host_keys(gerrit_hostname=None, organization="testorg")

        assert result is None

    @patch("github2gerrit.ssh_discovery.fetch_ssh_host_keys")
    def test_auto_discover_fetch_failure(self, mock_fetch: Mock) -> None:
        """Test auto-discovery when fetching fails."""
        mock_fetch.side_effect = SSHDiscoveryError("Connection failed")

        result = auto_discover_gerrit_host_keys(
            gerrit_hostname="example.com",
            gerrit_port=29418,
            organization="testorg",
        )

        assert result is None

    @patch.dict(os.environ, {"ORGANIZATION": "envorg"}, clear=True)
    @patch("github2gerrit.ssh_discovery.fetch_ssh_host_keys")
    @patch("github2gerrit.ssh_discovery.save_host_keys_to_config")
    def test_auto_discover_with_env_org(self, mock_save: Mock, mock_fetch: Mock) -> None:
        """Test auto-discovery using organization from environment."""
        mock_fetch.return_value = "example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."

        result = auto_discover_gerrit_host_keys(
            gerrit_hostname="example.com",
            # organization not provided, should use env
        )

        assert result is not None
        mock_save.assert_called_once()
        # Verify it used the environment organization
        call_args = mock_save.call_args
        assert "envorg" in call_args[0]

    @patch.dict(os.environ, {}, clear=True)
    @patch("github2gerrit.ssh_discovery.fetch_ssh_host_keys")
    def test_auto_discover_no_organization_no_save(self, mock_fetch: Mock) -> None:
        """Test auto-discovery without organization doesn't save."""
        mock_fetch.return_value = "example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."

        with patch("github2gerrit.ssh_discovery.save_host_keys_to_config") as mock_save:
            result = auto_discover_gerrit_host_keys(
                gerrit_hostname="example.com",
                organization=None,  # No organization provided
            )

            assert result is not None
            mock_save.assert_not_called()

    def test_save_host_keys_permission_error(self) -> None:
        """Test handling of permission errors when saving config."""
        with patch("pathlib.Path.write_text") as mock_write:
            mock_write.side_effect = PermissionError("Permission denied")

            with pytest.raises(SSHDiscoveryError) as exc_info:
                save_host_keys_to_config(
                    "example.com ssh-rsa KEY",
                    "testorg",
                    "/root/config.txt",  # Likely to cause permission error
                )

            assert "Failed to save host keys" in str(exc_info.value)

    def test_fetch_ssh_host_keys_with_comments(self) -> None:
        """Test SSH key fetching with comment lines in output."""
        with (
            patch("github2gerrit.ssh_discovery.is_host_reachable") as mock_reachable,
            patch("github2gerrit.ssh_discovery.run_cmd") as mock_run_cmd,
        ):
            mock_reachable.return_value = True
            mock_run_cmd.return_value = Mock(
                stdout=(
                    "# Comment line\n"
                    "example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ...\n"
                    "# Another comment\n"
                    "example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...\n"
                ),
                stderr="",
                returncode=0,
            )

            result = fetch_ssh_host_keys("example.com", 29418)

            # Comments should be filtered out
            assert "# Comment" not in result
            assert "ssh-rsa" in result
            assert "ssh-ed25519" in result
