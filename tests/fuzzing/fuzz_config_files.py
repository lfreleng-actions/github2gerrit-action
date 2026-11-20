# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""
Fuzz harness: configuration files and settings read from a repository.

A ``.gitreview`` file comes from the repository being transferred, a
``.netrc`` from the runner's home or working directory, and
``COMMIT_RULES_JSON`` from workflow configuration.  Each check below
quotes the contract it holds the code to.

``gitreview.parse_gitreview``
    Returns "a GitReviewInfo if at least host is present and non-empty,
    otherwise None"; the project is "without the .git suffix" and
    ``port_given`` records "whether the source named the port, or port
    is the parser's default".  A generated file, with or without its
    ``[gerrit]`` header, must read back as written.

``netrc.NetrcParser``
    Supports "the standard netrc format with machine, login, password,
    and default tokens" and "quoted strings with escape sequences".  Only
    :class:`NetrcParseError` may escape it.  A generated file, its values
    bare or quoted, with comment lines and inline comments, must read
    back as written, and a machine it does not name must get the
    ``default`` entry ("falls back to default entry if no specific
    match").  Quoted values here hold printable ASCII only: a value of
    one newline would read as the tokenizer's line marker.

``commit_rules.parse_commit_rules_json``
    "Returns None when json_str is empty or unparsable (a warning is
    logged but no exception is raised)."  A generated document must read
    back with keys and values stripped and an unknown location or
    separator replaced by its default.

Run by hand, from the repository root::

    python tests/fuzzing/fuzz_config_files.py -max_total_time=60
