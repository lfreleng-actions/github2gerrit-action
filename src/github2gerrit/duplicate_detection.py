# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Duplicate change detection for github2gerrit.

This module provides functionality to detect potentially duplicate changes
before submitting them to Gerrit, helping to prevent spam and redundant
submissions from automated tools like Dependabot.
"""

import hashlib
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from .gerrit_urls import create_gerrit_url_builder
from .github_api import GhPullRequest
from .github_api import GhRepository
from .github_api import build_client
from .github_api import get_repo_from_env
from .models import GitHubContext
from .trailers import extract_github_metadata


# Optional Gerrit REST API support
try:
    from pygerrit2 import GerritRestAPI
    from pygerrit2 import HTTPBasicAuth
except ImportError:
    GerritRestAPI = None
    HTTPBasicAuth = None


log = logging.getLogger(__name__)

__all__ = [
    "ChangeFingerprint",
    "DuplicateChangeError",
    "DuplicateDetector",
    "check_for_duplicates",
]


class DuplicateChangeError(Exception):
    """Raised when a duplicate change is detected."""

    def __init__(
        self,
        message: str,
        existing_prs: list[int],
        urls: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.existing_prs = existing_prs
        self.urls = urls or []


class ChangeFingerprint:
    """Represents a fingerprint of a change for duplicate detection."""

    def __init__(
        self, title: str, body: str = "", files_changed: list[str] | None = None
    ):
        self.title = title.strip()
        self.body = (body or "").strip()
        self.files_changed = sorted(files_changed or [])
        self._normalized_title = self._normalize_title(title)
        self._content_hash = self._compute_content_hash()

    def _normalize_title(self, title: str) -> str:
        """Normalize PR title for comparison."""
        # Remove common prefixes/suffixes
        normalized = title.strip()

        # Remove conventional commit prefixes like "feat:", "fix:", etc.
        normalized = re.sub(
            r"^(feat|fix|docs|style|refactor|test|chore|ci|build|perf)"
            r"(\(.+?\))?: ",
            "",
            normalized,
            flags=re.IGNORECASE,
        )

        # Remove markdown formatting
        normalized = re.sub(r"[*_`]", "", normalized)

        # Remove version number variations for dependency updates
        # E.g., "from 0.6 to 0.8" -> "from x.y.z to x.y.z"
        # Handle v-prefixed versions first, then plain versions
        normalized = re.sub(r"\bv\d+(\.\d+)*(-\w+)?\b", "vx.y.z", normalized)
        normalized = re.sub(r"\b\d+(\.\d+)+(-\w+)?\b", "x.y.z", normalized)
        normalized = re.sub(r"\b\d+\.\d+\b", "x.y.z", normalized)

        # Remove specific commit hashes
        normalized = re.sub(r"\b[a-f0-9]{7,40}\b", "commit_hash", normalized)

        # Normalize whitespace
        normalized = re.sub(r"\s+", " ", normalized).strip()

        return normalized.lower()

    def _compute_content_hash(self) -> str:
        """Compute a hash of the change content."""
        content = (
            f"{self._normalized_title}\n{self.body}\n"
            f"{','.join(self.files_changed)}"
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def is_similar_to(
        self, other: "ChangeFingerprint", similarity_threshold: float = 0.8
    ) -> bool:
        """Check if this fingerprint is similar to another."""
        # Exact normalized title match
        if self._normalized_title == other._normalized_title:
            return True

        # Content hash match
        if self._content_hash == other._content_hash:
            return True

        # Check for similar file changes (for dependency updates)
        if self.files_changed and other.files_changed:
            common_files = set(self.files_changed) & set(other.files_changed)
            union_files = set(self.files_changed) | set(other.files_changed)
            if common_files and union_files:
                overlap_ratio = len(common_files) / len(union_files)
                # If files overlap, check title similarity (lower threshold)
                if overlap_ratio > 0:
                    return self._titles_similar(other, 0.6)

        # Check title similarity even without file changes
        return self._titles_similar(other, similarity_threshold)

    def _titles_similar(
        self, other: "ChangeFingerprint", threshold: float
    ) -> bool:
        """Check if titles are similar using simple string similarity."""
        title1 = self._normalized_title
        title2 = other._normalized_title

        if not title1 or not title2:
            return False

        # Simple Jaccard similarity on words
        words1 = set(title1.split())
        words2 = set(title2.split())

        if not words1 or not words2:
            return False

        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return (intersection / union) >= threshold

    def __str__(self) -> str:
        return (
            f"ChangeFingerprint(title='{self.title[:50]}...', "
            f"hash={self._content_hash})"
        )


class DuplicateDetector:
    """Detects duplicate Gerrit changes for GitHub pull requests."""

    def __init__(
        self,
        repo: GhRepository,
        lookback_days: int = 7,
        duplicates_filter: str = "open",
    ):
        self.repo = repo
        self.lookback_days = lookback_days
        self._cutoff_date = datetime.now(UTC) - timedelta(days=lookback_days)
        self.duplicates_filter = duplicates_filter

    def _match_first_group(self, pattern: str, text: str) -> str:
        """Extract first regex group match from text."""
        match = re.search(pattern, text)
        return match.group(1) if match else ""

    def _resolve_gerrit_info_from_env_or_gitreview(
        self, gh: GitHubContext
    ) -> tuple[str, str] | None:
        """Resolve Gerrit host and project from environment or .gitreview file.

        Returns:
            Tuple of (host, project) if found, None otherwise
        """
        # First try environment variables (same as core module)
        gerrit_host = os.getenv("GERRIT_SERVER", "").strip()
        gerrit_project = os.getenv("GERRIT_PROJECT", "").strip()

        if gerrit_host and gerrit_project:
            return (gerrit_host, gerrit_project)

        # Try to read .gitreview file locally first
        gitreview_path = Path(".gitreview")
        if gitreview_path.exists():
            try:
                text = gitreview_path.read_text(encoding="utf-8")
                host = self._match_first_group(r"(?m)^host=(.+)$", text)
                proj = self._match_first_group(r"(?m)^project=(.+)$", text)
                if host and proj:
                    project = proj.removesuffix(".git")
                    return (host.strip(), project.strip())
                if host and not proj:
                    return (host.strip(), "")
            except Exception as exc:
                log.debug("Failed to read local .gitreview: %s", exc)

        # Try to fetch .gitreview remotely (simplified version of core logic)
        try:
            repo_full = gh.repository.strip() if gh.repository else ""
            if not repo_full:
                return None

            # Try a few common branches
            branches = []
            if gh.head_ref:
                branches.append(gh.head_ref)
            if gh.base_ref:
                branches.append(gh.base_ref)
            branches.extend(["master", "main"])

            for branch in branches:
                if not branch:
                    continue

                url = f"https://raw.githubusercontent.com/{repo_full}/{branch}/.gitreview"

                parsed = urllib.parse.urlparse(url)
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "raw.githubusercontent.com"
                ):
                    continue

                try:
                    log.debug("Fetching .gitreview from: %s", url)
                    with urllib.request.urlopen(url, timeout=5) as resp:
                        text_remote = resp.read().decode("utf-8")

                    host = self._match_first_group(
                        r"(?m)^host=(.+)$", text_remote
                    )
                    proj = self._match_first_group(
                        r"(?m)^project=(.+)$", text_remote
                    )

                    if host and proj:
                        project = proj.removesuffix(".git")
                        return (host.strip(), project.strip())
                    if host and not proj:
                        return (host.strip(), "")

                except Exception as exc:
                    log.debug(
                        "Failed to fetch .gitreview from %s: %s", url, exc
                    )
                    continue

        except Exception as exc:
            log.debug("Failed to resolve .gitreview remotely: %s", exc)

        return None

    def _build_gerrit_rest_client(self, gerrit_host: str) -> Any | None:
        """Build a Gerrit REST API client using centralized framework."""
        from .gerrit_rest import build_client_for_host

        http_user = (
            os.getenv("GERRIT_HTTP_USER", "").strip()
            or os.getenv("GERRIT_SSH_USER_G2G", "").strip()
        )
        http_pass = os.getenv("GERRIT_HTTP_PASSWORD", "").strip()

        try:
            return build_client_for_host(
                gerrit_host,
                timeout=8.0,
                max_attempts=3,
                http_user=http_user or None,
                http_password=http_pass or None,
            )
        except Exception as exc:
            log.debug("Failed to create Gerrit REST client: %s", exc)
            return None

    @staticmethod
    def _generate_github_change_hash(gh: GitHubContext) -> str:
        """Generate a deterministic hash for a GitHub PR to identify duplicates.

        This creates a SHA256 hash based on stable PR metadata that uniquely
        identifies the change content, making duplicate detection reliable
        regardless of comment formatting or API issues.

        Args:
            gh: GitHub context containing PR information

        Returns:
            Hex-encoded SHA256 hash string (first 16 characters for readability)
        """
        import hashlib

        # Build hash input from stable, unique PR identifiers
        # Use server_url + repository + pr_number for global uniqueness
        hash_input = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"

        # Create SHA256 hash and take first 16 characters for readability
        hash_bytes = hashlib.sha256(hash_input.encode("utf-8")).digest()
        hash_hex = hash_bytes.hex()[:16]

        log.debug(
            "Generated GitHub change hash for %s: %s", hash_input, hash_hex
        )
        return hash_hex

    def check_for_duplicates(
        self,
        target_pr: GhPullRequest,
        allow_duplicates: bool = False,
        gh: GitHubContext | None = None,
        expected_github_hash: str | None = None,
    ) -> None:
        """Check if the target PR is a duplicate via trailer-aware and subject
        equality against Gerrit.

        Implements a robust, dependency-free duplicate check with trailer
        awareness:
        - First check for existing changes with matching GitHub-Hash trailer
          (short-circuit)
        - Resolve Gerrit host/project from env or .gitreview
        - Query Gerrit changes updated within the lookback window (excluding
          abandoned)
        - Compare normalized subjects (first line) for exact equality
        - If any match, treat as duplicate and either warn or raise

        Args:
            target_pr: The GitHub PR to check for duplicates
            allow_duplicates: If True, log warnings instead of raising errors
            gh: GitHub context for resolving Gerrit configuration
            expected_github_hash: The GitHub-Hash trailer value expected for
                this PR
        """
        pr_number = getattr(target_pr, "number", 0)
        pr_title = (getattr(target_pr, "title", "") or "").strip()

        log.debug(
            "Checking PR #%d for duplicates via subject equality against "
            "Gerrit",
            pr_number,
        )

        if not pr_title:
            log.debug(
                "PR #%d has empty title; skipping duplicate check", pr_number
            )
            return
        if gh is None:
            log.debug("No GitHub context provided; skipping duplicate check")
            return

        # Resolve Gerrit target (host/project)
        gerrit_info = self._resolve_gerrit_info_from_env_or_gitreview(gh)
        if not gerrit_info:
            log.debug(
                "Unable to resolve Gerrit host/project; skipping duplicate "
                "check"
            )
            return
        gerrit_host, gerrit_project = gerrit_info

        # Helper: normalize subject like our existing title normalization
        def _normalize_subject(title: str) -> str:
            normalized = title.strip()
            normalized = re.sub(
                r"^(feat|fix|docs|style|refactor|test|chore|ci|build|perf)"
                r"(\(.+?\))?: ",
                "",
                normalized,
                flags=re.IGNORECASE,
            )
            normalized = re.sub(r"[*_`]", "", normalized)
            normalized = re.sub(
                r"\bv\d+(\.\d+)*(-\w+)?\b", "vx.y.z", normalized
            )
            normalized = re.sub(r"\b\d+(\.\d+)+(-\w+)?\b", "x.y.z", normalized)
            normalized = re.sub(r"\b\d+\.\d+\b", "x.y.z", normalized)
            normalized = re.sub(
                r"\b[a-f0-9]{7,40}\b", "commit_hash", normalized
            )
            normalized = re.sub(r"\s+", " ", normalized).strip()
            return normalized.lower()

        normalized_pr_subject = _normalize_subject(pr_title)
        log.debug(
            "Normalized PR subject for duplicate check: %s",
            normalized_pr_subject,
        )

        # Build Gerrit REST URL using centralized URL builder
        url_builder = create_gerrit_url_builder(gerrit_host)

        # Track which base path actually works for constructing display URLs
        successful_base_path = url_builder.base_path

        # Build query: limit to recent changes, exclude abandoned; prefer open
        cutoff_date = self._cutoff_date.date().isoformat()
        q_parts = []
        if gerrit_project:
            q_parts.append(f"project:{gerrit_project}")
        # Build status clause from DUPLICATE_TYPES filter (default: open)
        dup_filter = (self.duplicates_filter or "open").strip().lower()
        selected = [s.strip() for s in dup_filter.split(",") if s.strip()]
        valid = {
            "open": "status:open",
            "merged": "status:merged",
            "abandoned": "status:abandoned",
        }
        status_terms = [valid[s] for s in selected if s in valid]
        if not status_terms:
            status_clause = "status:open"
        elif len(status_terms) == 1:
            status_clause = status_terms[0]
        else:
            status_clause = "(" + " OR ".join(status_terms) + ")"
        q_parts.append(status_clause)
        q_parts.append(f"after:{cutoff_date}")
        query = " ".join(q_parts)
        encoded_q = urllib.parse.quote(query, safe="")

        def _load_gerrit_json(query_path: str) -> list[dict[str, object]]:
            try:
                # Use centralized client that handles base path and auth
                client = self._build_gerrit_rest_client(gerrit_host)
                if client is None:
                    log.debug(
                        "Gerrit client not available; skipping duplicate check"
                    )
                    return []

                log.debug("Querying Gerrit for duplicates: %s", query_path)
                data = client.get(query_path)
                if isinstance(data, list):
                    return data
                else:
                    return []
            except Exception as exc:
                log.debug("Gerrit query failed for %s: %s", query_path, exc)
                return []

        # Build query path for centralized client
        query_path = (
            f"/changes/?q={encoded_q}&n=50&o=CURRENT_COMMIT&o=CURRENT_FILES"
        )

        log.debug(
            "Gerrit duplicate query: host=%s project=%s filter=%s cutoff=%s "
            "path=%s",
            gerrit_host,
            gerrit_project or "(any)",
            dup_filter,
            cutoff_date,
            query_path,
        )
        changes = _load_gerrit_json(query_path)
        log.debug(
            "Gerrit query returned %d change(s) for project=%s filter=%s "
            "after=%s",
            len(changes),
            gerrit_project or "(any)",
            dup_filter,
            cutoff_date,
        )
        if changes:
            sample_subjects = ", ".join(
                str(c.get("subject") or "")[:60] for c in changes[:5]
            )
            log.debug("Sample subjects: %s", sample_subjects)

        # First pass: Check for trailer-based matches (GitHub-Hash)
        if expected_github_hash:
            log.debug(
                "Checking for GitHub-Hash trailer matches: %s",
                expected_github_hash,
            )
            trailer_matches: list[tuple[int, str]] = []

            for c in changes:
                # Extract commit message and check for GitHub trailers
                rev = str(c.get("current_revision") or "")
                revs_obj = c.get("revisions")
                revs = revs_obj if isinstance(revs_obj, dict) else {}
                cur_obj = revs.get(rev)
                cur = cur_obj if isinstance(cur_obj, dict) else {}
                commit = cur.get("commit") or {}
                msg = str(commit.get("message") or "")

                if msg:
                    github_metadata = extract_github_metadata(msg)
                    change_github_hash = github_metadata.get("GitHub-Hash", "")

                    if change_github_hash == expected_github_hash:
                        num = c.get("_number")
                        proj = str(c.get("project") or gerrit_project or "")
                        if isinstance(num, int):
                            trailer_matches.append((num, proj))
                            log.debug(
                                "Found GitHub-Hash trailer match: change %d, "
                                "hash %s",
                                num,
                                change_github_hash,
                            )

            if trailer_matches:
                log.debug(
                    "Found %d change(s) with matching GitHub-Hash trailer - "
                    "treating as update targets",
                    len(trailer_matches),
                )
                # These are update targets, not duplicates - allow them to
                # proceed
                return

        # Compare normalized subjects for exact equality
        matched: list[tuple[int, str]] = []
        for c in changes:
            subj = str(c.get("subject") or "").strip()
            if not subj:
                continue
            if _normalize_subject(subj) == normalized_pr_subject:
                num = c.get("_number")
                proj = str(c.get("project") or gerrit_project or "")
                if isinstance(num, int):
                    matched.append((num, proj))

        if not matched:
            # No exact subject match; proceed with similarity scoring across
            # candidates
            log.debug(
                "No exact-subject matches found; entering similarity scoring"
            )
            from .similarity import ScoringConfig
            from .similarity import aggregate_scores
            from .similarity import remove_commit_trailers
            from .similarity import score_bodies
            from .similarity import score_files
            from .similarity import score_subjects

            config = ScoringConfig()
            # Source features from the PR
            src_subjects = [pr_title]
            src_body = str(getattr(target_pr, "body", "") or "")
            src_files: list[str] = []
            try:
                get_files = getattr(target_pr, "get_files", None)
                if callable(get_files):
                    files_obj = get_files()
                    if isinstance(files_obj, Iterable):
                        for f in files_obj:
                            fname = getattr(f, "filename", None)
                            if fname:
                                src_files.append(str(fname))
            except Exception as exc:
                # Best-effort; if files cannot be retrieved, proceed without
                # them
                log.debug("Failed to retrieve PR files for scoring: %s", exc)

            best_score = 0.0
            best_reasons: list[str] = []
            hits: list[tuple[float, str, int | None]] = []
            all_nums: list[int] = []
            for c in changes:
                subj = str(c.get("subject") or "").strip()
                if not subj:
                    continue
                # Extract commit message and files from revisions
                # (CURRENT_COMMIT, CURRENT_FILES)
                rev = str(c.get("current_revision") or "")
                revs_obj = c.get("revisions")
                revs = revs_obj if isinstance(revs_obj, dict) else {}
                cur_obj = revs.get(rev)
                cur = cur_obj if isinstance(cur_obj, dict) else {}
                commit = cur.get("commit") or {}
                msg = str(commit.get("message") or "")
                cand_body_raw = ""
                if "\n" in msg:
                    cand_body_raw = msg.split("\n", 1)[1]
                cand_body = remove_commit_trailers(cand_body_raw)
                files_dict = cur.get("files") or {}
                cand_files = [
                    p
                    for p in files_dict
                    if isinstance(p, str) and not p.startswith("/")
                ]

                # Compute component scores
                s_res = score_subjects(src_subjects, subj)
                f_res = score_files(
                    src_files,
                    cand_files,
                    workflow_min_floor=config.workflow_min_floor,
                )
                b_res = score_bodies(src_body, cand_body)

                # Aggregate
                agg = aggregate_scores(
                    s_res.score, f_res.score, b_res.score, config=config
                )
                log.debug(
                    "Aggregate score computed: %.2f (s=%.2f f=%.2f b=%.2f)",
                    agg,
                    s_res.score,
                    f_res.score,
                    b_res.score,
                )

                # Build candidate reference and number using successful base
                # path
                num_obj = c.get("_number")
                num = int(num_obj) if isinstance(num_obj, int) else None
                proj = str(c.get("project") or gerrit_project or "")

                # Use the base path that actually worked for API calls
                display_url_builder = create_gerrit_url_builder(
                    gerrit_host, successful_base_path
                )
                ref = (
                    display_url_builder.change_url(proj, num)
                    if proj and isinstance(num, int)
                    else (f"change {num}" if isinstance(num, int) else "")
                )
                log.debug(
                    "Scoring candidate: ref=%s agg=%.2f (s=%.2f f=%.2f b=%.2f) "
                    "subj='%s'",
                    ref or "(none)",
                    agg,
                    s_res.score,
                    f_res.score,
                    b_res.score,
                    subj[:200],
                )

                # Track best (for reasons)
                if agg > best_score:
                    best_score = agg
                    # Deduplicate reasons preserving order
                    best_reasons = list(
                        dict.fromkeys(
                            s_res.reasons + f_res.reasons + b_res.reasons
                        )
                    )

                # Collect all candidates above threshold
                if agg >= config.similarity_threshold and ref:
                    hits.append((agg, ref, num))
                    if isinstance(num, int):
                        all_nums.append(num)

            log.debug(
                "Similarity scoring found %d hit(s) (threshold=%.2f)",
                len(hits),
                config.similarity_threshold,
            )
            if hits:
                hits_sorted = sorted(hits, key=lambda t: t[0], reverse=True)

                # Log each matching change individually
                for s, u, _ in hits_sorted:
                    if u:
                        log.info("Score: %.2f  URL: %s", s, u)
                msg = (
                    f"Similar Gerrit change(s) detected "
                    f"[≥ {config.similarity_threshold:.2f}]"
                )
                if best_reasons:
                    msg += f" (Reasons: {', '.join(best_reasons)})"
                if allow_duplicates:
                    log.warning("GERRIT DUPLICATE DETECTED (allowed): %s", msg)
                    return
                raise DuplicateChangeError(msg, all_nums)

        # Construct human-friendly references for logs
        matching_numbers: list[int] = []
        match_lines: list[str] = []
        duplicate_urls: list[str] = []
        for n, proj in matched:
            if proj:
                # Use the base path that actually worked for API calls
                display_url_builder = create_gerrit_url_builder(
                    gerrit_host, successful_base_path
                )
                url = display_url_builder.change_url(proj, n)
                match_lines.append(f"Score: 1.0  URL: {url}")
                duplicate_urls.append(url)
                log.debug("Score: 1.0  URL: %s", url)
            else:
                match_lines.append(f"Score: 1.0  URL: change {n}")
                duplicate_urls.append(f"change {n}")
                log.debug("Score: 1.0  URL: change %s", n)
            matching_numbers.append(n)

        if not matched:
            log.debug(
                "No exact subject matches and no similarity matches; "
                "duplicate check passes"
            )
            return

        # Remove PR number from message since cli.py already includes it
        full_message = "subject matches existing Gerrit change(s)"
        if allow_duplicates:
            log.debug("GERRIT DUPLICATE DETECTED (allowed): %s", full_message)
            return
        raise DuplicateChangeError(
            full_message, matching_numbers, duplicate_urls
        )


def check_for_duplicates(
    gh: GitHubContext,
    allow_duplicates: bool = False,
    lookback_days: int = 7,
    expected_github_hash: str | None = None,
) -> None:
    """Convenience function to check for duplicates.

    Args:
        gh: GitHub context containing PR information
        allow_duplicates: If True, only log warnings; if False, raise exception
        lookback_days: Number of days to look back for similar PRs
        expected_github_hash: The GitHub-Hash trailer value expected for this PR

    Raises:
        DuplicateChangeError: If duplicates found and allow_duplicates=False
    """
    if not gh.pr_number:
        log.debug("No PR number provided, skipping duplicate check")
        return

    try:
        client = build_client()
        repo = get_repo_from_env(client)

        # Get the target PR
        target_pr = repo.get_pull(gh.pr_number)

        # Create detector and check
        detector = DuplicateDetector(
            repo,
            lookback_days=lookback_days,
            duplicates_filter=os.getenv("DUPLICATE_TYPES", "open"),
        )
        detector.check_for_duplicates(
            target_pr,
            allow_duplicates=allow_duplicates,
            gh=gh,
            expected_github_hash=expected_github_hash,
        )

        log.debug("Duplicate check completed for PR #%d", gh.pr_number)

    except DuplicateChangeError:
        # Re-raise duplicate errors
        raise
    except Exception as exc:
        log.warning(
            "Duplicate detection failed for PR #%d: %s", gh.pr_number, exc
        )
        # Don't fail the entire process if duplicate detection has issues
