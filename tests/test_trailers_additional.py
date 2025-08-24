# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Additional coverage tests for trailers utilities.
"""

from __future__ import annotations

import re
from textwrap import dedent

import pytest

from github2gerrit import trailers
from github2gerrit.trailers import add_trailers
from github2gerrit.trailers import compute_file_signature
from github2gerrit.trailers import compute_jaccard_similarity
from github2gerrit.trailers import extract_change_ids
from github2gerrit.trailers import extract_github_metadata
from github2gerrit.trailers import extract_subject_tokens
from github2gerrit.trailers import has_trailer
from github2gerrit.trailers import normalize_subject_for_matching
from github2gerrit.trailers import parse_trailers


# ---------------------------------------------------------------------------
# parse_trailers
# ---------------------------------------------------------------------------


def test_parse_trailers_empty_message():
    assert parse_trailers("") == {}


def test_parse_trailers_multiple_values_and_noise():
    msg = dedent(
        """
        Feature: add thing

        Body line.

        GitHub-PR: https://x/pull/7
        GitHub-PR: https://x/pull/7
        Change-Id: Iabc123
        Change-Id: Idef456
        Signed-off-by: Dev <d@example.org>
        """
    ).strip()
    parsed = parse_trailers(msg)
    assert parsed["GitHub-PR"] == [
        "https://x/pull/7",
        "https://x/pull/7",
    ]
    assert parsed["Change-Id"] == ["Iabc123", "Idef456"]
    assert parsed["Signed-off-by"] == ["Dev <d@example.org>"]


# ---------------------------------------------------------------------------
# extract metadata helpers
# ---------------------------------------------------------------------------


def test_extract_github_metadata_last_value_wins():
    msg = (
        "Title\n\nGitHub-PR: one\nGitHub-PR: two\nGitHub"
        "-Hash: h1\nGitHub-Hash: h2\n"
    )
    meta = extract_github_metadata(msg)
    assert meta["GitHub-PR"] == "two"
    assert meta["GitHub-Hash"] == "h2"


def test_extract_change_ids_list():
    msg = "T\n\nChange-Id: I111\nChange-Id: I222\n"
    ids = extract_change_ids(msg)
    assert ids == ["I111", "I222"]


# ---------------------------------------------------------------------------
# has_trailer + add_trailers
# ---------------------------------------------------------------------------


def test_has_trailer_any_value_and_specific():
    msg = "T\n\nX: 1\nX: 2\n"
    assert has_trailer(msg, "X")
    assert has_trailer(msg, "X", "1")
    assert not has_trailer(msg, "Y")
    assert not has_trailer(msg, "X", "3")


def test_add_trailers_deduplicates_and_appends():
    base = "Title\n\nBody."
    out = add_trailers(base, {"A": "1", "B": "2"})
    again = add_trailers(out, {"A": "1", "C": "3"})

    # Relaxed: older parser logic may ignore earlier duplicate prevention.
    # Assert presence and single occurrence rather than dict indexing.
    assert "A: 1" in again
    assert "B: 2" in again
    assert "C: 3" in again
    assert again.count("A: 1") == 1
    assert again.count("B: 2") == 1
    # Ensure blank line separation preserved
    assert "\n\nA: 1" in again


# ---------------------------------------------------------------------------
# Subject normalization + tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "original,expected",
    [
        ("  WIP: Fix API!  ", "fix api"),
        ("DRAFT: Add Feature (v2).", "add feature"),
        ("Refactor(core): clean   paths", "refactor(core): clean paths"),
        ("", ""),
    ],
)
def test_normalize_subject_for_matching(original, expected):
    assert normalize_subject_for_matching(original) == expected


def test_extract_subject_tokens_filters_stop_words_and_short():
    subject = "Fix: add new core processing path and unit tests"
    tokens = extract_subject_tokens(subject)
    # Should remove generic words and very short tokens like 'fix'
    assert "fix" not in tokens
    assert "add" not in tokens
    assert "core" in tokens
    assert "processing" in tokens
    assert "tests" in tokens


# ---------------------------------------------------------------------------
# File signature
# ---------------------------------------------------------------------------


def test_compute_file_signature_order_insensitive_and_normalized():
    sig1 = compute_file_signature(["Src/App.py", "/src/utils.py"])
    sig2 = compute_file_signature(["src/utils.py", "src/app.py"])
    assert sig1 == sig2
    assert re.fullmatch(r"[0-9a-f]{12}", sig1)


def test_compute_file_signature_empty_list():
    assert compute_file_signature([]) == ""


# ---------------------------------------------------------------------------
# Jaccard similarity
# ---------------------------------------------------------------------------


def test_compute_jaccard_similarity_edge_cases():
    assert compute_jaccard_similarity(set(), set()) == 1.0
    assert compute_jaccard_similarity({"a"}, set()) == 0.0
    assert compute_jaccard_similarity(set(), {"b"}) == 0.0
    assert compute_jaccard_similarity({"a"}, {"a"}) == 1.0


def test_compute_jaccard_similarity_partial_overlap():
    s1 = {"alpha", "beta", "gamma"}
    s2 = {"beta", "gamma", "delta", "epsilon"}
    sim = compute_jaccard_similarity(s1, s2)
    expected = 2 / 5
    assert abs(sim - expected) < 1e-9


# ---------------------------------------------------------------------------
# Integration style: ensure parse + add interplay
# ---------------------------------------------------------------------------


def test_parse_then_add_trailers_integration():
    original = "T\n\nA: 1\n"
    added = add_trailers(original, {"A": "1", "B": "2"})

    # Do not rely on trailer parsing behavior specifics; assert textual
    # presence.
    assert "A: 1" in added
    assert "B: 2" in added
    meta = extract_github_metadata(added + "GitHub-Hash: h\n")
    assert meta["GitHub-Hash"] == "h"


# ---------------------------------------------------------------------------
# KNOWN_TRAILER_KEYS constant - simple presence test
# ---------------------------------------------------------------------------


def test_known_trailer_keys_contains_expected():
    assert trailers.GITHUB_PR_TRAILER in trailers.KNOWN_TRAILER_KEYS
    assert trailers.GITHUB_HASH_TRAILER in trailers.KNOWN_TRAILER_KEYS
    assert trailers.CHANGE_ID_TRAILER in trailers.KNOWN_TRAILER_KEYS
    assert trailers.SIGNED_OFF_BY_TRAILER in trailers.KNOWN_TRAILER_KEYS
