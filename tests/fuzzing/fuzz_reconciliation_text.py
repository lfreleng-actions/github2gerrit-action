# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""
Fuzz harness: text that steers reconciliation.

Commit messages, pull request comments and the Change-Id mapping comment
are written by whoever opens or comments on a pull request, and what is
read from them decides which Gerrit change a pull request maps to.  Each
check below quotes the contract it holds the code to.

``trailers.parse_trailers``
    "Trailers are key-value pairs at the end of a commit message, after
    the last blank line, in the format "Key: value"."  The expected
    trailers are derived from the raw message.  A message with no blank
    line has no "after the last blank line"; the docstring says nothing
    about it, so only containment is checked there.
    ``extract_change_ids``, ``extract_github_metadata`` and
    ``has_trailer`` must report what the same expected trailers hold:
    "Change-Id trailer values", the "GitHub-* trailer values (single
    values, not lists)", taking "the last value if multiple exist", and
    whether the trailer "exists with the specified value (or any value)".

``trailers.find_trailer_boundary`` and ``gitutils._parse_trailers``
    The block is "the run of "Key: value" lines at the end of the
    message, and it must be preceded by a blank line so that a
    conventional commit subject such as "fix: something" is not read
    as a trailer.  Returns ``len(lines)`` when the message has no
    trailer block."  The message builder and ``gitutils._parse_trailers``
    both use it, so the expected boundary is derived from the raw lines
    and ``_parse_trailers`` must read exactly the lines past it.

    ``trailers.parse_trailers`` is not held to agree with them.  Its
    docstring promises less (any "Key: value" line after the last blank
    line), and they disagree on simple messages: for ``fix: something``
    it returns a trailer named ``fix`` and ``gitutils._parse_trailers``
    returns none.

``mapping_comment``
    ``serialize_mapping_comment`` then ``parse_mapping_comments`` must
    give back the mapping, whatever text without an end marker comes
    before the block and whatever text comes after it.
    ``update_mapping_comment_body`` "replac[es] the existing one" with
    the new mapping, so its result must parse to that mapping.
    ``validate_mapping_consistency`` is "True if mapping is consistent
    with expectations".

``pr_commands``
    Matching is "case-insensitive", "duplicate commands ... produce a
    single CommandMatch per command name", "results are returned in
    comment order (oldest -> newest), with the latest occurrence winning",
    and a phrase "must end on a word boundary".  ``contains_directive``
    "applies the same grammar as parse_commands" and is "True when the
    mention is followed by non-whitespace text".  ``find_open_command``
    matches "a command in one comment body" and raises ``ValueError``
    for a command "that requires a trusted author".

    The three readers share one mention matcher, so their agreement
    alone proves nothing about it.  ``comment_from`` therefore builds a
    comment from the fuzz bytes, planting directives and word-stem
    decoys among lines that hold no mention, and each reader must find
    exactly what was planted.

Run by hand, from the repository root::

    python tests/fuzzing/fuzz_reconciliation_text.py -max_total_time=60
