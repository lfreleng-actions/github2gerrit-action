# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Common trailer constants and parsing utilities.

This module provides shared functionality for working with Git commit
trailers, particularly GitHub PR metadata trailers used for reconciliation.
"""

import logging
import re


log = logging.getLogger(__name__)


# Trailer key constants
GITHUB_PR_TRAILER = "GitHub-PR"
GITHUB_HASH_TRAILER = "GitHub-Hash"
CHANGE_ID_TRAILER = "Change-Id"
SIGNED_OFF_BY_TRAILER = "Signed-off-by"

# Standard trailer keys we recognize
KNOWN_TRAILER_KEYS = {
    GITHUB_PR_TRAILER,
    GITHUB_HASH_TRAILER,
    CHANGE_ID_TRAILER,
    SIGNED_OFF_BY_TRAILER,
}


def parse_trailers(commit_message: str) -> dict[str, list[str]]:
    """
    Parse Git-style trailers from a commit message.

    Trailers are key-value pairs at the end of a commit message,
    after the last blank line, in the format "Key: value".

    Args:
        commit_message: Full commit message text

    Returns:
        Dictionary mapping trailer keys to lists of values
    """
    trailers: dict[str, list[str]] = {}

    if not commit_message.strip():
        return trailers

    lines = commit_message.strip().split("\n")

    # Find the start of trailers (after the last blank line)
    trailer_start = 0
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            trailer_start = i + 1
            break

    # Parse trailer lines
    for raw_line in lines[trailer_start:]:
        line = raw_line.strip()
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()

            if key:
                if key not in trailers:
                    trailers[key] = []
                trailers[key].append(value)

    return trailers


def extract_github_metadata(commit_message: str) -> dict[str, str]:
    """
    Extract GitHub PR metadata trailers from a commit message.

    Args:
        commit_message: Full commit message text

    Returns:
        Dictionary with GitHub-* trailer values (single values, not lists)
    """
    trailers = parse_trailers(commit_message)
    metadata = {}

    for key in [GITHUB_PR_TRAILER, GITHUB_HASH_TRAILER]:
        values = trailers.get(key, [])
        if values:
            # Take the last value if multiple exist
            metadata[key] = values[-1]

    return metadata


def extract_change_ids(commit_message: str) -> list[str]:
    """
    Extract Change-Id trailer values from a commit message.

    Args:
        commit_message: Full commit message text

    Returns:
        List of Change-Id values found
    """
    trailers = parse_trailers(commit_message)
    return trailers.get(CHANGE_ID_TRAILER, [])


def has_trailer(
    commit_message: str, key: str, value: str | None = None
) -> bool:
    """
    Check if a commit message contains a specific trailer.

    Args:
        commit_message: Full commit message text
        key: Trailer key to check for
        value: Optional specific value to match (if None, any value matches)

    Returns:
        True if the trailer exists with the specified value (or any value)
    """
    trailers = parse_trailers(commit_message)

    if key not in trailers:
        return False

    if value is None:
        return True

    return value in trailers[key]


def add_trailers(commit_message: str, new_trailers: dict[str, str]) -> str:
    """
    Add trailers to a commit message, avoiding duplicates.

    Args:
        commit_message: Original commit message
        new_trailers: Dictionary of trailers to add (key -> value)

    Returns:
        Commit message with trailers added
    """
    if not new_trailers:
        return commit_message

    existing_trailers = parse_trailers(commit_message)
    msg = commit_message.rstrip()

    # Collect trailers that need to be added
    to_add = []
    for key, value in new_trailers.items():
        if key not in existing_trailers or value not in existing_trailers[key]:
            to_add.append(f"{key}: {value}")

    if to_add:
        if msg and not msg.endswith("\n"):
            msg += "\n"
        if not msg.endswith("\n\n"):
            msg += "\n"
        msg += "\n".join(to_add) + "\n"

    return msg


def normalize_subject_for_matching(subject: str) -> str:
    """
    Normalize a commit subject line for similarity matching.

    This removes common noise and standardizes the format to improve
    matching accuracy when commits have minor subject changes.

    Args:
        subject: Original commit subject line

    Returns:
        Normalized subject for comparison
    """
    if not subject:
        return ""

    # Remove common prefixes and suffixes
    normalized = subject.strip()

    # Remove version numbers and tags in brackets/parentheses
    normalized = re.sub(r"\s*[\[\(][vV]?\d+[\.\d]*[\]\)]\s*", " ", normalized)

    # Remove "WIP:", "DRAFT:", etc. prefixes
    normalized = re.sub(
        r"^\s*(WIP|DRAFT|TODO|FIXME|HACK):\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )

    # Remove trailing punctuation and whitespace
    normalized = re.sub(r"[.!]+\s*$", "", normalized)

    # Normalize whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()

    # Convert to lowercase for case-insensitive matching
    return normalized.lower()


def compute_file_signature(file_paths: list[str]) -> str:
    """
    Compute a normalized signature for a set of file paths.

    This creates a deterministic hash of the files touched by a commit,
    useful for matching commits that affect the same files.

    Args:
        file_paths: List of file paths

    Returns:
        Hex string signature of the file set
    """
    import hashlib

    if not file_paths:
        return ""

    # Normalize paths: lowercase, remove leading/trailing slashes
    normalized_paths = []
    for path in file_paths:
        normalized = path.strip().lower()
        normalized = normalized.strip("/")
        if normalized:
            normalized_paths.append(normalized)

    # Sort for deterministic ordering
    normalized_paths.sort()

    # Create hash of the sorted, normalized path list
    content = "\n".join(normalized_paths)
    hash_obj = hashlib.sha256(content.encode("utf-8"))
    return hash_obj.hexdigest()[:12]  # 12 hex chars = 48 bits


def extract_subject_tokens(subject: str) -> set[str]:
    """
    Extract meaningful tokens from a subject line for similarity matching.

    Args:
        subject: Commit subject line

    Returns:
        Set of normalized tokens
    """
    if not subject:
        return set()

    # Normalize the subject
    normalized = normalize_subject_for_matching(subject)

    # Split on common delimiters and filter out short/common words
    tokens = re.split(r"[\s\-_\.,:;/\\]+", normalized)

    # Filter tokens: must be at least 3 chars and not common stop words
    stop_words = {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "can",
        "had",
        "has",
        "was",
        "one",
        "our",
        "out",
        "day",
        "get",
        "use",
        "man",
        "new",
        "now",
        "old",
        "see",
        "two",
        "way",
        "who",
        "its",
        "did",
        "yes",
        "his",
        "her",
        "him",
        "how",
        "may",
        "say",
        "she",
        "add",
        "fix",
        "set",
        "put",
        "run",
        "try",
        "let",
        "end",
    }

    meaningful_tokens = set()
    for raw_token in tokens:
        token = raw_token.strip()
        if len(token) >= 3 and token not in stop_words:
            meaningful_tokens.add(token)

    return meaningful_tokens


def compute_jaccard_similarity(set1: set[str], set2: set[str]) -> float:
    """
    Compute Jaccard similarity between two sets of tokens.

    Args:
        set1: First set of tokens
        set2: Second set of tokens

    Returns:
        Jaccard similarity coefficient (0.0 to 1.0)
    """
    if not set1 and not set2:
        return 1.0

    if not set1 or not set2:
        return 0.0

    intersection = len(set1.intersection(set2))
    union = len(set1.union(set2))

    return intersection / union if union > 0 else 0.0
