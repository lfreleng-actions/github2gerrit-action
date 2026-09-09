# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Additional sources of approval authority.

The fork approval gate trusts a reviewer on ``author_association`` (see
:mod:`github2gerrit.trust`).  On a Gerrit mirror the people who review
the code live in Gerrit, and nothing guarantees they appear on GitHub
with any standing at all.  Where that happens no review can ever clear
the gate, and the feature is unusable for that project.

This module supplies two opt-in ways to name approvers directly.  Both
default to off: they **widen** the approver set, and widening a trust
decision is not something to do silently.

Everything else about the gate still applies to an approval admitted
here.  It must be an approving review bound to the current head commit,
a trusted ``CHANGES_REQUESTED`` still blocks, and the pull request
author is still excluded whatever list names them.  That last point
matters most \u2014 it is the one guarantee GitHub enforces structurally,
and no widening may cost us it.

Where the file is read from
~~~~~~~~~~~~~~~~~~~~~~~~~~~
``INFO.yaml`` is read from the **base** repository, at the pull
request's **base ref**, for the same reason ``.gitreview`` is (see
``Orchestrator._read_gitreview``): a fork must never supply the file
that decides who may authorise the fork's own transfer.  Both operands
come from GitHub's own metadata for the base side of the pull request,
which the head cannot influence.  Where the base ref is unknown the
read is declined rather than falling back to the default branch, which
would answer with a roster the pull request does not target.

The ``id`` field is not a GitHub login
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
In the Linux Foundation's schema ``id`` is an LFID.  On
``opendaylight/mdsal`` the committers are ``rovarga``, ``tpantelis``
and ``JieHan2017``, and there is no ``github_id`` field at all.

Treating an LFID as a GitHub login is an impersonation vector: whoever
registers a GitHub username matching an unclaimed LFID inherits
committer authority.  So ``github_id`` is preferred wherever the file
carries one, and matching on ``id`` is a *separate* opt-in that a
project must choose knowingly.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

import yaml

from .utils import env_bool


__all__ = [
    "APPROVER_LOGINS_ENV",
    "INFO_YAML_LFID_ENV",
    "INFO_YAML_PATH",
    "USE_INFO_YAML_ENV",
    "describe_additional_sources",
    "explicit_approvers",
    "parse_info_yaml",
    "resolve_additional_approvers",
]

log = logging.getLogger("github2gerrit.approvers")

APPROVER_LOGINS_ENV = "G2G_APPROVER_LOGINS"
"""Comma-separated GitHub logins whose approving reviews always count."""

USE_INFO_YAML_ENV = "G2G_APPROVERS_FROM_INFO_YAML"
"""Enable reading approvers from the base repository's ``INFO.yaml``."""

INFO_YAML_LFID_ENV = "G2G_INFO_YAML_MATCH_LFID"
"""Also treat an ``INFO.yaml`` ``id`` as a GitHub login.

Off by default: ``id`` is an LFID, and matching it against a GitHub
login lets whoever registers that username on GitHub inherit committer
authority.
"""

INFO_YAML_PATH = "INFO.yaml"
"""Path of the file within the base repository."""

_PERSON_KEYS = ("project_lead",)
"""Top-level keys holding a single authority-bearing person.

``primary_contact`` is deliberately absent.  It names whoever should be
contacted about the project, which is often the lead but may be a
separate operational or administrative contact.  Admitting it would
grant that account approval authority the file never claimed for it.
"""

_ROSTER_KEYS = ("committers",)
"""Top-level keys holding a list of people."""


def explicit_approvers() -> frozenset[str]:
    """Return the logins named by :data:`APPROVER_LOGINS_ENV`.

    Returns:
        Lower-cased logins.  Empty when the variable is unset or blank,
        which is the default and leaves the gate keyed on
        ``author_association`` alone.
    """
    raw = os.getenv(APPROVER_LOGINS_ENV, "").strip()
    if not raw:
        return frozenset()

    parsed = frozenset(
        part.strip().lower() for part in raw.split(",") if part.strip()
    )
    if parsed:
        log.debug(
            "Additional approvers from %s: %s",
            APPROVER_LOGINS_ENV,
            sorted(parsed),
        )
    return parsed


def _logins_from_person(person: Any, *, match_lfid: bool) -> Iterable[str]:
    """Yield the GitHub logins a single ``INFO.yaml`` entry names."""
    if not isinstance(person, dict):
        return

    github_id = person.get("github_id")
    if isinstance(github_id, str) and github_id.strip():
        yield github_id.strip().lower()
        # An explicit github_id is authoritative for this person, so
        # their LFID adds nothing and would only widen the set.
        return

    if not match_lfid:
        return

    lfid = person.get("id")
    if isinstance(lfid, str) and lfid.strip():
        yield lfid.strip().lower()


