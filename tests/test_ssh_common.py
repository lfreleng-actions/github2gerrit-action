# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for the ssh_common module containing SSH utilities."""

import logging
from pathlib import Path
from typing import Any

from _pytest.logging import LogCaptureFixture

from github2gerrit.ssh_common import augment_known_hosts
from github2gerrit.ssh_common import build_git_ssh_command
from github2gerrit.ssh_common import build_non_interactive_ssh_env
from github2gerrit.ssh_common import build_ssh_options


class TestBuildSshOptions:
    """Test the build_ssh_options function."""

    def test_build_ssh_options_minimal(self) -> None:
        """Test build_ssh_options with minimal parameters."""
        options = build_ssh_options()

        expected_options = [
            "-F /dev/null",
            "-o IdentitiesOnly=yes",
            "-o IdentityAgent=none",
            "-o BatchMode=yes",
            "-o PreferredAuthentications=publickey",
            "-o PasswordAuthentication=no",
            "-o PubkeyAcceptedKeyTypes=+ssh-rsa",
            "-o ConnectTimeout=10",
            "-o StrictHostKeyChecking=yes",
        ]

        for expected_option in expected_options:
            assert expected_option in options

    def test_build_ssh_options_with_key_path(self) -> None:
        """Test build_ssh_options with SSH key path."""
        key_path = "/path/to/key"
        options = build_ssh_options(key_path=key_path)

        assert f"-i {key_path}" in options

    def test_build_ssh_options_with_known_hosts_path(self) -> None:
        """Test build_ssh_options with known hosts path."""
        known_hosts_path = "/path/to/known_hosts"
        options = build_ssh_options(known_hosts_path=known_hosts_path)

        assert f"-o UserKnownHostsFile={known_hosts_path}" in options

    def test_build_ssh_options_identities_only_false(self) -> None:
        """Test build_ssh_options with identities_only=False."""
        options = build_ssh_options(identities_only=False)

        assert "-o IdentitiesOnly=yes" not in options
        assert "-o IdentityAgent=none" not in options

    def test_build_ssh_options_strict_host_checking_false(self) -> None:
        """Test build_ssh_options with strict_host_checking=False."""
        options = build_ssh_options(strict_host_checking=False)

        assert "-o StrictHostKeyChecking=yes" not in options

    def test_build_ssh_options_batch_mode_false(self) -> None:
        """Test build_ssh_options with batch_mode=False."""
        options = build_ssh_options(batch_mode=False)

        assert "-o BatchMode=yes" not in options

    def test_build_ssh_options_custom_timeout(self) -> None:
        """Test build_ssh_options with custom connect timeout."""
        timeout = 30
        options = build_ssh_options(connect_timeout=timeout)

        assert f"-o ConnectTimeout={timeout}" in options

    def test_build_ssh_options_additional_options(self) -> None:
        """Test build_ssh_options with additional options."""
        additional = ["-o ServerAliveInterval=60", "-o ServerAliveCountMax=3"]
        options = build_ssh_options(additional_options=additional)

        for option in additional:
            assert option in options

    def test_build_ssh_options_all_parameters(self) -> None:
        """Test build_ssh_options with all parameters set."""
        key_path = "/path/to/key"
        known_hosts_path = "/path/to/known_hosts"
        additional = ["-o ServerAliveInterval=60"]

        options = build_ssh_options(
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            identities_only=True,
            strict_host_checking=True,
            batch_mode=True,
            connect_timeout=15,
            additional_options=additional,
        )

        expected_options = [
            "-F /dev/null",
            f"-i {key_path}",
            f"-o UserKnownHostsFile={known_hosts_path}",
            "-o IdentitiesOnly=yes",
            "-o IdentityAgent=none",
            "-o BatchMode=yes",
            "-o PreferredAuthentications=publickey",
            "-o PasswordAuthentication=no",
            "-o PubkeyAcceptedKeyTypes=+ssh-rsa",
            "-o ConnectTimeout=15",
            "-o StrictHostKeyChecking=yes",
            "-o ServerAliveInterval=60",
        ]

        for expected_option in expected_options:
            assert expected_option in options

    def test_build_ssh_options_pathlib_paths(self) -> None:
        """Test build_ssh_options with pathlib.Path objects."""
        key_path = Path("/path/to/key")
        known_hosts_path = Path("/path/to/known_hosts")

        options = build_ssh_options(
            key_path=key_path,
            known_hosts_path=known_hosts_path,
        )

        assert f"-i {key_path}" in options
        assert f"-o UserKnownHostsFile={known_hosts_path}" in options


