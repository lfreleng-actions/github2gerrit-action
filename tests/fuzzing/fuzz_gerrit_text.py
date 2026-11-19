# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""
Fuzz harness: pull request text that reaches Gerrit or steers matching.

A pull request's title, body and comments are written by its author.
The body and comments are cleaned before they go into a Gerrit commit
message or review comment, and the title and body are normalised before
the reconciler compares one change with another.  Each check below
quotes the contract it holds the code to.

``sanitize_gerrit_comment``
    "Removes HTML, markdown formatting, emoji codes, and excessive
    whitespace."  No HTML tag may survive, and no line may end in
    whitespace.

``filter_pr_body``
    Returns the "filtered body, or original body if no filtering
    needed": when ``should_filter_pr_body`` says no filtering is needed,
    the body must come back unchanged.  ``filter_dependabot_pr_body``
    must accept any body.

``similarity.normalize_subject`` and ``similarity.normalize_body``
    Both "collapse whitespace"; the body is lowercased and has URLs
    removed and "numeric IDs (e.g., #1234) [normalised] to a
    placeholder".

``similarity.remove_commit_trailers``
    Removes the listed trailers (``Change-Id``, ``Signed-off-by``,
    ``Issue-ID``, ``GitHub-Hash``, ``GitHub-PR``, ``Co-authored-by``) and
    otherwise leaves the message's lines as they were.  The keys match
    case-insensitively, as ``re.IGNORECASE`` does: the Python ``re``
    documentation says an ASCII letter then matches its other case and
    "4 additional non-ASCII letters": U+0130, U+0131, U+017F and
    U+212A, and nothing else.  Full case folding differs (``ß`` folds
    to ``ss``), so the oracle does not use it.

Run by hand, from the repository root::

    python tests/fuzzing/fuzz_gerrit_text.py -max_total_time=60
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


