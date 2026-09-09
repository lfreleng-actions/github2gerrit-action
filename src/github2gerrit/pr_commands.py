# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Extensible PR command system for ``@github2gerrit`` directives.

This module provides a modular, registry-based framework for parsing
``@github2gerrit <command>`` directives found in GitHub pull request
comments.  Commands are discovered by scanning PR comment bodies for
lines that begin with the ``@github2gerrit`` mention followed by a
recognised command phrase.

Design principles
─────────────────
- **Registry-based**: new commands are added by appending a
  ``CommandDefinition`` to ``COMMAND_REGISTRY``; no change to this
  module's parsing logic is required.
- **Case-insensitive**: command matching ignores case so that
  ``@github2gerrit Create Missing Change`` works identically to
  ``@github2gerrit create missing change``.
- **Idempotent**: duplicate commands in the same or different comments
  produce a single ``CommandMatch`` per command name.
- **Deterministic ordering**: results are returned in comment order
  (oldest → newest), with the *latest* occurrence winning for
  deduplication purposes.
- **Minimal coupling**: the module depends only on the standard library
  and exposes typed dataclasses consumed by the orchestrator.

.. warning::

   **This module performs no authorisation.** Every function here
   assumes its input has already been filtered to comments from trusted
   authors.  Passing raw comments straight from the GitHub API lets any
   user of a public mirror direct the tool — the defect fixed in issue
   #382.

   Adding a command to ``COMMAND_REGISTRY`` requires no changes here,
   but it does **not** grant that command a security gate on its own.
   Consume commands through :mod:`github2gerrit.pr_directives`, which
   fetches, authorises and parses in one step, rather than calling
   :func:`parse_commands`, :func:`find_command` or :func:`has_command`
   directly.

   :func:`find_open_command` is the single, deliberate exception, and
   it enforces its own limits: it refuses any command that has not set
   ``requires_trust=False``.  A command may only do so when it *grants
   nothing* — when it makes the tool look again rather than act, so
   that every authorisation decision is re-taken from the API
   afterwards.  ``requires_trust`` defaults to ``True``, so a new
   command stays confined to trusted authors unless its author
   deliberately opts out.

Supported commands
──────────────────
``create missing change`` (aliases ``create missing``, ``create-missing``)
    Instructs the tool to create a new Gerrit change when an UPDATE
    operation cannot locate an existing one.  This addresses the
    scenario where the original ``opened`` event failed and subsequent
    ``synchronize`` events cannot find a change to update.

    Requires a trusted author: it makes the tool act.

``check`` (aliases ``recheck``, ``retry``)
    Asks the tool to re-evaluate a pull request now, transferring it
    when a maintainer has approved it.  It exists because GitHub
    withholds this repository's credentials from a review on a fork
    pull request, so the approving review cannot perform the transfer
    itself.

    Open to any author: it makes the tool look again, and the answer
    still comes from re-reading the pull request's reviews.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field


__all__ = [
    "CMD_CHECK",
    "CMD_CREATE_MISSING",
    "COMMAND_REGISTRY",
    "CommandDefinition",
    "CommandMatch",
    "CommandParseResult",
    "contains_directive",
    "find_command",
    "find_open_command",
    "has_command",
    "list_commands",
    "parse_commands",
    "register_command",
]

log = logging.getLogger("github2gerrit.pr_commands")

# ── Mention prefix ──────────────────────────────────────────────────
# The canonical mention that triggers command parsing.
MENTION_PREFIX = "@github2gerrit"

# Compiled pattern: start of line (or after whitespace), the mention,
# then at least one whitespace character followed by the command text.
# We capture everything after the mention as the raw command string.
_MENTION_RE = re.compile(
    rf"(?:^|\s){re.escape(MENTION_PREFIX)}\s+(.+)",
    re.IGNORECASE | re.MULTILINE,
)