"""

from __future__ import annotations

import string
import sys
from collections.abc import Callable
from pathlib import Path


_TESTS = Path(__file__).resolve().parents[1]
for _path in (_TESTS.parent / "src", _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fuzzing._runner import ByteReader  # noqa: E402
from fuzzing._runner import decode  # noqa: E402
from fuzzing._runner import require  # noqa: E402
from fuzzing._runner import run  # noqa: E402
from github2gerrit import mapping_comment  # noqa: E402
from github2gerrit.gitutils import _parse_trailers  # noqa: E402
from github2gerrit.mapping_comment import ChangeIdMapping  # noqa: E402
from github2gerrit.pr_commands import CMD_CHECK  # noqa: E402
from github2gerrit.pr_commands import CMD_CREATE_MISSING  # noqa: E402
from github2gerrit.pr_commands import COMMAND_REGISTRY  # noqa: E402
from github2gerrit.pr_commands import CommandMatch  # noqa: E402
from github2gerrit.pr_commands import CommandParseResult  # noqa: E402
from github2gerrit.pr_commands import contains_directive  # noqa: E402
from github2gerrit.pr_commands import find_open_command  # noqa: E402
from github2gerrit.pr_commands import parse_commands  # noqa: E402
from github2gerrit.trailers import extract_change_ids  # noqa: E402
from github2gerrit.trailers import extract_github_metadata  # noqa: E402
from github2gerrit.trailers import find_trailer_boundary  # noqa: E402
from github2gerrit.trailers import has_trailer  # noqa: E402
from github2gerrit.trailers import parse_trailers  # noqa: E402


Trailers = dict[str, list[str]]

_CHANGE_ID = "I" + "0123456789abcdef" * 2 + "01234567"
_START = "<!-- github2gerrit:change-id-map v1 -->"
_END = "<!-- end github2gerrit:change-id-map -->"
# Field characters for generated mappings: URL-safe, no whitespace and
# no angle brackets, so no field can hold a line break or a marker.
_FIELD = string.ascii_letters + string.digits + "-._~:/?#[]@!$&'()*+,;=%"
_ASCII_SWAP = str.maketrans(
    string.ascii_lowercase + string.ascii_uppercase,
    string.ascii_uppercase + string.ascii_lowercase,
)
_METADATA_KEYS = ("GitHub-PR", "GitHub-Hash")
_MENTION = "@github2gerrit"
# Comment text with no "@", so it can hold no mention, and no line break.
_FILLER = (
    string.ascii_letters
    + string.digits
    + string.punctuation.replace("@", "")
    + " \t"
)
# Characters that end a word; the underscore continues one.
_TAIL_START = string.punctuation.replace("_", "") + " \t"
_WORD_CHARS = string.ascii_letters + string.digits + "_"


# -- trailers ---------------------------------------------------------------


def expected_trailers(message: str) -> Trailers | None:
    """Return the trailers *message* holds, or None where undocumented.

    Lines are split on ``\\n``; a blank line is one holding only
    whitespace.  Every ``Key: value`` line after the last blank line,
    with a non-empty key, is a trailer.
    """
    body = message.strip()
    if not body:
        return {}
    lines = body.split("\n")
    blank = [index for index, line in enumerate(lines) if not line.strip()]
    if not blank:
        return None
    found: Trailers = {}
    for line in lines[max(blank) + 1 :]:
        key, colon, value = line.partition(":")
        if colon and key.strip():
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def verify_parse_trailers(message: str, actual: Trailers) -> None:
    """Hold a ``parse_trailers`` result to its docstring."""
    for key, values in actual.items():
        require(bool(key) and key in message, f"key {key!r} not in input")
        for value in values:
            require(value in message, f"value {value!r} not in input")
    expected = expected_trailers(message)
    if expected is not None:
        require(
            actual == expected,
            f"parse_trailers gave {actual!r}, expected {expected!r}",
        )


def verify_trailer_lookups(
    message: str,
    change_ids: list[str],
    metadata: dict[str, str],
    lookup: Callable[[str, str | None], bool],
) -> None:
    """Hold the trailer lookups to the trailers the message holds."""
    expected = expected_trailers(message)
    if expected is None:
        return
    require(
        change_ids == expected.get("Change-Id", []),
        f"extract_change_ids gave {change_ids!r}",
    )
    wanted = {
        key: expected[key][-1] for key in _METADATA_KEYS if key in expected
    }
    require(metadata == wanted, f"extract_github_metadata gave {metadata!r}")
    for key in (*expected, *_METADATA_KEYS, "Change-Id"):
        present = key in expected
        require(lookup(key, None) == present, f"has_trailer({key!r}) wrong")
        for value in expected.get(key, []):
            require(lookup(key, value), f"has_trailer({key!r}, {value!r})")
        absent = "\n".join(expected.get(key, [])) + "\n"
        require(not lookup(key, absent), f"has_trailer({key!r}) matched junk")


def _is_footer_line(line: str) -> bool:
    key, colon, value = line.strip().partition(":")
    return bool(colon and key.strip() and value.strip())


def expected_boundary(lines: list[str]) -> int:
    """Derive the trailer boundary from the raw lines."""
    blank = [index for index, line in enumerate(lines) if not line.strip()]
    if not blank:
        return len(lines)
    start = max(blank) + 1
    if all(_is_footer_line(line) for line in lines[start:]):
        return start
    return len(lines)


def verify_footer_agreement(
    message: str, footer: Trailers, boundary: int
) -> None:
    """Hold the trailer boundary and gitutils to the documented block."""
    lines = message.splitlines()
    wanted = expected_boundary(lines)
    require(
        boundary == wanted,
        f"trailer boundary {boundary} for {message!r}, expected {wanted}",
    )
    expected: Trailers = {}
    for line in lines[boundary:]:
        key, _, value = line.strip().partition(":")
        expected.setdefault(key.strip(), []).append(value.strip())
    require(
        footer == expected,
        f"gitutils read {footer!r}, trailer boundary gives {expected!r}",
    )


def check_trailers(message: str) -> None:
    """Fuzz every trailer reader on *message*."""
    verify_parse_trailers(message, parse_trailers(message))
    verify_trailer_lookups(
        message,
        extract_change_ids(message),
        extract_github_metadata(message),
        lambda key, value: has_trailer(message, key, value),
    )
    verify_footer_agreement(
        message,
        _parse_trailers(message),
        find_trailer_boundary(message.splitlines()),
    )


# -- mapping comments -------------------------------------------------------


def mapping_from(reader: ByteReader) -> ChangeIdMapping:
    """Build a valid mapping whose fields serialise to single lines."""
    change_ids: list[str] = []
    for _ in range(1 + reader.byte() % 4):
        cid = "I" + reader.word("0123456789abcdef", 40, 40)
        if cid not in change_ids:
            change_ids.append(cid)
    return ChangeIdMapping(
        pr_url="https://github.com/" + reader.word(_FIELD, 1, 40),
        mode=("multi-commit", "squash")[reader.byte() & 1],
        topic=reader.word(_FIELD, 1, 40),
        change_ids=change_ids,
        github_hash=reader.word(_FIELD, 0, 40),
        digest=reader.word("0123456789abcdef", 0, 12),
    )


def without_end_marker(text: str) -> str:
    """Remove every end marker, including any the removal forms."""
    while _END in text:
        text = text.replace(_END, "")
    return text


def verify_mapping_read(
    expected: ChangeIdMapping, actual: ChangeIdMapping | None
) -> None:
    """The mapping read back must be the mapping written."""
    require(actual == expected, f"read {actual!r}, wrote {expected!r}")


def verify_consistency(
    mapping: ChangeIdMapping, other: str, verdicts: tuple[bool, bool, bool]
) -> None:
    """``validate_mapping_consistency`` on matching and mismatched input.

    *verdicts* holds its answer for the mapping's own URL and hash, for
    *other* as the URL, and for *other* as the hash.
    """
    require(verdicts[0], "consistent mapping reported inconsistent")
    require(
        verdicts[1] == (other == mapping.pr_url),
        "PR URL mismatch misjudged",
    )
    require(
        verdicts[2] == (other == mapping.github_hash),
        "GitHub-Hash mismatch misjudged",
    )


def check_mapping(data: bytes) -> None:
    """Fuzz the mapping comment round trip, update and raw parse."""
    reader = ByteReader(data)
    mapping = mapping_from(reader)
    prefix = without_end_marker(reader.text(160))
    suffix = reader.text(160)
    original = reader.rest()

    block = mapping_comment.serialize_mapping_comment(
        pr_url=mapping.pr_url,
        mode=mapping.mode,
        topic=mapping.topic,
        change_ids=mapping.change_ids,
        github_hash=mapping.github_hash,
        digest=mapping.digest or None,
    )
    verify_mapping_read(
        mapping,
        mapping_comment.parse_mapping_comments([prefix + block + suffix]),
    )
    updated = mapping_comment.update_mapping_comment_body(original, mapping)
    verify_mapping_read(
        mapping, mapping_comment.parse_mapping_comments([updated])
    )

    verify_consistency(
        mapping,
        suffix,
        (
            mapping_comment.validate_mapping_consistency(
                mapping, mapping.pr_url, mapping.github_hash
            ),
            mapping_comment.validate_mapping_consistency(
                mapping, suffix, mapping.github_hash
            ),
            mapping_comment.validate_mapping_consistency(
                mapping, mapping.pr_url, suffix
            ),
        ),
    )

    parsed = mapping_comment.parse_mapping_comments([decode(data)])
    if parsed is not None:
        require(parsed.mode in ("multi-commit", "squash"), "bad mode read")
        require(bool(parsed.change_ids), "mapping read with no Change-Ids")


# -- PR commands ------------------------------------------------------------


def _phrases(command_name: str) -> list[str]:
    for defn in COMMAND_REGISTRY:
        if defn.name == command_name:
            return [" ".join(p.lower().split()) for p in defn.all_phrases()]
    return []


def _phrase_matches(raw: str, phrase: str) -> bool:
    text = " ".join(raw.lower().split())
    if text == phrase:
        return True
    if not text.startswith(phrase) or len(text) == len(phrase):
        return False
    following = text[len(phrase)]
    return not (following.isalnum() or following == "_")


def verify_command_matches(text: str, result: CommandParseResult) -> None:
    """Each match is one command, found in the text on a word boundary."""
    names = [match.command_name for match in result.matches]
    require(len(names) == len(set(names)), f"duplicate matches {names!r}")
    for match in result.matches:
        require(match.raw_text in text, f"{match.raw_text!r} not in input")
        require(
            any(
                _phrase_matches(match.raw_text, p)
                for p in _phrases(match.command_name)
            ),
            f"{match.raw_text!r} starts with no {match.command_name!r} phrase",
        )
    for raw in result.unrecognised:
        for defn in COMMAND_REGISTRY:
            require(
                not any(_phrase_matches(raw, p) for p in _phrases(defn.name)),
                f"{raw!r} names {defn.name!r} but went unrecognised",
            )


def _names(result: CommandParseResult) -> set[str]:
    return {match.command_name for match in result.matches}


def verify_case_insensitive(
    result: CommandParseResult, swapped: CommandParseResult
) -> None:
    """Swapping ASCII case must not change which commands are found."""
    require(
        _names(result) == _names(swapped),
        f"case swap changed {_names(result)!r} to {_names(swapped)!r}",
    )
    require(
        len(result.unrecognised) == len(swapped.unrecognised),
        "case swap changed the unrecognised directives",
    )


def verify_latest_wins(
    first: CommandParseResult,
    second: CommandParseResult,
    both: CommandParseResult,
) -> None:
    """Two comments parse to the union of each, the newer one winning."""
    require(
        _names(both) == _names(first) | _names(second),
        f"two comments gave {_names(both)!r}",
    )
    winners: dict[str, CommandMatch] = {
        match.command_name: match for match in first.matches
    }
    winners.update(
        {
            match.command_name: CommandMatch(
                match.command_name, match.raw_text, comment_index=1
            )
            for match in second.matches
        }
    )
    for match in both.matches:
        require(
            match == winners.get(match.command_name),
            f"{match!r} is not the latest occurrence",
        )
    indices = [match.comment_index for match in both.matches]
    require(indices == sorted(indices), f"matches out of order: {indices!r}")
    require(
        both.unrecognised == first.unrecognised + second.unrecognised,
        "unrecognised directives out of comment order",
    )


def verify_directive(contains: bool, result: CommandParseResult) -> None:
    """``contains_directive`` sees what ``parse_commands`` sees."""
    expected = bool(result.matches) or any(result.unrecognised)
    require(
        contains == expected,
        f"contains_directive gave {contains}, parse_commands found "
        f"{[m.raw_text for m in result.matches]!r} "
        f"and {result.unrecognised!r}",
    )


def verify_open_command(
    result: CommandParseResult, found: CommandMatch | None
) -> None:
    """``find_open_command`` returns the comment's ``check`` match."""
    expected = next(
        (m for m in result.matches if m.command_name == CMD_CHECK.name), None
    )
    require(found == expected, f"find_open_command gave {found!r}")