"""

from __future__ import annotations

import contextlib
import json
import string
import sys
from pathlib import Path


_TESTS = Path(__file__).resolve().parents[1]
for _path in (_TESTS.parent / "src", _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fuzzing._runner import ByteReader  # noqa: E402
from fuzzing._runner import decode  # noqa: E402
from fuzzing._runner import require  # noqa: E402
from fuzzing._runner import run  # noqa: E402
from github2gerrit.commit_rules import CommitRule  # noqa: E402
from github2gerrit.commit_rules import CommitRulesConfig  # noqa: E402
from github2gerrit.commit_rules import parse_commit_rules_json  # noqa: E402
from github2gerrit.gitreview import DEFAULT_GERRIT_PORT  # noqa: E402
from github2gerrit.gitreview import GitReviewInfo  # noqa: E402
from github2gerrit.gitreview import parse_gitreview  # noqa: E402
from github2gerrit.netrc import NetrcCredentials  # noqa: E402
from github2gerrit.netrc import NetrcParseError  # noqa: E402
from github2gerrit.netrc import NetrcParser  # noqa: E402


_HOST = string.ascii_lowercase + string.digits + ".-"
_PROJECT = string.ascii_letters + string.digits + "/_.-"
_BARE = string.ascii_letters + string.digits + "._-@!$%^&*()+=:/"
_PRINTABLE = "".join(chr(code) for code in range(0x20, 0x7F))
_KEY = string.ascii_letters + string.digits + "-_"


# -- .gitreview ---------------------------------------------------------------


def verify_gitreview(text: str, info: GitReviewInfo | None) -> None:
    """Any text gives no info or info with a host and a sound port."""
    if info is None:
        return
    require(bool(info.host), f"empty host read from {text!r}")
    require(info.host == info.host.strip(), f"host {info.host!r} unstripped")
    require(
        info.port_given or info.port == DEFAULT_GERRIT_PORT,
        f"port {info.port} read without port_given",
    )


def gitreview_from(reader: ByteReader) -> tuple[str, GitReviewInfo]:
    """Build a ``.gitreview`` file and the info it holds."""
    host = reader.word(_HOST, 1, 30)
    project = reader.word(_PROJECT, 0, 30) + (".git" * (reader.byte() % 3))
    port: int | None = reader.below(100_000) if reader.flag() else None
    equals = ("=", " = ", "= ", "\t=\t")[reader.byte() % 4]
    lines = [f"host{equals}{host}", f"project{equals}{project}"]
    if port is not None:
        lines.append(f"port{equals}{port}")
    lines.sort(key=lambda _line: reader.byte())
    if reader.flag():
        lines.insert(0, "# managed by hand")
    if reader.flag():
        lines.insert(0, "[gerrit]")
    expected = GitReviewInfo(
        host=host,
        port=DEFAULT_GERRIT_PORT if port is None else port,
        project=project.strip().removesuffix(".git"),
        port_given=port is not None,
    )
    return "\n".join(lines) + "\n", expected


def verify_gitreview_read(
    expected: GitReviewInfo, info: GitReviewInfo | None
) -> None:
    """A generated file reads back as written."""
    got = (
        None
        if info is None
        else (info.host, info.port, info.project, info.port_given)
    )
    want = (expected.host, expected.port, expected.project, expected.port_given)
    require(got == want, f"read {got!r}, wrote {want!r}")


# -- .netrc -----------------------------------------------------------------


def _netrc_value(reader: ByteReader) -> tuple[str, str]:
    """Return a value and its netrc spelling, bare or quoted."""
    if reader.flag():
        value = "x-" + reader.word(_BARE, 0, 16)
        return value, value
    value = reader.word(_PRINTABLE, 1, 16)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return value, f'"{escaped}"'


def netrc_from(
    reader: ByteReader,
) -> tuple[str, dict[str, NetrcCredentials], NetrcCredentials | None]:
    """Build a ``.netrc`` file, the entries it holds and its default."""
    entries: dict[str, NetrcCredentials] = {}
    lines: list[str] = []
    default: NetrcCredentials | None = None
    default_line = ""
    for _ in range(1 + reader.byte() % 4):
        login, login_text = _netrc_value(reader)
        password, password_text = _netrc_value(reader)
        if reader.byte() % 5 == 0:
            if default is not None:
                continue
            default = NetrcCredentials("default", login, password)
            head = "default"
        else:
            machine = "m-" + reader.word(_HOST, 1, 20)
            if machine in entries:
                continue
            entries[machine] = NetrcCredentials(machine, login, password)
            head = f"machine {machine}"
        sep = (" ", "\n", "\t", "\n  ")[reader.byte() % 4]
        line = f"{head}{sep}login {login_text}{sep}password {password_text}"
        if reader.flag():
            # A comment may name keywords; none of them may take effect.
            comment = reader.word(_PRINTABLE, 0, 16)
            if reader.flag():
                comment = f"login x-{comment} password x-{comment}"
            line += " # " + comment
        if reader.flag():
            lines.append("# " + reader.word(_PRINTABLE, 0, 16))
        if head == "default":
            # netrc(5) wants the default entry last.
            default_line = line
        else:
            lines.append(line)
    if default_line:
        lines.append(default_line)
    return "\n".join(lines) + "\n", entries, default


def verify_netrc_read(
    entries: dict[str, NetrcCredentials],
    default: NetrcCredentials | None,
    parser: NetrcParser,
) -> None:
    """A generated file reads back as written."""
    require(
        sorted(parser.machines) == sorted(entries),
        f"machines {parser.machines!r}, wrote {sorted(entries)!r}",
    )
    for machine, creds in entries.items():
        got = parser.get_credentials(machine.upper())
        require(got == creds, f"{machine}: read {got!r}")
    unknown = parser.get_credentials("not-a-machine.invalid")
    require(unknown == default, f"default read as {unknown!r}")


# -- COMMIT_RULES_JSON --------------------------------------------------------

_LOCATIONS = (None, "trailer", "body", " BODY ", "Trailer", "elsewhere")
_SEPARATORS = (None, "blank_line", "none", "NONE ", "blank-line")


def _rule_from(reader: ByteReader) -> tuple[dict[str, str], CommitRule]:
    key = reader.word(_KEY, 1, 12)
    pad = (" " * (reader.byte() % 3), " " * (reader.byte() % 3))
    value = reader.text(24)
    location = _LOCATIONS[reader.byte() % len(_LOCATIONS)]
    separator = _SEPARATORS[reader.byte() % len(_SEPARATORS)]
    entry = {"key": f"{pad[0]}{key}{pad[1]}", "value": value}
    if location is not None:
        entry["location"] = location
    if separator is not None:
        entry["separator"] = separator
    expected_location = (location or "trailer").strip().lower()
    expected_separator = (separator or "blank_line").strip().lower()
    rule = CommitRule(
        key=key,
        value=value.strip(),
        location=(
            expected_location
            if expected_location in ("trailer", "body")
            else "trailer"
        ),
        separator=(
            expected_separator
            if expected_separator in ("blank_line", "none")
            else "blank_line"
        ),
    )
    return entry, rule


def commit_rules_from(reader: ByteReader) -> tuple[str, CommitRulesConfig]:
    """Build a ``COMMIT_RULES_JSON`` document and the config it holds."""
    document: dict[str, object] = {}
    expected = CommitRulesConfig()
    defaults: list[dict[str, str]] = []
    for _ in range(reader.byte() % 3):
        entry, rule = _rule_from(reader)
        defaults.append(entry)
        expected.defaults.append(rule)
    if defaults:
        document["defaults"] = defaults
    for section, target in (
        ("projects", expected.projects),
        ("actors", expected.actors),
    ):
        names: dict[str, list[dict[str, str]]] = {}
        for _ in range(reader.byte() % 3):
            name = reader.word(_KEY + "/", 1, 12)
            if name in names:
                continue
            names[name] = []
            target[name] = []
            for _ in range(1 + reader.byte() % 2):
                entry, rule = _rule_from(reader)
                names[name].append(entry)
                target[name].append(rule)
        if names or reader.flag():
            document[section] = names
    return json.dumps(document, ensure_ascii=reader.flag()), expected


def verify_commit_rules_read(
    expected: CommitRulesConfig, config: CommitRulesConfig | None
) -> None:
    """A generated document reads back as written."""
    require(config == expected, f"read {config!r}, wrote {expected!r}")


# -- entry points -------------------------------------------------------------


def check(data: bytes) -> None:
    """The fuzzed function: run every check on one input."""
    text = decode(data)

    verify_gitreview(text, parse_gitreview(text))
    with contextlib.suppress(NetrcParseError):
        NetrcParser(text)
    parse_commit_rules_json(text)

    reader = ByteReader(data)
    gitreview_text, expected_info = gitreview_from(reader)
    verify_gitreview_read(expected_info, parse_gitreview(gitreview_text))
    netrc_text, entries, default = netrc_from(reader)
    verify_netrc_read(entries, default, NetrcParser(netrc_text))
    rules_text, expected_rules = commit_rules_from(reader)
    verify_commit_rules_read(
        expected_rules, parse_commit_rules_json(rules_text)
    )


SEEDS: tuple[bytes, ...] = (
    *(
        seed.encode()
        for seed in (
            "",
            "[gerrit]\nhost=gerrit.example.org\nport=29418\n"
            "project=releng/builder.git\ndefaultbranch=master\n",
            "  host = h\n\tport=\nproject=p.git.git\n[gerrit\n",
            "[DEFAULT]\nhost=d\n[gerrit]\nport=1\n",
            "host=h\nport=" + "1" * 4400,
            'machine gerrit.example.org login "a\\\\" password "p\\"#q" # x\n'
            "default login anon password guest\n",
            "machine m login l password\n",
            "macdef init\ncd /\n\nmachine m login l password p\n",
            '{"defaults": [{"key": " Issue-ID ", "value": " ABC-1 "}], '
            '"projects": {"releng/x": [{"key": "Type", "value": "fix", '
            '"location": " BODY ", "separator": "none"}]}, "actors": []}',
            "1" * 5000,
            "[" * 20_000,
            "\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r",
            "\xff" * 64,
        )
    ),
    # Generates 'default login "*/W\\" password "M#Ee/      "': an
    # escaped backslash before a closing quote once made the "#" inside
    # the next quoted value start a comment.
    b"OM\x80\xa1\n\x1c\xe3K\x19\xcc\x0f\x8c\xf5<0O\xaf\x8d\x843\x18\xaf/;"
    b"\xb1\xd8E\xbc\x00\xe8\xddI\x87\x1f\xf1V \x8fJlL\x00J\xe8\xc3]\xcc"
    b"\xbaV0\xfc\x13\n\xcd\x96<\\\x8a-\x03%\xa4\xcd",
)


def main() -> None:
    """Fuzz :func:`check` under Atheris."""
    import atheris  # type: ignore[import-not-found, import-untyped, unused-ignore]

    run(atheris, check, SEEDS)


if __name__ == "__main__":
    main()