class TestBuildGitSshCommand:
    """Test the build_git_ssh_command function."""

    def test_build_git_ssh_command_minimal(self, caplog: LogCaptureFixture) -> None:
        """Test build_git_ssh_command with minimal parameters."""
        with caplog.at_level(logging.DEBUG):
            command = build_git_ssh_command()

        assert command.startswith("ssh ")
        assert "-F /dev/null" in command
        assert "-o IdentitiesOnly=yes" in command
        assert "-o BatchMode=yes" in command

        # Should log the command
        assert "Generated SSH command:" in caplog.text

    def test_build_git_ssh_command_with_key_path(self, caplog: LogCaptureFixture) -> None:
        """Test build_git_ssh_command with SSH key path."""
        key_path = "/path/to/secret/key"

        with caplog.at_level(logging.DEBUG):
            command = build_git_ssh_command(key_path=key_path)

        assert f"-i {key_path}" in command

        # Should mask the key path in logs
        assert "[KEY_PATH]" in caplog.text
        assert key_path not in caplog.text

    def test_build_git_ssh_command_no_key_path(self, caplog: LogCaptureFixture) -> None:
        """Test build_git_ssh_command without key path logs unmasked."""
        with caplog.at_level(logging.DEBUG):
            build_git_ssh_command()

        # Without key path, should log the actual command
        assert "Generated SSH command:" in caplog.text
        assert "[KEY_PATH]" not in caplog.text

    def test_build_git_ssh_command_custom_parameters(self) -> None:
        """Test build_git_ssh_command with custom parameters."""
        command = build_git_ssh_command(
            strict_host_checking=False,
            connect_timeout=20,
            additional_options=["-v"],
        )

        assert "-o StrictHostKeyChecking=yes" not in command
        assert "-o ConnectTimeout=20" in command
        assert "-v" in command

    def test_build_git_ssh_command_pathlib_path(self) -> None:
        """Test build_git_ssh_command with pathlib.Path objects."""
        key_path = Path("/path/to/key")
        known_hosts_path = Path("/path/to/known_hosts")

        command = build_git_ssh_command(
            key_path=key_path,
            known_hosts_path=known_hosts_path,
        )

        assert f"-i {key_path}" in command
        assert f"-o UserKnownHostsFile={known_hosts_path}" in command


class TestBuildNonInteractiveSshEnv:
    """Test the build_non_interactive_ssh_env function."""

    def test_build_non_interactive_ssh_env(self) -> None:
        """Test that build_non_interactive_ssh_env returns correct environment."""
        env = build_non_interactive_ssh_env()

        expected_env = {
            "SSH_AUTH_SOCK": "",
            "SSH_AGENT_PID": "",
            "SSH_ASKPASS": "/usr/bin/false",
            "DISPLAY": "",
            "SSH_ASKPASS_REQUIRE": "never",
        }

        assert env == expected_env

    def test_build_non_interactive_ssh_env_immutable(self) -> None:
        """Test that multiple calls return independent dictionaries."""
        env1 = build_non_interactive_ssh_env()
        env2 = build_non_interactive_ssh_env()

        # Should be equal but not the same object
        assert env1 == env2
        assert env1 is not env2

        # Modifying one should not affect the other
        env1["SSH_AUTH_SOCK"] = "modified"
        assert env2["SSH_AUTH_SOCK"] == ""


