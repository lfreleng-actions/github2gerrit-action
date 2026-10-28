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

import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from .config_ini import DEFAULT_CONFIG_PATH as DEFAULT_CONFIG_PATH
from .config_ini import _coerce_value
from .config_ini import _load_ini
from .config_ini import _select_section
from .config_ini import (
    save_derived_parameters_to_config as save_derived_parameters_to_config,
)
from .project_names import ProjectSource
from .utils import env_bool
from .utils import setting_bool


log = logging.getLogger("github2gerrit.config")

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
    "SKIP_FORKS",
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
    "G2G_RESOLVE_PROJECT_VIA_GERRIT",
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

# Which derived keys hold a *guess*: a value inferred with nothing to
# confirm it, as opposed to one read from `.gitreview`, confirmed against
# Gerrit, or written in a configuration file. A guess is worth exporting
# where nothing else names a project, because a Gerrit query against it
# costs nothing if wrong; a push against it does not, and the consumer
# that pushes needs to be able to tell. Same scoping and same rules as
# DERIVED_KEYS_ENV; a key recorded here is always also recorded there.
GUESSED_KEYS_ENV = "G2G_GUESSED_KEYS"

# Serialises the read-modify-write below.  The comment above explains
# why the record is process-scoped rather than per-pull-request, and
# that reasoning still holds -- but "established once before the pool
# starts" was an invariant stated in prose and enforced by nothing.  A
# second marking call from inside per-pull-request processing would
# interleave read and write between workers, and the lost key would
# silently turn a derived value back into apparent operator intent:
# the #386 bug, returning with no error to point at.
_DERIVED_KEYS_LOCK = threading.Lock()


def _keys_recorded_in(env_name: str) -> set[str]:
    """Return the config keys currently recorded in *env_name*."""
    raw = os.getenv(env_name, "")
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def _record_keys_in(env_name: str, keys: Iterable[str]) -> None:
    """Add *keys* to the record in *env_name*, under the lock."""
    incoming = {k.strip().upper() for k in keys if k and k.strip()}
    if not incoming:
        return
    with _DERIVED_KEYS_LOCK:
        os.environ[env_name] = ",".join(
            sorted(_keys_recorded_in(env_name) | incoming)
        )


def _derived_keys() -> set[str]:
    """Return the set of config keys currently recorded as derived."""
    return _keys_recorded_in(DERIVED_KEYS_ENV)


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
    _record_keys_in(DERIVED_KEYS_ENV, keys)


def mark_guessed_keys(keys: Iterable[str]) -> None:
    """Record which derived keys hold a guess; see :data:`GUESSED_KEYS_ENV`.

    Callers record a key here in addition to :func:`mark_derived_keys`,
    never instead of it.
    """
    _record_keys_in(GUESSED_KEYS_ENV, keys)


def is_derived_key(key: str) -> bool:
    """Return ``True`` when ``key``'s current value came from derivation.

    A ``False`` answer covers both "explicitly configured" and "not set
    at all", so callers should test the value's presence separately.
    """
    return key.strip().upper() in _derived_keys()


def is_guessed_key(key: str) -> bool:
    """Return ``True`` when ``key``'s derived value is a guess."""
    return key.strip().upper() in _keys_recorded_in(GUESSED_KEYS_ENV)


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


def _read_gitreview_info(repository: str | None = None) -> Any:
    """Read ``.gitreview`` host and project for the current context.

    Delegates to :func:`gitreview.read_gitreview_for_context`, which
    holds the provenance rules.  Kept as a module attribute so tests can
    substitute the read.
    """
    from .gitreview import read_gitreview_for_context

    return read_gitreview_for_context(repository)


def _read_gitreview_host(repository: str | None = None) -> str | None:
    """Return only the host from :func:`_read_gitreview_info`."""
    info = _read_gitreview_info(repository)
    return info.host if info else None


