# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
#
# Configuration loader for github2gerrit.
#
# This module provides a simple INI-based configuration system that lets
# you define per-organization settings in a file such as:
#
#   ~/.config/github2gerrit/configuration.txt
#
# Example:
#
#   [default]
#   GERRIT_SERVER = "gerrit.example.org"
#   GERRIT_SERVER_PORT = "29418"
#
#   [onap]
#   GERRIT_HTTP_USER = "modesevenindustrialsolutions"
#   GERRIT_HTTP_PASSWORD = "my_gerrit_token"
#   GERRIT_PROJECT = "integration/test-repo"
#   REVIEWERS_EMAIL = "a@example.org,b@example.org"
#   PRESERVE_GITHUB_PRS = "true"
#
# Values are returned as strings with surrounding quotes stripped.
# Boolean-like values are normalized to "true"/"false" strings.
# You can reference environment variables using ${ENV:VAR_NAME}.
#
# Precedence model (recommended):
#   - CLI flags (highest)
#   - Environment variables
#   - Config file values (loaded by this module)
#   - Tool defaults (lowest)
#
# Callers can:
#   - load_org_config() to retrieve a dict of key->value (strings)
#   - apply_config_to_env() to export values to process environment for
#     any keys not already set by the environment/runner
#
# Notes:
#   - Section names are matched case-insensitively.
#   - If no organization is provided, we try ORGANIZATION, then
#     GITHUB_REPOSITORY_OWNER from the environment.
#   - A [default] section can provide baseline values for all orgs.
#   - Unknown keys are preserved (uppercased) to keep this future-proof.

from __future__ import annotations

import configparser
import logging
import os
import re
from pathlib import Path
from typing import Any
from typing import cast


log = logging.getLogger("github2gerrit.config")

DEFAULT_CONFIG_PATH = "~/.config/github2gerrit/configuration.txt"

# Recognized keys. Unknown keys will be reported as warnings to help
# users catch typos and missing functionality.
KNOWN_KEYS: set[str] = {
    # Action inputs
    "SUBMIT_SINGLE_COMMITS",
    "USE_PR_AS_COMMIT",
    "FETCH_DEPTH",
    "GERRIT_KNOWN_HOSTS",
    "GERRIT_SSH_PRIVKEY_G2G",
    "GERRIT_SSH_USER_G2G",
    "GERRIT_SSH_USER_G2G_EMAIL",
    "ORGANIZATION",
    "REVIEWERS_EMAIL",
    "PR_NUMBER",
    "SYNC_ALL_OPEN_PRS",
    "PRESERVE_GITHUB_PRS",
    "ALLOW_GHE_URLS",
    "DRY_RUN",
    "ALLOW_DUPLICATES",
    "DUPLICATE_TYPES",
    "ISSUE_ID",
    "G2G_VERBOSE",
    "G2G_SKIP_GERRIT_COMMENTS",
    "G2G_ENABLE_DERIVATION",
    "G2G_AUTO_SAVE_CONFIG",
    "GITHUB_TOKEN",
    # Optional inputs (reusable workflow compatibility)
    "GERRIT_SERVER",
    "GERRIT_SERVER_PORT",
    "GERRIT_HTTP_BASE_PATH",
    "GERRIT_PROJECT",
    # Gerrit REST auth
    "GERRIT_HTTP_USER",
    "GERRIT_HTTP_PASSWORD",
    # Reconciliation configuration
    "REUSE_STRATEGY",
    "SIMILARITY_SUBJECT",
    "SIMILARITY_FILES",
    "ALLOW_ORPHAN_CHANGES",
    "PERSIST_SINGLE_MAPPING_COMMENT",
    "LOG_RECONCILE_JSON",
}

_ENV_REF = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_refs(value: str) -> str:
    """Expand ${ENV:VAR} references using current environment."""

    def repl(match: re.Match[str]) -> str:
        var = match.group(1)
        return os.getenv(var, "") or ""

    return _ENV_REF.sub(repl, value)


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        return v[1:-1]
    return v