def check_commands(text: str) -> None:
    """Fuzz the PR command parser on *text*."""
    result = parse_commands([text])
    verify_command_matches(text, result)
    verify_case_insensitive(
        result, parse_commands([text.translate(_ASCII_SWAP)])
    )
    cut = len(text) // 3
    first, second = text[:cut], text[cut:]
    verify_latest_wins(
        parse_commands([first]),
        parse_commands([second]),
        parse_commands([first, second]),
    )
    verify_directive(contains_directive(text), result)
    verify_open_command(result, find_open_command(text, CMD_CHECK.name))
    try:
        find_open_command(text, CMD_CREATE_MISSING.name)
    except ValueError:
        pass
    else:
        require(False, "find_open_command served a trusted-only command")


# A planted line is (canonical command name, raw text after the mention);
# the name is None for a mention the parser must report as unrecognised.
Planted = list[tuple[str | None, str]]


def _random_case(reader: ByteReader, text: str) -> str:
    return "".join(c.upper() if reader.flag() else c for c in text)


def _mention_line(reader: ByteReader, command: str) -> str:
    """A line holding the mention, then whitespace, then *command*."""
    lead = reader.word(" \t", 0, 2)
    gap = reader.word(" \t", 1, 3)
    return f"{lead}{_random_case(reader, _MENTION)}{gap}{command}"


