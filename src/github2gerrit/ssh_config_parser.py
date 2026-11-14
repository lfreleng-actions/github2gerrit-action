# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
SSH configuration parser for github2gerrit.

This module provides functionality to parse SSH client configuration files
to extract user settings for Gerrit hosts, enabling personalized username
derivation instead of relying solely on organization-based defaults.

Key features:
- Parses ~/.ssh/config format
- Supports Host patterns and wildcards
- Resolves canonical hostnames
- Extracts User directives for specific hosts
- Thread-safe operation

Nothing here is cached. Everything credential derivation depends on --
the G2G_RESPECT_USER_SSH environment variable, the SSH config on disk
and the git identity -- is read afresh on every call, because each of
them can change during a single process (cli._load_effective_inputs
sets the environment variable at runtime). Holding no process-wide
state is also what makes the "thread-safe operation" claim above true
rather than aspirational.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from .utils import env_bool


log = logging.getLogger(__name__)


class SSHConfig:
    """Parser for SSH client configuration files.

    Handles parsing of OpenSSH client configuration format with support
    for Host patterns, wildcards, and canonical hostname resolution.
    """

    def __init__(self, config_path: Path | None = None):
        """Initialize SSH config parser.

        Args:
            config_path: Path to SSH config file. Defaults to ~/.ssh/config
        """
        if config_path is None:
            config_path = Path.home() / ".ssh" / "config"

        self.config_path = config_path
        self._parsed_config: list[dict[str, Any]] = []
        self._loaded = False

    def load(self) -> None:
        """Load and parse the SSH configuration file."""
        if not self.config_path.exists():
            log.debug("SSH config file not found: %s", self.config_path)
            self._parsed_config = []
            self._loaded = True
            return

        try:
            with self.config_path.open("r", encoding="utf-8") as f:
                content = f.read()

            self._parsed_config = self._parse_config(content)
            self._loaded = True
            log.debug(
                "Loaded SSH config from %s with %d entries",
                self.config_path,
                len(self._parsed_config),
            )

        except Exception as exc:
            log.warning(
                "Failed to load SSH config from %s: %s", self.config_path, exc
            )
            self._parsed_config = []
            self._loaded = True

    def get_user_for_host(
        self, hostname: str, port: int | None = None
    ) -> str | None:
        """Get the SSH user for a specific hostname.

        Args:
            hostname: Target hostname (e.g., "gerrit.linuxfoundation.org")
            port: Target port number (optional)

        Returns:
            SSH username if found in config, None otherwise
        """
        if not self._loaded:
            self.load()

        # Find matching host entries in order of precedence
        for entry in self._parsed_config:
            if self._host_matches(hostname, entry.get("host_patterns", [])):
                # Check port if specified in both config and request
                config_port = entry.get("port")
                if (
                    port is not None
                    and config_port is not None
                    and int(config_port) != port
                ):
                    continue

                user = entry.get("user")
                if user:
                    log.debug(
                        "Found SSH user '%s' for host '%s' (port %s)",
                        user,
                        hostname,
                        port,
                    )
                    return str(user)

        log.debug("No SSH user found for host '%s' (port %s)", hostname, port)
        return None

    def _parse_config(self, content: str) -> list[dict[str, Any]]:
        """Parse SSH config content into structured data.

        Args:
            content: Raw SSH config file content

        Returns:
            List of configuration entries with host patterns and directives
        """
        entries = []
        current_entry: dict[str, Any] = {}

        lines = content.splitlines()
        for line_num, raw_line in enumerate(lines, 1):
            line = raw_line.strip()

            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue

            # Split on whitespace, handling quoted values
            parts = self._split_config_line(line)
            if len(parts) < 2:
                continue

            keyword = parts[0].lower()

            if keyword == "host":
                if current_entry:
                    entries.append(current_entry)

                # Host patterns are all remaining parts
                host_patterns = parts[1:]
                current_entry = {
                    "host_patterns": host_patterns,
                    "line_num": line_num,
                }

            elif current_entry and keyword in [
                "user",
                "port",
                "hostname",
                "canonicaldomain",
                "canonicalizehostname",
            ]:
                # Store directive in current entry
                current_entry[keyword] = parts[1]

        # Don't forget the last entry
        if current_entry:
            entries.append(current_entry)

        return entries

    def _split_config_line(self, line: str) -> list[str]:
        """Split SSH config line respecting quoted values.

        Args:
            line: Configuration line to split

        Returns:
            List of configuration tokens
        """
        parts = []
        current = ""
        in_quotes = False

        i = 0
        while i < len(line):
            char = line[i]

            if char == '"' and (i == 0 or line[i - 1] != "\\"):
                in_quotes = not in_quotes
                # Don't include the quote in the output
                i += 1
                continue
            elif char in [" ", "\t"] and not in_quotes:
                if current:
                    parts.append(current)
                    current = ""
                # Skip whitespace
                while i + 1 < len(line) and line[i + 1] in [" ", "\t"]:
                    i += 1
            else:
                current += char

            i += 1

        if current:
            parts.append(current)

        return parts

    def _host_matches(self, hostname: str, patterns: list[str]) -> bool:
        """Check if hostname matches any of the host patterns.

        Args:
            hostname: Target hostname to match
            patterns: List of SSH host patterns (may contain wildcards)

        Returns:
            True if hostname matches any pattern
        """
        if not patterns:
            return False

        for pattern in patterns:
            if self._pattern_matches(hostname, pattern):
                return True

        return False

    def _pattern_matches(self, hostname: str, pattern: str) -> bool:
        """Check if hostname matches a specific SSH host pattern.

        Supports SSH-style wildcards (* and ?) and exact matches.

        Args:
            hostname: Target hostname
            pattern: SSH host pattern

        Returns:
            True if hostname matches the pattern
        """
        # Remove quotes if present
        pattern = pattern.strip("\"'")

        # Exact match
        if pattern == hostname:
            return True

        # Convert SSH pattern to regex
        # * matches any sequence of characters
        # ? matches any single character
        regex_pattern = pattern.replace(".", r"\.")
        regex_pattern = regex_pattern.replace("*", ".*")
        regex_pattern = regex_pattern.replace("?", ".")
        regex_pattern = f"^{regex_pattern}$"

        try:
            return bool(re.match(regex_pattern, hostname))
        except re.error:
            # An invalid regex cannot match. The exact-match case is
            # already handled above, so there is nothing left to match.
            return False


