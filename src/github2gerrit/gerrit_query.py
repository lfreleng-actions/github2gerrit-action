# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Gerrit query utilities for topic-based change discovery.

This module provides functions to query Gerrit REST API for changes
based on topics, with support for pagination and safe parsing.
"""

import logging
from dataclasses import dataclass
from typing import Any

from .gerrit_rest import GerritRestClient


log = logging.getLogger(__name__)


@dataclass
class GerritChange:
    """Represents a Gerrit change from query results."""

    change_id: str
    number: str
    subject: str
    status: str
    current_revision: str
    files: list[str]
    commit_message: str
    topic: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GerritChange":
        """Create GerritChange from Gerrit REST API response."""
        # Extract files from current revision
        files = []
        current_revision = data.get("current_revision", "")
        if current_revision and "revisions" in data:
            revision_data = data["revisions"].get(current_revision, {})
            files = list(revision_data.get("files", {}).keys())

        # Extract commit message
        commit_message = ""
        if current_revision and "revisions" in data:
            revision_data = data["revisions"].get(current_revision, {})
            commit_message = revision_data.get("commit", {}).get("message", "")

        return cls(
            change_id=data.get("change_id", ""),
            number=str(data.get("_number", "")),
            subject=data.get("subject", ""),
            status=data.get("status", ""),
            current_revision=current_revision,
            files=files,
            commit_message=commit_message,
            topic=data.get("topic"),
        )


def query_changes_by_topic(
    client: GerritRestClient,
    topic: str,
    *,
    statuses: list[str] | None = None,
    max_results: int = 100,
) -> list[GerritChange]:
    """
    Query Gerrit for changes matching the given topic.

    Args:
        client: Gerrit REST client
        topic: Topic name to search for
        statuses: List of change statuses to include (default: ["NEW"])
        max_results: Maximum number of results to return

    Returns:
        List of GerritChange objects
    """
    if statuses is None:
        statuses = ["NEW"]

    # Build query string
    status_query = " OR ".join(f"status:{status}" for status in statuses)
    query = f"topic:{topic} AND ({status_query})"

    log.debug("Querying Gerrit for changes: %s", query)

    try:
        changes = _execute_query_with_pagination(
            client, query, max_results=max_results
        )
        log.debug(
            "Found %d changes for topic '%s' with statuses %s",
            len(changes),
            topic,
            statuses,
        )
    except Exception as exc:
        log.warning(
            "Failed to query Gerrit changes for topic '%s': %s", topic, exc
        )
        return []
    else:
        return changes


def _execute_query_with_pagination(
    client: GerritRestClient,
    query: str,
    *,
    max_results: int = 100,
    page_size: int = 25,
) -> list[GerritChange]:
    """
    Execute Gerrit query with pagination support.

    Args:
        client: Gerrit REST client
        query: Gerrit query string
        max_results: Maximum total results to return
        page_size: Results per page

    Returns:
        List of GerritChange objects
    """
    all_changes: list[GerritChange] = []
    start = 0

    while len(all_changes) < max_results:
        remaining = max_results - len(all_changes)
        current_limit = min(page_size, remaining)

        try:
            # Build query URL with parameters
            # Gerrit REST API: /changes/?q=query&n=limit&S=skip&o=options
            query_params = [
                f"q={query}",
                f"n={current_limit}",
                f"S={start}",
                "o=CURRENT_REVISION",
                "o=CURRENT_FILES",
                "o=CURRENT_COMMIT",
            ]
            query_path = f"/changes/?{'&'.join(query_params)}"

            response = client.get(query_path)

            if not response:
                break

            # Gerrit REST API returns a list of change objects
            if not isinstance(response, list):
                log.warning(
                    "Unexpected Gerrit query response format: %s",
                    type(response),
                )
                break

            page_changes = []
            for change_data in response:
                try:
                    change = GerritChange.from_dict(change_data)
                    page_changes.append(change)
                except Exception as exc:
                    log.debug("Skipping malformed change data: %s", exc)
                    continue

            all_changes.extend(page_changes)

            # If we got fewer results than requested, we've reached the end
            if len(page_changes) < current_limit:
                break

            start += len(page_changes)

        except Exception as exc:
            log.warning(
                "Failed to fetch Gerrit changes page (start=%d, limit=%d): %s",
                start,
                current_limit,
                exc,
            )
            break

    return all_changes[:max_results]


def extract_pr_metadata_from_commit_message(
    commit_message: str,
) -> dict[str, str]:
    """
    Extract GitHub PR metadata trailers from a commit message.

    Args:
        commit_message: Full commit message text

    Returns:
        Dictionary with extracted metadata (GitHub-PR, GitHub-Hash, etc.)
    """
    metadata = {}

    # Look for trailer-style metadata at the end of the commit message
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
            if key.startswith("GitHub-"):
                metadata[key] = value

    return metadata


def validate_pr_metadata_match(
    gerrit_changes: list[GerritChange],
    expected_pr_url: str,
    expected_github_hash: str,
) -> list[GerritChange]:
    """
    Filter Gerrit changes to only those matching the expected PR metadata.

    This prevents cross-PR contamination by ensuring changes belong to
    the same GitHub PR based on trailer metadata.

    Args:
        gerrit_changes: List of changes from Gerrit query
        expected_pr_url: Expected GitHub PR URL
        expected_github_hash: Expected GitHub-Hash trailer value

    Returns:
        Filtered list of changes matching the PR metadata
    """
    validated_changes = []

    for change in gerrit_changes:
        metadata = extract_pr_metadata_from_commit_message(
            change.commit_message
        )

        # Check GitHub-PR URL match
        pr_url = metadata.get("GitHub-PR", "")
        if pr_url and pr_url != expected_pr_url:
            log.debug(
                "Excluding change %s: PR URL mismatch (expected=%s, found=%s)",
                change.change_id,
                expected_pr_url,
                pr_url,
            )
            continue

        # Check GitHub-Hash match
        github_hash = metadata.get("GitHub-Hash", "")
        if github_hash and github_hash != expected_github_hash:
            log.debug(
                "Excluding change %s: GitHub-Hash mismatch "
                "(expected=%s, found=%s)",
                change.change_id,
                expected_github_hash,
                github_hash,
            )
            continue

        validated_changes.append(change)

    if len(validated_changes) != len(gerrit_changes):
        log.info(
            "Filtered Gerrit changes: %d -> %d "
            "(excluded %d due to metadata mismatch)",
            len(gerrit_changes),
            len(validated_changes),
            len(gerrit_changes) - len(validated_changes),
        )

    return validated_changes