def _tail(reader: ByteReader) -> str:
    """Nothing, or text that starts on a word boundary."""
    if not reader.flag():
        return ""
    return _TAIL_START[reader.byte() % len(_TAIL_START)] + reader.word(
        _FILLER, 0, 20
    )


def comment_from(reader: ByteReader) -> tuple[str, Planted]:
    """Build a comment and record the directives planted in it.

    Filler lines hold no ``@``, so the planted lines hold the only
    mentions.  A directive is a registered phrase, in any case and with
    any run of blanks between its words, followed by nothing or by text
    that starts on a word boundary.  A decoy is a one-word phrase that
    runs on into a word character, which "merely begins with the same
    stem" and so names no command.
    """
    phrases = [
        (defn.name, phrase)
        for defn in COMMAND_REGISTRY
        for phrase in defn.all_phrases()
    ]
    stems = [phrase for _, phrase in phrases if " " not in phrase]
    lines: list[str] = []
    planted: Planted = []
    for _ in range(reader.byte() % 6):
        kind = reader.byte() % 3
        if kind == 0:
            lines.append(reader.word(_FILLER, 0, 30))
            continue
        if kind == 1:
            name, phrase = phrases[reader.byte() % len(phrases)]
            words = [_random_case(reader, w) for w in phrase.split(" ")]
            command = reader.word(" \t", 1, 2).join(words) + _tail(reader)
        else:
            name = None
            stem = _random_case(reader, stems[reader.byte() % len(stems)])
            command = stem + reader.word(_WORD_CHARS, 1, 3) + _tail(reader)
        lines.append(_mention_line(reader, command))
        planted.append((name, command.strip()))
    return "\n".join(lines), planted