@dataclass(frozen=True)
class DerivedParameters:
    """What :func:`derive_gerrit_parameters_detailed` worked out.

    Attributes:
        values: Config keys to their derived values, as
            :func:`derive_gerrit_parameters` returns them.
        project_source: Where ``GERRIT_PROJECT`` in *values* came from;
            see :data:`project_names.ProjectSource`.  ``None`` when no
            project was derived.  Two questions hang on it: only a
            *confirmed* project (``.gitreview`` or Gerrit) may displace
            one somebody wrote in a configuration file, and a *guess* is
            worth exporting for queries but must stay labelled so the
            push refuses it.
        host_from_gitreview: ``True`` when ``GERRIT_SERVER`` in *values*
            is the ``.gitreview`` host.  It travels with the project as
            one target and, like a confirmed project, may displace a
            per-organization configuration value.
        port_from_gitreview: ``True`` when ``GERRIT_SERVER_PORT`` in
            *values* is the ``.gitreview`` port.  Tracked apart from the
            host: each field has its own precedence, so the file can win
            the port while an explicit server wins the host.  The port
            is exported only when the file supplies it; without a file
            there is nothing to derive it from, and the Gerrit default
            applies downstream.
    """

    values: dict[str, str] = field(default_factory=dict)
    project_source: ProjectSource | None = None
    host_from_gitreview: bool = False
    port_from_gitreview: bool = False

    @property
    def project_confirmed(self) -> bool:
        """``True`` when ``.gitreview`` or Gerrit supplied the project."""
        return self.project_source in ("gitreview", "gerrit")

    @property
    def project_guessed(self) -> bool:
        """``True`` when the project is a reading of a hyphenated name."""
        return self.project_source == "guess"


def derive_gerrit_parameters(
    organization: str | None, repository: str | None = None
) -> dict[str, str]:
    """Derive Gerrit parameters; see :func:`derive_gerrit_parameters_detailed`.

    Returns only the derived values.  Callers that must know whether
    the project is a guess use the detailed form.
    """
    return derive_gerrit_parameters_detailed(organization, repository).values


def _derive_gerrit_project(
    repository: str | None,
    gitreview: Any,
    gerrit_host: str,
    config: dict[str, str],
) -> tuple[str, ProjectSource | None]:
    """Derive ``GERRIT_PROJECT`` from ``.gitreview``, else the repository.

    A ``.gitreview`` read on behalf of a pull request whose base ref is
    not yet known came from a default branch, which may not be the
    branch the pull request targets; its host is kept (servers do not
    vary by branch, and local SSH-identity derivation needs one) but
    its project is not this pull request's to export.  Without a
    ``.gitreview`` project the lookup may ask Gerrit, on *gerrit_host*,
    the effective host the caller settled.  The loaded configuration
    goes along because this runs before the file has been exported to
    the environment, and the paths that consume the result never see
    the orchestrator's later, environment-only reading of the same
    settings.  An explicit ``GERRIT_PROJECT`` in the environment makes
    the lookup moot, exactly as ``.gitreview`` does:
    :func:`apply_config_to_env` would discard the answer.

    Returns the project (``""`` when the repository is unusable) and its
    :data:`project_names.ProjectSource`.
    """
    if not repository or "/" not in repository:
        return "", None

    from .gitreview import context_provenance
    from .project_names import opted_in_gerrit_project_lister
    from .project_names import resolve_repo_names

    gitreview_project = (gitreview.project if gitreview else "").strip()
    if gitreview_project and context_provenance(repository).provisional:
        log.debug(
            "Base ref not yet known for this pull request; not taking "
            "project %r from a default-branch .gitreview",
            gitreview_project,
        )
        gitreview_project = ""
    lookup_moot = bool(gitreview_project) or bool(
        (os.getenv("GERRIT_PROJECT") or "").strip()
    )
    names = resolve_repo_names(
        repository,
        gitreview_project=gitreview_project or None,
        list_projects=None
        if lookup_moot
        else opted_in_gerrit_project_lister(gerrit_host, config=config),
    )
    return names.project_gerrit, names.source


