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
  ``CommandDefinition`` to ``COMMAND_REGISTRY``; no other code changes
  are required for recognition.
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

Supported commands
──────────────────
``create missing change`` (alias ``create-missing``)
    Instructs the tool to create a new Gerrit change when an UPDATE
    operation cannot locate an existing one.  This addresses the
    scenario where the original ``opened`` event failed and subsequent
    ``synchronize`` events cannot find a change to update.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field


__all__ = [
    "CMD_CREATE_MISSING",
    "COMMAND_REGISTRY",
    "CommandDefinition",
    "CommandMatch",
    "CommandParseResult",
    "find_command",
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
    """

    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    hidden: bool = False

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


def parse_commands(comment_bodies: list[str]) -> CommandParseResult:
    """Scan PR comment bodies for ``@github2gerrit`` commands.

    Comments are processed oldest-first.  When the same command appears
    in multiple comments the *latest* occurrence is kept (deduplication
    by canonical command name).

    Args:
        comment_bodies: Ordered list of comment body strings
            (oldest → newest).

    Returns:
        A ``CommandParseResult`` containing de-duplicated matches and
        any unrecognised directives.
    """
    phrase_index = _build_phrase_index()
    # Track latest match per canonical name (overwritten by newer comments).
    seen: dict[str, CommandMatch] = {}
    unrecognised: list[str] = []

    for idx, body in enumerate(comment_bodies):
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
        matches=list(seen.values()),
        unrecognised=unrecognised,
    )

    if result.has_matches:
        log.info(
            "Found %d @github2gerrit command(s) in PR comments: %s",
            len(result.matches),
            ", ".join(m.command_name for m in result.matches),
        )

    return result


def has_command(comment_bodies: list[str], command_name: str) -> bool:
    """Check whether a specific command exists in the PR comments.

    This is a convenience wrapper around ``parse_commands`` for the
    common case where only one command matters.

    Args:
        comment_bodies: Ordered list of comment body strings.
        command_name: Canonical command name to check for.

    Returns:
        ``True`` if the command was found in at least one comment.
    """
    result = parse_commands(comment_bodies)
    return result.has(command_name)


def find_command(
    comment_bodies: list[str],
    command_name: str,
) -> CommandMatch | None:
    """Find a specific command match in the PR comments.

    Args:
        comment_bodies: Ordered list of comment body strings.
        command_name: Canonical command name to search for.

    Returns:
        The ``CommandMatch`` if found, otherwise ``None``.
    """
    result = parse_commands(comment_bodies)
    target = command_name.lower().strip()
    for m in result.matches:
        if m.command_name == target:
            return m
    return None


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
    phrase is a prefix of the normalised text (to tolerate trailing
    punctuation like periods or extra context).

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
        if normalised.startswith(phrase) and len(phrase) > best_length:
            best_match = canonical
            best_length = len(phrase)

    return best_match
