# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Utilities for subject/body/files similarity scoring.

This module provides normalization helpers and scoring interfaces that
will be used by the duplicate detection pipeline to compute similarity
between:
- A source pull-request (or a squashed commit to be submitted), and
- Candidate existing changes (e.g., in Gerrit).

Design goals:
- Deterministic, testable helpers with explicit inputs/outputs.
- Clear separation between normalization, feature extraction, and scoring.
- Explainability: each scorer returns both a score and human-readable reasons.

Implementation notes:
- Most functions are fully implemented with robust normalization and scoring logic.
- The similarity scoring system supports exact matches, fuzzy matching, and automation-aware detection.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher


# Public API surface
__all__ = [
    "ScoreResult",
    "ScoringConfig",
    "aggregate_scores",
    "classify_automation_context",
    "extract_dependency_package_from_subject",
    "jaccard",
    "normalize_body",
    "normalize_subject",
    "remove_commit_trailers",
    "score_bodies",
    "score_files",
    "score_subjects",
    "sequence_ratio",
]


@dataclass(frozen=True)
class ScoringConfig:
    """
    Tunable weights and thresholds for similarity aggregation.

    Attributes:
        subject_weight: Weight applied to the subject similarity score.
        files_weight: Weight applied to the files similarity score.
        body_weight: Weight applied to the body similarity score.
        similarity_threshold: Minimum aggregated score to consider a match.
        workflow_min_floor:
            Minimum score floor for 'workflow-files-in-both' cases.
    """

    subject_weight: float = 0.45
    files_weight: float = 0.35
    body_weight: float = 0.20
    similarity_threshold: float = 0.80
    workflow_min_floor: float = 0.50


@dataclass(frozen=True)
class ScoreResult:
    """
    Result of a specific similarity check.

    Attributes:
        score: A value in [0.0, 1.0] representing similarity confidence.
        reasons: Human-readable, short explanations for the score.
    """

    score: float
    reasons: list[str]


def normalize_subject(subject: str) -> str:
    """
    Normalize a subject/first-line string for robust comparison.

    Recommended cleanups (to be implemented):
    - Lowercase.
    - Remove conventional commit prefixes (feat:, fix:, chore:, etc.).
    - Remove semantic versions and commit hashes.
    - Collapse whitespace.
    - Strip punctuation where helpful.

    Returns:
        Normalized subject string.
    """
    s = (subject or "").strip()
    # Remove conventional commit prefixes
    s = re.sub(
        r"^(feat|fix|docs|style|refactor|test|chore|ci|build|perf)(\(.+?\))?:\s*",
        "",
        s,
        flags=re.IGNORECASE,
    )
    # Remove lightweight markdown punctuation
    s = re.sub(r"[*_`]", "", s)
    # Normalize versions and commit hashes
    s = re.sub(r"\bv\d+(\.\d+)*(-[\w.]+)?\b", "vx.y.z", s)
    s = re.sub(r"\b\d+(\.\d+)+(-[\w.]+)?\b", "x.y.z", s)
    s = re.sub(r"\b\d+\.\d+\b", "x.y.z", s)
    s = re.sub(r"\b[a-f0-9]{7,40}\b", "commit_hash", s)
    # Normalize whitespace and lowercase
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def normalize_body(body: str | None) -> str:
    """
    Normalize a PR/commit body string for robust comparison.

    Recommended cleanups (to be implemented):
    - Lowercase.
    - Remove URLs, commit hashes, dates, and version numbers.
    - Normalize numeric IDs (e.g., #1234) to a placeholder.
    - Collapse whitespace.
    - Consider removing templated boilerplate for known automation tools.

    Args:
        body: Raw body text; may be None.

    Returns:
        Normalized body string (possibly empty).
    """
    if not body:
        return ""
    b = body.lower()
    # Remove URLs
    b = re.sub(r"https?://\S+", "", b)
    # Normalize versions and commit hashes and dates
    b = re.sub(
        r"v?\d+\.\d+\.\d+(?:\.\d+)?(?:-[a-z0-9.-]+)?",
        "VERSION",
        b,
        flags=re.IGNORECASE,
    )
    b = re.sub(r"\b[a-f0-9]{7,40}\b", "COMMIT", b)
    b = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", b)
    # Normalize issue/PR references
    b = re.sub(r"#\d+", "#NUMBER", b)
    # Collapse whitespace
    b = re.sub(r"\s+", " ", b).strip()
    return b


