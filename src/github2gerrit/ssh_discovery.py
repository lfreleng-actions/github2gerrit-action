# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
SSH host key auto-discovery for github2gerrit.

This module provides functionality to automatically discover and fetch SSH
host keys for Gerrit servers, eliminating the need for manual
GERRIT_KNOWN_HOSTS configuration.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

from .external_api import ApiType
from .external_api import external_api_call
from .gitutils import CommandError
from .gitutils import run_cmd


log = logging.getLogger(__name__)


class SSHDiscoveryError(Exception):
    """Raised when SSH host key discovery fails."""


# Error message constants to comply with TRY003
_MSG_HOST_UNREACHABLE = "Host {hostname}:{port} is not reachable. Check network connectivity and server availability."
_MSG_NO_KEYS_FOUND = (
    "No SSH host keys found for {hostname}:{port}. The server may not be running SSH or may be blocking connections."
)
_MSG_NO_VALID_KEYS = (
    "No valid SSH host keys found for {hostname}:{port}. The ssh-keyscan output was empty or malformed."
)
_MSG_CONNECTION_FAILED = "Failed to connect to {hostname}:{port} for SSH key discovery. Error: {error}"
_MSG_KEYSCAN_FAILED = "ssh-keyscan failed with return code {returncode}: {error}"
_MSG_UNEXPECTED_ERROR = "Unexpected error during SSH key discovery for {hostname}:{port}: {error}"
_MSG_SAVE_FAILED = "Failed to save host keys to configuration file {config_file}: {error}"


def is_host_reachable(hostname: str, port: int, timeout: int = 5) -> bool:
    """Check if a host and port are reachable via TCP."""
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


@external_api_call(ApiType.SSH, "fetch_ssh_host_keys")
def fetch_ssh_host_keys(hostname: str, port: int = 22, timeout: int = 10) -> str:
    """
    Fetch SSH host keys for a given hostname and port using ssh-keyscan.

    Args:
        hostname: The hostname to scan
        port: The SSH port (default: 22)
        timeout: Connection timeout in seconds (default: 10)

    Returns:
        A string containing the host keys in known_hosts format

    Raises:
        SSHDiscoveryError: If the host keys cannot be fetched
    """
    log.debug("Fetching SSH host keys for %s:%d", hostname, port)

    # First check if the host is reachable
    if not is_host_reachable(hostname, port, timeout=5):
        raise SSHDiscoveryError(_MSG_HOST_UNREACHABLE.format(hostname=hostname, port=port))

    try:
        # Use ssh-keyscan to fetch all available key types
        cmd = [
            "ssh-keyscan",
            "-p",
            str(port),
            "-T",
            str(timeout),
            "-t",
            "rsa,ecdsa,ed25519",
            hostname,
        ]

        result = run_cmd(cmd, timeout=timeout + 5)

        if not result.stdout or not result.stdout.strip():
            raise SSHDiscoveryError(  # noqa: TRY301
                _MSG_NO_KEYS_FOUND.format(hostname=hostname, port=port)
            )

        # Validate that we got proper known_hosts format
        lines = result.stdout.strip().split("\n")
        valid_lines = []

        for line in lines:
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith("#"):
                continue

            # Basic validation: should have hostname, key type, and key
            parts = stripped_line.split()
            if len(parts) >= 3:
                valid_lines.append(stripped_line)

        if not valid_lines:
            raise SSHDiscoveryError(  # noqa: TRY301
                _MSG_NO_VALID_KEYS.format(hostname=hostname, port=port)
            )

        discovered_keys = "\n".join(valid_lines)
        log.info(
            "Successfully discovered %d SSH host key(s) for %s:%d",
            len(valid_lines),
            hostname,
            port,
        )
        log.debug("Discovered keys:\n%s", discovered_keys)

    except CommandError as exc:
        if exc.returncode == 1:
            # ssh-keyscan returns 1 when it can't connect
            error_msg = exc.stderr or exc.stdout or "Connection failed"
            raise SSHDiscoveryError(
                _MSG_CONNECTION_FAILED.format(hostname=hostname, port=port, error=error_msg)
            ) from exc
        else:
            error_msg = exc.stderr or exc.stdout or "Unknown error"
            raise SSHDiscoveryError(_MSG_KEYSCAN_FAILED.format(returncode=exc.returncode, error=error_msg)) from exc
    except Exception as exc:
        raise SSHDiscoveryError(_MSG_UNEXPECTED_ERROR.format(hostname=hostname, port=port, error=exc)) from exc
    else:
        return discovered_keys


def extract_gerrit_info_from_gitreview(content: str) -> tuple[str, int] | None:
    """
    Extract Gerrit hostname and port from .gitreview file content.

    Args:
        content: The content of a .gitreview file

    Returns:
        A tuple of (hostname, port) or None if not found
    """
    hostname = None
    port = 29418  # Default Gerrit SSH port

    for line in content.split("\n"):
        stripped_line = line.strip()
        if "=" not in stripped_line:
            continue

        key, value = stripped_line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "host":
            hostname = value
        elif key == "port":
            try:
                port = int(value)
            except ValueError:
                log.warning("Invalid port in .gitreview: %s", value)

    return (hostname, port) if hostname else None


@external_api_call(ApiType.SSH, "discover_and_save_host_keys")
def discover_and_save_host_keys(hostname: str, port: int, organization: str, config_path: str | None = None) -> str:
    """
    Discover SSH host keys and save them to the organization's configuration.

    Args:
        hostname: Gerrit hostname
        port: Gerrit SSH port
        organization: GitHub organization name for config section
        config_path: Path to config file (optional, uses default if not
            provided)

    Returns:
        The discovered host keys string

    Raises:
        SSHDiscoveryError: If discovery or saving fails
    """
    # Discover the host keys
    host_keys = fetch_ssh_host_keys(hostname, port)

    # Save to configuration file
    save_host_keys_to_config(host_keys, organization, config_path)

    return host_keys