def _normalize_bool_like(value: str) -> str | None:
    """Return 'true'/'false' for boolean-like values, else None."""
    s = value.strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return "true"
    if s in {"0", "false", "no", "off"}:
        return "false"
    return None


def _coerce_value(raw: str) -> str:
    """Coerce a raw string to normalized representation."""
    expanded = _expand_env_refs(raw)
    unquoted = _strip_quotes(expanded)
    # Normalize escaped newline sequences into real newlines so that values
    # like SSH keys or known_hosts entries can be specified inline using
    # '\n' or '\r\n' in configuration files.
    normalized_newlines = (
        unquoted.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\r\n", "\n")
    )

    # Additional sanitization for SSH private keys
    if (
        "-----BEGIN" in normalized_newlines
        and "PRIVATE KEY-----" in normalized_newlines
    ) or (
        "ssh-" in normalized_newlines.lower()
        and "key" in normalized_newlines.lower()
    ):
        # Clean up SSH key formatting: remove extra whitespace, normalize
        # line endings
        lines = normalized_newlines.split("\n")
        sanitized_lines = []
        for line in lines:
            cleaned = line.strip()
            if cleaned:
                # Remove any stray quotes that might have been embedded in the
                # key content
                cleaned = cleaned.replace('"', "").replace("'", "")
                sanitized_lines.append(cleaned)
        normalized_newlines = "\n".join(sanitized_lines)

    b = _normalize_bool_like(normalized_newlines)
    return b if b is not None else normalized_newlines


def _select_section(
    cp: configparser.RawConfigParser,
    org: str,
) -> str | None:
    """Find a section name case-insensitively."""
    target = org.strip().lower()
    for sec in cp.sections():
        if sec.strip().lower() == target:
            return sec
    return None


def _load_ini(path: Path) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser()
    # Preserve option case; mypy requires a cast for attribute requirement
    cast(Any, cp).optionxform = str
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw_text = fh.read()
        # Pre-process simple multi-line quoted values of the form:
        #   key = "
        #   line1
        #   line2
        #   "
        # We collapse these into a single line with '\n' escapes so that
        # configparser can ingest them reliably; later, _coerce_value()
        # converts the escapes back to real newlines.
        #
        # We also handle SSH private keys and other multi-line values that
        # might have formatting inconsistencies by sanitizing them.
        lines = raw_text.splitlines()
        out_lines: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            eq_idx = line.find("=")
            if eq_idx != -1:
                left = line[: eq_idx + 1]
                rhs = line[eq_idx + 1 :].strip()

                # Handle standard multi-line quoted values: key = "
                if rhs == '"':
                    i += 1
                    block: list[str] = []
                    # Collect until a line with only a closing quote
                    # (ignoring spaces)
                    while i < len(lines) and lines[i].strip() != '"':
                        block.append(lines[i])
                        i += 1
                    if i < len(lines) and lines[i].strip() == '"':
                        joined = "\\n".join(block)
                        out_lines.append(f'{left} "{joined}"')
                        i += 1
                        continue
                    else:
                        # No closing quote found; fall through
                        # and keep original line
                        log.debug(
                            "Multi-line quote not properly closed for line: %s",
                            line[:50],
                        )
                        out_lines.append(line)
                        continue

                # Handle SSH private keys and other values that start with a
                # quote
                # but contain embedded content that might confuse configparser
                elif rhs.startswith('"') and not rhs.endswith('"'):
                    # This looks like a multi-line value that starts on the
                    # same line
                    # Collect all content until we find a line ending with a
                    # quote
                    content_lines = [rhs[1:]]  # Remove opening quote
                    i += 1

                    while i < len(lines):
                        current_line = lines[i]
                        if current_line.strip().endswith(
                            '"'
                        ) and not current_line.strip().endswith('\\"'):
                            # Found closing quote - remove it and add final line
                            final_content = current_line.rstrip()
                            if final_content.endswith('"'):
                                final_content = final_content[:-1]
                            # Only add if there's content after removing quote
                            if final_content:
                                content_lines.append(final_content)
                            break
                        else:
                            content_lines.append(current_line)
                        i += 1

                    # Join all content and sanitize for SSH keys
                    full_content = "\\n".join(content_lines)

                    # Special handling for SSH private keys - remove extra
                    # whitespace and line breaks
                    key_name = left.split("=")[0].strip().upper()
                    if "SSH" in key_name and "KEY" in key_name:
                        # For SSH keys, clean up base64 content by removing
                        # whitespace within lines
                        sanitized_lines = []
                        for content_line in content_lines:
                            cleaned = content_line.strip()
                            # Preserve SSH key headers/footers but clean base64
                            # content
                            if cleaned.startswith("-----") or not cleaned:
                                sanitized_lines.append(cleaned)
                            else:
                                # Remove any embedded quotes and whitespace from
                                # base64 content
                                cleaned = (
                                    cleaned.replace('"', "")
                                    .replace("'", "")
                                    .strip()
                                )
                                if cleaned:
                                    sanitized_lines.append(cleaned)
                        full_content = "\\n".join(sanitized_lines)

                    log.debug(
                        "Processed multi-line value for key %s (length: %d)",
                        left.split("=")[0].strip(),
                        len(full_content),
                    )
                    out_lines.append(f'{left} "{full_content}"')
                    i += 1
                    continue

            out_lines.append(line)
            i += 1

        preprocessed = "\n".join(out_lines) + ("\n" if out_lines else "")
        cp.read_string(preprocessed)
    except FileNotFoundError:
        log.debug("Config file not found: %s", path)
    except Exception as exc:
        log.warning("Failed to read config file %s: %s", path, exc)
    return cp