def remove_commit_trailers(message: str) -> str:
    """
    Remove commit trailers from a commit message body.

    Examples of trailers to remove (to be implemented):
    - Change-Id: Iabc123...
    - Signed-off-by: Name <email>
    - Issue-ID: ABC-123
    - GitHub-Hash: deadbeefcafebabe
    - GitHub-PR: https://github.com/org/repo/pull/123
    - Co-authored-by: ...

    Args:
        message: Full commit message including subject/body/trailers.

    Returns:
        Message body with trailers removed.
    """
    lines = (message or "").splitlines()
    out: list[str] = []
    trailer_re = re.compile(r"(?i)^(change-id|signed-off-by|issue-id|github-hash|github-pr|co-authored-by):")
    for ln in lines:
        if trailer_re.match(ln.strip()):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def extract_dependency_package_from_subject(subject: str) -> str:
    """
    Extract likely dependency/package name from a dependency update subject.

    Examples to consider (to be implemented):
    - "Bump requests from 2.31.0 to 2.32.0" -> "requests"
    - "chore: update org/tool from v1.2.3 to v1.2.4" -> "org/tool"

    Args:
        subject: The (possibly unnormalized) subject line.

    Returns:
        Package identifier, or empty string if none could be extracted.
    """
    s = (subject or "").lower()
    patterns = [
        r"(?:chore:\s*)?bump\s+([^\s]+)\s+from\s+",
        r"(?:chore:\s*)?update\s+([^\s]+)\s+from\s+",
        r"(?:chore:\s*)?upgrade\s+([^\s]+)\s+from\s+",
    ]
    for pat in patterns:
        m = re.search(pat, s)
        if m:
            pkg = m.group(1).strip().strip("'\"")
            return pkg
    return ""


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """
    Compute Jaccard similarity between two sets of strings.

    Args:
        a: Iterable of items.
        b: Iterable of items.

    Returns:
        Jaccard index in [0.0, 1.0].
    """
    sa = set(a)
    sb = set(b)
    if not sa and not sb:
        return 1.0
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return len(sa & sb) / union


def sequence_ratio(a: str, b: str) -> float:
    """
    Compute a sequence similarity ratio for two strings.

    Implementation note:
    - Intended to wrap an algorithm like difflib.SequenceMatcher in a
      testable and replaceable interface.

    Returns:
        Ratio in [0.0, 1.0].
    """
    return float(SequenceMatcher(None, a or "", b or "").ratio())


def classify_automation_context(
    title: str,
    body: str | None,
    author: str | None,
) -> list[str]:
    """
    Identify automation signals present in the change context.

    Signals to detect (to be implemented):
    - Dependabot (titles/bodies/author names, frontmatter fields).
    - pre-commit autoupdates.
    - GitHub Actions bumps (uses: owner/action@version).

    Returns:
        List of detected signals (e.g., ["dependabot", "github-actions"]).
    """
    text = f"{title or ''} {body or ''} {author or ''}".lower()
    signals: list[str] = []
    if "dependabot" in text or "dependency-name:" in text:
        signals.append("dependabot")
    if "pre-commit" in text or ".pre-commit-config.yaml" in text:
        signals.append("pre-commit")
    if "github actions" in text or ".github/workflows" in text or "uses:" in text:
        signals.append("github-actions")
    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for s in signals:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def score_subjects(
    source_subjects: Sequence[str],
    candidate_subject: str,
    *,
    strong_match_threshold: float = 0.95,
) -> ScoreResult:
    """
    Score subject similarity between one or more source subjects
    and a candidate.

    Behavior:
    - Normalize all subjects.
    - If any exact normalized match -> score=1.0, reasons include
      "Exact subject match".
    - Otherwise compute the maximum ratio across subjects vs candidate.
    - If both look like dependency updates and refer to the same package,
      prefer 1.0.
    """
    reasons: list[str] = []
    cand_norm = normalize_subject(candidate_subject)
    best_ratio = 0.0
    for src in source_subjects:
        src_norm = normalize_subject(src)
        if src_norm == cand_norm and src_norm:
            return ScoreResult(score=1.0, reasons=["Exact subject match"])
        # Prefer package equality for dependency updates
        pkg_src = extract_dependency_package_from_subject(src)
        pkg_cand = extract_dependency_package_from_subject(candidate_subject)
        if pkg_src and pkg_cand and pkg_src == pkg_cand:
            return ScoreResult(score=1.0, reasons=[f"Same dependency package: {pkg_src}"])
        r = sequence_ratio(src_norm, cand_norm)
        if r > best_ratio:
            best_ratio = r
    if best_ratio >= strong_match_threshold:
        reasons.append(f"Strongly similar subjects (ratio: {best_ratio:.2f})")
    elif best_ratio > 0:
        reasons.append(f"Similar subjects (ratio: {best_ratio:.2f})")
    return ScoreResult(score=best_ratio, reasons=reasons)