def parse_info_yaml(text: str, *, match_lfid: bool = False) -> frozenset[str]:
    """Extract approver logins from ``INFO.yaml`` content.

    Reads the project lead and the committer roster, and nothing else.
    ``primary_contact`` is deliberately excluded; see
    :data:`_PERSON_KEYS`.

    YAML anchors and merge keys are resolved by the parser, so a
    committer written as ``<<: *project_lead`` is understood, and
    duplicates collapse into the returned set.

    Args:
        text: Raw file content.
        match_lfid: Whether to fall back to the ``id`` field when an
            entry carries no ``github_id``.  See the module docstring
            for why this is off by default.

    Returns:
        Lower-cased logins.  Empty when the file is unparsable or
        names nobody \u2014 a malformed file must not widen the approver
        set, and must not raise into the gate either.
    """
    try:
        parsed = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        # RecursionError is not a YAMLError: PyYAML raises it on
        # deeply nested input, and letting it escape would turn a
        # malformed base file into a failed workflow rather than the
        # empty result this function promises.
        log.warning("Could not parse %s; ignoring it: %s", INFO_YAML_PATH, exc)
        return frozenset()

    if not isinstance(parsed, dict):
        log.warning("%s is not a mapping; ignoring it", INFO_YAML_PATH)
        return frozenset()

    logins: set[str] = set()
    for key in _PERSON_KEYS:
        logins.update(
            _logins_from_person(parsed.get(key), match_lfid=match_lfid)
        )
    for key in _ROSTER_KEYS:
        roster = parsed.get(key)
        if isinstance(roster, list):
            for person in roster:
                logins.update(
                    _logins_from_person(person, match_lfid=match_lfid)
                )

    return frozenset(logins)


def _fetch_info_yaml(repo_obj: Any, ref: str) -> str:
    """Read ``INFO.yaml`` from *repo_obj* at *ref*.

    Returns an empty string on any failure.  A repository without the
    file is the common case, not an error.
    """
    try:
        content = repo_obj.get_contents(INFO_YAML_PATH, ref=ref)
        raw = getattr(content, "decoded_content", b"") or b""
        return bytes(raw).decode("utf-8")
    except Exception as exc:
        log.debug("Could not read %s at ref %r: %s", INFO_YAML_PATH, ref, exc)
        return ""


def resolve_additional_approvers(
    *,
    base_repo: Any | None = None,
    base_ref: str = "",
) -> frozenset[str]:
    """Return every login admitted by an opt-in approver source.

    Args:
        base_repo: The **base** repository object, exposing
            ``get_contents(path, ref=...)``.  Passing the head
            repository would let a fork nominate its own approvers.
        base_ref: The pull request's base ref.  Required for the
            ``INFO.yaml`` source: without it the read is declined
            rather than falling back to the default branch, which
            would answer with a different branch's roster than the one
            the pull request targets.  ``.gitreview`` resolution fails
            closed in exactly the same situation.

    Returns:
        Lower-cased logins, empty when no source is enabled.
    """
    logins = set(explicit_approvers())

    if env_bool(USE_INFO_YAML_ENV, False):
        logins.update(_info_yaml_approvers(base_repo, base_ref))

    return frozenset(logins)


def _info_yaml_approvers(
    base_repo: Any | None, base_ref: str
) -> frozenset[str]:
    """Resolve approvers from ``INFO.yaml``, or name nobody."""
    if base_repo is None:
        log.debug("No base repository available; skipping %s", INFO_YAML_PATH)
        return frozenset()

    ref = base_ref.strip()
    if not ref:
        log.warning(
            "No authoritative base ref for this pull request; declining to "
            "read %s. Reading the default branch instead could authorise "
            "from a different branch's roster than the one the pull "
            "request targets.",
            INFO_YAML_PATH,
        )
        return frozenset()

    text = _fetch_info_yaml(base_repo, ref)
    if not text:
        return frozenset()

    logins = parse_info_yaml(
        text, match_lfid=env_bool(INFO_YAML_LFID_ENV, False)
    )
    if logins:
        log.debug(
            "Additional approvers from %s at ref %r: %s",
            INFO_YAML_PATH,
            ref,
            sorted(logins),
        )
    return logins


def describe_additional_sources() -> str:
    """Return a human-readable summary of the enabled sources.

    Empty when none is enabled, so callers can omit the clause
    entirely rather than telling a contributor about machinery the
    project does not use.
    """
    sources: list[str] = []
    if explicit_approvers():
        sources.append("a configured approver list")
    if env_bool(USE_INFO_YAML_ENV, False):
        detail = (
            "`github_id` or `id`"
            if env_bool(INFO_YAML_LFID_ENV, False)
            else "`github_id`"
        )
        sources.append(
            f"the base repository's `{INFO_YAML_PATH}` (matched on {detail})"
        )
    return ", ".join(sources)