def _detect_org() -> str | None:
    # Prefer explicit ORGANIZATION, then GitHub default env var
    org = os.getenv("ORGANIZATION", "").strip()
    if org:
        return org
    owner = os.getenv("GITHUB_REPOSITORY_OWNER", "").strip()
    return owner or None


def _merge_dicts(
    base: dict[str, str],
    override: dict[str, str],
) -> dict[str, str]:
    out = dict(base)
    out.update(override)
    return out


def _normalize_keys(d: dict[str, str]) -> dict[str, str]:
    return {k.strip().upper(): v for k, v in d.items() if k.strip()}


def load_org_config(
    org: str | None = None,
    path: str | Path | None = None,
) -> dict[str, str]:
    """Load configuration for a GitHub organization.

    Args:
      org:
        Name of the GitHub org (stanza). If not provided, inferred from
        ORGANIZATION or GITHUB_REPOSITORY_OWNER environment variables.
      path:
        Path to the INI file. If not provided, uses:
        ~/.config/github2gerrit/configuration.txt
        If G2G_CONFIG_PATH is set, it takes precedence.

    Returns:
      A dict mapping KEY -> value (strings). Unknown keys are preserved,
      known boolean-like values are normalized to 'true'/'false', quotes
      are stripped, and ${ENV:VAR} are expanded.
    """
    if path is None:
        path = os.getenv("G2G_CONFIG_PATH", "").strip() or DEFAULT_CONFIG_PATH
    cfg_path = Path(path).expanduser()

    cp = _load_ini(cfg_path)
    effective_org = org or _detect_org()
    result: dict[str, str] = {}

    # Start with [default]
    if cp.has_section("default"):
        for k, v in cp.items("default"):
            result[k.strip().upper()] = _coerce_value(v)

    # Overlay with [org] if present
    if effective_org:
        chosen = _select_section(cp, effective_org)
        if chosen:
            for k, v in cp.items(chosen):
                result[k.strip().upper()] = _coerce_value(v)
        else:
            log.debug(
                "Org section '%s' not found in %s",
                effective_org,
                cfg_path,
            )

    normalized = _normalize_keys(result)

    # Report unknown configuration keys to help users catch typos
    unknown_keys = set(normalized.keys()) - KNOWN_KEYS
    log.debug("All parsed keys from config: %s", sorted(normalized.keys()))
    log.debug("Known keys: %s", sorted(KNOWN_KEYS))
    if unknown_keys:
        log.warning(
            "Unknown configuration keys found in [%s]: %s. "
            "These will be ignored. Check for typos or missing functionality.",
            effective_org or "default",
            ", ".join(sorted(unknown_keys)),
        )

    return normalized


