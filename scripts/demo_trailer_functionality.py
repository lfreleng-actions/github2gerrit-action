#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Demo script showcasing the implemented trailer functionality.

This script demonstrates the trailer-aware duplicate detection and
enhanced reconciliation features that have been implemented.
"""

import sys
from pathlib import Path


# Add src to Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from github2gerrit.mapping_comment import ChangeIdMapping
from github2gerrit.mapping_comment import compute_mapping_digest
from github2gerrit.mapping_comment import parse_mapping_comments
from github2gerrit.mapping_comment import serialize_mapping_comment
from github2gerrit.mapping_comment import update_mapping_comment_body
from github2gerrit.mapping_comment import validate_mapping_consistency
from github2gerrit.trailers import add_trailers
from github2gerrit.trailers import compute_file_signature
from github2gerrit.trailers import compute_jaccard_similarity
from github2gerrit.trailers import extract_change_ids
from github2gerrit.trailers import extract_github_metadata
from github2gerrit.trailers import extract_subject_tokens
from github2gerrit.trailers import has_trailer
from github2gerrit.trailers import normalize_subject_for_matching
from github2gerrit.trailers import parse_trailers


def demo_trailer_parsing():
    """Demo trailer parsing and manipulation."""
    print("=== Trailer Parsing Demo ===")

    # Example commit message with trailers
    commit_msg = """Fix critical bug in network parser

This commit resolves an issue where the network parser would fail
to handle malformed packets properly, causing crashes.

The fix adds proper validation and error handling.

Change-Id: I1234567890abcdef1234567890abcdef12345678
Signed-off-by: Developer <dev@example.com>
GitHub-PR: https://github.com/owner/repo/pull/123
GitHub-Hash: abc12345"""

    print("Original commit message:")
    print(commit_msg)
    print()

    # Parse all trailers
    trailers = parse_trailers(commit_msg)
    print("Parsed trailers:")
    for key, values in trailers.items():
        print(f"  {key}: {values}")
    print()

    # Extract GitHub metadata specifically
    github_meta = extract_github_metadata(commit_msg)
    print("GitHub metadata:")
    for key, value in github_meta.items():
        print(f"  {key}: {value}")
    print()

    # Extract Change-IDs
    change_ids = extract_change_ids(commit_msg)
    print(f"Change-IDs found: {change_ids}")
    print()

    # Check for specific trailers
    print("Trailer checks:")
    print(f"  Has Change-Id: {has_trailer(commit_msg, 'Change-Id')}")
    print(f"  Has GitHub-PR: {has_trailer(commit_msg, 'GitHub-PR')}")
    print(
        f"  Has specific GitHub-Hash: "
        f"{has_trailer(commit_msg, 'GitHub-Hash', 'abc12345')}"
    )
    print()


def demo_trailer_addition():
    """Demo adding trailers to commit messages."""
    print("=== Trailer Addition Demo ===")

    base_commit = """Implement new feature for user authentication

This adds OAuth 2.0 support with proper token validation
and refresh token handling."""

    # Add GitHub trailers
    new_trailers = {
        "Change-Id": "I9876543210fedcba9876543210fedcba98765432",
        "GitHub-PR": "https://github.com/owner/repo/pull/456",
        "GitHub-Hash": "def67890",
        "Signed-off-by": "Engineer <engineer@example.com>",
    }

    enhanced_commit = add_trailers(base_commit, new_trailers)

    print("Base commit:")
    print(base_commit)
    print()
    print("After adding trailers:")
    print(enhanced_commit)
    print()


def demo_subject_normalization():
    """Demo subject normalization and similarity matching."""
    print("=== Subject Normalization Demo ===")

    test_subjects = [
        "WIP: Fix parser bug [v1.2.3] !!",
        "Fix parser bug",
        "DRAFT: Fix critical parser issue (v2.0)",
        "Fix Parser Bug.",
        "fix parser bug in network module",
    ]

    print("Subject normalization:")
    for subject in test_subjects:
        normalized = normalize_subject_for_matching(subject)
        print(f"  '{subject}' → '{normalized}'")
    print()

    # Demo token extraction and similarity
    print("Token extraction and similarity:")
    subject1 = "Fix critical bug in network parser"
    subject2 = "Fix important bug in network module"

    tokens1 = extract_subject_tokens(subject1)
    tokens2 = extract_subject_tokens(subject2)
    similarity = compute_jaccard_similarity(tokens1, tokens2)

    print(f"Subject 1: '{subject1}'")
    print(f"  Tokens: {tokens1}")
    print(f"Subject 2: '{subject2}'")
    print(f"  Tokens: {tokens2}")
    print(f"  Jaccard similarity: {similarity:.2f}")
    print()


def demo_file_signatures():
    """Demo file signature computation."""
    print("=== File Signature Demo ===")

    # Different file sets
    files1 = ["src/main.py", "src/utils.py", "tests/test_main.py"]
    files2 = ["src/main.py", "src/utils.py", "tests/test_main.py"]  # Same
    files3 = [
        "SRC/MAIN.PY",
        "src/utils.py",
        "tests/test_main.py/",
    ]  # Different case/slashes
    files4 = [
        "src/parser.py",
        "src/network.py",
        "tests/test_parser.py",
    ]  # Different files

    sig1 = compute_file_signature(files1)
    sig2 = compute_file_signature(files2)
    sig3 = compute_file_signature(files3)
    sig4 = compute_file_signature(files4)

    print("File signature computation:")
    print(f"  Files 1: {files1}")
    print(f"    Signature: {sig1}")
    print(f"  Files 2 (identical): {files2}")
    print(f"    Signature: {sig2}")
    print(f"    Match files 1: {sig1 == sig2}")
    print(f"  Files 3 (case/slash differences): {files3}")
    print(f"    Signature: {sig3}")
    print(f"    Match files 1: {sig1 == sig3}")
    print(f"  Files 4 (different files): {files4}")
    print(f"    Signature: {sig4}")
    print(f"    Match files 1: {sig1 == sig4}")
    print()


def demo_mapping_comments():
    """Demo mapping comment functionality."""
    print("=== Mapping Comment Demo ===")

    # Create a mapping comment
    change_ids = [
        "I1111111111111111111111111111111111111111",
        "I2222222222222222222222222222222222222222",
        "I3333333333333333333333333333333333333333",
    ]

    comment = serialize_mapping_comment(
        pr_url="https://github.com/owner/repo/pull/789",
        mode="multi-commit",
        topic="GH-owner-repo-789",
        change_ids=change_ids,
        github_hash="xyz98765",
    )

    print("Generated mapping comment:")
    print(comment)
    print()

    # Parse it back
    parsed = parse_mapping_comments([comment])
    if parsed:
        print("Parsed mapping:")
        print(f"  PR URL: {parsed.pr_url}")
        print(f"  Mode: {parsed.mode}")
        print(f"  Topic: {parsed.topic}")
        print(f"  Change-IDs: {parsed.change_ids}")
        print(f"  GitHub-Hash: {parsed.github_hash}")
        print()

    # Demo updating existing comment
    existing_comment = """Some existing comment text.

