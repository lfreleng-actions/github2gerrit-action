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
import threading
from collections.abc import Iterable
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
    "ISSUE_ID_LOOKUP_JSON",
    "COMMIT_RULES_JSON",
    "CLOSE_MERGED_PRS",
    "CREATE_MISSING",
    "AUTOMATION_ONLY",
    "CLEANUP_ABANDONED",
    "CLEANUP_GERRIT",
    "NORMALISE_COMMIT",
    "VERBOSE",
    "FORCE",
    "CI_TESTING",
    "USE_LOCAL_ACTION",
    "G2G_VERBOSE",
    "G2G_SKIP_GERRIT_COMMENTS",
    "G2G_ENABLE_DERIVATION",
    "G2G_AUTO_SAVE_CONFIG",
    "G2G_USE_SSH_AGENT",
    "G2G_APPROVER_LOGINS",
    "G2G_APPROVERS_FROM_INFO_YAML",
    "G2G_INFO_YAML_MATCH_LFID",
    "G2G_NO_GERRIT",
    "G2G_DISABLED",
    "G2G_TOPIC_PREFIX",
    "G2G_TRUSTED_ASSOCIATIONS",
    "G2G_LOG_LEVEL",
    "G2G_SHOW_PROGRESS",
    "G2G_RESPECT_USER_SSH",
    "G2G_DRYRUN_DISABLE_NETWORK",
    "G2G_ANON_SUPERSEDE_FALLBACK",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    # Gerrit event dispatch context (workflow_dispatch)
    "GERRIT_BRANCH",
    "GERRIT_CHANGE_URL",
    "GERRIT_EVENT_TYPE",
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
    "SIMILARITY_UPDATE_FACTOR",
    "SIMILARITY_FILES",
    "ALLOW_ORPHAN_CHANGES",
    "PERSIST_SINGLE_MAPPING_COMMENT",
    "LOG_RECONCILE_JSON",
    "VERIFY_DIGEST_STRICT",
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


def _sanitize_ssh_key_content(content_lines: list[str]) -> str:
    """Clean the base64 content of an inline multi-line SSH key value."""
    sanitized_lines: list[str] = []
    for content_line in content_lines:
        cleaned = content_line.strip()
        # Preserve SSH key headers/footers but clean base64 content
        if cleaned.startswith("-----") or not cleaned:
            sanitized_lines.append(cleaned)
            continue
        # Remove embedded quotes and all whitespace from base64 content.
        # Base64 bodies contain no whitespace, so stripping any embedded
        # spaces/tabs (e.g. from wrapped copy-paste) repairs the content
        # rather than corrupting it. Headers/footers are preserved above.
        cleaned = cleaned.replace('"', "").replace("'", "")
        cleaned = "".join(cleaned.split())
        if cleaned:
            sanitized_lines.append(cleaned)
    return "\\n".join(sanitized_lines)


def _consume_multiline_quote(
    lines: list[str],
    start: int,
    left: str,
    out_lines: list[str],
) -> int:
    """Collapse a `key = "` ... `"` block into a single escaped line.

    Returns the index of the next unprocessed line.
    """
    i = start + 1
    block: list[str] = []
    # Collect until a line with only a closing quote (ignoring spaces)
    while i < len(lines) and lines[i].strip() != '"':
        block.append(lines[i])
        i += 1
    if i < len(lines) and lines[i].strip() == '"':
        joined = "\\n".join(block)
        out_lines.append(f'{left} "{joined}"')
        return i + 1
    # No closing quote found; keep the original opening line.
    log.debug(
        "Multi-line quote not properly closed for line: %s",
        lines[start][:50],
    )
    out_lines.append(lines[start])
    return i