def apply_config_to_env(cfg: dict[str, str]) -> None:
    """Set environment variables for any keys not already set.

    This is useful to make configuration values visible to downstream
    code that reads via os.environ, while still letting explicit env
    or CLI flags take precedence.

    We only set keys that are not already present in the environment.
    """
    for k, v in cfg.items():
        if (os.getenv(k) or "").strip() == "":
            os.environ[k] = v


def filter_known(
    cfg: dict[str, str],
    include_extra: bool = True,
) -> dict[str, str]:
    """Return a filtered view of cfg.

    If include_extra is False, only keys from KNOWN_KEYS are included.
    If True (default), all keys are included.
    """
    if include_extra:
        return dict(cfg)
    return {k: v for k, v in cfg.items() if k in KNOWN_KEYS}


def _is_github_actions_context() -> bool:
    """Detect if running in GitHub Actions environment."""
    return (
        os.getenv("GITHUB_ACTIONS") == "true"
        or os.getenv("GITHUB_EVENT_NAME", "").strip() != ""
    )


def _is_local_cli_context() -> bool:
    """Detect if running as local CLI tool."""
    return not _is_github_actions_context()


def derive_gerrit_parameters(organization: str | None) -> dict[str, str]:
    """Derive Gerrit parameters from GitHub organization name.

    Args:
        organization: GitHub organization name

    Returns:
        Dict with derived parameter values:
        - GERRIT_SSH_USER_G2G: [org].gh2gerrit
        - GERRIT_SSH_USER_G2G_EMAIL: releng+[org]-gh2gerrit@linuxfoundation.org
        - GERRIT_SERVER: gerrit.[org].org
    """
    if not organization:
        return {}

    org = organization.strip().lower()
    return {
        "GERRIT_SSH_USER_G2G": f"{org}.gh2gerrit",
        "GERRIT_SSH_USER_G2G_EMAIL": (
            f"releng+{org}-gh2gerrit@linuxfoundation.org"
        ),
        "GERRIT_SERVER": f"gerrit.{org}.org",
    }