def derive_gerrit_parameters_detailed(
    organization: str | None, repository: str | None = None
) -> DerivedParameters:
    """Derive Gerrit parameters using SSH config, git config, and org fallback.

    Priority order for server derivation:
    1. An explicit ``GERRIT_SERVER`` already in the environment (one the
       operator set, not one an earlier derivation pass exported); this
       is the host the orchestrator pushes to
    2. .gitreview host field (local file or fetched from GitHub)
    3. Per-org configuration file entry (GERRIT_SERVER)
    4. Heuristic fallback: gerrit.[org].org

    The host and the project are exported as one target: the closed-
    pull-request handler and the cleanup sweeps query them together,
    so a project resolved from ``.gitreview`` (or confirmed against its
    host) must travel with that host, not with one from a per-org file.

    Priority order for project derivation:
    1. .gitreview project field, from the same read as the host
    2. Gerrit's own project list, when ``G2G_RESOLVE_PROJECT_VIA_GERRIT``
       is enabled and the run permits Gerrit calls
    3. The GitHub repository name, every hyphen read as a path
       separator (see :mod:`github2gerrit.project_names`); a guess when
       the name has hyphens, and reported as one

    An explicit ``GERRIT_PROJECT`` already in the environment outranks
    all of these and survives :func:`apply_config_to_env` untouched, so
    the Gerrit lookup is not made in that case: its answer would be
    discarded, and the request would cost an authenticated round trip
    on every such run.

    The project matters here, not only in the orchestrator, because the
    closed-pull-request handler and the cleanup sweeps query Gerrit
    with whatever this function derives, and they run before the
    orchestrator ever resolves ``.gitreview`` itself.  Deriving the raw
    GitHub name -- ``multicloud-openstack`` for a project that is
    really ``multicloud/openstack`` -- made those queries match nothing
    and left every such change open (#441).  The Gerrit lookup is
    offered here for the same reason: those paths return before the
    orchestrator runs, so an option honoured only there would leave
    them with the guess the option exists to avoid.

    Priority order for credential derivation:
    1. SSH config user for gerrit.* hosts (checks generic and specific patterns)
    2. Git user email from local git configuration
    3. Fallback to organization-based derivation

    Args:
        organization: GitHub organization name for fallback
        repository: GitHub repository in owner/repo format (optional)

    Returns:
        The derived values, keyed as follows, and whether the project
        among them is a guess:
        - GERRIT_SSH_USER_G2G: From SSH config or [org].gh2gerrit
        - GERRIT_SSH_USER_G2G_EMAIL: From git config or fallback email
        - GERRIT_SERVER: An explicit environment value, else .gitreview,
          else config, else gerrit.[org].org
        - GERRIT_PROJECT: From .gitreview or Gerrit, else guessed
    """
    if not organization:
        return DerivedParameters()

    org = organization.strip().lower()

    # Check if we have a config file entry for this organization
    config = load_org_config(org)
    configured_server = config.get("GERRIT_SERVER", "").strip()
    explicit_server = (os.getenv("GERRIT_SERVER") or "").strip()
    if explicit_server and is_derived_key("GERRIT_SERVER"):
        explicit_server = ""
    explicit_port = (os.getenv("GERRIT_SERVER_PORT") or "").strip()
    if explicit_port and is_derived_key("GERRIT_SERVER_PORT"):
        explicit_port = ""

    # Read .gitreview once for both host and project. The project is
    # per-repository and the configuration file cannot supply it, so
    # the file is worth reading whenever a repository is known.
    # CI_TESTING is documented as ignoring the file altogether, and
    # what is derived here is exported for the rest of the run, so the
    # file is not read at all in that mode rather than read and
    # discarded later.
    ci_testing = setting_bool("CI_TESTING", config)
    if ci_testing:
        log.debug("CI_TESTING enabled: not reading .gitreview for derivation")
    gitreview = _read_gitreview_info(repository) if not ci_testing else None
    gitreview_host = (gitreview.host if gitreview else "").strip()

    # One effective host, in the orchestrator's order: an explicit
    # GERRIT_SERVER, else .gitreview, else the per-organization file,
    # else the heuristic. It is the host the push will go to, so it is
    # the host exported here -- paired with the project below for the
    # closed-pull-request handler and the cleanup sweeps -- the host
    # the project lookup asks, and the host SSH credentials are derived
    # for. Confirming a project or picking an identity on any other
    # host would be confirming or picking for the wrong server.
    gerrit_host = (
        explicit_server
        or gitreview_host
        or configured_server
        or f"gerrit.{org}.org"
    )
    # Provenance follows the tier selected, not the value: an explicit
    # server that happens to equal the file's host is still explicit.
    host_from_gitreview = not explicit_server and bool(gitreview_host)

    if host_from_gitreview:
        log.debug(
            "Using Gerrit host from .gitreview: %s%s",
            gitreview_host,
            f" (over configured {configured_server})"
            if configured_server and configured_server != gitreview_host
            else "",
        )
    elif (
        explicit_server and gitreview_host and gitreview_host != explicit_server
    ):
        log.debug(
            "Using explicit GERRIT_SERVER %s over .gitreview host %s",
            explicit_server,
            gitreview_host,
        )

    gerrit_project, project_source = _derive_gerrit_project(
        repository, gitreview, gerrit_host, config
    )

    # Try to use SSH config and git config for personalized credentials
    ssh_user: str | None = None
    git_email: str | None = None
    try:
        from .ssh_config_parser import derive_gerrit_credentials

        ssh_user, git_email = derive_gerrit_credentials(gerrit_host, org)
    except ImportError:
        # ssh_config_parser unavailable: organisation-based fallbacks apply
        pass

    result = {
        "GERRIT_SSH_USER_G2G": ssh_user or f"{org}.gh2gerrit",
        "GERRIT_SSH_USER_G2G_EMAIL": git_email
        or f"releng+{org}-gh2gerrit@linuxfoundation.org",
        "GERRIT_SERVER": gerrit_host,
    }
    # The port has its own tier, settled by the same rule as the host:
    # an explicit input, else the file, else nothing here (the Gerrit
    # default applies downstream). The closed-pull-request handler
    # abandons over SSH on whatever port the environment holds, so the
    # file's port is exported whenever it is the winning tier -- with an
    # explicit host too, since the orchestrator pairs that host with the
    # file's port in exactly that case. A file that omits port= has
    # nothing to say about it; the parser's default is not the file's
    # word and must not displace a configured port.
    port_from_gitreview = (
        not explicit_port and gitreview is not None and gitreview.port_given
    )
    if port_from_gitreview and gitreview is not None:
        result["GERRIT_SERVER_PORT"] = str(gitreview.port)
    if gerrit_project:
        result["GERRIT_PROJECT"] = gerrit_project
    return DerivedParameters(
        values=result,
        project_source=project_source,
        host_from_gitreview=host_from_gitreview,
        port_from_gitreview=port_from_gitreview,
    )


