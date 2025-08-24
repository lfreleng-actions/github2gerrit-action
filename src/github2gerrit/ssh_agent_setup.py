# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
SSH agent-based authentication for github2gerrit.

This module provides functionality to use SSH agent for authentication
instead of writing private keys to disk, which is more secure and
avoids file permission issues in CI environments.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .gitutils import CommandError
from .gitutils import run_cmd
from .ssh_common import augment_known_hosts_with_bracketed_entries


log = logging.getLogger(__name__)


class SSHAgentError(Exception):
    """Raised when SSH agent operations fail."""


# Error message constants to comply with TRY003
_MSG_PARSE_FAILED = "Failed to parse ssh-agent output"
_MSG_START_FAILED = "Failed to start SSH agent: {error}"
_MSG_NOT_STARTED = "SSH agent not started"
_MSG_ADD_FAILED = "ssh-add failed: {error}"
_MSG_ADD_TIMEOUT = "ssh-add timed out"
_MSG_ADD_KEY_FAILED = "Failed to add key to SSH agent: {error}"
_MSG_SETUP_HOSTS_FAILED = "Failed to setup known hosts: {error}"
_MSG_HOSTS_NOT_CONFIGURED = "Known hosts not configured"
_MSG_LIST_FAILED = "Failed to list keys: {error}"
_MSG_NO_KEYS_LOADED = "No keys were loaded into SSH agent"
_MSG_SSH_AGENT_NOT_FOUND = "ssh-agent not found in PATH"
_MSG_SSH_ADD_NOT_FOUND = "ssh-add not found in PATH"


class SSHAgentManager:
    """Manages SSH agent lifecycle and key loading for secure authentication."""

    def __init__(self, workspace: Path):
        """Initialize SSH agent manager.

        Args:
            workspace: The workspace directory for storing temporary files
        """
        self.workspace = workspace
        self.agent_pid: int | None = None
        self.auth_sock: str | None = None
        self.known_hosts_path: Path | None = None
        self._original_env: dict[str, str] = {}

    def start_agent(self) -> None:
        """Start a new SSH agent process."""
        try:
            # Locate ssh-agent executable
            ssh_agent_path = shutil.which("ssh-agent")
            if not ssh_agent_path:
                _raise_ssh_agent_not_found()
            assert ssh_agent_path is not None  # for mypy  # noqa: S101

            # Start ssh-agent and capture its output
            result = run_cmd([ssh_agent_path, "-s"], timeout=10)

            # Parse the ssh-agent output to get environment variables
            for line in result.stdout.strip().split("\n"):
                if line.startswith("SSH_AUTH_SOCK="):
                    # Format: SSH_AUTH_SOCK=/path/to/socket; export SSH_AUTH_SOCK;
                    value = line.split("=", 1)[1].split(";")[0].strip()
                    self.auth_sock = value
                elif line.startswith("SSH_AGENT_PID="):
                    # Format: SSH_AGENT_PID=12345; export SSH_AGENT_PID;
                    value = line.split("=", 1)[1].split(";")[0].strip()
                    self.agent_pid = int(value)

            if not self.auth_sock or not self.agent_pid:
                _raise_parse_error()

            # Store original environment
            self._original_env = {
                "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", ""),
                "SSH_AGENT_PID": os.environ.get("SSH_AGENT_PID", ""),
            }

            # Set environment variables for this process
            if self.auth_sock:
                os.environ["SSH_AUTH_SOCK"] = self.auth_sock
            if self.agent_pid:
                os.environ["SSH_AGENT_PID"] = str(self.agent_pid)

            log.debug("Started SSH agent with PID %d, socket %s", self.agent_pid, self.auth_sock)

        except Exception as exc:
            raise SSHAgentError(_MSG_START_FAILED.format(error=exc)) from exc

    def add_key(self, private_key_content: str) -> None:
        """Add a private key to the SSH agent.

        Args:
            private_key_content: The private key content as a string
        """
        if not self.auth_sock:
            raise SSHAgentError(_MSG_NOT_STARTED)

        # Locate ssh-add executable
        ssh_add_path = shutil.which("ssh-add")
        if not ssh_add_path:
            _raise_ssh_add_not_found()
        assert ssh_add_path is not None  # for mypy  # noqa: S101

        process = None
        try:
            # Use ssh-add with stdin to add the key
            process = subprocess.Popen(  # noqa: S603
                [ssh_add_path, "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    "SSH_AUTH_SOCK": self.auth_sock,
                    "SSH_AGENT_PID": str(self.agent_pid),
                },
            )

            stdout, stderr = process.communicate(input=private_key_content.strip() + "\n", timeout=10)

            if process.returncode != 0:
                _raise_add_key_error(stderr)

            log.debug("Successfully added SSH key to agent")

        except subprocess.TimeoutExpired as exc:
            if process:
                process.kill()
            raise SSHAgentError(_MSG_ADD_TIMEOUT) from exc
        except Exception as exc:
            raise SSHAgentError(_MSG_ADD_KEY_FAILED.format(error=exc)) from exc

    def setup_known_hosts(self, known_hosts_content: str) -> None:
        """Setup known hosts file.

        Args:
            known_hosts_content: The known hosts content
        """
        try:
            # Create tool-specific SSH directory
            tool_ssh_dir = self.workspace / ".ssh-g2g"
            tool_ssh_dir.mkdir(mode=0o700, exist_ok=True)

            # Write known hosts file (normalize/augment with [host]:port entries)
            self.known_hosts_path = tool_ssh_dir / "known_hosts"
            host = (os.getenv("GERRIT_SERVER") or "").strip()
            port = (os.getenv("GERRIT_SERVER_PORT") or "29418").strip()
            try:
                port_int = int(port)
            except Exception:
                port_int = 29418

            # Use centralized augmentation logic
            augmented_content = augment_known_hosts_with_bracketed_entries(known_hosts_content, host, port_int)

            with open(self.known_hosts_path, "w", encoding="utf-8") as f:
                f.write(augmented_content)
            self.known_hosts_path.chmod(0o644)

            log.debug("Known hosts written to %s", self.known_hosts_path)

        except Exception as exc:
            raise SSHAgentError(_MSG_SETUP_HOSTS_FAILED.format(error=exc)) from exc

    def get_git_ssh_command(self) -> str:
        """Generate GIT_SSH_COMMAND for SSH agent-based authentication.

        Returns:
            SSH command string for git operations
        """
        if not self.known_hosts_path:
            raise SSHAgentError(_MSG_HOSTS_NOT_CONFIGURED)

        ssh_options = [
            "-F /dev/null",
            f"-o UserKnownHostsFile={self.known_hosts_path}",
            "-o IdentitiesOnly=no",  # Allow SSH agent
            "-o BatchMode=yes",
            "-o PreferredAuthentications=publickey",
            "-o StrictHostKeyChecking=yes",
            "-o PasswordAuthentication=no",
            "-o PubkeyAcceptedKeyTypes=+ssh-rsa",
            "-o ConnectTimeout=10",
        ]

        return f"ssh {' '.join(ssh_options)}"

    def get_ssh_env(self) -> dict[str, str]:
        """Get environment variables for SSH operations.

        Returns:
            Dictionary of environment variables
        """
        if not self.auth_sock:
            raise SSHAgentError(_MSG_NOT_STARTED)

        return {
            "SSH_AUTH_SOCK": self.auth_sock,
            "SSH_AGENT_PID": str(self.agent_pid),
        }

    def list_keys(self) -> str:
        """List keys currently loaded in the agent.

        Returns:
            Output from ssh-add -l
        """
        if not self.auth_sock:
            raise SSHAgentError(_MSG_NOT_STARTED)

        try:
            # Locate ssh-add executable
            ssh_add_path = shutil.which("ssh-add")
            if not ssh_add_path:
                _raise_ssh_add_not_found()
            assert ssh_add_path is not None  # for mypy  # noqa: S101

            result = run_cmd(
                [ssh_add_path, "-l"],
                env={
                    **os.environ,
                    "SSH_AUTH_SOCK": self.auth_sock,
                    "SSH_AGENT_PID": str(self.agent_pid),
                },
                timeout=5,
            )
        except CommandError as exc:
            if exc.returncode == 1:
                return "No keys loaded"
            raise SSHAgentError(_MSG_LIST_FAILED.format(error=exc)) from exc
        except Exception as exc:
            raise SSHAgentError(_MSG_LIST_FAILED.format(error=exc)) from exc
        else:
            return result.stdout

    def cleanup(self) -> None:
        """Clean up SSH agent and temporary files."""
        try:
            # Kill SSH agent if we started it
            if self.agent_pid:
                try:
                    run_cmd(["/bin/kill", str(self.agent_pid)], timeout=5)
                    log.debug("SSH agent (PID %d) terminated", self.agent_pid)
                except Exception as exc:
                    log.warning("Failed to kill SSH agent: %s", exc)

            # Restore original environment
            for key, value in self._original_env.items():
                if value:
                    os.environ[key] = value
                elif key in os.environ:
                    del os.environ[key]

            # Clean up temporary files
            tool_ssh_dir = self.workspace / ".ssh-g2g"
            if tool_ssh_dir.exists():
                import shutil

                shutil.rmtree(tool_ssh_dir)
                log.debug("Cleaned up temporary SSH directory: %s", tool_ssh_dir)

        except Exception as exc:
            log.warning("Failed to clean up SSH agent: %s", exc)
        finally:
            self.agent_pid = None
            self.auth_sock = None
            self.known_hosts_path = None


