# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Commit rules engine for flexible commit message customization.

This module provides a JSON-driven mechanism for injecting arbitrary
key-value lines into commit messages, supporting per-project and
per-actor overrides.  It generalizes the existing Issue-ID support
to handle requirements like FD.io VPP's ``Type:`` field.

JSON schema (``COMMIT_RULES_JSON``):

.. code-block:: json

   {
     "defaults": [
       {"key": "Issue-ID", "value": "CIMAN-33", "location": "trailer"},
       {"key": "Type",     "value": "ci",       "location": "body",
        "separator": "blank_line"}
     ],
     "projects": {
       "vpp": [
         {"key": "Type",     "value": "ci",       "location": "body",
          "separator": "blank_line"},
         {"key": "Issue-ID", "value": "CIMAN-33", "location": "trailer"}
       ]
     },
     "actors": {
       "dependabot[bot]": [
         {"key": "Type",     "value": "ci",       "location": "body"},
         {"key": "Issue-ID", "value": "CIMAN-33", "location": "trailer"}
       ]
     }
   }

Resolution precedence (highest wins):

1. ``actors[<github_actor>]`` — per-actor overrides
2. ``projects[<gerrit_project>]`` — per-project overrides
3. ``defaults`` — baseline rules for all projects/actors
4. Existing ``ISSUE_ID`` input still takes priority over any
   ``Issue-ID`` rule produced here.

Locations:

* ``trailer`` (default) — appended to the Git trailer block at the
  end of the commit message (alongside Change-Id, Signed-off-by, …).
* ``body`` — inserted into the commit body, before the trailer block.

Separators (only meaningful for ``location: body``):

* ``blank_line`` (default) — preceded by a blank line.
* ``none`` — appended directly without extra blank lines.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field


__all__ = [
    "CommitRule",
    "ResolvedCommitRules",
    "apply_body_rules",
    "apply_trailer_rules",
    "parse_commit_rules_json",
    "resolve_rules",
]

log = logging.getLogger(__name__)

# Valid values for CommitRule.location
VALID_LOCATIONS = frozenset({"trailer", "body"})

# Valid values for CommitRule.separator
VALID_SEPARATORS = frozenset({"blank_line", "none"})


@dataclass(frozen=True)
class CommitRule:
    """A single commit message rule (key-value pair with placement info).

    Attributes:
        key: The label/key name (e.g. ``Type``, ``Issue-ID``, ``Ticket``).
        value: The value to insert.
        location: Where to place it — ``"trailer"`` (default) or ``"body"``.
        separator: How to separate from surrounding content when
            ``location`` is ``"body"`` — ``"blank_line"`` (default)
            or ``"none"``.
    """

    key: str
    value: str
    location: str = "trailer"
    separator: str = "blank_line"

    def format_line(self) -> str:
        """Return the formatted ``Key: value`` string."""
        return f"{self.key}: {self.value}"


@dataclass
class ResolvedCommitRules:
    """The fully-resolved set of rules ready to apply to a commit message.

    Rules are split by location for convenient consumption by the
    commit-message builder in :pymod:`github2gerrit.core`.
    """

    body_rules: list[CommitRule] = field(default_factory=list)
    trailer_rules: list[CommitRule] = field(default_factory=list)

    @property
    def has_rules(self) -> bool:
        """True when at least one rule is present."""
        return bool(self.body_rules or self.trailer_rules)

    def get_trailer_value(self, key: str) -> str | None:
        """Return the value for a trailer rule matching *key*, or ``None``."""
        for rule in self.trailer_rules:
            if rule.key == key:
                return rule.value
        return None

    def get_body_value(self, key: str) -> str | None:
        """Return the value for a body rule matching *key*, or ``None``."""
        for rule in self.body_rules:
            if rule.key == key:
                return rule.value
        return None


def _parse_rule_entry(
    entry: object,
    *,
    context: str,
) -> CommitRule | None:
    """Parse a single rule dict into a :class:`CommitRule`.

    Returns ``None`` (with a warning) when the entry is invalid.
    """
    if not isinstance(entry, dict):
        log.warning(
            "⚠️  Skipping non-dict rule entry in %s: %r",
            context,
            entry,
        )
        return None

    key = entry.get("key")
    value = entry.get("value")

    if not key or not isinstance(key, str):
        log.warning(
            "⚠️  Skipping rule with missing/invalid 'key' in %s: %r",
            context,
            entry,
        )
        return None

    if value is None or not isinstance(value, str):
        log.warning(
            "⚠️  Skipping rule with missing/invalid 'value' in %s: %r",
            context,
            entry,
        )
        return None

    location = str(entry.get("location", "trailer")).strip().lower()
    if location not in VALID_LOCATIONS:
        log.warning(
            "⚠️  Unknown location '%s' in %s (expected %s)"
            " — defaulting to 'trailer'",
            location,
            context,
            ", ".join(sorted(VALID_LOCATIONS)),
        )
        location = "trailer"

    separator = str(entry.get("separator", "blank_line")).strip().lower()
    if separator not in VALID_SEPARATORS:
        log.warning(
            "⚠️  Unknown separator '%s' in %s (expected %s)"
            " — defaulting to 'blank_line'",
            separator,
            context,
            ", ".join(sorted(VALID_SEPARATORS)),
        )
        separator = "blank_line"

    return CommitRule(
        key=key.strip(),
        value=value.strip(),
        location=location,
        separator=separator,
    )


