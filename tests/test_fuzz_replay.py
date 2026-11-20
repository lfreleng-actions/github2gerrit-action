# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Replay the fuzz harness seeds and prove each oracle can fail.

The harnesses under ``tests/fuzzing`` run under Atheris by hand.  This
module runs their check functions on every seed in the normal suite, so a
change that breaks a contract on a known input fails here, and feeds each
oracle a wrong result to show it rejects one.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fuzzing import fuzz_config_files
from fuzzing import fuzz_gerrit_text
from fuzzing import fuzz_reconciliation_text
from fuzzing._runner import ByteReader
from fuzzing._runner import ContractViolation

from github2gerrit.commit_rules import CommitRulesConfig
from github2gerrit.gitreview import GitReviewInfo
from github2gerrit.mapping_comment import ChangeIdMapping
from github2gerrit.netrc import NetrcCredentials
from github2gerrit.netrc import NetrcParser
from github2gerrit.pr_commands import CommandMatch
from github2gerrit.pr_commands import CommandParseResult


HARNESSES = (fuzz_reconciliation_text, fuzz_gerrit_text, fuzz_config_files)

SEED_CASES = [
    pytest.param(harness.check, seed, id=f"{harness.__name__}-{index}")
    for harness in HARNESSES
    for index, seed in enumerate(harness.SEEDS)
]


@pytest.mark.parametrize(("check", "seed"), SEED_CASES)
def test_seed_meets_contract(
    check: Callable[[bytes], None], seed: bytes
) -> None:
    check(seed)


_MAPPING = ChangeIdMapping(
    pr_url="https://github.com/o/r/pull/1",
    mode="squash",
    topic="t",
    change_ids=["I" + "a" * 40],
)
_CHECK = CommandMatch("check", "check")