def save_host_keys_to_config(host_keys: str, organization: str, config_path: str | None = None) -> None:
    """
    Save SSH host keys to the organization's configuration file.

    Args:
        host_keys: The host keys in known_hosts format
        organization: GitHub organization name for config section
        config_path: Path to config file (optional, uses default if not
            provided)

    Raises:
        SSHDiscoveryError: If saving fails
    """
    from .config import DEFAULT_CONFIG_PATH

    if config_path is None:
        config_path = os.getenv("G2G_CONFIG_PATH", "").strip() or DEFAULT_CONFIG_PATH

    config_file = Path(config_path).expanduser()

    try:
        # Ensure the directory exists
        config_file.parent.mkdir(parents=True, exist_ok=True)

        # Read existing configuration
        existing_content = ""
        if config_file.exists():
            existing_content = config_file.read_text(encoding="utf-8")

        # Parse existing content to find the organization section
        lines = existing_content.split("\n")
        new_lines = []
        in_org_section = False
        org_section_found = False
        gerrit_known_hosts_updated = False

        for line in lines:
            stripped = line.strip()

            # Check for section headers
            if stripped.startswith("[") and stripped.endswith("]"):
                section_name = stripped[1:-1].strip().lower()
                in_org_section = section_name == organization.lower()
                if in_org_section:
                    org_section_found = True

            # If we're in the org section and find GERRIT_KNOWN_HOSTS, replace
            elif in_org_section and "=" in line:
                key = line.split("=", 1)[0].strip().upper()
                if key == "GERRIT_KNOWN_HOSTS":
                    # Replace with new host keys (properly escaped for INI)
                    escaped_keys = host_keys.replace("\n", "\\n")
                    new_lines.append(f'GERRIT_KNOWN_HOSTS = "{escaped_keys}"')
                    gerrit_known_hosts_updated = True
                    continue

            new_lines.append(line)

        # If organization section wasn't found, add it
        if not org_section_found:
            if new_lines and new_lines[-1].strip():
                new_lines.append("")  # Add blank line before new section
            new_lines.append(f"[{organization}]")
            escaped_keys = host_keys.replace("\n", "\\n")
            new_lines.append(f'GERRIT_KNOWN_HOSTS = "{escaped_keys}"')
            gerrit_known_hosts_updated = True

        # If section existed but didn't have GERRIT_KNOWN_HOSTS, add it
        elif not gerrit_known_hosts_updated:
            # Find the end of the organization section and add the key there
            section_end = len(new_lines)
            for i, line in enumerate(new_lines):
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    section_name = stripped[1:-1].strip().lower()
                    if section_name == organization.lower():
                        # Find the end of this section
                        for j in range(i + 1, len(new_lines)):
                            if new_lines[j].strip().startswith("["):
                                section_end = j
                                break
                        break

            # Insert the GERRIT_KNOWN_HOSTS entry
            escaped_keys = host_keys.replace("\n", "\\n")
            new_lines.insert(section_end, f'GERRIT_KNOWN_HOSTS = "{escaped_keys}"')

        # Write the updated configuration
        config_file.write_text("\n".join(new_lines), encoding="utf-8")

        log.info(
            "Successfully saved SSH host keys to configuration file: %s [%s]",
            config_file,
            organization,
        )

    except Exception as exc:
        raise SSHDiscoveryError(_MSG_SAVE_FAILED.format(config_file=config_file, error=exc)) from exc


def auto_discover_gerrit_host_keys(
    gerrit_hostname: str | None = None,
    gerrit_port: int | None = None,
    organization: str | None = None,
    save_to_config: bool = True,
) -> str | None:
    """
    Automatically discover Gerrit SSH host keys and optionally save to config.

    This is the main entry point for auto-discovery functionality.

    Args:
        gerrit_hostname: Gerrit hostname (if not provided, tries to detect
            from context)
        gerrit_port: Gerrit SSH port (defaults to 29418)
        organization: GitHub organization (if not provided, tries to detect
            from env)
        save_to_config: Whether to save discovered keys to config file

    Returns:
        The discovered host keys string, or None if discovery failed
    """
    try:
        # Set defaults
        if gerrit_port is None:
            gerrit_port = 29418

        if organization is None:
            organization = (os.getenv("ORGANIZATION") or os.getenv("GITHUB_REPOSITORY_OWNER") or "").strip()

        if not gerrit_hostname:
            log.debug("No Gerrit hostname provided for auto-discovery")
            return None

        if not organization:
            log.warning("No organization specified for SSH host key auto-discovery. Cannot save to configuration file.")
            save_to_config = False

        log.info(
            "Attempting to auto-discover SSH host keys for %s:%d",
            gerrit_hostname,
            gerrit_port,
        )

        # Discover the host keys
        host_keys = fetch_ssh_host_keys(gerrit_hostname, gerrit_port)

        # Save to configuration if requested and possible
        if save_to_config and organization:
            save_host_keys_to_config(host_keys, organization)
            log.info(
                "SSH host keys automatically discovered and saved to config "
                "for organization '%s'. Future runs will use the cached keys.",
                organization,
            )
        else:
            log.info(
                "SSH host keys discovered but not saved to configuration. "
                "Set ORGANIZATION environment variable to enable auto-saving."
            )

    except SSHDiscoveryError as exc:
        log.warning("SSH host key auto-discovery failed: %s", exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error during SSH host key auto-discovery: %s", exc)
        return None
    else:
        return host_keys