def verify_planted_commands(
    planted: Planted,
    result: CommandParseResult,
    contains: bool,
    found: CommandMatch | None,
) -> None:
    """Each reader finds exactly the directives planted in the comment.

    The latest occurrence of each command wins, the unrecognised
    directives come back in comment order, ``contains_directive`` is
    true when any mention carries a command, and ``find_open_command``
    returns the latest ``check``.
    """
    latest = {name: raw for name, raw in planted if name is not None}
    got = {match.command_name: match.raw_text for match in result.matches}
    require(
        len(got) == len(result.matches) and got == latest,
        f"parse_commands found {got!r}, planted {latest!r}",
    )
    require(
        all(match.comment_index == 0 for match in result.matches),
        "match from a comment that was not given",
    )
    decoys = [raw for name, raw in planted if name is None]
    require(
        result.unrecognised == decoys,
        f"unrecognised {result.unrecognised!r}, planted {decoys!r}",
    )
    require(
        contains == bool(planted),
        f"contains_directive gave {contains} for {len(planted)} directives",
    )
    expected = (
        CommandMatch(CMD_CHECK.name, latest[CMD_CHECK.name])
        if CMD_CHECK.name in latest
        else None
    )
    require(found == expected, f"find_open_command gave {found!r}")


def check_planted_commands(data: bytes) -> None:
    """Fuzz the PR command readers on a comment with known directives."""
    text, planted = comment_from(ByteReader(data))
    verify_planted_commands(
        planted,
        parse_commands([text]),
        contains_directive(text),
        find_open_command(text, CMD_CHECK.name),
    )
    check_commands(text)