def score_files(
    source_files: Sequence[str],
    candidate_files: Sequence[str],
    *,
    workflow_min_floor: float = ScoringConfig.workflow_min_floor,
) -> ScoreResult:
    """
    Score similarity based on changed file paths.

    Behavior:
    - Normalize paths (e.g., case, strip version fragments if needed).
    - Compute Jaccard similarity across filename sets.
    - If both sides include one or more files under .github/workflows/,
      floor the score to workflow_min_floor.
    """

    def _nf(p: str) -> str:
        q = (p or "").strip().lower()
        # Remove embedded version-like fragments
        q = re.sub(r"v?\d+\.\d+\.\d+(?:\.\d+)?(?:-[\w.-]+)?", "", q)
        q = re.sub(r"\s+", " ", q).strip()
        return q

    src_set = {_nf(f) for f in source_files if f}
    cand_set = {_nf(f) for f in candidate_files if f}
    score = jaccard(src_set, cand_set)
    # Workflow floor if both sides modify workflow files
    workflows_src = any(s.startswith(".github/workflows/") for s in src_set)
    workflows_cand = any(s.startswith(".github/workflows/") for s in cand_set)
    reasons: list[str] = []
    if workflows_src and workflows_cand and score < workflow_min_floor:
        score = max(score, float(workflow_min_floor))
        reasons.append("Both modify workflow files (.github/workflows/*)")
    if src_set or cand_set:
        reasons.append(
            f"File overlap Jaccard: {score:.2f} (|n|={len(src_set & cand_set)}, |U|={len(src_set | cand_set)})"
        )
    return ScoreResult(score=score, reasons=reasons)


def score_bodies(
    source_body: str | None,
    candidate_body: str | None,
) -> ScoreResult:
    """
    Score similarity based on normalized body text and automation patterns.
    """
    if not source_body or not candidate_body:
        return ScoreResult(score=0.0, reasons=[])
    # Very short bodies: exact match or zero
    if len(source_body.strip()) < 50 or len(candidate_body.strip()) < 50:
        if normalize_body(source_body) == normalize_body(candidate_body):
            return ScoreResult(score=1.0, reasons=["Short bodies exactly match"])
        return ScoreResult(score=0.0, reasons=[])
    reasons: list[str] = []
    # Automation-aware checks
    src_text = source_body or ""
    cand_text = candidate_body or ""
    src_is_dep = "dependabot" in src_text.lower() or "dependency-name:" in src_text.lower()
    cand_is_dep = "dependabot" in cand_text.lower() or "dependency-name:" in cand_text.lower()
    if src_is_dep and cand_is_dep:
        pkg1 = ""
        pkg2 = ""
        m1 = re.search(r"dependency-name:\s*([^\s\n]+)", src_text, flags=re.IGNORECASE)
        m2 = re.search(
            r"dependency-name:\s*([^\s\n]+)",
            cand_text,
            flags=re.IGNORECASE,
        )
        if m1:
            pkg1 = m1.group(1).strip()
        if m2:
            pkg2 = m2.group(1).strip()
        if pkg1 and pkg2 and pkg1 == pkg2:
            return ScoreResult(score=0.95, reasons=[f"Dependabot package match: {pkg1}"])
        # Different packages -> slight similarity for being both dependabot
        reasons.append("Both look like Dependabot bodies")
        # do not return yet; fall through to normalized ratio
    src_is_pc = "pre-commit" in src_text.lower() or ".pre-commit-config.yaml" in src_text.lower()
    cand_is_pc = "pre-commit" in cand_text.lower() or ".pre-commit-config.yaml" in cand_text.lower()
    if src_is_pc and cand_is_pc:
        return ScoreResult(score=0.9, reasons=["Both look like pre-commit updates"])
    src_is_actions = (
        "github actions" in src_text.lower() or ".github/workflows" in src_text.lower() or "uses:" in src_text.lower()
    )
    cand_is_actions = (
        "github actions" in cand_text.lower()
        or ".github/workflows" in cand_text.lower()
        or "uses:" in cand_text.lower()
    )
    if src_is_actions and cand_is_actions:
        a1 = re.search(r"uses:\s*([^@\s]+)", src_text, flags=re.IGNORECASE)
        a2 = re.search(r"uses:\s*([^@\s]+)", cand_text, flags=re.IGNORECASE)
        if a1 and a2 and a1.group(1).strip() and a1.group(1).strip() == a2.group(1).strip():
            return ScoreResult(
                score=0.9,
                reasons=[f"Same GitHub Action: {a1.group(1).strip()}"],
            )
        reasons.append("Both look like GitHub Actions updates")
    # Fallback to normalized sequence ratio
    nb1 = normalize_body(source_body)
    nb2 = normalize_body(candidate_body)
    ratio = sequence_ratio(nb1, nb2)
    if ratio >= 0.6:
        reasons.append(f"Similar bodies (ratio: {ratio:.2f})")
    return ScoreResult(score=ratio, reasons=reasons)


def aggregate_scores(
    subject_score: float,
    files_score: float,
    body_score: float,
    *,
    config: ScoringConfig | None = None,
) -> float:
    """
    Aggregate component scores into a single confidence value.

    Args:
        subject_score: Score in [0,1] for subject similarity.
        files_score: Score in [0,1] for files similarity.
        body_score: Score in [0,1] for body similarity.
        config: Weighting and threshold configuration.

    Returns:
        Weighted average in [0,1].
    """
    if config is None:
        config = ScoringConfig()
    w_sum = float(config.subject_weight + config.files_weight + config.body_weight)
    if w_sum <= 0:
        return 0.0
    total = (
        config.subject_weight * float(subject_score)
        + config.files_weight * float(files_score)
        + config.body_weight * float(body_score)
    )
    return max(0.0, min(1.0, total / w_sum))