def _consume_inline_quote(
    lines: list[str],
    start: int,
    left: str,
    rhs: str,
    out_lines: list[str],
) -> int:
    """Collapse a value that opens with `"` but spans multiple lines.

    Handles SSH private keys and other values that start with a quote but
    contain embedded content that might otherwise confuse configparser.
    Returns the index of the next unprocessed line.
    """
    content_lines = [rhs[1:]]  # Remove opening quote
    i = start + 1
    while i < len(lines):
        current_line = lines[i]
        stripped = current_line.strip()
        if stripped.endswith('"') and not stripped.endswith('\\"'):
            # Found closing quote - remove it and add final line
            final_content = current_line.rstrip()
            if final_content.endswith('"'):
                final_content = final_content[:-1]
            # Only add if there's content after removing quote
            if final_content:
                content_lines.append(final_content)
            break
        content_lines.append(current_line)
        i += 1

    # Join all content and sanitize for SSH keys
    full_content = "\\n".join(content_lines)

    # Special handling for SSH private keys - remove extra whitespace
    # and line breaks
    key_name = left.split("=")[0].strip().upper()
    if "SSH" in key_name and "KEY" in key_name:
        full_content = _sanitize_ssh_key_content(content_lines)

    log.debug(
        "Processed multi-line value for key %s (length: %d)",
        left.split("=")[0].strip(),
        len(full_content),
    )
    out_lines.append(f'{left} "{full_content}"')
    return i + 1


def _preprocess_config_text(raw_text: str) -> str:
    """Collapse multi-line quoted values into single escaped lines.

    Pre-process simple multi-line quoted values of the form::

        key = "
        line1
        line2
        "

    We collapse these into a single line with '\\n' escapes so that
    configparser can ingest them reliably; later, _coerce_value()
    converts the escapes back to real newlines. SSH private keys and
    other multi-line values with formatting inconsistencies are
    sanitized as part of this process.
    """
    lines = raw_text.splitlines()
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        eq_idx = line.find("=")
        if eq_idx == -1:
            out_lines.append(line)
            i += 1
            continue

        left = line[: eq_idx + 1]
        rhs = line[eq_idx + 1 :].strip()

        # Handle standard multi-line quoted values: key = "
        if rhs == '"':
            i = _consume_multiline_quote(lines, i, left, out_lines)
            continue

        if rhs.startswith('"') and not rhs.endswith('"'):
            i = _consume_inline_quote(lines, i, left, rhs, out_lines)
            continue

        out_lines.append(line)
        i += 1

    return "\n".join(out_lines) + ("\n" if out_lines else "")