# ── Data models ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandDefinition:
    """Definition of a recognised ``@github2gerrit`` command.

    Attributes:
        name: Canonical command name (lower-case, space-separated).
        aliases: Alternative phrasings that map to the same command.
        description: Human-readable description shown in help output.
        hidden: If ``True`` the command is recognised but omitted from
            user-facing documentation helpers.
        requires_trust: Whether the command may only be obeyed when a
            trusted author issued it.  Defaults to ``True``, so a new
            command is restricted to trusted authors unless it
            deliberately opts out.  Only a command that *grants
            nothing* may set this ``False`` — see
            :func:`find_open_command`.
    """

    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    hidden: bool = False
    requires_trust: bool = True

    def all_phrases(self) -> tuple[str, ...]:
        """Return all phrases that match this command (canonical + aliases)."""
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class CommandMatch:
    """Result of matching a single command in a PR comment.

    Attributes:
        command_name: Canonical name of the matched command.
        raw_text: The original text that matched (preserving case).
        comment_index: Zero-based index of the comment in the list that
            contained the match (useful for audit / logging).
    """

    command_name: str
    raw_text: str
    comment_index: int = 0


@dataclass
class CommandParseResult:
    """Aggregated result from scanning all PR comments.

    Attributes:
        matches: De-duplicated command matches (latest occurrence wins).
        unrecognised: Raw strings that followed the mention prefix but
            did not match any registered command.
    """

    matches: list[CommandMatch] = field(default_factory=list)
    unrecognised: list[str] = field(default_factory=list)

    @property
    def has_matches(self) -> bool:
        """Return ``True`` when at least one command was recognised."""
        return len(self.matches) > 0

    def has(self, command_name: str) -> bool:
        """Check whether *command_name* appears in the matched commands."""
        target = command_name.lower().strip()
        return any(m.command_name == target for m in self.matches)


# ── Command registry ────────────────────────────────────────────────

# Mutable list; extend via ``register_command()`` or direct append.
COMMAND_REGISTRY: list[CommandDefinition] = []


def register_command(defn: CommandDefinition) -> CommandDefinition:
    """Register a new command definition and return it for chaining."""
    COMMAND_REGISTRY.append(defn)
    log.debug("Registered @github2gerrit command: %s", defn.name)
    return defn


# ── Built-in commands ───────────────────────────────────────────────

CMD_CREATE_MISSING = register_command(
    CommandDefinition(
        name="create missing change",
        aliases=("create-missing", "create missing"),
        description=(
            "Create a Gerrit change when an UPDATE operation cannot "
            "find an existing one. Use this when the original 'opened' "
            "event failed and subsequent PR updates keep failing."
        ),
    )
)

CMD_CHECK = register_command(
    CommandDefinition(
        name="check",
        aliases=("recheck", "retry"),
        description=(
            "Re-evaluate this pull request now. Use it after a "
            "maintainer approves a pull request raised from a fork: "
            "the approving review cannot transfer the change itself, "
            "because GitHub denies it this repository's credentials. "
            "It re-reads the reviews and grants nothing by itself, so "
            "anybody may ask for it."
        ),
        # The one command safe to obey from any author: it asks the
        # tool to look again, and the looking re-takes every
        # authorisation decision from the API.
        requires_trust=False,
    )
)

# Future commands can be registered here, for example:
#
# CMD_FORCE_RESUBMIT = register_command(
#     CommandDefinition(
#         name="force resubmit",
#         aliases=("resubmit",),
#         description="Force a complete resubmission of the PR to Gerrit.",
#     )
# )


# ── Normalisation helpers ───────────────────────────────────────────


def _normalise_phrase(text: str) -> str:
    """Collapse whitespace and lowercase for comparison."""
    return " ".join(text.lower().split())