WRONG_RESULTS: list[tuple[str, Callable[[], None]]] = [
    (
        "parse_trailers misses a trailer",
        lambda: fuzz_reconciliation_text.verify_parse_trailers(
            "S\n\nKey: v", {}
        ),
    ),
    (
        "parse_trailers invents a value",
        lambda: fuzz_reconciliation_text.verify_parse_trailers(
            "S", {"S": ["x"]}
        ),
    ),
    (
        "extract_change_ids misses a Change-Id",
        lambda: fuzz_reconciliation_text.verify_trailer_lookups(
            "S\n\nChange-Id: I1", [], {}, lambda key, value: True
        ),
    ),
    (
        "extract_github_metadata keeps the first value",
        lambda: fuzz_reconciliation_text.verify_trailer_lookups(
            "S\n\nGitHub-PR: 1\nGitHub-PR: 2",
            [],
            {"GitHub-PR": "1"},
            lambda key, value: True,
        ),
    ),
    (
        "has_trailer answers yes to everything",
        lambda: fuzz_reconciliation_text.verify_trailer_lookups(
            "S\n\nA: b", [], {}, lambda key, value: True
        ),
    ),
    (
        "trailer boundary disagrees with gitutils",
        lambda: fuzz_reconciliation_text.verify_footer_agreement(
            "S\n\nA: b", {}, 2
        ),
    ),
    (
        "trailer boundary follows no blank line",
        lambda: fuzz_reconciliation_text.verify_footer_agreement(
            "fix: x", {"fix": ["x"]}, 0
        ),
    ),
    (
        "trailer boundary admits a non-trailer line",
        lambda: fuzz_reconciliation_text.verify_footer_agreement(
            "S\n\nA: b\nplain", {"A": ["b"]}, 2
        ),
    ),
    (
        "trailer removal folds sharp s to ss",
        lambda: fuzz_gerrit_text.verify_trailers_removed("I\u00dfue-ID: X", ""),
    ),
    (
        "mapping read back wrong",
        lambda: fuzz_reconciliation_text.verify_mapping_read(_MAPPING, None),
    ),
    (
        "consistency check accepts a wrong URL",
        lambda: fuzz_reconciliation_text.verify_consistency(
            _MAPPING, "other", (True, True, False)
        ),
    ),
    (
        "duplicate command matches",
        lambda: fuzz_reconciliation_text.verify_command_matches(
            "check check", CommandParseResult([_CHECK, _CHECK])
        ),
    ),
    (
        "command matched off a word boundary",
        lambda: fuzz_reconciliation_text.verify_command_matches(
            "checkout", CommandParseResult([CommandMatch("check", "checkout")])
        ),
    ),
    (
        "command name changes with case",
        lambda: fuzz_reconciliation_text.verify_case_insensitive(
            CommandParseResult([_CHECK]), CommandParseResult()
        ),
    ),
    (
        "older comment wins",
        lambda: fuzz_reconciliation_text.verify_latest_wins(
            CommandParseResult([_CHECK]),
            CommandParseResult([_CHECK]),
            CommandParseResult([_CHECK]),
        ),
    ),
    (
        "contains_directive misses a match",
        lambda: fuzz_reconciliation_text.verify_directive(
            False, CommandParseResult([_CHECK])
        ),
    ),
    (
        "find_open_command misses a check",
        lambda: fuzz_reconciliation_text.verify_open_command(
            CommandParseResult([_CHECK]), None
        ),
    ),
    (
        "planted directive dropped",
        lambda: fuzz_reconciliation_text.verify_planted_commands(
            [("check", "check")], CommandParseResult(), False, None
        ),
    ),
    (
        "planted decoy obeyed",
        lambda: fuzz_reconciliation_text.verify_planted_commands(
            [(None, "checkout")],
            CommandParseResult([CommandMatch("check", "checkout")]),
            True,
            CommandMatch("check", "checkout"),
        ),
    ),
    (
        "find_open_command returns an older check",
        lambda: fuzz_reconciliation_text.verify_planted_commands(
            [("check", "check"), ("check", "retry")],
            CommandParseResult([CommandMatch("check", "retry")]),
            True,
            _CHECK,
        ),
    ),
    (
        "HTML tag survives sanitising",
        lambda: fuzz_gerrit_text.verify_sanitized("", "<b>x</b>"),
    ),
    (
        "sanitised line ends in whitespace",
        lambda: fuzz_gerrit_text.verify_sanitized("", "x \ny"),
    ),
    (
        "body changed with no filter needed",
        lambda: fuzz_gerrit_text.verify_unfiltered("a", False, "b"),
    ),
    (
        "normalised subject keeps a tab",
        lambda: fuzz_gerrit_text.verify_normalized("a\tb", ""),
    ),
    (
        "normalised body keeps a URL",
        lambda: fuzz_gerrit_text.verify_normalized("", "see http://x"),
    ),
    (
        "normalised body keeps an issue number",
        lambda: fuzz_gerrit_text.verify_normalized("", "#12"),
    ),
    (
        "trailer survives removal",
        lambda: fuzz_gerrit_text.verify_trailers_removed(
            "Change-Id: I1", "Change-Id: I1"
        ),
    ),
    (
        "trailer removal invents a line",
        lambda: fuzz_gerrit_text.verify_trailers_removed("a", "b"),
    ),
    (
        "trailer removal deletes the body",
        lambda: fuzz_gerrit_text.verify_trailers_removed("Subject\n\nBody", ""),
    ),
    (
        "trailer removal reorders lines",
        lambda: fuzz_gerrit_text.verify_trailers_removed("a\nb", "b\na"),
    ),
    (
        "trailer removal drops a duplicate line",
        lambda: fuzz_gerrit_text.verify_trailers_removed("a\na", "a"),
    ),
    (
        "gitreview port read without port_given",
        lambda: fuzz_config_files.verify_gitreview(
            "", GitReviewInfo(host="h", port=1, port_given=False)
        ),
    ),
    (
        "gitreview read back wrong",
        lambda: fuzz_config_files.verify_gitreview_read(
            GitReviewInfo(host="h"), GitReviewInfo(host="h", project="p")
        ),
    ),
    (
        "gitreview read as None",
        lambda: fuzz_config_files.verify_gitreview_read(
            GitReviewInfo(host="h"), None
        ),
    ),
    (
        "netrc read back wrong",
        lambda: fuzz_config_files.verify_netrc_read(
            {"m": NetrcCredentials("m", "l", "p")},
            None,
            NetrcParser("machine m login l password q\n"),
        ),
    ),
    (
        "netrc default not used",
        lambda: fuzz_config_files.verify_netrc_read(
            {},
            NetrcCredentials("default", "l", "p"),
            NetrcParser(""),
        ),
    ),
    (
        "commit rules read back wrong",
        lambda: fuzz_config_files.verify_commit_rules_read(
            CommitRulesConfig(), None
        ),
    ),
]


@pytest.mark.parametrize(
    "wrong", [pytest.param(case, id=name) for name, case in WRONG_RESULTS]
)
def test_oracle_rejects_wrong_result(wrong: Callable[[], None]) -> None:
    with pytest.raises(ContractViolation):
        wrong()


def test_byte_reader_reads_past_end_as_zero() -> None:
    reader = ByteReader(b"\x05")
    assert reader.byte() == 5
    assert reader.byte() == 0
    assert reader.word("ab", 2, 2) == "aa"
    assert reader.rest() == ""