<!-- github2gerrit:change-id-map v1 -->
PR: https://github.com/owner/repo/pull/789
Mode: squash
Topic: old-topic
Change-Ids:
  I0000000000000000000000000000000000000000
GitHub-Hash: old123
<!-- end github2gerrit:change-id-map -->

More comment text."""

    new_mapping = ChangeIdMapping(
        pr_url="https://github.com/owner/repo/pull/789",
        mode="multi-commit",
        topic="updated-topic",
        change_ids=change_ids,
        github_hash="new456",
    )

    updated_comment = update_mapping_comment_body(existing_comment, new_mapping)

    print("Updated comment (replace-in-place):")
    print(updated_comment)
    print()

    # Demo validation
    is_valid = validate_mapping_consistency(
        new_mapping,
        expected_pr_url="https://github.com/owner/repo/pull/789",
        expected_github_hash="new456",
    )
    print(f"Mapping validation result: {is_valid}")

    # Demo digest computation
    digest = compute_mapping_digest(change_ids)
    print(f"Change-ID list digest: {digest}")
    print()


def demo_duplicate_detection_simulation():
    """Demo the trailer-aware duplicate detection concept."""
    print("=== Duplicate Detection Simulation ===")

    print("Scenario: Rerunning github2gerrit on a PR")
    print()

    # Simulate a PR rerun
    pr_url = "https://github.com/owner/repo/pull/123"
    github_hash = "abc12345"  # Generated from PR metadata

    print(f"PR URL: {pr_url}")
    print(f"Expected GitHub-Hash: {github_hash}")
    print()

    # Simulate existing Gerrit change with trailer
    existing_commit_msg = f"""Fix network parser bug

This resolves crashes when handling malformed packets.

Change-Id: I1234567890abcdef1234567890abcdef12345678
Signed-off-by: Developer <dev@example.com>
GitHub-PR: {pr_url}
GitHub-Hash: {github_hash}"""

    print("Existing Gerrit change commit message:")
    print(existing_commit_msg)
    print()

    # Check for GitHub metadata match
    existing_meta = extract_github_metadata(existing_commit_msg)
    existing_hash = existing_meta.get("GitHub-Hash", "")
    existing_pr = existing_meta.get("GitHub-PR", "")

    print("Duplicate detection logic:")
    print(f"  Existing GitHub-Hash: {existing_hash}")
    print(f"  Expected GitHub-Hash: {github_hash}")
    print(f"  Hash match: {existing_hash == github_hash}")
    print(f"  Existing GitHub-PR: {existing_pr}")
    print(f"  Expected GitHub-PR: {pr_url}")
    print(f"  PR match: {existing_pr == pr_url}")
    print()

    if existing_hash == github_hash and existing_pr == pr_url:
        print("✅ RESULT: Trailer-aware match found!")
        print(
            "   This change will be treated as an UPDATE TARGET, not a "
            "duplicate."
        )
        print("   The existing Change-ID will be reused for the new patch set.")
    else:
        print("❌ RESULT: No trailer match found.")
        print("   Proceeding with subject-based duplicate detection...")
    print()


def main():
    """Run all demo functions."""
    print("GitHub2Gerrit Trailer Functionality Demo")
    print("=" * 50)
    print()

    try:
        demo_trailer_parsing()
        demo_trailer_addition()
        demo_subject_normalization()
        demo_file_signatures()
        demo_mapping_comments()
        demo_duplicate_detection_simulation()

        print("Demo completed successfully!")
        print()
        print("Key capabilities demonstrated:")
        print("✅ Trailer parsing and manipulation")
        print("✅ Subject normalization and similarity matching")
        print("✅ File signature computation")
        print("✅ Mapping comment management")
        print("✅ Trailer-aware duplicate detection")

    except Exception as e:
        print(f"Demo failed with error: {e}")
        print("This may indicate missing dependencies or import issues.")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