def _build_phrase_index() -> dict[str, str]:
    """Build a mapping from every known phrase to its canonical name.

    Returns:
        ``{normalised_phrase: canonical_name}`` for every registered
        command and all of its aliases.
    """
    index: dict[str, str] = {}
    for defn in COMMAND_REGISTRY:
        canonical = defn.name.lower().strip()
        for phrase in defn.all_phrases():
            key = _normalise_phrase(phrase)
            if key in index and index[key] != canonical:
                log.warning(
                    "Phrase '%s' maps to both '%s' and '%s'; "
                    "last registration wins",
                    key,
                    index[key],
                    canonical,
                )
            index[key] = canonical
    return index


# ── Public API ──────────────────────────────────────────────────────


def contains_directive(body: str) -> bool:
    """Report whether *body* holds a syntactically valid directive.

    Applies the same grammar as :func:`parse_commands`, so a bare
    ``@github2gerrit`` mention is *not* a directive — consistent with
    that function, which reports it as neither a match nor an
    unrecognised directive.

    A mention followed by whitespace alone is likewise not a directive.
    The mention pattern's ``.`` matches spaces, so such text satisfies
    the regex, but there is no command in it for anyone to act on.

    Callers use this to distinguish an ordinary comment that happens to
    mention the tool from one that attempts to direct it.

    Args:
        body: Comment body text.

    Returns:
        ``True`` when the mention is followed by non-whitespace text.
    """
    if not body:
        return False
    match = _MENTION_RE.search(body)
    return match is not None and bool(match.group(1).strip())


def parse_commands(
    trusted_comment_bodies: list[str],
) -> CommandParseResult:
    """Scan trusted PR comment bodies for ``@github2gerrit`` commands.

    Comments are processed oldest-first.  When the same command appears
    in multiple comments the *latest* occurrence is kept (deduplication
    by canonical command name).

    .. warning::

       Performs no authorisation.  *trusted_comment_bodies* must
       already exclude comments from untrusted authors; prefer
       :func:`github2gerrit.pr_directives.scan_pr_directives`, which
       does that for you.

    Args:
        trusted_comment_bodies: Ordered list of comment body strings
            (oldest → newest), already filtered by author trust.

    Returns:
        A ``CommandParseResult`` containing de-duplicated matches and
        any unrecognised directives.
    """
    phrase_index = _build_phrase_index()
    # Track latest match per canonical name (overwritten by newer comments).
    seen: dict[str, CommandMatch] = {}
    unrecognised: list[str] = []

    for idx, body in enumerate(trusted_comment_bodies):
        if not body:
            continue
        for m in _MENTION_RE.finditer(body):
            raw_command = m.group(1).strip()
            normalised = _normalise_phrase(raw_command)

            # Try exact match first, then progressively shorter prefixes
            # to tolerate trailing punctuation or extra words.
            matched_name = _match_command(normalised, phrase_index)

            if matched_name is not None:
                match = CommandMatch(
                    command_name=matched_name,
                    raw_text=raw_command,
                    comment_index=idx,
                )
                seen[matched_name] = match
                log.debug(
                    "Matched command '%s' (raw: '%s') in comment #%d",
                    matched_name,
                    raw_command,
                    idx,
                )
            else:
                unrecognised.append(raw_command)
                log.debug(
                    "Unrecognised @github2gerrit directive: '%s' "
                    "in comment #%d",
                    raw_command,
                    idx,
                )

    result = CommandParseResult(
        # Sort by the comment index of the *winning* occurrence so that
        # results are in comment order (oldest → newest) as documented.
        matches=sorted(seen.values(), key=lambda m: m.comment_index),
        unrecognised=unrecognised,
    )

    if result.has_matches:
        log.info(
            "Found %d @github2gerrit command(s) in PR comments: %s",
            len(result.matches),
            ", ".join(m.command_name for m in result.matches),
        )

    return result


def has_command(trusted_comment_bodies: list[str], command_name: str) -> bool:
    """Check whether a specific command exists in trusted PR comments.

    This is a convenience wrapper around ``parse_commands`` for the
    common case where only one command matters.

    .. warning::

       Performs no authorisation; see :func:`parse_commands`.

    Args:
        trusted_comment_bodies: Ordered list of comment body strings,
            already filtered by author trust.
        command_name: Canonical command name to check for.

    Returns:
        ``True`` if the command was found in at least one comment.
    """
    result = parse_commands(trusted_comment_bodies)
    return result.has(command_name)


