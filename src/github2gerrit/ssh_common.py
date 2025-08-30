# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Common SSH utilities for consistent SSH configuration across modules.

This module provides shared SSH functionality to avoid duplication between
CLI and core SSH setup routines, ensuring consistent behavior and options.
"""

import logging
import os
from pathlib import Path


log = logging.getLogger(__name__)


def build_ssh_options(
    key_path: str | Path | None = None,
    known_hosts_path: str | Path | None = None,
    identities_only: bool = True,
    strict_host_checking: bool = True,
    batch_mode: bool = True,
    connect_timeout: int = 10,
    additional_options: list[str] | None = None,
    respect_user_ssh_config: bool | None = None,
) -> list[str]:
    """Build a list of SSH options for secure, isolated SSH configuration.

    Args:
        key_path: Path to SSH private key file
        known_hosts_path: Path to known_hosts file
        identities_only: Only use specified identities (prevents agent scanning)
        strict_host_checking: Enable strict host key checking
        batch_mode: Enable batch mode (non-interactive)
        connect_timeout: Connection timeout in seconds
        additional_options: Additional SSH options to include
        respect_user_ssh_config: If True, respect user SSH config; if None, check G2G_RESPECT_USER_SSH env var

    Returns:
        List of SSH option strings suitable for ssh command line
    """
    # Check if we should respect user SSH config
    if respect_user_ssh_config is None:
        respect_user_ssh_config = os.getenv("G2G_RESPECT_USER_SSH", "false").lower() in ("true", "1", "yes")

    options = []
    if not respect_user_ssh_config:
        options.append("-F /dev/null")  # Ignore user SSH config

    if key_path:
        options.append(f"-i {key_path}")

    if known_hosts_path:
        options.append(f"-o UserKnownHostsFile={known_hosts_path}")

    if identities_only and not respect_user_ssh_config:
        options.extend(
            [
                "-o IdentitiesOnly=yes",  # Critical: prevents SSH agent scanning
                "-o IdentityAgent=none",
            ]
        )

    if batch_mode:
        options.append("-o BatchMode=yes")

    options.extend(
        [
            "-o PreferredAuthentications=publickey",
            "-o PasswordAuthentication=no",
            "-o PubkeyAcceptedKeyTypes=+ssh-rsa",
            f"-o ConnectTimeout={connect_timeout}",
        ]
    )

    if strict_host_checking:
        options.append("-o StrictHostKeyChecking=yes")

    if additional_options:
        options.extend(additional_options)

    return options


def build_git_ssh_command(
    key_path: str | Path | None = None,
    known_hosts_path: str | Path | None = None,
    identities_only: bool = True,
    strict_host_checking: bool = True,
    batch_mode: bool = True,
    connect_timeout: int = 10,
    additional_options: list[str] | None = None,
    respect_user_ssh_config: bool | None = None,
) -> str:
    """Build GIT_SSH_COMMAND for secure, isolated SSH configuration.

    This prevents SSH from scanning the user's SSH agent or using
    unintended keys by setting appropriate SSH options, unless
    respect_user_ssh_config is True.

    Args:
        key_path: Path to SSH private key file
        known_hosts_path: Path to known_hosts file
        identities_only: Only use specified identities (prevents agent scanning)
        strict_host_checking: Enable strict host key checking
        batch_mode: Enable batch mode (non-interactive)
        connect_timeout: Connection timeout in seconds
        additional_options: Additional SSH options to include
        respect_user_ssh_config: If True, respect user SSH config; if None, check G2G_RESPECT_USER_SSH env var

    Returns:
        Complete SSH command string suitable for GIT_SSH_COMMAND
    """
    ssh_options = build_ssh_options(
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        identities_only=identities_only,
        strict_host_checking=strict_host_checking,
        batch_mode=batch_mode,
        connect_timeout=connect_timeout,
        additional_options=additional_options,
        respect_user_ssh_config=respect_user_ssh_config,
    )

    ssh_cmd = f"ssh {' '.join(ssh_options)}"

    # Log masked version for security
    if key_path:
        masked_cmd = ssh_cmd.replace(str(key_path), "[KEY_PATH]")
        log.debug("Generated SSH command: %s", masked_cmd)
    else:
        log.debug("Generated SSH command: %s", ssh_cmd)

    return ssh_cmd


def build_non_interactive_ssh_env() -> dict[str, str]:
    """Build environment variables for non-interactive SSH operations.

    This creates an environment that prevents SSH from using agents,
    asking for passwords, or displaying prompts.

    Returns:
        Dictionary of environment variables for non-interactive SSH
    """
    return {
        "SSH_AUTH_SOCK": "",
        "SSH_AGENT_PID": "",
        "SSH_ASKPASS": "/usr/bin/false",
        "DISPLAY": "",
        "SSH_ASKPASS_REQUIRE": "never",
    }


def augment_known_hosts_with_bracketed_entries(
    known_hosts_content: str,
    hostname: str,
    port: int = 22,
) -> str:
    """Augment known_hosts content with bracketed [host]:port entries for non-standard ports.

    This function adds bracketed [host]:port variants for existing plain host entries
    to satisfy StrictHostKeyChecking with non-standard SSH ports.

    Args:
        known_hosts_content: Original known_hosts content
        hostname: Hostname to augment
        port: SSH port (default 22)

    Returns:
        Augmented known_hosts content (always normalized to end with single newline)
    """
    if not known_hosts_content.strip():
        return known_hosts_content

    original_lines = [ln.rstrip() for ln in known_hosts_content.strip().splitlines() if ln.strip()]
    augmented = list(original_lines)

    # Add bracketed [host]:port variants for non-standard ports if hostname provided
    if hostname and original_lines:
        bracket_prefix = f"[{hostname}]:{port} "
        plain_prefix = f"{hostname} "
        existing = set(augmented)

        for ln in original_lines:
            if ln.startswith(plain_prefix):
                suffix = ln[len(plain_prefix) :]
                candidate = bracket_prefix + suffix
                if candidate not in existing:
                    augmented.append(candidate)
                    existing.add(candidate)

    # Always normalize to strip + newline for consistency
    return "\n".join(augmented).strip() + "\n"


def merge_known_hosts_content(
    base_content: str,
    additional_content: str,
) -> str:
    """Merge additional known_hosts content into base content without duplicates.

    Args:
        base_content: Original known_hosts content
        additional_content: Additional content to merge

    Returns:
        Merged known_hosts content
    """
    if not additional_content or not additional_content.strip():
        return base_content

    if not base_content or not base_content.strip():
        return additional_content.strip() + "\n"

    existing_lines = [ln for ln in base_content.splitlines() if ln.strip()]
    existing_set = set(existing_lines)

    for ln in additional_content.splitlines():
        s = ln.strip()
        if s and s not in existing_set:
            existing_lines.append(s)
            existing_set.add(s)

    return "\n".join(existing_lines).strip() + "\n"


def augment_known_hosts(
    known_hosts_path: Path,
    hostname: str,
    port: int = 22,
) -> None:
    """Augment known_hosts file with host key for the given hostname.

    Args:
        known_hosts_path: Path to known_hosts file
        hostname: Hostname to add to known_hosts
        port: SSH port (default 22)
    """
    # This is a placeholder for known hosts augmentation logic
    # The actual implementation would use ssh-keyscan or similar
    # to fetch and add host keys to the known_hosts file
    log.debug("Would augment known_hosts at %s with %s:%d", known_hosts_path, hostname, port)