def get_ssh_user_for_gerrit(
    gerrit_host: str, gerrit_port: int = 29418
) -> str | None:
    """Get SSH user for Gerrit host from SSH configuration.

    Convenience function to extract SSH user for a Gerrit server from
    the user's SSH configuration.  The file is parsed on every call.
    Caching the parse was tried and removed: keying on the path served a
    stale parse once the file was rewritten, and keying on stat metadata
    still collides when an in-place rewrite preserves size and inode on
    a filesystem with coarse timestamps.  A correct key means hashing
    the contents, which costs more than the parse it saves -- around
    0.2ms for a fifty-host config, once or twice per process.

    Args:
        gerrit_host: Gerrit server hostname
        gerrit_port: Gerrit SSH port (default: 29418)

    Returns:
        SSH username if found in config, None otherwise
    """
    return SSHConfig().get_user_for_host(gerrit_host, gerrit_port)


def _get_respect_user_ssh_setting() -> bool:
    """Read the G2G_RESPECT_USER_SSH environment variable.

    Read on every call rather than memoised: cli._load_effective_inputs
    sets this variable at runtime when it detects local execution, so a
    value captured earlier in the process can disagree with the
    environment.  The flag selects between two entirely different
    credential sources, which makes a stale read a wrong-identity bug.

    Returns:
        Boolean value of G2G_RESPECT_USER_SSH environment variable
    """
    return env_bool("G2G_RESPECT_USER_SSH")