def _parse_rules_list(
    raw: object,
    *,
    context: str,
) -> list[CommitRule]:
    """Parse a JSON array of rule dicts into a list of :class:`CommitRule`."""
    if not isinstance(raw, list):
        log.warning(
            "⚠️  Expected array for %s, got %s — skipping",
            context,
            type(raw).__name__,
        )
        return []

    rules: list[CommitRule] = []
    for entry in raw:
        rule = _parse_rule_entry(entry, context=context)
        if rule is not None:
            rules.append(rule)
    return rules


@dataclass
class CommitRulesConfig:
    """Parsed representation of the full ``COMMIT_RULES_JSON`` document."""

    defaults: list[CommitRule] = field(default_factory=list)
    projects: dict[str, list[CommitRule]] = field(default_factory=dict)
    actors: dict[str, list[CommitRule]] = field(default_factory=dict)


def parse_commit_rules_json(json_str: str) -> CommitRulesConfig | None:
    """Parse the ``COMMIT_RULES_JSON`` string into a :class:`CommitRulesConfig`.

    Returns ``None`` when *json_str* is empty or unparsable (a warning
    is logged but no exception is raised — matching the existing
    Issue-ID JSON error-handling convention).
    """
    if not json_str or not json_str.strip():
        return None

    try:
        data = json.loads(json_str)
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError is a ValueError. json.loads also raises a bare
        # ValueError for an integer longer than sys.get_int_max_str_digits()
        # and RecursionError for arrays or objects nested too deeply.
        log.warning(
            "⚠️  Warning: COMMIT_RULES_JSON was not valid JSON "
            "(parse error: %s)",
            exc,
        )
        return None

    if not isinstance(data, dict):
        log.warning(
            "⚠️  Warning: COMMIT_RULES_JSON must be a JSON object, got %s",
            type(data).__name__,
        )
        return None

    config = CommitRulesConfig()

    # -- defaults -------------------------------------------------------
    if "defaults" in data:
        config.defaults = _parse_rules_list(
            data["defaults"], context="defaults"
        )

    # -- projects ----------------------------------------------------------
    projects_raw = data.get("projects")
    if projects_raw is not None:
        if not isinstance(projects_raw, dict):
            log.warning(
                "⚠️  Expected object for 'projects', got %s — skipping",
                type(projects_raw).__name__,
            )
        else:
            for project_name, rules_raw in projects_raw.items():
                parsed = _parse_rules_list(
                    rules_raw, context=f"projects.{project_name}"
                )
                if parsed:
                    config.projects[str(project_name)] = parsed

    # -- actors ------------------------------------------------------------
    actors_raw = data.get("actors")
    if actors_raw is not None:
        if not isinstance(actors_raw, dict):
            log.warning(
                "⚠️  Expected object for 'actors', got %s — skipping",
                type(actors_raw).__name__,
            )
        else:
            for actor_name, rules_raw in actors_raw.items():
                parsed = _parse_rules_list(
                    rules_raw, context=f"actors.{actor_name}"
                )
                if parsed:
                    config.actors[str(actor_name)] = parsed

    return config


