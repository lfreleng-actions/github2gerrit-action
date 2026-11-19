# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""Tests for the shared gitreview module.

Covers:
- GitReviewInfo / GerritInfo data model
- parse_gitreview() — pure text parser
- read_local_gitreview() — local file reader
- _build_branch_list() — branch list construction
- _validate_raw_url() — URL validation
- fetch_gitreview_raw() — raw.githubusercontent.com fetcher
- fetch_gitreview_github_api() — PyGithub API fetcher
- fetch_gitreview() — unified multi-strategy fetcher
- read_gitreview_host() — convenience host-only accessor
- make_gitreview_info() — factory with auto-derived base_path
- derive_base_path() — static known-host lookup
- GerritInfo backward-compatible alias
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.gitreview import DEFAULT_GERRIT_PORT
from github2gerrit.gitreview import GerritInfo
from github2gerrit.gitreview import GitReviewInfo
from github2gerrit.gitreview import _build_branch_list
from github2gerrit.gitreview import _validate_raw_url
from github2gerrit.gitreview import derive_base_path
from github2gerrit.gitreview import fetch_gitreview
from github2gerrit.gitreview import fetch_gitreview_github_api
from github2gerrit.gitreview import fetch_gitreview_raw
from github2gerrit.gitreview import make_gitreview_info
from github2gerrit.gitreview import parse_gitreview
from github2gerrit.gitreview import read_gitreview_host
from github2gerrit.gitreview import read_local_gitreview


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

TYPICAL_GITREVIEW = (
    "[gerrit]\n"
    "host=gerrit.linuxfoundation.org\n"
    "port=29418\n"
    "project=releng/lftools.git\n"
)

MINIMAL_GITREVIEW = "[gerrit]\nhost=gerrit.example.org\n"

SPACES_AROUND_EQUALS = (
    "[gerrit]\nhost = git.opendaylight.org\nport = 29418\nproject = aaa.git\n"
)

MIXED_CASE_KEYS = (
    "[gerrit]\nHost=gerrit.example.org\nPort=29419\nProject=apps/widgets.git\n"
)

NO_HOST = "[gerrit]\nport=29418\nproject=foo.git\n"

EMPTY_HOST = "[gerrit]\nhost=\nport=29418\nproject=foo.git\n"

NO_PORT = "[gerrit]\nhost=gerrit.example.org\nproject=foo.git\n"

NO_PROJECT = "[gerrit]\nhost=gerrit.example.org\nport=29418\n"

WHITESPACE_HOST = "[gerrit]\nhost=  gerrit.example.org  \n"

NON_DEFAULT_PORT = (
    "[gerrit]\nhost=gerrit.acme.org\nport=29419\nproject=acme/widgets.git\n"
)

# The form LF automation wrote into ONAP repositories in 2020, as found in
# onap/cps: a leading blank line and every line indented by eight spaces.
# git-review reads it, since configparser ignores leading whitespace.
INDENTED_GITREVIEW = (
    "\n"
    "        [gerrit]\n"
    "        host=gerrit.onap.org\n"
    "        port=29418\n"
    "        project=cps\n"
    "        defaultbranch=master\n"
    "        "
)


# -----------------------------------------------------------------------
# GitReviewInfo data model
# -----------------------------------------------------------------------


class TestGitReviewInfoModel:
    """Tests for the GitReviewInfo frozen dataclass."""

    def test_default_values(self) -> None:
        info = GitReviewInfo(host="h")
        assert info.host == "h"
        assert info.port == DEFAULT_GERRIT_PORT
        assert info.project == ""
        assert info.base_path is None

    def test_is_valid_with_host(self) -> None:
        assert GitReviewInfo(host="h").is_valid is True

    def test_is_valid_empty_host(self) -> None:
        assert GitReviewInfo(host="").is_valid is False

    def test_frozen(self) -> None:
        info = GitReviewInfo(host="h")
        with pytest.raises(AttributeError):
            info.host = "other"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = GitReviewInfo(host="h", port=1, project="p", base_path="bp")
        b = GitReviewInfo(host="h", port=1, project="p", base_path="bp")
        assert a == b

    def test_inequality_on_base_path(self) -> None:
        a = GitReviewInfo(host="h", base_path=None)
        b = GitReviewInfo(host="h", base_path="infra")
        assert a != b