def _merge_derived(
    cfg: dict[str, str],
    derived: DerivedParameters,
    *,
    organization: str,
    repository: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Fill *cfg*'s missing keys from *derived*, displacing the target.

    Only missing or empty keys are filled -- with one exception, for the
    Gerrit target.  A ``GERRIT_PROJECT`` that *cfg* carries from the
    configuration file is a per-organization fallback, and a project
    derived for this repository from its own ``.gitreview``, or
    confirmed against Gerrit, outranks it: the CLOSE dispatch consumes
    the resulting inputs before the orchestrator runs and never
    consults provenance, so leaving the file's value in place would
    keep cleanup on the wrong project despite the read.  The
    ``.gitreview`` host and port displace the file's ``GERRIT_SERVER``
    and ``GERRIT_SERVER_PORT`` for the same reason: the three are
    queried together, and the pipeline pushes to the ``.gitreview``
    target, so they have to come from one place.  Nothing inferred from
    the name alone displaces a project, whether a guess or the one
    reading of a hyphen-free name; the file's value was at least written
    down by somebody.  An explicit environment value is untouched either
    way, since :func:`apply_config_to_env` never overwrites one.

    Returns the merged mapping and the keys derivation supplied.
    """
    result = dict(cfg)
    newly_derived: dict[str, str] = {}

    def _env_blank(key: str) -> bool:
        return (os.getenv(key) or "").strip() == ""

    replaceable = {
        key
        for key, confirmed in (
            ("GERRIT_PROJECT", derived.project_confirmed),
            ("GERRIT_SERVER", derived.host_from_gitreview),
            ("GERRIT_SERVER_PORT", derived.port_from_gitreview),
        )
        if confirmed and cfg.get(key, "").strip() and _env_blank(key)
    }

    for key, value in derived.values.items():
        replacing = key in replaceable
        if key not in result or not result[key].strip() or replacing:
            if replacing:
                log.debug(
                    "Replacing configuration-file %s %r with %r resolved "
                    "for repository %s",
                    key,
                    result[key],
                    value,
                    repository,
                )
            log.debug(
                "Deriving %s from organization '%s': %s",
                key,
                organization,
                value,
            )
            result[key] = value
            newly_derived[key] = value
    return result, newly_derived


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
    - gerrit_project: From the repository's .gitreview, else guessed from
      its name (see :func:`derive_gerrit_parameters_detailed`)

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
    # outrank it. A GERRIT_SERVER, and a GERRIT_SERVER_PORT, from the
    # file are marked the same way: sensible per-organization defaults,
    # but an explicit server or port now outranks .gitreview at the
    # push, and a default must not carry that authority. Only a value
    # the operator set for this run does.
    #
    # Recorded ahead of the early returns below, because this describes
    # what cfg already carries rather than anything derivation adds.
    # Neither a missing organization nor G2G_ENABLE_DERIVATION=false
    # makes a config-file value any more like operator intent, and
    # apply_config_to_env still exports it in both cases.
    if mark_derived:
        mark_derived_keys(
            key
            for key in ("GERRIT_PROJECT", "GERRIT_SERVER", "GERRIT_SERVER_PORT")
            if cfg.get(key, "").strip() and (os.getenv(key) or "").strip() == ""
        )

    if not organization:
        return cfg

    is_github_actions = _is_github_actions_context()
    # Read through env_bool rather than parsing here, so a blank value
    # counts as absent. A reusable workflow forwarding an undefined
    # repository variable sets this to the empty string, and treating
    # that as "false" would disable derivation for every consumer who
    # never configured it.
    enable_derivation = env_bool("G2G_ENABLE_DERIVATION", True)

    if not enable_derivation:
        log.debug(
            "Parameter derivation disabled. Set G2G_ENABLE_DERIVATION=true to "
            "enable automatic derivation."
        )
        return cfg

    # Only derive parameters that are missing or empty, except for the
    # Gerrit target; _merge_derived explains the exception.
    derived = derive_gerrit_parameters_detailed(organization, repository)
    result, newly_derived = _merge_derived(
        cfg, derived, organization=organization, repository=repository
    )

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
            landing = [
                key
                for key in newly_derived
                if (os.getenv(key) or "").strip() == ""
            ]
            mark_derived_keys(landing)
            # A guessed project is exported so that Gerrit queries have
            # something to scope by, but the consumer that pushes must
            # be able to tell it from a project somebody confirmed.
            if derived.project_guessed and "GERRIT_PROJECT" in landing:
                mark_guessed_keys(["GERRIT_PROJECT"])

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