def resolve_rules(
    config: CommitRulesConfig | None,
    *,
    gerrit_project: str = "",
    github_actor: str = "",
) -> ResolvedCommitRules:
    """Resolve the effective rules for a given project and actor.

    Resolution order (last writer wins per ``key``):

    1. ``defaults``
    2. ``projects[gerrit_project]`` (overrides defaults for matching keys)
    3. ``actors[github_actor]`` (overrides everything for matching keys)

    Returns a :class:`ResolvedCommitRules` partitioned by location.
    """
    result = ResolvedCommitRules()

    if config is None:
        return result

    # Merge rules in precedence order, keyed by rule.key so that
    # higher-precedence layers replace lower ones.
    merged: dict[str, CommitRule] = {}

    # Layer 1: defaults
    for rule in config.defaults:
        merged[rule.key] = rule

    # Layer 2: project-specific overrides
    if gerrit_project:
        project_rules = config.projects.get(gerrit_project, [])
        for rule in project_rules:
            merged[rule.key] = rule

    # Layer 3: actor-specific overrides
    if github_actor:
        actor_rules = config.actors.get(github_actor, [])
        for rule in actor_rules:
            merged[rule.key] = rule

    # Partition into body and trailer lists (preserving insertion order)
    for rule in merged.values():
        if rule.location == "body":
            result.body_rules.append(rule)
        else:
            result.trailer_rules.append(rule)

    if result.has_rules:
        rule_keys = [r.key for r in merged.values()]
        log.debug(
            "Resolved %d commit rule(s) for project=%r actor=%r: %s",
            len(merged),
            gerrit_project,
            github_actor,
            ", ".join(rule_keys),
        )

    return result


def apply_body_rules(
    body: str,
    rules: ResolvedCommitRules,
) -> str:
    """Insert body-location rules into a commit message body.

    Body rules are appended after existing body text but before
    the trailer block.  The ``separator`` attribute of each rule
    controls blank-line insertion.

    Args:
        body: The commit message body (everything before trailers).
        rules: The resolved rules (only ``body_rules`` are used).

    Returns:
        The body with body-location rule lines inserted.
    """
    if not rules.body_rules:
        return body

    result = body.rstrip()

    for rule in rules.body_rules:
        line = rule.format_line()

        # Skip if already present as a standalone line in body
        if line in result.splitlines():
            log.debug("Body rule '%s' already present — skipping", rule.key)
            continue

        if rule.separator == "blank_line":
            # Ensure blank line before the new content
            if result and not result.endswith("\n\n"):
                if result.endswith("\n"):
                    result += "\n"
                else:
                    result += "\n\n"
            result += line
        else:
            # separator == "none"
            if result and not result.endswith("\n"):
                result += "\n"
            result += line

        log.info("✅ Added body rule: %s: %s", rule.key, rule.value)

    return result


def apply_trailer_rules(
    trailers_ordered: list[str],
    rules: ResolvedCommitRules,
    *,
    existing_trailers: dict[str, list[str]] | None = None,
    issue_id_override: str = "",
) -> list[str]:
    """Prepend trailer-location rules to an ordered trailer list.

    Custom trailer rules are inserted *before* the standard trailers
    (Issue-ID, Signed-off-by, Change-Id, GitHub-PR, GitHub-Hash)
    to keep them at the top of the trailer block.

    The ``issue_id_override`` parameter allows the legacy ``ISSUE_ID``
    input to take precedence over any ``Issue-ID`` rule from the
    commit-rules JSON.

    Args:
        trailers_ordered: The existing ordered trailer list (mutated
            in-place and also returned for convenience).
        rules: The resolved rules (only ``trailer_rules`` are used).
        existing_trailers: Already-parsed trailers from the original
            commit message, used for deduplication.
        issue_id_override: If non-empty, any ``Issue-ID`` rule is
            skipped because the explicit ``ISSUE_ID`` input takes
            precedence.

    Returns:
        The *trailers_ordered* list with rule trailers prepended.
    """
    if not rules.trailer_rules:
        return trailers_ordered

    if existing_trailers is None:
        existing_trailers = {}

    # Build the new trailer lines to insert (in reverse so we can
    # insert at position 0 and maintain order).
    to_insert: list[str] = []

    for rule in rules.trailer_rules:
        # Skip Issue-ID rules when an explicit ISSUE_ID is provided
        if rule.key == "Issue-ID" and issue_id_override.strip():
            log.debug(
                "Skipping commit-rule Issue-ID=%s (overridden by "
                "ISSUE_ID input=%s)",
                rule.value,
                issue_id_override.strip(),
            )
            continue

        # Skip if this exact key+value already present in existing trailers
        existing_values = existing_trailers.get(rule.key, [])
        if rule.value in existing_values:
            log.debug(
                "Trailer rule '%s: %s' already present — skipping",
                rule.key,
                rule.value,
            )
            continue

        line = rule.format_line()

        # Skip if already in the ordered list
        if line in trailers_ordered:
            log.debug(
                "Trailer rule '%s' already in ordered list — skipping",
                rule.key,
            )
            continue

        to_insert.append(line)
        log.info("✅ Added trailer rule: %s: %s", rule.key, rule.value)

    # Prepend in order (insert at the beginning, before Issue-ID /
    # Signed-off-by / Change-Id that the caller adds afterwards).
    # We insert at position 0 in reverse order to maintain the
    # original rule ordering.
    for line in reversed(to_insert):
        trailers_ordered.insert(0, line)

    return trailers_ordered