class TestGerritInfoAlias:
    """GerritInfo must be the same class as GitReviewInfo."""

    def test_alias_identity(self) -> None:
        assert GerritInfo is GitReviewInfo

    def test_construct_via_alias(self) -> None:
        info = GerritInfo(host="h", port=29418, project="p")
        assert isinstance(info, GitReviewInfo)
        assert info.host == "h"

    def test_import_from_core_still_works(self) -> None:
        """Verify the re-export from core.py is the same class."""
        from github2gerrit.core import GerritInfo as CoreGerritInfo

        assert CoreGerritInfo is GitReviewInfo


# -----------------------------------------------------------------------
# derive_base_path — static known-host lookup
# -----------------------------------------------------------------------


class TestDeriveBasePath:
    def test_known_host(self) -> None:
        assert derive_base_path("gerrit.linuxfoundation.org") == "infra"

    def test_known_host_case_insensitive(self) -> None:
        assert derive_base_path("Gerrit.LinuxFoundation.Org") == "infra"

    def test_known_host_with_whitespace(self) -> None:
        assert derive_base_path("  gerrit.linuxfoundation.org  ") == "infra"

    def test_unknown_host(self) -> None:
        assert derive_base_path("gerrit.example.org") is None

    def test_empty_host(self) -> None:
        assert derive_base_path("") is None


# -----------------------------------------------------------------------
# parse_gitreview — pure parser
# -----------------------------------------------------------------------


class TestParseGitreview:
    def test_typical(self) -> None:
        info = parse_gitreview(TYPICAL_GITREVIEW)
        assert info is not None
        assert info.host == "gerrit.linuxfoundation.org"
        assert info.port == 29418
        assert info.project == "releng/lftools"
        assert info.base_path == "infra"

    def test_minimal_host_only(self) -> None:
        info = parse_gitreview(MINIMAL_GITREVIEW)
        assert info is not None
        assert info.host == "gerrit.example.org"
        assert info.port == DEFAULT_GERRIT_PORT
        assert info.project == ""
        assert info.base_path is None

    def test_spaces_around_equals(self) -> None:
        info = parse_gitreview(SPACES_AROUND_EQUALS)
        assert info is not None
        assert info.host == "git.opendaylight.org"
        assert info.port == 29418
        assert info.project == "aaa"

    def test_mixed_case_keys(self) -> None:
        info = parse_gitreview(MIXED_CASE_KEYS)
        assert info is not None
        assert info.host == "gerrit.example.org"
        assert info.port == 29419
        assert info.project == "apps/widgets"

    def test_no_host_returns_none(self) -> None:
        assert parse_gitreview(NO_HOST) is None

    def test_empty_host_returns_none(self) -> None:
        assert parse_gitreview(EMPTY_HOST) is None

    def test_no_port_defaults(self) -> None:
        info = parse_gitreview(NO_PORT)
        assert info is not None
        assert info.port == DEFAULT_GERRIT_PORT
        # And says so: a default is not the file's word on the port.
        assert info.port_given is False

    def test_no_project_ok(self) -> None:
        info = parse_gitreview(NO_PROJECT)
        assert info is not None
        assert info.project == ""

    def test_strips_whitespace_from_host(self) -> None:
        info = parse_gitreview(WHITESPACE_HOST)
        assert info is not None
        assert info.host == "gerrit.example.org"

    def test_removes_dot_git_suffix(self) -> None:
        info = parse_gitreview(NON_DEFAULT_PORT)
        assert info is not None
        assert info.project == "acme/widgets"

    def test_non_default_port(self) -> None:
        info = parse_gitreview(NON_DEFAULT_PORT)
        assert info is not None
        assert info.port == 29419
        assert info.port_given is True

    def test_unicode_decimal_port(self) -> None:
        # int() accepts any Unicode decimal digits, as the \d in the port
        # pattern does; Arabic-Indic "٢٩٤١٩" is 29419.
        text = "[gerrit]\nhost=h.example.org\nport=\u0662\u0669\u0664\u0661\u0669\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.port == 29419
        assert info.port_given is True

    def test_empty_string(self) -> None:
        assert parse_gitreview("") is None

    def test_garbage_text(self) -> None:
        assert parse_gitreview("nothing useful here\n") is None

    def test_base_path_derived_for_lf_host(self) -> None:
        text = "[gerrit]\nhost=gerrit.linuxfoundation.org\nproject=foo.git\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.base_path == "infra"

    def test_base_path_none_for_unknown_host(self) -> None:
        text = "[gerrit]\nhost=gerrit.acme.org\nproject=foo.git\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.base_path is None

    def test_project_without_git_suffix(self) -> None:
        text = "[gerrit]\nhost=h\nproject=releng/builder\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.project == "releng/builder"

    def test_tabs_around_equals(self) -> None:
        text = "[gerrit]\nhost\t=\tgerrit.example.org\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org"

    def test_space_indented_keys(self) -> None:
        info = parse_gitreview(INDENTED_GITREVIEW)
        assert info is not None
        assert info.host == "gerrit.onap.org"
        assert info.port == 29418
        assert info.port_given is True
        assert info.project == "cps"

    def test_tab_indented_keys(self) -> None:
        text = (
            "[gerrit]\n\thost=gerrit.example.org\n\tport=29419\n\tproject=a/b\n"
        )
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org"
        assert info.port == 29419
        assert info.project == "a/b"

    def test_key_inside_a_line_is_not_matched(self) -> None:
        text = "[gerrit]\nnote = set host=wrong.example.org\nxhost=wrong\n"
        assert parse_gitreview(text) is None

    def test_headerless_file_is_searched_whole(self) -> None:
        info = parse_gitreview("host=h.example.org\nport=29419\nproject=p\n")
        assert info is not None
        assert (info.host, info.port, info.project) == (
            "h.example.org",
            29419,
            "p",
        )

    def test_file_configparser_rejects_is_searched_whole(self) -> None:
        # A duplicate section makes configparser raise, so git-review
        # cannot read this file at all; the lenient scan still serves it.
        text = "[gerrit]\nhost=a.example.org\n[gerrit]\nhost=b.example.org\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "a.example.org"

    @pytest.mark.parametrize("key", ["host", "port", "project"])
    def test_continuation_line_is_not_a_key(self, key: str) -> None:
        # configparser folds a line indented deeper than the option
        # above it into that option's value; git-review never sees it
        # as a key, so neither may this parser.
        value = {"host": "wrong.example.org", "port": "1", "project": "wrong"}
        text = (
            "[gerrit]\n"
            "host=right.example.org\n"
            "port=29419\n"
            "project=right\n"
            "note = explanation\n"
            f"    {key}={value[key]}\n"
        )
        info = parse_gitreview(text)
        assert info is not None
        assert (info.host, info.port, info.project) == (
            "right.example.org",
            29419,
            "right",
        )

    @pytest.mark.parametrize(
        "gap",
        ["\n", "# comment\n", "; comment\n"],
        ids=["blank", "hash", "semi"],
    )
    def test_continuation_survives_blank_and_comment_lines(
        self, gap: str
    ) -> None:
        text = f"[gerrit]\nnote = x\n{gap}    host=wrong.example.org\n"
        assert parse_gitreview(text) is None