def setup_ssh_agent_auth(workspace: Path, private_key_content: str, known_hosts_content: str) -> SSHAgentManager:
    """Setup SSH agent-based authentication.

    Args:
        workspace: The workspace directory
        private_key_content: SSH private key content
        known_hosts_content: Known hosts content

    Returns:
        Configured SSHAgentManager instance

    Raises:
        SSHAgentError: If setup fails
    """
    manager = SSHAgentManager(workspace)

    try:
        # Start SSH agent
        manager.start_agent()

        # Add the private key
        manager.add_key(private_key_content)

        # Setup known hosts
        manager.setup_known_hosts(known_hosts_content)

        # Verify key was added
        keys_list = manager.list_keys()
        if "No keys loaded" in keys_list:
            _raise_no_keys_error()

        log.info("SSH agent authentication configured successfully")
        log.debug("Loaded keys: %s", keys_list)

    except Exception:
        # Clean up on failure
        manager.cleanup()
        raise
    else:
        return manager


def _raise_parse_error() -> None:
    """Raise SSH agent parse error."""
    raise SSHAgentError(_MSG_PARSE_FAILED)


def _raise_add_key_error(stderr: str) -> None:
    """Raise SSH key addition error."""
    raise SSHAgentError(_MSG_ADD_FAILED.format(error=stderr))


def _raise_ssh_agent_not_found() -> None:
    """Raise SSH agent not found error."""
    raise SSHAgentError(_MSG_SSH_AGENT_NOT_FOUND)


def _raise_ssh_add_not_found() -> None:
    """Raise SSH add not found error."""
    raise SSHAgentError(_MSG_SSH_ADD_NOT_FOUND)


def _raise_no_keys_error() -> None:
    """Raise no keys loaded error."""
    raise SSHAgentError(_MSG_NO_KEYS_LOADED)