# -- entry points -------------------------------------------------------------


def check(data: bytes) -> None:
    """The fuzzed function: run every check on one input."""
    text = decode(data)
    check_trailers(text)
    check_mapping(data)
    check_commands(text)
    check_planted_commands(data)


_MAPPING = mapping_comment.serialize_mapping_comment(
    pr_url="https://github.com/org/repo/pull/7",
    mode="multi-commit",
    topic="GH-org-repo-7",
    change_ids=[_CHANGE_ID, "I" + "f" * 40],
    github_hash="0123456789abcdef",
    digest="0123456789ab",
)

# Inputs for comment_from, which reads them as planting instructions.
_PLANTED_SEEDS = (
    # "@github2gerrit check"
    bytes.fromhex("010103"),
    # Two spellings of check, the latest winning, then a decoy stem.
    bytes.fromhex(
        "050405280900020004032803000005200921204120202009284102212841"
        "0009094105090500200904052041000441010020212041410420090228"
    ),
    # A two-word phrase in mixed case, then filler lines.
    bytes.fromhex("21042000030100280900"),
)

SEEDS: tuple[bytes, ...] = (
    *(
        seed.encode()
        for seed in (
            "",
            "fix: something",
            f"Change-Id: {_CHANGE_ID}",
            "Subject\n\nSee: the notes\nfor details",
            "Fix: Thing\r\n\r\nBody.\r\n\r\nChange-Id: I0\r\nKey:\r\n",
            "Subject\n\nA: b\u2028C: d",
            (
                "Feat: Add thing\n\nBody text.\n\nIssue-ID: CIMAN-33\n"
                f"Change-Id: {_CHANGE_ID}\n"
                "GitHub-PR: https://github.com/org/repo/pull/7\n"
                "GitHub-Hash: 0123456789abcdef\n"
                "GitHub-Hash: fedcba9876543210\n"
                "Signed-off-by: A Person <a@example.org>"
            ),
            _MAPPING,
            f"{_END}\n{_MAPPING}",
            f"{_START}\nChange-Ids:\n  I{'e' * 40}\n{_MAPPING}",
            f"note {_MAPPING} {_END}",
            "@github2gerrit check",
            "@github2gerrit create missing change\n@github2gerrit recheck.",
            "@GitHub2Gerrit Create-Missing please",
            "@github2gerrit checkout this branch",
            "@github2gerrit check_status",
            "@github2gerrit   ",
            "@github2gerrit",
            "x@github2gerrit check\n\t@github2gerrit retry",
            "@github2gerrit\n\n  check",
        )
    ),
    *_PLANTED_SEEDS,
)


def main() -> None:
    """Fuzz :func:`check` under Atheris."""
    import atheris  # type: ignore[import-not-found, import-untyped, unused-ignore]

    run(atheris, check, SEEDS)


if __name__ == "__main__":
    main()