# Layouts on which parse_gitreview must agree with configparser, the
# reader git-review uses. Each is a valid .gitreview a repository could
# hold; the parser is lenient beyond this, but never contrary to it.
CONFIGPARSER_LAYOUTS = [
    pytest.param(TYPICAL_GITREVIEW, id="flat"),
    pytest.param(INDENTED_GITREVIEW, id="all-indented"),
    pytest.param(
        "[gerrit]\n\thost=h.example.org\n\tport=29419\n\tproject=p\n",
        id="tab-indented",
    ),
    pytest.param(
        "[gerrit]\n    host=h.example.org\n",
        id="indented-under-flat-header",
    ),
    pytest.param(
        "[gerrit]\n    host=h.example.org\nport=29419\n",
        id="dedent-after-indent",
    ),
    pytest.param(
        "[gerrit]\nnote = x\n    host=wrong.example.org\n", id="continuation"
    ),
    pytest.param(
        "[gerrit]\nnote = x\n    host=wrong.example.org\nhost=h.example.org\n",
        id="continuation-then-key",
    ),
    pytest.param(
        "[gerrit]\nhost=h.example.org\n    port=1\n", id="continuation-of-host"
    ),
    pytest.param(
        "[gerrit]\n  note = x\n    port=1\n  host=h.example.org\n",
        id="deeper-option-ends-run",
    ),
    pytest.param(
        "[other]\n    host=wrong.example.org\n    port=1\n    project=wrong\n"
        "[gerrit]\nhost=right.example.org\nport=29419\nproject=right\n",
        id="indented-keys-in-other-section",
    ),
    pytest.param(
        "[other]\nhost=wrong.example.org\nport=1\nproject=wrong\n"
        "[gerrit]\nhost=right.example.org\nport=29419\nproject=right\n",
        id="flat-keys-in-earlier-section",
    ),
    pytest.param(
        "[gerrit]\nhost=right.example.org\n[other]\nport=1\nproject=wrong\n",
        id="flat-keys-in-later-section",
    ),
    pytest.param(
        "[DEFAULT]\nhost=default.example.org\nport=29419\n[gerrit]\nproject=p\n",
        id="default-section-fills-gerrit",
    ),
    pytest.param(
        "[DEFAULT]\nhost=default.example.org\nport=1\n"
        "[gerrit]\nhost=right.example.org\nport=29419\n",
        id="gerrit-overrides-earlier-default",
    ),
    pytest.param(
        "[gerrit]\nhost=right.example.org\n[DEFAULT]\nhost=default.example.org\n",
        id="gerrit-overrides-later-default",
    ),
    pytest.param(
        "[DEFAULT]\nhost=default.example.org\n[gerrit]\nhost=\n",
        id="empty-host-overrides-default",
    ),
    pytest.param(
        "[DEFAULT]\nport=1\n[gerrit]\nhost=h.example.org\nport=\n",
        id="empty-port-overrides-default",
    ),
    pytest.param(
        "[DEFAULT]\nproject=wrong\n[gerrit]\nhost=h.example.org\nproject=\n",
        id="empty-project-overrides-default",
    ),
]