_TESTS = Path(__file__).resolve().parents[1]
for _path in (_TESTS.parent / "src", _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fuzzing._runner import ByteReader  # noqa: E402
from fuzzing._runner import require  # noqa: E402
from fuzzing._runner import run  # noqa: E402
from github2gerrit import pr_content_filter  # noqa: E402
from github2gerrit.similarity import normalize_body  # noqa: E402
from github2gerrit.similarity import normalize_subject  # noqa: E402
from github2gerrit.similarity import remove_commit_trailers  # noqa: E402


_HTML_TAG = re.compile(r"<[A-Za-z/!?][^<>]*>")
_AUTHORS = ("dependabot[bot]", "pre-commit-ci[bot]", "someone", "")
_TRAILER_KEYS = (
    "change-id",
    "signed-off-by",
    "issue-id",
    "github-hash",
    "github-pr",
    "co-authored-by",
)


def verify_sanitized(comment: str, sanitized: str) -> None:
    """A sanitised comment holds no HTML tag and no trailing whitespace."""
    tag = _HTML_TAG.search(sanitized)
    require(tag is None, f"tag {tag and tag.group()!r} survived {comment!r}")
    for line in sanitized.splitlines():
        require(line == line.rstrip(), f"line {line!r} ends in whitespace")


def verify_unfiltered(body: str, needed: bool, filtered: str) -> None:
    """A body that needs no filtering comes back as it was."""
    if not needed:
        require(filtered == body, f"unfiltered body changed to {filtered!r}")


def _require_collapsed(kind: str, text: str) -> None:
    require(text == text.strip(), f"{kind} {text!r} not stripped")
    require("  " not in text, f"{kind} {text!r} has a run of spaces")
    require(
        all(char == " " or not char.isspace() for char in text),
        f"{kind} {text!r} holds whitespace other than a space",
    )


def verify_normalized(subject: str, body: str) -> None:
    """Normalised subjects and bodies collapse whitespace, as documented."""
    _require_collapsed("subject", subject)
    _require_collapsed("body", body)
    url = re.search(r"https?://\S", body)
    require(url is None, f"URL left in normalised body {body!r}")
    issue = re.search(r"#\d", body)
    require(issue is None, f"numeric ID left in normalised body {body!r}")


# The non-ASCII letters re.IGNORECASE matches to an ASCII letter.
_IGNORECASE_EXTRA = {"\u0130": "i", "\u0131": "i", "\u017f": "s", "\u212a": "k"}


def _ignorecase_key(text: str) -> str:
    return "".join(
        _IGNORECASE_EXTRA.get(char, char.lower() if char.isascii() else char)
        for char in text
    )


def _is_listed_trailer(line: str) -> bool:
    head, colon, _ = line.strip().partition(":")
    return bool(colon) and _ignorecase_key(head) in _TRAILER_KEYS


def verify_trailers_removed(message: str, stripped: str) -> None:
    """Every listed trailer line goes; every other line stays, in order."""
    expected = "\n".join(
        line for line in message.splitlines() if not _is_listed_trailer(line)
    ).strip()
    require(
        stripped == expected,
        f"removing trailers from {message!r} gave {stripped!r},"
        f" not {expected!r}",
    )


def check(data: bytes) -> None:
    """The fuzzed function: run every check on one input."""
    reader = ByteReader(data)
    author = _AUTHORS[reader.byte() % len(_AUTHORS)]
    title = reader.text(80)
    text = reader.rest()

    verify_sanitized(text, pr_content_filter.sanitize_gerrit_comment(text))
    verify_unfiltered(
        text,
        pr_content_filter.should_filter_pr_body(title, text, author),
        pr_content_filter.filter_pr_body(title, text, author),
    )
    pr_content_filter.filter_dependabot_pr_body(text)
    verify_normalized(normalize_subject(title), normalize_body(text))
    verify_trailers_removed(text, remove_commit_trailers(text))


_DEPENDABOT_BODY = (
    "Bumps [actions/checkout](https://github.com/actions/checkout) "
    "from 4.1.0 to 4.2.0.\n"
    "<details>\n<summary>Release notes</summary>\n"
    '<p><em>Sourced from <a href="https://github.com/actions/checkout'
    "/releases\">actions/checkout's releases</a>.</em></p>\n"
    "<h2>v4.2.0</h2>\n<h3>:sparkles: New features</h3>\n"
    "<ul>\n<li>Add <code>sparse-checkout</code> by @someone in "
    "#1234</li>\n</ul>\n</details>\n\n"
    "[![Dependabot compatibility score](https://camo.githubusercontent"
    ".com/abc)](https://docs.github.com/en/github)\n\n"
    "Dependabot will resolve any conflicts with this PR as long as you "
    "don't alter it yourself.\n\n"
    "---\n\n<details>\n<summary>Dependabot commands and options"
    "</summary>\n<br />\n\nYou can trigger Dependabot actions by "
    "commenting on this PR:\n</details>\n"
)

SEEDS: tuple[bytes, ...] = tuple(
    seed.encode()
    for seed in (
        "",
        "\x00Bump actions/checkout from 4.1.0 to 4.2.0" + _DEPENDABOT_BODY,
        "\x01\x10[pre-commit.ci] pre-commit autoupdate"
        "<!--pre-commit.ci start-->\nupdates:\n- [github.com/x/y: v1 → v2]"
        "(https://github.com/x/y/compare/v1...v2)\n"
        "<!--pre-commit.ci end-->",
        "\x02\x00<script>alert(1)</script><b>bold</b> :bug: text  \n...",
        "\x02\x00<<a>b>x <scr<script></script>ipt>",
        "\x02\x00[[a](b)](c) :[a](x)b: line\u2028next \t",
        "\x02\x00a\n\n...\n\nb\n\n\n\n…",
        "\x02\x0cFix: Thing v1.2.3Body https://x.y/z #123 2024-01-01 "
        "deadbeefcafe\n\nChange-Id: I0\n  Signed-off-by: A <a@b>\n"
        "co-AUTHORED-by: B\nNot-A-Trailer: kept",
        "\x02\x05\u3000x\u3000Line one\r\nline two\x0bthree\x1cfour",
        "\x02\x00change-\u0131d: I1\nI\u00dfue-ID: X\n"
        "\u0130ssue-ID: Y\nchange-\u0130d: I2\nS\u0131gned-off-by: A\n"
        "\u017figned-off-by: B\nGitHub-Ha\u017fh: C\nIssue-\u0130D: D\n"
        "Change-I\u0307d: kept\nCo-authored-by\u212a: kept\n"
        "GITHUB-PR: E\nKept line",
    )
)


def main() -> None:
    """Fuzz :func:`check` under Atheris."""
    import atheris  # type: ignore[import-not-found, import-untyped, unused-ignore]

    run(atheris, check, SEEDS)


if __name__ == "__main__":
    main()