def _read_git_user_email() -> str | None:
    """Read the configured git user email.

    Read on every call rather than memoised, so that a change of git
    identity or working directory is reflected in derived credentials.

    Returns:
        Git user email if configured, None otherwise
    """
    return get_git_user_email()


def _validate_git_executable(git_path: str) -> bool:
    """Validate that the git path points to a legitimate git executable.

    Performs basic security checks to ensure the executable is safe to run.

    Args:
        git_path: Path to the git executable

    Returns:
        True if the executable appears to be legitimate git, False otherwise
    """
    try:
        import os
        from pathlib import Path

        # Check if the path exists and is a file
        path_obj = Path(git_path)
        if not path_obj.exists() or not path_obj.is_file():
            return False

        # Check if the file is executable
        if not os.access(git_path, os.X_OK):
            return False

        # Validate it's actually git by checking version (safe operation)
        try:
            result = subprocess.run(  # noqa: S603
                [git_path, "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            # Git should respond with version info starting with "git version"
            return (
                result.returncode == 0
                and "git version" in result.stdout.lower()
            )
        except (subprocess.TimeoutExpired, OSError):
            return False

    except Exception:
        # On any error, be conservative and reject
        return False


def get_git_user_email() -> str | None:
    """Get user email from git configuration.

    Reads the git user.email configuration value from the local
    git configuration.

    Returns:
        Git user email if configured, None otherwise
    """
    try:
        import shutil

        git_path = shutil.which("git")
        if not git_path:
            log.debug("Git executable not found in PATH")
            return None

        if not _validate_git_executable(git_path):
            log.debug("Git executable validation failed: %s", git_path)
            return None

        result = subprocess.run(  # noqa: S603
            [git_path, "config", "user.email"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

        if result.returncode == 0 and result.stdout.strip():
            email = result.stdout.strip()
            log.debug("Found git user email: %s", email)
            return email
        else:
            log.debug("No git user email configured")
            return None

    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("Failed to read git user email: %s", exc)
        return None


def derive_gerrit_credentials(
    gerrit_host: str, organization: str, gerrit_port: int = 29418
) -> tuple[str | None, str | None]:
    """Derive Gerrit SSH credentials using multiple sources.

    Attempts to derive Gerrit SSH username and email using the following
    priority order:
    1. SSH config user for the specific Gerrit host
    2. Git user email from local git configuration
    3. Fallback to organization-based derivation

    Deliberately not memoised.  The result depends on
    G2G_RESPECT_USER_SSH, on ~/.ssh/config and on the git identity, none
    of which are arguments, so a cache keyed on the arguments alone
    could serve a personal identity where the organization service
    account was asked for.

    Args:
        gerrit_host: Gerrit server hostname
        organization: GitHub organization name for fallback
        gerrit_port: Gerrit SSH port (default: 29418)

    Returns:
        Tuple of (ssh_username, email_address) where either may be None
    """

    log.debug(
        "Deriving Gerrit credentials for %s:%d (org: %s)",
        gerrit_host,
        gerrit_port,
        organization,
    )

    # Check if we should respect user SSH config (local mode)
    respect_user_ssh = _get_respect_user_ssh_setting()

    ssh_user = None
    git_email = None

    if respect_user_ssh:
        # Try SSH config first
        ssh_user = get_ssh_user_for_gerrit(gerrit_host, gerrit_port)
        # Try git config for email
        git_email = _read_git_user_email()
        log.debug(
            "Local mode: using SSH config user=%s, git email=%s",
            ssh_user,
            git_email,
        )

    # Fallback to organization-based derivation if needed
    fallback_user = f"{organization}.gh2gerrit"
    fallback_email = f"releng+{organization}-gh2gerrit@linuxfoundation.org"

    final_user = ssh_user if ssh_user else fallback_user
    final_email = git_email if git_email else fallback_email

    log.debug(
        "Derived credentials: user=%s (source: %s), email=%s (source: %s)",
        final_user,
        "ssh-config" if ssh_user else "org-fallback",
        final_email,
        "git-config" if git_email else "org-fallback",
    )

    return final_user, final_email