def _first_line(value: str) -> str:
    """Return the first line of a configparser value, stripped.

    configparser joins continuation lines into the value; a .gitreview
    field is single-line, so only the first line is comparable.
    """
    return value.strip().split("\n", 1)[0].strip()


@pytest.mark.parametrize("text", CONFIGPARSER_LAYOUTS)
def test_agrees_with_configparser(text: str) -> None:
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    section = parser["gerrit"]
    info = parse_gitreview(text)
    expected_host = _first_line(section.get("host", ""))
    if not expected_host:
        assert info is None
        return
    assert info is not None
    assert info.host == expected_host
    port = _first_line(section.get("port", "")) or str(DEFAULT_GERRIT_PORT)
    assert info.port == int(port)
    project = _first_line(section.get("project", "")).removesuffix(".git")
    assert info.project == project


# -----------------------------------------------------------------------
# read_local_gitreview — local file reader
# -----------------------------------------------------------------------


class TestReadLocalGitreview:
    def test_reads_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / ".gitreview"
        p.write_text(TYPICAL_GITREVIEW, encoding="utf-8")
        info = read_local_gitreview(p)
        assert info is not None
        assert info.host == "gerrit.linuxfoundation.org"

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert read_local_gitreview(tmp_path / "nonexistent") is None

    def test_returns_none_for_malformed(self, tmp_path: Path) -> None:
        p = tmp_path / ".gitreview"
        p.write_text("garbage\n", encoding="utf-8")
        assert read_local_gitreview(p) is None

    def test_reads_indented_file(self, tmp_path: Path) -> None:
        p = tmp_path / ".gitreview"
        p.write_text(INDENTED_GITREVIEW, encoding="utf-8")
        info = read_local_gitreview(p)
        assert info is not None
        assert info.host == "gerrit.onap.org"
        assert info.project == "cps"

    def test_returns_none_for_unreadable(self, tmp_path: Path) -> None:
        p = tmp_path / ".gitreview"
        p.write_text(TYPICAL_GITREVIEW, encoding="utf-8")
        p.chmod(0o000)
        result = read_local_gitreview(p)
        # Restore permissions for cleanup
        p.chmod(0o644)
        assert result is None

    def test_defaults_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".gitreview").write_text(
            MINIMAL_GITREVIEW, encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        info = read_local_gitreview()
        assert info is not None
        assert info.host == "gerrit.example.org"

    def test_defaults_to_cwd_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert read_local_gitreview() is None


# -----------------------------------------------------------------------
# _build_branch_list — branch list construction
# -----------------------------------------------------------------------


class TestBuildBranchList:
    def test_defaults_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        result = _build_branch_list()
        assert result == ["master", "main"]

    def test_extra_branches_prepended(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        result = _build_branch_list(extra_branches=["feature/a", "develop"])
        assert result == ["feature/a", "develop", "master", "main"]

    def test_env_refs_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "pr-branch")
        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        result = _build_branch_list()
        # "main" should appear once even though it's in env + defaults
        assert result == ["pr-branch", "main", "master"]

    def test_env_refs_skipped_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "pr-branch")
        monkeypatch.setenv("GITHUB_BASE_REF", "develop")
        result = _build_branch_list(include_env_refs=False)
        assert result == ["master", "main"]

    def test_deduplication(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "master")
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        result = _build_branch_list(extra_branches=["master"])
        # "master" should appear exactly once
        assert result.count("master") == 1

    def test_empty_entries_filtered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "")
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        result = _build_branch_list(extra_branches=["", "valid", ""])
        assert "" not in result
        assert "valid" in result

    def test_custom_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        result = _build_branch_list(default_branches=["develop", "trunk"])
        assert result == ["develop", "trunk"]

    def test_order_is_extra_then_env_then_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "env-head")
        monkeypatch.setenv("GITHUB_BASE_REF", "env-base")
        result = _build_branch_list(
            extra_branches=["first"],
            default_branches=["last"],
        )
        assert result == ["first", "env-head", "env-base", "last"]


