# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""
Bot author detection for GitHub PRs.

Determines whether a PR was authored by a known automation bot (e.g.,
Dependabot, pre-commit-ci, Renovate) vs. a human developer.

This distinction is critical for Gerrit NACR (Non-Author Code Review):
when G2G preserves a human developer as the git commit author, Gerrit
treats that developer as the change owner, blocking them from
self-approving their own G2G-submitted change.  For bot PRs this is
not an issue because bots never review their own changes.
"""

from __future__ import annotations

import logging
import re


__all__ = ["KNOWN_BOT_LOGINS", "is_bot_author"]

log = logging.getLogger("github2gerrit.bot_detection")

# GitHub login names for well-known automation bots.
# Entries are compared case-insensitively.
KNOWN_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "dependabot[bot]",
        "pre-commit-ci[bot]",
        "renovate[bot]",
        "github-actions[bot]",
        "snyk-bot",
        "mergify[bot]",
        "greenkeeper[bot]",
        "depfu[bot]",
        "imgbot[bot]",
        "allcontributors[bot]",
        "codecov[bot]",
        "stale[bot]",
        "lgtm-com[bot]",
        "sonarcloud[bot]",
    }
)

# Pre-computed lowercase set for O(1) lookups in is_bot_author()
_KNOWN_BOT_LOGINS_LOWER: frozenset[str] = frozenset(
    b.lower() for b in KNOWN_BOT_LOGINS
)

# Pattern that catches any GitHub App bot login (ends with [bot])
_BOT_SUFFIX_RE = re.compile(r"\[bot\]$", re.IGNORECASE)


def is_bot_author(author_login: str | None) -> bool:
    """Determine whether a GitHub PR author is an automation bot.

    Detection strategy (any match → True):
    1. Exact match against ``KNOWN_BOT_LOGINS`` (case-insensitive).
    2. Login ends with ``[bot]`` (GitHub App convention).
    3. Login contains ``bot`` as a distinct word boundary.

    Args:
        author_login: The GitHub ``user.login`` of the PR author.
            May be empty/None, in which case the function returns False
            (treat unknown authors conservatively as human).

    Returns:
        True if the author is a known bot, False otherwise.
    """
    if not author_login:
        return False

    login_lower = author_login.strip().lower()

    # 1. Exact match against known bots
    if login_lower in _KNOWN_BOT_LOGINS_LOWER:
        log.debug("Bot detected (exact match): %s", author_login)
        return True

    # 2. GitHub App convention: login ends with [bot]
    if _BOT_SUFFIX_RE.search(login_lower):
        log.debug("Bot detected ([bot] suffix): %s", author_login)
        return True

    # 3. Heuristic: "bot" as word boundary (catches custom bots)
    #    Exclude common false positives like "abbott", "robot" etc.
    if re.search(r"(?<![a-z])bot(?![a-z])", login_lower):
        log.debug("Bot detected (word-boundary heuristic): %s", author_login)
        return True

    log.debug("Author classified as human: %s", author_login)
    return False
