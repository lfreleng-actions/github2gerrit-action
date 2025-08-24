# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Mapping comment utilities for serializing and deserializing Change-ID mapping
blocks.

This module handles the structured PR comments that track the mapping between
local commits and Gerrit Change-IDs for reconciliation purposes.
"""

import logging
from dataclasses import dataclass


log = logging.getLogger(__name__)

# Error message constants to comply with TRY003
_MSG_INVALID_MODE = "Invalid mode"
_MSG_NO_CHANGE_IDS = "At least one Change-ID is required"
_MSG_INVALID_CHANGE_ID_FORMAT = "Invalid Change-ID format"


@dataclass
class ChangeIdMapping:
    """Represents a Change-ID mapping from a PR comment (with optional
    digest)."""

    pr_url: str
    mode: str  # "multi-commit" or "squash"
    topic: str
    change_ids: list[str]
    github_hash: str = ""
    digest: str = ""

    def __post_init__(self) -> None:
        """Validate mapping data after initialization."""
        if self.mode not in ("multi-commit", "squash"):
            raise ValueError(_MSG_INVALID_MODE)

        if not self.change_ids:
            raise ValueError(_MSG_NO_CHANGE_IDS)

        # Validate Change-Id format
        for cid in self.change_ids:
            if not cid.startswith("I") or len(cid) < 8:
                raise ValueError(_MSG_INVALID_CHANGE_ID_FORMAT)


def serialize_mapping_comment(
    pr_url: str,
    mode: str,
    topic: str,
    change_ids: list[str],
    github_hash: str,
    digest: str | None = None,
) -> str:
    """
    Serialize a Change-ID mapping into a structured PR comment.

    Args:
        pr_url: Full GitHub PR URL
        mode: Submission mode ("multi-commit" or "squash")
        topic: Gerrit topic name
        change_ids: Ordered list of Change-IDs
        github_hash: GitHub-Hash trailer value for verification

    Returns:
        Formatted comment body with mapping block
    """
    if not change_ids:
        raise ValueError(_MSG_NO_CHANGE_IDS)

    lines = [
        "<!-- github2gerrit:change-id-map v1 -->",
        f"PR: {pr_url}",
        f"Mode: {mode}",
        f"Topic: {topic}",
        "Change-Ids:",
    ]

    # Add indented Change-IDs
    for cid in change_ids:
        lines.append(f"  {cid}")

    if digest:
        lines.append(f"Digest: {digest}")

    lines.extend(
        [
            f"GitHub-Hash: {github_hash}",
            "<!-- end github2gerrit:change-id-map -->",
        ]
    )

    return "\n".join(lines)


def parse_mapping_comments(comment_bodies: list[str]) -> ChangeIdMapping | None:
    """
    Parse Change-ID mapping from PR comment bodies.

    Scans comments from oldest to newest, returning the latest valid mapping.

    Args:
        comment_bodies: List of comment body texts to scan

    Returns:
        Latest valid ChangeIdMapping or None if no mapping found
    """
    latest_mapping: ChangeIdMapping | None = None

    start_marker = "<!-- github2gerrit:change-id-map v1 -->"
    end_marker = "<!-- end github2gerrit:change-id-map -->"

    for body in comment_bodies:
        if start_marker not in body or end_marker not in body:
            continue

        try:
            # Extract the mapping block
            start_idx = body.find(start_marker)
            end_idx = body.find(end_marker)

            if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
                continue

            block = body[start_idx + len(start_marker) : end_idx].strip()
            mapping = _parse_mapping_block(block)

            if mapping:
                latest_mapping = mapping
                log.debug(
                    "Found mapping with %d Change-IDs in %s mode",
                    len(mapping.change_ids),
                    mapping.mode,
                )

        except Exception as exc:
            log.debug("Failed to parse mapping block: %s", exc)
            continue

    if latest_mapping:
        log.debug(
            "Recovered mapping with %d Change-ID(s) for topic '%s'",
            len(latest_mapping.change_ids),
            latest_mapping.topic,
        )

    return latest_mapping


def _parse_mapping_block(block: str) -> ChangeIdMapping | None:
    """
    Parse a single mapping block into a ChangeIdMapping.

    Args:
        block: Mapping block content (without HTML markers)

    Returns:
        Parsed ChangeIdMapping or None if invalid
    """
    lines = [line.strip() for line in block.split("\n")]

    pr_url = ""
    mode = ""
    topic = ""
    change_ids: list[str] = []
    github_hash = ""
    digest = ""

    in_change_ids = False

    for line in lines:
        if not line:
            continue

        if line.startswith("PR:"):
            pr_url = line[3:].strip()
        elif line.startswith("Mode:"):
            mode = line[5:].strip()
        elif line.startswith("Topic:"):
            topic = line[6:].strip()
        elif line.startswith("Change-Ids:"):
            in_change_ids = True
        elif line.startswith("GitHub-Hash:"):
            github_hash = line[12:].strip()
            in_change_ids = False
        elif line.startswith("Digest:"):
            digest = line[7:].strip()
        elif in_change_ids and line.startswith("I"):
            # Extract Change-ID (handle potential whitespace/formatting)
            cid = line.split()[0]
            if cid not in change_ids:  # Avoid duplicates
                change_ids.append(cid)

    # Validate required fields (github_hash is optional for backward
    # compatibility)
    if not all([pr_url, mode, topic, change_ids]):
        log.debug(
            "Incomplete mapping block: pr_url=%s, mode=%s, topic=%s, "
            "change_ids=%d, github_hash=%s",
            bool(pr_url),
            mode,
            bool(topic),
            len(change_ids),
            bool(github_hash),
        )
        return None

    try:
        return ChangeIdMapping(
            pr_url=pr_url,
            mode=mode,
            topic=topic,
            change_ids=change_ids,
            github_hash=github_hash or "",
            digest=digest or "",
        )
    except ValueError as exc:
        log.debug("Invalid mapping data: %s", exc)
        return None


def find_mapping_comments(comment_bodies: list[str]) -> list[int]:
    """
    Find indices of comments containing Change-ID mapping blocks.

    Args:
        comment_bodies: List of comment body texts

    Returns:
        List of comment indices that contain mapping blocks
    """
    indices = []
    start_marker = "<!-- github2gerrit:change-id-map v1 -->"

    for i, body in enumerate(comment_bodies):
        if start_marker in body:
            indices.append(i)

    return indices


def update_mapping_comment_body(
    original_body: str,
    new_mapping: ChangeIdMapping,
) -> str:
    """
    Update a comment body with a new mapping, replacing the existing one.

    Args:
        original_body: Original comment body text
        new_mapping: New mapping to insert

    Returns:
        Updated comment body with new mapping
    """
    start_marker = "<!-- github2gerrit:change-id-map v1 -->"
    end_marker = "<!-- end github2gerrit:change-id-map -->"

    # Generate new mapping block
    new_block = serialize_mapping_comment(
        pr_url=new_mapping.pr_url,
        mode=new_mapping.mode,
        topic=new_mapping.topic,
        change_ids=new_mapping.change_ids,
        github_hash=new_mapping.github_hash,
    )

    # If no existing mapping, append the new one
    if start_marker not in original_body:
        if original_body and not original_body.endswith("\n"):
            return original_body + "\n\n" + new_block
        else:
            return original_body + new_block

    # Replace existing mapping
    start_idx = original_body.find(start_marker)
    end_idx = original_body.find(end_marker)

    if start_idx == -1 or end_idx == -1:
        # Malformed existing mapping, append new one
        return original_body + "\n\n" + new_block

    # Include the end marker in the replacement
    end_idx += len(end_marker)

    # Replace the old mapping with the new one
    updated_body = (
        original_body[:start_idx] + new_block + original_body[end_idx:]
    )

    return updated_body.strip()


def compute_mapping_digest(change_ids: list[str]) -> str:
    """
    Compute a digest of the Change-ID list for quick comparison.

    Args:
        change_ids: Ordered list of Change-IDs

    Returns:
        SHA-256 digest (first 12 hex chars) of the ordered Change-IDs
    """
    import hashlib

    content = "\n".join(change_ids)
    hash_obj = hashlib.sha256(content.encode("utf-8"))
    return hash_obj.hexdigest()[:12]


def validate_mapping_consistency(
    mapping: ChangeIdMapping,
    expected_pr_url: str,
    expected_github_hash: str,
) -> bool:
    """
    Validate that a mapping is consistent with expected PR metadata.

    Args:
        mapping: Parsed mapping to validate
        expected_pr_url: Expected GitHub PR URL
        expected_github_hash: Expected GitHub-Hash value

    Returns:
        True if mapping is consistent with expectations
    """
    if mapping.pr_url != expected_pr_url:
        log.warning(
            "Mapping PR URL mismatch: expected=%s, found=%s",
            expected_pr_url,
            mapping.pr_url,
        )
        return False

    if mapping.github_hash != expected_github_hash:
        log.warning(
            "Mapping GitHub-Hash mismatch: expected=%s, found=%s",
            expected_github_hash,
            mapping.github_hash,
        )
        return False

    return True