def apply_parameter_derivation(
    cfg: dict[str, str],
    organization: str | None = None,
    save_to_config: bool = True,
) -> dict[str, str]:
    """Apply dynamic parameter derivation for missing Gerrit parameters.

    This function derives standard Gerrit parameters when they are not
    explicitly configured. The derivation is based on the GitHub organization:

    - gerrit_ssh_user_g2g: [org].gh2gerrit
    - gerrit_ssh_user_g2g_email: releng+[org]-gh2gerrit@linuxfoundation.org
    - gerrit_server: gerrit.[org].org

    Derivation behavior:
    - Default: Automatic derivation enabled (G2G_ENABLE_DERIVATION=true by
      default)
    - Can be disabled by setting G2G_ENABLE_DERIVATION=false

    Args:
        cfg: Configuration dictionary to augment
        organization: GitHub organization name for derivation
        save_to_config: Whether to save derived parameters to config file

    Returns:
        Configuration dictionary with derived values for missing parameters
    """
    if not organization:
        return cfg

    # Check execution context to determine derivation strategy
    is_github_actions = _is_github_actions_context()
    enable_derivation = os.getenv(
        "G2G_ENABLE_DERIVATION", "true"
    ).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    if not enable_derivation:
        log.debug(
            "Parameter derivation disabled. Set G2G_ENABLE_DERIVATION=true to "
            "enable automatic derivation."
        )
        return cfg

    # Only derive parameters that are missing or empty
    derived = derive_gerrit_parameters(organization)
    result = dict(cfg)
    newly_derived = {}

    for key, value in derived.items():
        if key not in result or not result[key].strip():
            log.debug(
                "Deriving %s from organization '%s': %s (context: %s)",
                key,
                organization,
                value,
                "GitHub Actions" if is_github_actions else "Local CLI",
            )
            result[key] = value
            newly_derived[key] = value

    if newly_derived:
        log.debug(
            "Derived parameters applied for organization '%s' (%s): %s",
            organization,
            "GitHub Actions" if is_github_actions else "Local CLI",
            ", ".join(f"{k}={v}" for k, v in newly_derived.items()),
        )
    # Save newly derived parameters to configuration file for future use
    # Default to true for local CLI, false for GitHub Actions
    default_auto_save = "false" if _is_github_actions_context() else "true"
    auto_save_enabled = os.getenv(
        "G2G_AUTO_SAVE_CONFIG", default_auto_save
    ).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if save_to_config and newly_derived and auto_save_enabled:
        # Save to config in local CLI mode to create persistent configuration
        try:
            save_derived_parameters_to_config(organization, newly_derived)
            log.debug(
                "Automatically saved derived parameters to configuration "
                "file for organization '%s'. "
                "This creates a persistent configuration that you can "
                "customize if needed.",
                organization,
            )
        except Exception as exc:
            log.warning("Failed to save derived parameters to config: %s", exc)

    return result


def save_derived_parameters_to_config(
    organization: str,
    derived_params: dict[str, str],
    config_path: str | None = None,
) -> None:
    """Save derived parameters to the organization's configuration file.

    This function updates the configuration file to include any derived
    parameters that are not already present in the organization section.
    This creates a persistent configuration that users can modify if needed.

    Args:
        organization: GitHub organization name for config section
        derived_params: Dictionary of derived parameter key-value pairs
        config_path: Path to config file (optional, uses default if not
                    provided)

    Raises:
        Exception: If saving fails
    """
    if not organization or not derived_params:
        return

    if config_path is None:
        config_path = (
            os.getenv("G2G_CONFIG_PATH", "").strip() or DEFAULT_CONFIG_PATH
        )

    config_file = Path(config_path).expanduser()

    try:
        # Only update when a configuration file already exists
        if not config_file.exists():
            log.debug(
                "Configuration file does not exist; skipping auto-save of "
                "derived parameters: %s",
                config_file,
            )
            return

        # Parse existing content using configparser
        cp = _load_ini(config_file)

        # Find or create the organization section
        org_section = _select_section(cp, organization)
        if org_section is None:
            # Section doesn't exist, we'll need to add it
            cp.add_section(organization)
            org_section = organization

        # Add derived parameters that don't already exist
        params_added = []
        for key, value in derived_params.items():
            if not cp.has_option(org_section, key):
                cp.set(org_section, key, f'"{value}"')
                params_added.append(key)

        # Only write if we added parameters
        if params_added:
            # Write the updated configuration
            with config_file.open("w", encoding="utf-8") as f:
                cp.write(f)

            log.debug(
                "Saved derived parameters to configuration file %s [%s]: %s",
                config_file,
                organization,
                ", ".join(params_added),
            )

    except Exception as exc:
        log.warning(
            "Failed to save derived parameters to configuration file %s: %s",
            config_file,
            exc,
        )


def overlay_missing(
    primary: dict[str, str],
    fallback: dict[str, str],
) -> dict[str, str]:
    """Merge fallback into primary for any missing keys.

    This is a helper when composing precedence:
      merged = overlay_missing(env_view, config_view)
    """
    merged = dict(primary)
    for k, v in fallback.items():
        if k not in merged or merged[k] == "":
            merged[k] = v
    return merged