# -----------------------------------------------------------------------
# _validate_raw_url — URL validation
# -----------------------------------------------------------------------


class TestValidateRawUrl:
    def test_valid_url(self) -> None:
        assert _validate_raw_url(
            "https://raw.githubusercontent.com/owner/repo/refs/heads/main/.gitreview"
        )

    def test_http_rejected(self) -> None:
        assert not _validate_raw_url(
            "http://raw.githubusercontent.com/owner/repo/refs/heads/main/.gitreview"
        )

    def test_wrong_host_rejected(self) -> None:
        assert not _validate_raw_url(
            "https://evil.example.com/owner/repo/refs/heads/main/.gitreview"
        )

    def test_empty_rejected(self) -> None:
        assert not _validate_raw_url("")


# -----------------------------------------------------------------------
# fetch_gitreview_raw — raw.githubusercontent.com fetcher
# -----------------------------------------------------------------------


def _mock_urlopen_for_text(text: str) -> MagicMock:
    """Create a mock for urllib.request.urlopen that returns the given text."""
    mock_response = MagicMock()
    mock_response.read.return_value = text.encode("utf-8")
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


class TestFetchGitreviewRaw:
    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_fetches_first_valid_branch(
        self,
        mock_urlopen: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        mock_urlopen.return_value = _mock_urlopen_for_text(TYPICAL_GITREVIEW)

        info = fetch_gitreview_raw(
            "lfit/releng-lftools", include_env_refs=False
        )
        assert info is not None
        assert info.host == "gerrit.linuxfoundation.org"
        assert info.project == "releng/lftools"
        assert info.base_path == "infra"

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_tries_extra_branches_first(
        self,
        mock_urlopen: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        # First call (for "custom-branch") succeeds
        mock_urlopen.return_value = _mock_urlopen_for_text(MINIMAL_GITREVIEW)

        info = fetch_gitreview_raw(
            "owner/repo",
            branches=["custom-branch"],
            include_env_refs=False,
        )
        assert info is not None
        # Verify the URL used the custom branch
        call_args = mock_urlopen.call_args
        assert "custom-branch" in call_args[0][0]

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_falls_through_on_failure(
        self,
        mock_urlopen: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        # First branch fails, second succeeds
        mock_urlopen.side_effect = [
            OSError("404 Not Found"),
            _mock_urlopen_for_text(MINIMAL_GITREVIEW),
        ]

        info = fetch_gitreview_raw("owner/repo", include_env_refs=False)
        assert info is not None
        assert info.host == "gerrit.example.org"
        assert mock_urlopen.call_count == 2

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_returns_none_when_all_fail(
        self,
        mock_urlopen: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        mock_urlopen.side_effect = OSError("Network unreachable")
        assert fetch_gitreview_raw("owner/repo", include_env_refs=False) is None

    def test_returns_none_for_empty_repo(self) -> None:
        assert fetch_gitreview_raw("") is None

    def test_returns_none_for_repo_without_slash(self) -> None:
        assert fetch_gitreview_raw("noslash") is None

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_url_encodes_branch_names(
        self,
        mock_urlopen: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        mock_urlopen.return_value = _mock_urlopen_for_text(MINIMAL_GITREVIEW)

        fetch_gitreview_raw(
            "owner/repo",
            branches=["feature/my branch"],
            include_env_refs=False,
        )
        url_used = mock_urlopen.call_args[0][0]
        # Space should be encoded as %20, slash preserved
        assert "feature/my%20branch" in url_used

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_preserves_slash_in_branch_names(
        self,
        mock_urlopen: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        mock_urlopen.return_value = _mock_urlopen_for_text(MINIMAL_GITREVIEW)

        fetch_gitreview_raw(
            "owner/repo",
            branches=["feature/foo"],
            include_env_refs=False,
        )
        url_used = mock_urlopen.call_args[0][0]
        assert "feature/foo" in url_used
        assert "feature%2Ffoo" not in url_used

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_deduplicates_branches(
        self,
        mock_urlopen: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        # All branches fail so we can count attempts
        mock_urlopen.side_effect = OSError("fail")

        fetch_gitreview_raw(
            "owner/repo",
            branches=["master", "main", "master"],
            include_env_refs=False,
        )
        # Should only try master and main (2), not master twice (3)
        assert mock_urlopen.call_count == 2


# -----------------------------------------------------------------------
# fetch_gitreview_github_api — PyGithub API fetcher
# -----------------------------------------------------------------------


class TestFetchGitreviewGithubApi:
    def test_successful_fetch(self) -> None:
        mock_content = MagicMock()
        mock_content.decoded_content = TYPICAL_GITREVIEW.encode("utf-8")

        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = mock_content

        info = fetch_gitreview_github_api(mock_repo)
        assert info is not None
        assert info.host == "gerrit.linuxfoundation.org"
        mock_repo.get_contents.assert_called_once_with(".gitreview")

    def test_with_ref(self) -> None:
        mock_content = MagicMock()
        mock_content.decoded_content = MINIMAL_GITREVIEW.encode("utf-8")

        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = mock_content

        info = fetch_gitreview_github_api(mock_repo, ref="develop")
        assert info is not None
        mock_repo.get_contents.assert_called_once_with(
            ".gitreview", ref="develop"
        )

    def test_empty_content(self) -> None:
        mock_content = MagicMock()
        mock_content.decoded_content = b""

        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = mock_content

        assert fetch_gitreview_github_api(mock_repo) is None

    def test_404_returns_none(self) -> None:
        mock_repo = MagicMock()
        mock_repo.get_contents.side_effect = Exception("404 Not Found")

        assert fetch_gitreview_github_api(mock_repo) is None

    def test_malformed_returns_none(self) -> None:
        mock_content = MagicMock()
        mock_content.decoded_content = b"nothing useful"

        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = mock_content

        assert fetch_gitreview_github_api(mock_repo) is None

    def test_missing_decoded_content_attr(self) -> None:
        """Handle objects without decoded_content gracefully."""
        mock_content = object()  # No decoded_content attribute at all

        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = mock_content

        # getattr(content, "decoded_content", b"") returns b""
        assert fetch_gitreview_github_api(mock_repo) is None


# -----------------------------------------------------------------------
# fetch_gitreview — unified multi-strategy fetcher
# -----------------------------------------------------------------------


class TestFetchGitreview:
    def test_local_file_wins(self, tmp_path: Path) -> None:
        p = tmp_path / ".gitreview"
        p.write_text(TYPICAL_GITREVIEW, encoding="utf-8")

        info = fetch_gitreview(local_path=p)
        assert info is not None
        assert info.host == "gerrit.linuxfoundation.org"

    def test_skip_local_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When skip_local=True, local file is not read even if it exists."""
        p = tmp_path / ".gitreview"
        p.write_text(TYPICAL_GITREVIEW, encoding="utf-8")
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        # No repo_obj, no repo_full → all strategies exhausted
        info = fetch_gitreview(local_path=p, skip_local=True, repo_full="")
        assert info is None

    def test_local_path_none_skips_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        info = fetch_gitreview(local_path=None, repo_full="")
        assert info is None

    def test_github_api_fallback(self, tmp_path: Path) -> None:
        """When local file is missing, GitHub API is tried."""
        mock_content = MagicMock()
        mock_content.decoded_content = MINIMAL_GITREVIEW.encode("utf-8")

        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = mock_content

        info = fetch_gitreview(
            local_path=tmp_path / "nonexistent",
            repo_obj=mock_repo,
        )
        assert info is not None
        assert info.host == "gerrit.example.org"

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_raw_url_fallback(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When local and API both fail, raw URL is tried."""
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        mock_urlopen.return_value = _mock_urlopen_for_text(MINIMAL_GITREVIEW)

        info = fetch_gitreview(
            local_path=tmp_path / "nonexistent",
            repo_obj=None,
            repo_full="owner/repo",
            include_env_refs=False,
        )
        assert info is not None
        assert info.host == "gerrit.example.org"

    def test_all_strategies_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        info = fetch_gitreview(
            local_path=tmp_path / "nonexistent",
            repo_obj=None,
            repo_full="",
        )
        assert info is None

    def test_local_takes_priority_over_api(self, tmp_path: Path) -> None:
        """Local file should win even when repo_obj is provided."""
        p = tmp_path / ".gitreview"
        p.write_text(
            "[gerrit]\nhost=local.example.org\nproject=local/proj.git\n",
            encoding="utf-8",
        )

        mock_content = MagicMock()
        mock_content.decoded_content = (
            b"[gerrit]\nhost=api.example.org\nproject=api/proj.git\n"
        )
        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = mock_content

        info = fetch_gitreview(local_path=p, repo_obj=mock_repo)
        assert info is not None
        assert info.host == "local.example.org"
        # API should NOT have been called
        mock_repo.get_contents.assert_not_called()

    def test_api_takes_priority_over_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub API should win over raw URL fallback."""
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        mock_content = MagicMock()
        mock_content.decoded_content = (
            b"[gerrit]\nhost=api.example.org\nproject=api/proj.git\n"
        )
        mock_repo = MagicMock()
        mock_repo.get_contents.return_value = mock_content

        with patch(
            "github2gerrit.gitreview.urllib.request.urlopen"
        ) as mock_urlopen:
            info = fetch_gitreview(
                local_path=tmp_path / "nonexistent",
                repo_obj=mock_repo,
                repo_full="owner/repo",
            )

        assert info is not None
        assert info.host == "api.example.org"
        mock_urlopen.assert_not_called()


# -----------------------------------------------------------------------
# read_gitreview_host — convenience host-only accessor
# -----------------------------------------------------------------------


class TestReadGitreviewHost:
    def test_returns_host_from_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".gitreview").write_text(
            TYPICAL_GITREVIEW, encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        result = read_gitreview_host()
        assert result == "gerrit.linuxfoundation.org"

    def test_returns_none_when_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        assert read_gitreview_host() is None

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_falls_back_to_remote(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        mock_urlopen.return_value = _mock_urlopen_for_text(
            "[gerrit]\nhost=git.opendaylight.org\n"
        )

        result = read_gitreview_host("opendaylight/aaa")
        assert result == "git.opendaylight.org"

    def test_uses_github_repository_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        with patch(
            "github2gerrit.gitreview.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_for_text(
                MINIMAL_GITREVIEW
            )
            result = read_gitreview_host()

        assert result == "gerrit.example.org"


# -----------------------------------------------------------------------
# make_gitreview_info — factory with auto base_path
# -----------------------------------------------------------------------


class TestMakeGitreviewInfo:
    def test_basic_construction(self) -> None:
        info = make_gitreview_info("gerrit.example.org", 29418, "proj")
        assert info.host == "gerrit.example.org"
        assert info.port == 29418
        assert info.project == "proj"
        assert info.base_path is None  # unknown host

    def test_auto_derives_base_path_for_known_host(self) -> None:
        info = make_gitreview_info("gerrit.linuxfoundation.org", 29418, "foo")
        assert info.base_path == "infra"

    def test_explicit_base_path_overrides(self) -> None:
        info = make_gitreview_info(
            "gerrit.linuxfoundation.org",
            base_path="custom",
        )
        assert info.base_path == "custom"

    def test_explicit_none_suppresses_lookup(self) -> None:
        info = make_gitreview_info(
            "gerrit.linuxfoundation.org",
            base_path=None,
        )
        assert info.base_path is None

    def test_default_port(self) -> None:
        info = make_gitreview_info("h")
        assert info.port == DEFAULT_GERRIT_PORT

    def test_default_project(self) -> None:
        info = make_gitreview_info("h")
        assert info.project == ""

    def test_result_is_frozen(self) -> None:
        info = make_gitreview_info("h")
        with pytest.raises(AttributeError):
            info.host = "other"  # type: ignore[misc]

    def test_result_is_gitreviewinfo(self) -> None:
        info = make_gitreview_info("h")
        assert isinstance(info, GitReviewInfo)
        assert isinstance(info, GerritInfo)


# -----------------------------------------------------------------------
# Integration: config.py delegation
# -----------------------------------------------------------------------


class TestConfigDelegation:
    """Verify config._read_gitreview_host delegates to shared module."""

    def test_local_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".gitreview").write_text(
            TYPICAL_GITREVIEW, encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        from github2gerrit.config import _read_gitreview_host

        result = _read_gitreview_host()
        assert result == "gerrit.linuxfoundation.org"

    def test_none_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

        from github2gerrit.config import _read_gitreview_host

        assert _read_gitreview_host() is None

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_remote_fallback(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        mock_urlopen.return_value = _mock_urlopen_for_text(
            "[gerrit]\nhost=git.opendaylight.org\n"
        )

        from github2gerrit.config import _read_gitreview_host

        result = _read_gitreview_host("opendaylight/aaa")
        assert result == "git.opendaylight.org"


# -----------------------------------------------------------------------
# Integration: core.py GerritInfo re-export
# -----------------------------------------------------------------------


class TestCoreReExport:
    """Verify core.py re-exports GerritInfo from gitreview module."""

    def test_import_works(self) -> None:
        from github2gerrit.core import GerritInfo as CoreGerritInfo

        info = CoreGerritInfo(host="h", port=1, project="p")
        assert info.host == "h"
        assert info.port == 1
        assert info.project == "p"

    def test_base_path_field_available(self) -> None:
        from github2gerrit.core import GerritInfo as CoreGerritInfo

        info = CoreGerritInfo(host="h", port=1, project="p", base_path="bp")
        assert info.base_path == "bp"

    def test_backward_compatible_without_base_path(self) -> None:
        from github2gerrit.core import GerritInfo as CoreGerritInfo

        info = CoreGerritInfo(host="h", port=1, project="p")
        assert info.base_path is None


# -----------------------------------------------------------------------
# Edge cases and regression tests
# -----------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and regressions from the original implementations."""

    def test_parse_handles_windows_line_endings(self) -> None:
        text = "[gerrit]\r\nhost=gerrit.example.org\r\nport=29418\r\n"
        info = parse_gitreview(text)
        assert info is not None
        # .strip() in the parser should handle trailing \r
        assert info.host == "gerrit.example.org"

    def test_parse_handles_trailing_whitespace(self) -> None:
        text = "[gerrit]\nhost=gerrit.example.org   \nport=29418\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org"

    def test_parse_captures_inline_comments_as_value(self) -> None:
        """INI-style comments after values are NOT standard for .gitreview.
        The parser should capture the full line after '=' and strip it,
        so an inline comment becomes part of the value."""
        text = "[gerrit]\nhost=gerrit.example.org # primary\nport=29418\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "gerrit.example.org # primary"

    @patch("github2gerrit.gitreview.urllib.request.urlopen")
    def test_raw_fetch_with_refs_heads_prefix(
        self,
        mock_urlopen: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the URL includes refs/heads/ prefix."""
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        mock_urlopen.return_value = _mock_urlopen_for_text(MINIMAL_GITREVIEW)

        fetch_gitreview_raw("owner/repo", include_env_refs=False)
        url = mock_urlopen.call_args[0][0]
        assert "/refs/heads/master/.gitreview" in url

    def test_parse_multiple_sections_takes_first_host(self) -> None:
        """If there are duplicate host= lines, take the first one (regex behaviour)."""
        text = "[gerrit]\nhost=first.example.org\nhost=second.example.org\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.host == "first.example.org"

    def test_make_gitreview_info_known_host_gets_base_path(self) -> None:
        """Regression: _resolve_gerrit_info in core.py should auto-derive base_path."""
        info = make_gitreview_info(
            host="gerrit.linuxfoundation.org",
            port=29418,
            project="releng/builder",
        )
        assert info.base_path == "infra"

    def test_gitreview_info_is_hashable(self) -> None:
        """Frozen dataclasses should be hashable for use in sets/dicts."""
        info = GitReviewInfo(host="h", port=1, project="p")
        assert hash(info) is not None
        s = {info}
        assert info in s

    @pytest.mark.parametrize(
        "header", ["[gerrit]\n", ""], ids=["configparser", "line-scan"]
    )
    def test_parse_oversized_port_reads_as_absent(self, header: str) -> None:
        """A port too long for int() falls back to the default, not raise."""
        text = f"{header}host=gerrit.example.org\nport={'1' * 5000}\n"
        info = parse_gitreview(text)
        assert info is not None
        assert info.port == DEFAULT_GERRIT_PORT
        assert info.port_given is False
