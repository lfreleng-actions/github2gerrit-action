# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Multi-pass reconciliation matcher for GitHub commits to Gerrit changes.

This module implements the core matching algorithm that pairs local GitHub
commits with existing Gerrit changes using multiple strategies, from most
reliable to least reliable.
"""

import logging
from dataclasses import dataclass
from enum import Enum

from .gerrit_query import GerritChange
from .trailers import compute_file_signature
from .trailers import compute_jaccard_similarity
from .trailers import extract_change_ids
from .trailers import extract_subject_tokens
from .trailers import normalize_subject_for_matching


log = logging.getLogger(__name__)


class MatchStrategy(Enum):
    """Matching strategy used to pair commits with changes."""

    TRAILER = "trailer"  # Direct Change-ID match from commit trailer
    SUBJECT_EXACT = "subject_exact"  # Exact normalized subject match
    FILE_SIGNATURE = "file_signature"  # Same file set hash
    SUBJECT_SIMILARITY = "subject_similarity"  # Jaccard similarity >= threshold


@dataclass
class LocalCommit:
    """Represents a local commit to be matched."""

    index: int  # Position in commit list (for ordering)
    sha: str
    subject: str
    files: list[str]
    commit_message: str
    existing_change_id: str | None = None  # From commit trailer


@dataclass
class MatchResult:
    """Result of matching a local commit to a Gerrit change."""

    local_commit: LocalCommit
    gerrit_change: GerritChange | None
    strategy: MatchStrategy | None
    confidence: float  # 0.0 to 1.0
    change_id: str  # Reused or newly generated


@dataclass
class ReconciliationResult:
    """Complete reconciliation result with summary statistics."""

    matches: list[MatchResult]
    reused_count: int
    new_count: int
    orphaned_changes: list[GerritChange]
    strategy_counts: dict[MatchStrategy, int]

    @property
    def change_ids(self) -> list[str]:
        """Extract ordered list of Change-IDs from matches."""
        return [match.change_id for match in self.matches]


class ReconciliationMatcher:
    """Multi-pass matcher for reconciling commits with Gerrit changes."""

    def __init__(
        self,
        *,
        similarity_threshold: float = 0.7,
        allow_duplicate_subjects: bool = True,
    ):
        """
        Initialize the matcher with configuration.

        Args:
            similarity_threshold: Minimum Jaccard similarity for subject
                matching
            allow_duplicate_subjects: Allow multiple commits with same subject
        """
        self.similarity_threshold = similarity_threshold
        self.allow_duplicate_subjects = allow_duplicate_subjects

    def reconcile(
        self,
        local_commits: list[LocalCommit],
        gerrit_changes: list[GerritChange],
    ) -> ReconciliationResult:
        """
        Perform multi-pass reconciliation of local commits with Gerrit changes.

        Args:
            local_commits: Ordered list of local commits to match
            gerrit_changes: List of existing Gerrit changes from topic query

        Returns:
            Complete reconciliation result with matches and statistics
        """
        if not local_commits:
            return ReconciliationResult(
                matches=[],
                reused_count=0,
                new_count=0,
                orphaned_changes=gerrit_changes.copy(),
                strategy_counts={},
            )

        # Phase 3 hardening: conflict / duplicate Change-Id detection before
        # any pass
        # Build a map of existing Change-Id trailers present on local commits;
        # if one appears on more than one commit we abort with actionable
        # guidance.
        seen_trailer_ids: dict[str, list[int]] = {}
        for lc in local_commits:
            if lc.existing_change_id:
                seen_trailer_ids.setdefault(lc.existing_change_id, []).append(
                    lc.index
                )
        duplicate_trailers = {
            cid: idxs for cid, idxs in seen_trailer_ids.items() if len(idxs) > 1
        }
        if duplicate_trailers:
            details = ", ".join(
                f"{cid} -> positions {idxs}"
                for cid, idxs in duplicate_trailers.items()
            )
            msg = (
                f"Duplicate Change-Id trailer reuse detected across multiple "
                f"local commits: {details}. "
                "Amend commits to ensure each uses a distinct Change-Id or "
                "drop conflicting trailers."
            )
            raise ValueError(msg)

        log.info(
            "Starting reconciliation: %d local commits, %d Gerrit changes",
            len(local_commits),
            len(gerrit_changes),
        )

        # Track used changes to prevent duplicate matching
        used_changes: set[str] = set()
        matches: list[MatchResult] = []
        strategy_counts: dict[MatchStrategy, int] = {}

        # Pass A: Trailer-based matching (highest priority)
        remaining_commits = self._match_by_trailer(
            local_commits,
            gerrit_changes,
            used_changes,
            matches,
            strategy_counts,
        )

        # Pass B: Exact subject matching
        remaining_commits = self._match_by_subject_exact(
            remaining_commits,
            gerrit_changes,
            used_changes,
            matches,
            strategy_counts,
        )

        # Pass C: File signature matching
        remaining_commits = self._match_by_file_signature(
            remaining_commits,
            gerrit_changes,
            used_changes,
            matches,
            strategy_counts,
        )

        # Pass D: Subject similarity matching
        remaining_commits = self._match_by_subject_similarity(
            remaining_commits,
            gerrit_changes,
            used_changes,
            matches,
            strategy_counts,
        )

        # Generate new Change-IDs for unmatched commits
        for commit in remaining_commits:
            new_change_id = self._generate_change_id()
            matches.append(
                MatchResult(
                    local_commit=commit,
                    gerrit_change=None,
                    strategy=None,
                    confidence=0.0,
                    change_id=new_change_id,
                )
            )

        # Sort matches by original commit index to maintain order
        matches.sort(key=lambda m: m.local_commit.index)

        # Identify orphaned changes
        orphaned = [
            change
            for change in gerrit_changes
            if change.change_id not in used_changes
        ]

        reused_count = len(used_changes)
        new_count = len(local_commits) - reused_count

        result = ReconciliationResult(
            matches=matches,
            reused_count=reused_count,
            new_count=new_count,
            orphaned_changes=orphaned,
            strategy_counts=strategy_counts,
        )

        # Enhanced Phase 3 summary: pass counts & ID classifications
        self._log_reconciliation_summary(result)
        return result

    def _match_by_trailer(
        self,
        commits: list[LocalCommit],
        gerrit_changes: list[GerritChange],
        used_changes: set[str],
        matches: list[MatchResult],
        strategy_counts: dict[MatchStrategy, int],
    ) -> list[LocalCommit]:
        """Pass A: Match commits that already have Change-ID trailers."""
        remaining = []

        # Build lookup map for Gerrit changes
        gerrit_by_id = {change.change_id: change for change in gerrit_changes}

        for commit in commits:
            if not commit.existing_change_id:
                remaining.append(commit)
                continue

            cid = commit.existing_change_id
            if cid in gerrit_by_id and cid not in used_changes:
                gerrit_change = gerrit_by_id[cid]
                matches.append(
                    MatchResult(
                        local_commit=commit,
                        gerrit_change=gerrit_change,
                        strategy=MatchStrategy.TRAILER,
                        confidence=1.0,
                        change_id=cid,
                    )
                )
                used_changes.add(cid)
                strategy_counts[MatchStrategy.TRAILER] = (
                    strategy_counts.get(MatchStrategy.TRAILER, 0) + 1
                )
                log.debug("Trailer match: %s -> %s", commit.sha[:8], cid)
            else:
                # Change-ID not found in Gerrit or already used
                remaining.append(commit)
                if cid in used_changes:
                    log.warning(
                        "Duplicate Change-ID %s in commit %s (already used)",
                        cid,
                        commit.sha[:8],
                    )

        return remaining

    def _match_by_subject_exact(
        self,
        commits: list[LocalCommit],
        gerrit_changes: list[GerritChange],
        used_changes: set[str],
        matches: list[MatchResult],
        strategy_counts: dict[MatchStrategy, int],
    ) -> list[LocalCommit]:
        """Pass B: Match by exact normalized subject."""
        remaining = []

        # Build lookup map by normalized subject
        gerrit_by_subject: dict[str, list[GerritChange]] = {}
        for change in gerrit_changes:
            if change.change_id in used_changes:
                continue

            norm_subject = normalize_subject_for_matching(change.subject)
            if norm_subject not in gerrit_by_subject:
                gerrit_by_subject[norm_subject] = []
            gerrit_by_subject[norm_subject].append(change)

        for commit in commits:
            norm_subject = normalize_subject_for_matching(commit.subject)
            candidates = gerrit_by_subject.get(norm_subject, [])

            if not candidates:
                remaining.append(commit)
                continue

            # Use first available candidate (could enhance with additional
            # criteria)
            gerrit_change = candidates[0]
            matches.append(
                MatchResult(
                    local_commit=commit,
                    gerrit_change=gerrit_change,
                    strategy=MatchStrategy.SUBJECT_EXACT,
                    confidence=0.9,
                    change_id=gerrit_change.change_id,
                )
            )
            used_changes.add(gerrit_change.change_id)
            strategy_counts[MatchStrategy.SUBJECT_EXACT] = (
                strategy_counts.get(MatchStrategy.SUBJECT_EXACT, 0) + 1
            )

            # Remove from candidates to prevent reuse
            candidates.remove(gerrit_change)
            if not candidates:
                del gerrit_by_subject[norm_subject]

            log.debug(
                "Subject exact match: %s -> %s",
                commit.sha[:8],
                gerrit_change.change_id,
            )

        return remaining

    def _match_by_file_signature(
        self,
        commits: list[LocalCommit],
        gerrit_changes: list[GerritChange],
        used_changes: set[str],
        matches: list[MatchResult],
        strategy_counts: dict[MatchStrategy, int],
    ) -> list[LocalCommit]:
        """Pass C: Match by file signature (same set of files)."""
        remaining = []

        # Build lookup map by file signature
        gerrit_by_files: dict[str, list[GerritChange]] = {}
        for change in gerrit_changes:
            if change.change_id in used_changes:
                continue

            file_sig = compute_file_signature(change.files)
            if not file_sig:
                continue  # Skip changes with no files

            if file_sig not in gerrit_by_files:
                gerrit_by_files[file_sig] = []
            gerrit_by_files[file_sig].append(change)

        for commit in commits:
            file_sig = compute_file_signature(commit.files)
            if not file_sig:
                remaining.append(commit)
                continue

            candidates = gerrit_by_files.get(file_sig, [])
            if not candidates:
                remaining.append(commit)
                continue

            # Use first available candidate
            gerrit_change = candidates[0]
            matches.append(
                MatchResult(
                    local_commit=commit,
                    gerrit_change=gerrit_change,
                    strategy=MatchStrategy.FILE_SIGNATURE,
                    confidence=0.8,
                    change_id=gerrit_change.change_id,
                )
            )
            used_changes.add(gerrit_change.change_id)
            strategy_counts[MatchStrategy.FILE_SIGNATURE] = (
                strategy_counts.get(MatchStrategy.FILE_SIGNATURE, 0) + 1
            )

            # Remove from candidates
            candidates.remove(gerrit_change)
            if not candidates:
                del gerrit_by_files[file_sig]

            log.debug(
                "File signature match: %s -> %s (sig=%s)",
                commit.sha[:8],
                gerrit_change.change_id,
                file_sig,
            )

        return remaining

    def _match_by_subject_similarity(
        self,
        commits: list[LocalCommit],
        gerrit_changes: list[GerritChange],
        used_changes: set[str],
        matches: list[MatchResult],
        strategy_counts: dict[MatchStrategy, int],
    ) -> list[LocalCommit]:
        """Pass D: Match by subject token similarity (Jaccard)."""
        remaining = []

        # Get available Gerrit changes
        available_changes = [
            change
            for change in gerrit_changes
            if change.change_id not in used_changes
        ]

        for commit in commits:
            commit_tokens = extract_subject_tokens(commit.subject)
            if not commit_tokens:
                remaining.append(commit)
                continue

            best_match: tuple[GerritChange, float] | None = None

            for change in available_changes:
                change_tokens = extract_subject_tokens(change.subject)
                if not change_tokens:
                    continue

                similarity = compute_jaccard_similarity(
                    commit_tokens, change_tokens
                )
                if similarity >= self.similarity_threshold and (
                    best_match is None or similarity > best_match[1]
                ):
                    best_match = (change, similarity)

            if best_match:
                gerrit_change, confidence = best_match
                matches.append(
                    MatchResult(
                        local_commit=commit,
                        gerrit_change=gerrit_change,
                        strategy=MatchStrategy.SUBJECT_SIMILARITY,
                        confidence=confidence,
                        change_id=gerrit_change.change_id,
                    )
                )
                used_changes.add(gerrit_change.change_id)
                strategy_counts[MatchStrategy.SUBJECT_SIMILARITY] = (
                    strategy_counts.get(MatchStrategy.SUBJECT_SIMILARITY, 0) + 1
                )

                # Remove from available changes
                available_changes.remove(gerrit_change)

                log.debug(
                    "Similarity match: %s -> %s (confidence=%.2f)",
                    commit.sha[:8],
                    gerrit_change.change_id,
                    confidence,
                )
            else:
                remaining.append(commit)

        return remaining

    def _generate_change_id(self) -> str:
        """Generate a new Change-ID."""
        import hashlib
        import time

        # Simple Change-ID generation (could be enhanced)
        content = f"{time.time()}"
        hash_obj = hashlib.sha256(content.encode("utf-8"))
        return "I" + hash_obj.hexdigest()[:40]

    def _log_reconciliation_summary(self, result: ReconciliationResult) -> None:
        """Log a structured summary of reconciliation results."""
        total = len(result.matches)

        # Human-readable summary
        log.info(
            "Reconciliation complete: total=%d reused=%d new=%d orphaned=%d "
            "(passes: trailer=%d subject_exact=%d file_signature=%d "
            "subject_similarity=%d)",
            total,
            result.reused_count,
            result.new_count,
            len(result.orphaned_changes),
            result.strategy_counts.get(MatchStrategy.TRAILER, 0),
            result.strategy_counts.get(MatchStrategy.SUBJECT_EXACT, 0),
            result.strategy_counts.get(MatchStrategy.FILE_SIGNATURE, 0),
            result.strategy_counts.get(MatchStrategy.SUBJECT_SIMILARITY, 0),
        )

        # Strategy breakdown
        if result.strategy_counts:
            strategy_details = []
            for strategy, count in result.strategy_counts.items():
                strategy_details.append(f"{strategy.value}={count}")
            log.info(
                "Matching strategies (non-zero): %s", " ".join(strategy_details)
            )

        # Orphaned changes warning
        if result.orphaned_changes:
            orphaned_ids = [
                change.change_id for change in result.orphaned_changes
            ]
            log.warning(
                "Found %d orphaned Gerrit changes (no local counterpart): %s",
                len(orphaned_ids),
                ", ".join(orphaned_ids),
            )

        # Structured JSON debug line
        import json

        # Emit structured summary including per-pass counts and explicit lists
        # to aid downstream tooling or debugging (names kept stable for
        # automation).
        debug_data = {
            "total_local": total,
            "reused": result.reused_count,
            "new": result.new_count,
            "orphaned": len(result.orphaned_changes),
            "strategies": {
                strategy.value: count
                for strategy, count in result.strategy_counts.items()
            },
            "reused_ids": [
                m.change_id for m in result.matches if m.strategy is not None
            ],
            "new_ids": [
                m.change_id for m in result.matches if m.strategy is None
            ],
            "orphaned_ids": [c.change_id for c in result.orphaned_changes],
            "passes": {
                "A_trailer": result.strategy_counts.get(
                    MatchStrategy.TRAILER, 0
                ),
                "B_subject_exact": result.strategy_counts.get(
                    MatchStrategy.SUBJECT_EXACT, 0
                ),
                "C_file_signature": result.strategy_counts.get(
                    MatchStrategy.FILE_SIGNATURE, 0
                ),
                "D_subject_similarity": result.strategy_counts.get(
                    MatchStrategy.SUBJECT_SIMILARITY, 0
                ),
            },
        }
        # Keep INFO level for machine consumption while still concise
        log.info("RECONCILE_SUMMARY json=%s", json.dumps(debug_data))


def create_local_commit(
    index: int,
    sha: str,
    subject: str,
    files: list[str],
    commit_message: str,
) -> LocalCommit:
    """
    Create a LocalCommit with extracted Change-ID from the message.

    Args:
        index: Position in commit list
        sha: Commit SHA
        subject: Commit subject line
        files: List of modified file paths
        commit_message: Full commit message

    Returns:
        LocalCommit with extracted existing_change_id if present
    """
    existing_change_ids = extract_change_ids(commit_message)
    existing_change_id = (
        existing_change_ids[-1] if existing_change_ids else None
    )

    return LocalCommit(
        index=index,
        sha=sha,
        subject=subject,
        files=files,
        commit_message=commit_message,
        existing_change_id=existing_change_id,
    )