def _load_ini(path: Path) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser()
    # Preserve option case; mypy requires a cast for attribute requirement
    cast(Any, cp).optionxform = str
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw_text = fh.read()
        preprocessed = _preprocess_config_text(raw_text)
        cp.read_string(preprocessed)
    except FileNotFoundError as exc:
        log.debug("Config file not found: %s (%s)", path, exc)
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
    # Skip config file access in GitHub CI environment
    if _is_github_ci_mode():
        log.debug("GitHub CI mode detected: skipping configuration file access")
        return {}

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

    # Report unrecognized configuration keys to help users catch typos.
    # Unrecognized keys still apply to the environment (see
    # apply_config_to_env), so a typo produces a setting the tool
    # never reads rather than an error.
    unknown_keys = set(normalized.keys()) - KNOWN_KEYS
    log.debug("All parsed keys from config: %s", sorted(normalized.keys()))
    log.debug("Known keys: %s", sorted(KNOWN_KEYS))
    if unknown_keys:
        log.warning(
            "Unrecognized configuration keys found in [%s]: %s. "
            "These keys still export to the environment, but the tool "
            "does not consume them directly. Check for typos.",
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


# Provenance for configuration values.
#
# Several Gerrit parameters are *derived* when nothing configures them:
# the host from a `gerrit.[org].org` heuristic, the project from the
# GitHub repository name.  Once `apply_config_to_env` exports them they
# are indistinguishable from values an operator set deliberately, so a
# consumer with a better answer of its own (duplicate detection reads
# the pull request's `.gitreview`) cannot tell whether overriding the
# environment would be correcting a guess or discarding intent.
#
# Recording the derived keys answers that question.  The record lives in
# an environment variable because it describes *process-scoped
# configuration*, established once in `_load_effective_inputs` before
# the bulk `ThreadPoolExecutor` starts and identical for every pull
# request the process handles.  That is the opposite of per-pull-request
# state such as the approved SHA, which `cli.py` deliberately threads
# through return values precisely because concurrent workers would
# otherwise overwrite one another's values.
DERIVED_KEYS_ENV = "G2G_DERIVED_KEYS"

# Serialises the read-modify-write below.  The comment above explains
# why the record is process-scoped rather than per-pull-request, and
# that reasoning still holds -- but "established once before the pool
# starts" was an invariant stated in prose and enforced by nothing.  A
# second marking call from inside per-pull-request processing would
# interleave read and write between workers, and the lost key would
# silently turn a derived value back into apparent operator intent:
# the #386 bug, returning with no error to point at.
_DERIVED_KEYS_LOCK = threading.Lock()


def _derived_keys() -> set[str]:
    """Return the set of config keys currently recorded as derived."""
    raw = os.getenv(DERIVED_KEYS_ENV, "")
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def mark_derived_keys(keys: Iterable[str]) -> None:
    """Record which config keys hold derived fallbacks rather than
    operator intent.

    Additive: keys already recorded stay recorded, so several derivation
    passes accumulate rather than the last one winning.  The union is
    taken under a lock, so concurrent callers accumulate rather than
    overwriting one another.

    Args:
        keys: Config key names (case-insensitive).  Empty names are
            ignored.
    """
    incoming = {k.strip().upper() for k in keys if k and k.strip()}
    if not incoming:
        return
    with _DERIVED_KEYS_LOCK:
        os.environ[DERIVED_KEYS_ENV] = ",".join(
            sorted(_derived_keys() | incoming)
        )


def is_derived_key(key: str) -> bool:
    """Return ``True`` when ``key``'s current value came from derivation.

    A ``False`` answer covers both "explicitly configured" and "not set
    at all", so callers should test the value's presence separately.
    """
    return key.strip().upper() in _derived_keys()


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
    """Check if we're running within a GitHub Actions environment."""
    return (
        os.getenv("GITHUB_ACTIONS") == "true"
        or os.getenv("GITHUB_EVENT_NAME", "").strip() != ""
    )


def _is_github_ci_mode() -> bool:
    """Detect if running in GitHub CI environment.

    Returns:
        True if running in GitHub CI, False if running locally
    """
    return (
        os.getenv("GITHUB_ACTIONS") == "true"
        or os.getenv("GITHUB_EVENT_NAME", "").strip() != ""
    )


def _is_local_cli_context() -> bool:
    """Detect if running as local CLI tool."""
    return not _is_github_actions_context()


def _read_gitreview_host(repository: str | None = None) -> str | None:
    """Read the Gerrit host from the .gitreview file.

    Delegates to the shared :mod:`github2gerrit.gitreview` module which
    consolidates all ``.gitreview`` parsing and fetching logic.

    This runs during parameter derivation, before the pull request
    context is read, so provenance comes from ``PR_HEAD_REPO`` as the
    composite action exports it.

    ``GITHUB_HEAD_REF`` is consulted only when the head is known to live
    in the base repository.  A fork chooses its own branch name, and one
    matching a real base-repository branch would otherwise select that
    branch's host.  Keeping the head ref for a *trusted* head matters
    too: :meth:`Orchestrator._read_gitreview` reads the head tree in
    that case, and dropping it here would let derivation record one host
    while the pipeline pushes to another.

    Provenance that cannot be established counts as untrusted, so the
    absent-signal case falls back to the base ref alone.

    The local working-directory file is still read.  This layer cannot
    tell a pull request the *tool* is processing from one the *workflow*
    merely runs inside, so any attempt to skip it here also breaks
    legitimate local reads.  See issue #386: the fix belongs with the
    wider question of tracking where a configuration value came from.

    Args:
        repository: GitHub repository in owner/repo format (optional).
            Falls back to GITHUB_REPOSITORY env var.

    Returns:
        The host string from .gitreview, or None if unavailable.
    """
    from .gitreview import read_gitreview_host
    from .models import head_repo_is_trusted

    repo_full = (repository or os.getenv("GITHUB_REPOSITORY") or "").strip()
    head_repo = os.getenv("PR_HEAD_REPO", "").strip()
    trusted_head = head_repo_is_trusted(repo_full, head_repo)

    branches: list[str] = []
    if trusted_head:
        head_ref = os.getenv("GITHUB_HEAD_REF", "").strip()
        if head_ref:
            branches.append(head_ref)
    base_ref = os.getenv("GITHUB_BASE_REF", "").strip()
    if base_ref:
        branches.append(base_ref)

    return read_gitreview_host(
        repository,
        branches=tuple(branches),
        include_env_refs=False,
    )


def derive_gerrit_parameters(
    organization: str | None, repository: str | None = None
) -> dict[str, str]:
    """Derive Gerrit parameters using SSH config, git config, and org fallback.

    Priority order for server derivation:
    1. Per-org configuration file entry (GERRIT_SERVER)
    2. .gitreview host field (local file or fetched from GitHub)
    3. Heuristic fallback: gerrit.[org].org

    Priority order for credential derivation:
    1. SSH config user for gerrit.* hosts (checks generic and specific patterns)
    2. Git user email from local git configuration
    3. Fallback to organization-based derivation

    Args:
        organization: GitHub organization name for fallback
        repository: GitHub repository in owner/repo format (optional)

    Returns:
        Dict with derived parameter values:
        - GERRIT_SSH_USER_G2G: From SSH config or [org].gh2gerrit
        - GERRIT_SSH_USER_G2G_EMAIL: From git config or fallback email
        - GERRIT_SERVER: Resolved from config, .gitreview, or gerrit.[org].org
        - GERRIT_PROJECT: Derived from repository name if provided
    """
    if not organization:
        return {}

    org = organization.strip().lower()

    # Check if we have a config file entry for this organization
    config = load_org_config(org)
    configured_server = config.get("GERRIT_SERVER", "").strip()

    # Read .gitreview host only when no config file entry
    # (avoid unnecessary I/O)
    gitreview_host = (
        _read_gitreview_host(repository) if not configured_server else None
    )

    # Priority: config file > .gitreview > heuristic fallback
    gerrit_host = configured_server or gitreview_host or f"gerrit.{org}.org"

    if gitreview_host and not configured_server:
        log.debug(
            "Using Gerrit host from .gitreview: %s (instead of heuristic "
            "gerrit.%s.org)",
            gitreview_host,
            org,
        )

    # Derive GERRIT_PROJECT from repository if provided
    gerrit_project = ""
    if repository and "/" in repository:
        # Extract repo name from owner/repo format
        # For lfit/sandbox -> sandbox
        _owner, repo_name = repository.split("/", 1)
        gerrit_project = repo_name

    # Try to use SSH config and git config for personalized credentials
    try:
        from .ssh_config_parser import derive_gerrit_credentials

        ssh_user, git_email = derive_gerrit_credentials(gerrit_host, org)
    except ImportError:
        # Fallback to original behavior if ssh_config_parser not available
        result = {
            "GERRIT_SSH_USER_G2G": f"{org}.gh2gerrit",
            "GERRIT_SSH_USER_G2G_EMAIL": (
                f"releng+{org}-gh2gerrit@linuxfoundation.org"
            ),
            "GERRIT_SERVER": gerrit_host,
        }
        if gerrit_project:
            result["GERRIT_PROJECT"] = gerrit_project
        return result
    else:
        result = {
            "GERRIT_SSH_USER_G2G": ssh_user or f"{org}.gh2gerrit",
            "GERRIT_SSH_USER_G2G_EMAIL": git_email
            or (f"releng+{org}-gh2gerrit@linuxfoundation.org"),
            "GERRIT_SERVER": gerrit_host,
        }
        if gerrit_project:
            result["GERRIT_PROJECT"] = gerrit_project
        return result


def apply_parameter_derivation(
    cfg: dict[str, str],
    organization: str | None = None,
    repository: str | None = None,
    save_to_config: bool = True,
    *,
    mark_derived: bool = True,
) -> dict[str, str]:
    """Apply dynamic parameter derivation for missing Gerrit parameters.

    This function derives standard Gerrit parameters when they are not
    explicitly configured. The derivation is based on the GitHub organization
    and repository:

    - gerrit_ssh_user_g2g: [org].gh2gerrit
    - gerrit_ssh_user_g2g_email: releng+[org]-gh2gerrit@linuxfoundation.org
    - gerrit_server: gerrit.[org].org
    - gerrit_project: Derived from repository name
      (e.g., lfit/sandbox -> sandbox)

    Derivation behavior:
    - Default: Automatic derivation enabled (G2G_ENABLE_DERIVATION=true by
      default)
    - Can be disabled by setting G2G_ENABLE_DERIVATION=false

    Args:
        cfg: Configuration dictionary to augment
        organization: GitHub organization name for derivation
        repository: GitHub repository in owner/repo format (optional)
        save_to_config: Whether to save derived parameters to config file
        mark_derived: Whether to record the derived keys as provenance
            (see :func:`mark_derived_keys`).  Pass ``False`` from call
            sites that do not follow up with :func:`apply_config_to_env`,
            since a key that never reaches the environment must not be
            described as holding a derived value there.

    Returns:
        Configuration dictionary with derived values for missing parameters
    """
    # A GERRIT_PROJECT arriving from the configuration file is a
    # fallback, never intent for this specific repository: the file is
    # sectioned per-organization, so one project name there is wrong for
    # every repository in the org but one. That covers entries auto-saved
    # by releases before such writes stopped, and hand-written ones,
    # which are equally misscoped. Marking it derived keeps it usable as
    # a last resort while letting the per-pull-request .gitreview
    # outrank it.
    #
    # Recorded ahead of the early returns below, because this describes
    # what cfg already carries rather than anything derivation adds.
    # Neither a missing organization nor G2G_ENABLE_DERIVATION=false
    # makes a config-file project any more like operator intent, and
    # apply_config_to_env still exports it in both cases.
    if (
        mark_derived
        and cfg.get("GERRIT_PROJECT", "").strip()
        and (os.getenv("GERRIT_PROJECT") or "").strip() == ""
    ):
        mark_derived_keys(["GERRIT_PROJECT"])

    if not organization:
        return cfg

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
    derived = derive_gerrit_parameters(organization, repository)
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
        if mark_derived:
            # `newly_derived` records what derivation *supplied*, not
            # what will end up in the environment.  `apply_config_to_env`
            # writes a key only when its environment value is currently
            # empty, so a key derived here can still be shadowed by an
            # explicit environment value and must not be recorded as
            # derived.  Apply the same emptiness test the caller will
            # apply moments later; the two must agree.
            mark_derived_keys(
                key
                for key in newly_derived
                if (os.getenv(key) or "").strip() == ""
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
        derived_params: Dictionary of parameter names to values
        config_path: Path to config file (optional, uses default if not
            provided)
    """
    # Skip config file writes during dry-run mode
    if os.getenv("DRY_RUN", "").lower() in ("true", "1", "yes"):
        log.debug("Skipping config file write in dry-run mode")
        return
    if not organization or not derived_params:
        return

    # GERRIT_PROJECT is per-repository, but this file section is
    # per-organization: persisting it would hand every other repository
    # in the org one repository's project name, and would additionally
    # come back on the next run as configuration indistinguishable from
    # operator intent, defeating the provenance tracking that lets
    # duplicate detection prefer a per-pull-request .gitreview.
    derived_params = {
        k: v for k, v in derived_params.items() if k != "GERRIT_PROJECT"
    }
    if not derived_params:
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