def find_command(
    trusted_comment_bodies: list[str],
    command_name: str,
) -> CommandMatch | None:
    """Find a specific command match in trusted PR comments.

    .. warning::

       Performs no authorisation.  Prefer
       :func:`github2gerrit.pr_directives.find_pr_command`, which
       filters by author trust first.

    Args:
        trusted_comment_bodies: Ordered list of comment body strings,
            already filtered by author trust.
        command_name: Canonical command name to search for.

    Returns:
        The ``CommandMatch`` if found, otherwise ``None``.
    """
    result = parse_commands(trusted_comment_bodies)
    target = command_name.lower().strip()
    for m in result.matches:
        if m.command_name == target:
            return m
    return None


def find_open_command(body: str, command_name: str) -> CommandMatch | None:
    """Match a command in one comment body, ignoring who wrote it.

    The single sanctioned bypass of the authorship filter described in
    :mod:`github2gerrit.pr_directives`, and it refuses to serve a
    command that has not declared itself safe to obey from anybody.

    A command qualifies only when it **grants nothing**: it may cause
    the tool to look at a pull request again, never to act on one.
    Re-evaluation is safe from any author precisely because every
    authorisation decision is re-taken from the API afterwards, so the
    comment is a doorbell rather than a key.

    Args:
        body: A single comment body, unfiltered.
        command_name: Canonical command name to search for.

    Returns:
        The :class:`CommandMatch` if found, otherwise ``None``.

    Raises:
        ValueError: If *command_name* is unregistered, or is one that
            requires a trusted author.  Both are programming errors:
            reaching this function with such a command means the
            authorisation filter was bypassed by mistake.
    """
    target = command_name.lower().strip()
    defn = next(
        (c for c in COMMAND_REGISTRY if c.name.lower().strip() == target),
        None,
    )
    if defn is None:
        msg = f"unregistered command: {command_name!r}"
        raise ValueError(msg)
    if defn.requires_trust:
        msg = (
            f"command {defn.name!r} requires a trusted author; use "
            "github2gerrit.pr_directives.find_pr_command instead"
        )
        raise ValueError(msg)

    return find_command([body], defn.name)


def list_commands() -> list[CommandDefinition]:
    """Return all registered (non-hidden) commands.

    Useful for generating help text or documentation.
    """
    return [c for c in COMMAND_REGISTRY if not c.hidden]


# ── Internal helpers ────────────────────────────────────────────────


def _match_command(
    normalised: str,
    phrase_index: dict[str, str],
) -> str | None:
    """Attempt to match *normalised* text against the phrase index.

    Tries an exact match first, then checks whether any registered
    phrase begins the normalised text (to tolerate trailing punctuation
    like periods or extra context).

    The prefix must end on a word boundary.  A bare string prefix would
    make every longer word starting with a command name a match, so
    ``check`` would answer for ``checkout this branch``, ``checker`` and
    ``check_status``.  That matters more than it used to: the one-word
    commands are short, common English stems, and one of them starts
    the transfer pipeline.

    Returns:
        The canonical command name on match, or ``None``.
    """
    # Exact match
    if normalised in phrase_index:
        return phrase_index[normalised]

    # Prefix match — longest phrase wins to avoid ambiguity.
    best_match: str | None = None
    best_length = 0
    for phrase, canonical in phrase_index.items():
        if not normalised.startswith(phrase) or len(phrase) <= best_length:
            continue
        # The character ending the phrase must not continue a word.
        # Punctuation and whitespace both end one; a letter, digit or
        # underscore means the text merely begins with the same stem.
        # ``str.isalnum`` omits the underscore, so name it explicitly.
        following = normalised[len(phrase)]
        if following.isalnum() or following == "_":
            continue
        best_match = canonical
        best_length = len(phrase)

    return best_match