class TestAugmentKnownHosts:
    """Test the augment_known_hosts function."""

    def test_augment_known_hosts_basic(self, caplog: LogCaptureFixture) -> None:
        """Test augment_known_hosts with basic parameters."""
        known_hosts_path = Path("/path/to/known_hosts")
        hostname = "gerrit.example.com"

        with caplog.at_level(logging.DEBUG):
            augment_known_hosts(known_hosts_path, hostname)

        # Should log the operation (this is a placeholder implementation)
        assert "Would augment known_hosts" in caplog.text
        assert str(known_hosts_path) in caplog.text
        assert hostname in caplog.text
        assert ":22" in caplog.text  # Default port

    def test_augment_known_hosts_custom_port(self, caplog: LogCaptureFixture) -> None:
        """Test augment_known_hosts with custom port."""
        known_hosts_path = Path("/path/to/known_hosts")
        hostname = "gerrit.example.com"
        port = 29418

        with caplog.at_level(logging.DEBUG):
            augment_known_hosts(known_hosts_path, hostname, port)

        assert "Would augment known_hosts" in caplog.text
        assert f":{port}" in caplog.text

    def test_augment_known_hosts_different_hostnames(self, caplog: LogCaptureFixture) -> None:
        """Test augment_known_hosts with different hostname formats."""
        known_hosts_path = Path("/path/to/known_hosts")
        hostnames = [
            "gerrit.example.com",
            "192.168.1.100",
            "localhost",
            "gerrit-server.internal.company.com",
        ]

        for hostname in hostnames:
            with caplog.at_level(logging.DEBUG):
                augment_known_hosts(known_hosts_path, hostname)

            assert hostname in caplog.text


class TestIntegration:
    """Integration tests for ssh_common functions."""

    def test_ssh_options_in_git_command(self) -> None:
        """Test that ssh options are properly formatted in git command."""
        key_path = "/path/to/key"
        known_hosts_path = "/path/to/known_hosts"

        # Build SSH command
        command = build_git_ssh_command(
            key_path=key_path,
            known_hosts_path=known_hosts_path,
        )

        # Verify command structure
        assert command.startswith("ssh ")

        # Verify critical security options are present in the command string
        security_options = [
            "-F /dev/null",
            f"-i {key_path}",
            f"-o UserKnownHostsFile={known_hosts_path}",
            "-o IdentitiesOnly=yes",
            "-o IdentityAgent=none",
            "-o BatchMode=yes",
            "-o StrictHostKeyChecking=yes",
        ]

        for option in security_options:
            assert option in command

    def test_ssh_environment_isolation(self) -> None:
        """Test that SSH environment provides proper isolation."""
        env = build_non_interactive_ssh_env()

        # Verify that agent-related variables are cleared
        assert env["SSH_AUTH_SOCK"] == ""
        assert env["SSH_AGENT_PID"] == ""

        # Verify that interactive prompts are disabled
        assert env["SSH_ASKPASS"] == "/usr/bin/false"
        assert env["DISPLAY"] == ""
        assert env["SSH_ASKPASS_REQUIRE"] == "never"

    def test_consistent_option_ordering(self) -> None:
        """Test that option ordering is consistent across calls."""
        params: dict[str, Any] = {
            "key_path": "/path/to/key",
            "known_hosts_path": "/path/to/known_hosts",
            "connect_timeout": 15,
        }

        # Build same command multiple times
        commands = [build_git_ssh_command(**params) for _ in range(3)]

        # All commands should be identical
        assert len(set(commands)) == 1

    def test_option_combinations(self) -> None:
        """Test various combinations of SSH options."""
        base_params: dict[str, Any] = {
            "key_path": "/key",
            "known_hosts_path": "/hosts",
        }

        # Test different combinations
        combinations: list[dict[str, Any]] = [
            {"identities_only": False},
            {"strict_host_checking": False},
            {"batch_mode": False},
            {"connect_timeout": 5},
            {"additional_options": ["-v", "-4"]},
            {"identities_only": False, "batch_mode": False},
        ]

        for combo in combinations:
            params: dict[str, Any] = {**base_params, **combo}
            command = build_git_ssh_command(**params)

            # Should always start with ssh and contain basic options
            assert command.startswith("ssh ")
            assert "-F /dev/null" in command

            # Check specific combinations
            if not combo.get("identities_only", True):
                assert "-o IdentitiesOnly=yes" not in command

            if not combo.get("strict_host_checking", True):
                assert "-o StrictHostKeyChecking=yes" not in command

            if not combo.get("batch_mode", True):
                assert "-o BatchMode=yes" not in command
