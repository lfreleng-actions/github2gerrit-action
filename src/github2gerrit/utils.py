# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Common utilities used across multiple modules.

This module consolidates helper functions that were previously duplicated
across cli.py, core.py, and gitutils.py to reduce maintenance overhead
and ensure consistent behavior.
"""

import logging
import os
from typing import Any


def env_bool(name: str, default: bool = False) -> bool:
    """Parse boolean environment variable correctly handling string values.

    Args:
        name: Environment variable name
        default: Default value if variable is not set

    Returns:
        Boolean value parsed from environment variable
    """
    val = os.getenv(name)
    if val is None:
        return default
    s = val.strip().lower()
    return s in ("1", "true", "yes", "on")


def env_str(name: str, default: str = "") -> str:
    """Get string environment variable with default fallback.

    Args:
        name: Environment variable name
        default: Default value if variable is not set

    Returns:
        String value from environment variable or default
    """
    val = os.getenv(name)
    return val if val is not None else default


def parse_bool_env(value: str | None) -> bool:
    """Parse boolean environment variable correctly handling string values.

    Args:
        value: String value to parse as boolean

    Returns:
        Boolean value parsed from string
    """
    if value is None:
        return False
    s = value.strip().lower()
    return s in ("1", "true", "yes", "on")


def is_verbose_mode() -> bool:
    """Check if verbose mode is enabled via environment variable.

    Returns:
        True if G2G_VERBOSE environment variable is set to a truthy value
    """
    return os.getenv("G2G_VERBOSE", "").lower() in ("true", "1", "yes")


def log_exception_conditionally(logger: logging.Logger, message: str, *args: Any) -> None:
    """Log exception with traceback only if verbose mode is enabled.

    Args:
        logger: Logger instance to use
        message: Log message format string
        *args: Arguments for message formatting
    """
    if is_verbose_mode():
        logger.exception(message, *args)
    else:
        logger.error(message, *args)


def append_github_output(outputs: dict[str, str]) -> None:
    """Append key-value pairs to GitHub Actions output file.

    This function writes outputs to the GITHUB_OUTPUT file for use by
    subsequent steps in a GitHub Actions workflow. It handles multiline
    values using heredoc syntax when running in GitHub Actions.

    Args:
        outputs: Dictionary of key-value pairs to write to output
    """
    gh_out = os.getenv("GITHUB_OUTPUT")
    if not gh_out:
        return

    try:
        with open(gh_out, "a", encoding="utf-8") as fh:
            for key, val in outputs.items():
                if not val:
                    continue
                if "\n" in val:
                    fh.write(f"{key}<<G2G\n")
                    fh.write(f"{val}\n")
                    fh.write("G2G\n")
                else:
                    fh.write(f"{key}={val}\n")
    except Exception as exc:
        # Use a basic logger since we can't import from other modules
        # without creating circular dependencies
        logging.getLogger(__name__).debug("Failed to write GITHUB_OUTPUT: %s", exc)
