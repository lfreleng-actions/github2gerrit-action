# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import TypeVar

import pytest

from github2gerrit.config import _coerce_value
from github2gerrit.config import _expand_env_refs
from github2gerrit.config import _normalize_bool_like
from github2gerrit.config import _select_section
from github2gerrit.config import _strip_quotes
from github2gerrit.config import apply_config_to_env
from github2gerrit.config import apply_parameter_derivation
from github2gerrit.config import derive_gerrit_parameters
from github2gerrit.config import filter_known
from github2gerrit.config import load_org_config
from github2gerrit.config import overlay_missing


if TYPE_CHECKING:
    F = TypeVar("F", bound=Callable[..., object])

    def parametrize(*args: object, **kwargs: object) -> Callable[[F], F]: ...
else:
    from pytest import mark

    parametrize = mark.parametrize


def test_expand_env_refs_expands_present_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOO", "bar")
    monkeypatch.setenv("NUM", "123")
    value = "prefix-${ENV:FOO}-x-${ENV:NUM}-suffix"
    assert _expand_env_refs(value) == "prefix-bar-x-123-suffix"


def test_expand_env_refs_missing_vars_become_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING", raising=False)
    value = "start-${ENV:MISSING}-end"
    assert _expand_env_refs(value) == "start--end"


@parametrize(
    "raw, expected",
    [
        ('"hello"', "hello"),
        ("'hello'", "hello"),
        ('  " spaced "  ', " spaced "),
        ("noquotes", "noquotes"),
        ("", ""),
    ],
)
def test_strip_quotes_various_forms(raw: str, expected: str) -> None:
    assert _strip_quotes(raw) == expected


@parametrize(
    "raw, expected",
    [
        ("true", "true"),
        ("TRUE", "true"),
        ("Yes", "true"),
        ("on", "true"),
        ("1", "true"),
        ("false", "false"),
        ("FALSE", "false"),
        ("No", "false"),
        ("off", "false"),
        ("0", "false"),
        ("maybe", None),
        ("", None),
        ("  y  ", None),
    ],
)
def test_normalize_bool_like(raw: str, expected: str | None) -> None:
    assert _normalize_bool_like(raw) == expected


def test_coerce_value_handles_quotes_bools_and_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Quoted string with escaped newlines -> real newlines, quotes stripped
    multi = '"Line1\\nLine2\\nLine3"'
    assert _coerce_value(multi) == "Line1\nLine2\nLine3"

    # Mixed CRLF/escaped newlines normalized to LF
    mixed = '"A\\r\\nB\\nC\\r\\n"'
    assert _coerce_value(mixed) == "A\nB\nC\n"

    # Boolean-like normalization
    assert _coerce_value(" TRUE ") == "true"
    assert _coerce_value("no") == "false"

    # Environment expansion before quote stripping
    monkeypatch.setenv("TOKEN", "sekret")
    conf = '"Bearer ${ENV:TOKEN}"'
    assert _coerce_value(conf) == "Bearer sekret"


def test_select_section_case_insensitive() -> None:
    import configparser
    from typing import Any
    from typing import cast

    cp = configparser.RawConfigParser()
    cast(Any, cp).optionxform = str  # preserve key case
    cp.read_string(
        """
[Default]
A = 1
[MyOrg]
B = 2
"""
    )
    assert _select_section(cp, "myorg") == "MyOrg"
    assert _select_section(cp, "MYORG") == "MyOrg"
    assert _select_section(cp, "unknown") is None


def test_load_org_config_merges_default_and_org_and_normalizes(
    tmp_path: Path,
) -> None:
    cfg_text = """
[default]
GERRIT_SERVER = "gerrit.example.org"
PRESERVE_GITHUB_PRS = "false"

[OnAp]
GERRIT_HTTP_USER = "user1"
GERRIT_HTTP_PASSWORD = "${ENV:ONAP_GERRIT_TOKEN}"
SSH_BLOCK = "
-----BEGIN KEY-----
abc
-----END KEY-----
"
SUBMIT_SINGLE_COMMITS = "YES"
"""
    cfg_file = tmp_path / "configuration.txt"
    cfg_file.write_text(cfg_text, encoding="utf-8")

    # Provide token referenced via ${ENV:ONAP_GERRIT_TOKEN}
    os.environ["ONAP_GERRIT_TOKEN"] = "sekret-token"

    cfg = load_org_config(org="onap", path=cfg_file)

    # From default
    assert cfg["GERRIT_SERVER"] == "gerrit.example.org"
    # Bool normalized
    assert cfg["PRESERVE_GITHUB_PRS"] == "false"
    # Org values
    assert cfg["GERRIT_HTTP_USER"] == "user1"
    assert cfg["GERRIT_HTTP_PASSWORD"] == "sekret-token"
    # Multiline quoted block round-tripped to real newlines and quotes stripped
    assert cfg["SSH_BLOCK"] == "-----BEGIN KEY-----\nabc\n-----END KEY-----"
    # Bool-like normalization in org section
    assert cfg["SUBMIT_SINGLE_COMMITS"] == "true"


def test_load_org_config_uses_env_detected_org_and_path_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_text = """
[default]
A = "x"
[Acme]
A = "y"
B = "z"
"""
    cfg_file = tmp_path / "conf.ini"
    cfg_file.write_text(cfg_text, encoding="utf-8")

    monkeypatch.setenv("ORGANIZATION", "ACME")
    monkeypatch.setenv("G2G_CONFIG_PATH", str(cfg_file))

    cfg = load_org_config()

    assert cfg["A"] == "y"  # org overlays default
    assert cfg["B"] == "z"


def test_apply_config_to_env_sets_only_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pre-populate env with an existing value
    monkeypatch.setenv("EXISTING_KEY", "keepme")

    cfg = {
        "EXISTING_KEY": "donotoverride",
        "NEW_KEY": "setme",
        "ANOTHER": "value",
    }
    apply_config_to_env(cfg)

    assert os.getenv("EXISTING_KEY") == "keepme"
    assert os.getenv("NEW_KEY") == "setme"
    assert os.getenv("ANOTHER") == "value"


def test_unknown_config_keys_generate_warnings(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test that unknown configuration keys generate warning messages."""
    cfg_text = """
[default]
GERRIT_SERVER = "gerrit.example.org"
UNKNOWN_KEY = "some_value"

[onap]
REVIEWERS_EMAIL = "user@example.org"
TYPO_KEY = "another_value"
ANOTHER_UNKNOWN = "third_value"
"""
    cfg_file = tmp_path / "conf.ini"
    cfg_file.write_text(cfg_text, encoding="utf-8")

    with caplog.at_level("WARNING"):
        cfg = load_org_config(org="onap", path=cfg_file)

    # Should contain the known keys
    assert cfg["GERRIT_SERVER"] == "gerrit.example.org"
    assert cfg["REVIEWERS_EMAIL"] == "user@example.org"

    # Should also contain unknown keys (they're still passed through)
    assert cfg["UNKNOWN_KEY"] == "some_value"
    assert cfg["TYPO_KEY"] == "another_value"
    assert cfg["ANOTHER_UNKNOWN"] == "third_value"

    # Should have logged a warning about unknown keys
    warning_messages = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert len(warning_messages) == 1
    warning_msg = warning_messages[0]
    assert "Unknown configuration keys found in [onap]:" in warning_msg
    assert "UNKNOWN_KEY" in warning_msg
    assert "TYPO_KEY" in warning_msg
    assert "ANOTHER_UNKNOWN" in warning_msg
    assert "Check for typos or missing functionality" in warning_msg


def test_filter_known_with_and_without_extras() -> None:
    sample = {
        "SUBMIT_SINGLE_COMMITS": "true",  # known
        "REVIEWERS_EMAIL": "a@example.org",  # known
        "EXTRA_OPTION": "42",  # unknown
    }
    # include_extra=True returns all keys
    out_all = filter_known(sample, include_extra=True)
    assert out_all == sample

    # include_extra=False filters out unknown keys
    out_known = filter_known(sample, include_extra=False)
    assert "SUBMIT_SINGLE_COMMITS" in out_known
    assert "REVIEWERS_EMAIL" in out_known
    assert "EXTRA_OPTION" not in out_known


def test_overlay_missing_prefers_primary_and_fills_empty_strings() -> None:
    primary = {
        "A": "1",
        "B": "",
        "C": "keep",
    }
    fallback = {
        "B": "2",  # should fill because primary["B"] == ""
        "C": "override",  # should NOT override
        "D": "added",  # new key
    }
    merged = overlay_missing(primary, fallback)
    assert merged["A"] == "1"
    assert merged["B"] == "2"
    assert merged["C"] == "keep"
    assert merged["D"] == "added"


def test_derive_gerrit_parameters_basic() -> None:
    """Test basic parameter derivation from organization name."""
    derived = derive_gerrit_parameters("onap")
    assert derived["GERRIT_SSH_USER_G2G"] == "onap.gh2gerrit"
    assert derived["GERRIT_SSH_USER_G2G_EMAIL"] == "releng+onap-gh2gerrit@linuxfoundation.org"
    assert derived["GERRIT_SERVER"] == "gerrit.onap.org"


def test_derive_gerrit_parameters_case_normalization() -> None:
    """Test that organization name is normalized to lowercase."""
    derived = derive_gerrit_parameters("ONAP")
    assert derived["GERRIT_SSH_USER_G2G"] == "onap.gh2gerrit"
    assert derived["GERRIT_SSH_USER_G2G_EMAIL"] == "releng+onap-gh2gerrit@linuxfoundation.org"
    assert derived["GERRIT_SERVER"] == "gerrit.onap.org"


def test_derive_gerrit_parameters_with_spaces() -> None:
    """Test that spaces in organization name are handled."""
    derived = derive_gerrit_parameters("  o-ran-sc  ")
    assert derived["GERRIT_SSH_USER_G2G"] == "o-ran-sc.gh2gerrit"
    assert derived["GERRIT_SSH_USER_G2G_EMAIL"] == "releng+o-ran-sc-gh2gerrit@linuxfoundation.org"
    assert derived["GERRIT_SERVER"] == "gerrit.o-ran-sc.org"


def test_derive_gerrit_parameters_empty_org() -> None:
    """Test that empty organization returns empty dict."""
    assert derive_gerrit_parameters("") == {}
    assert derive_gerrit_parameters(None) == {}


def test_apply_parameter_derivation_fills_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that derivation only fills missing or empty values in GitHub Actions context."""
    # Simulate GitHub Actions environment to enable derivation
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")

    cfg = {
        "GERRIT_SSH_USER_G2G": "",  # empty, should be derived
        "GERRIT_SERVER": "custom.gerrit.com",  # already set, should not be overridden
        "OTHER_KEY": "value",  # unrelated key, should be preserved
    }

    result = apply_parameter_derivation(cfg, "onap")

    assert result["GERRIT_SSH_USER_G2G"] == "onap.gh2gerrit"  # derived
    assert result["GERRIT_SSH_USER_G2G_EMAIL"] == "releng+onap-gh2gerrit@linuxfoundation.org"  # derived
    assert result["GERRIT_SERVER"] == "custom.gerrit.com"  # preserved
    assert result["OTHER_KEY"] == "value"  # preserved


def test_apply_parameter_derivation_preserves_existing_values() -> None:
    """Test that existing non-empty values are not overridden."""
    cfg = {
        "GERRIT_SSH_USER_G2G": "custom.user",
        "GERRIT_SSH_USER_G2G_EMAIL": "custom@example.org",
        "GERRIT_SERVER": "custom.gerrit.com",
    }

    result = apply_parameter_derivation(cfg, "onap")

    # All values should remain unchanged
    assert result["GERRIT_SSH_USER_G2G"] == "custom.user"
    assert result["GERRIT_SSH_USER_G2G_EMAIL"] == "custom@example.org"
    assert result["GERRIT_SERVER"] == "custom.gerrit.com"


def test_apply_parameter_derivation_no_org() -> None:
    """Test that no derivation occurs when organization is not provided."""
    cfg = {
        "GERRIT_SSH_USER_G2G": "",
        "OTHER_KEY": "value",
    }

    result = apply_parameter_derivation(cfg, None)

    # Should return unchanged config
    assert result == cfg

    result = apply_parameter_derivation(cfg, "")
    assert result == cfg


def test_apply_parameter_derivation_github_actions_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that derivation works automatically in GitHub Actions context."""
    # Simulate GitHub Actions environment
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")

    cfg = {
        "GERRIT_SSH_USER_G2G": "",
        "GERRIT_SERVER": "",
        "OTHER_KEY": "value",
    }

    result = apply_parameter_derivation(cfg, "onap")

    # Should derive missing parameters
    assert result["GERRIT_SSH_USER_G2G"] == "onap.gh2gerrit"
    assert result["GERRIT_SSH_USER_G2G_EMAIL"] == "releng+onap-gh2gerrit@linuxfoundation.org"
    assert result["GERRIT_SERVER"] == "gerrit.onap.org"
    assert result["OTHER_KEY"] == "value"


def test_apply_parameter_derivation_local_cli_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that derivation is disabled in local CLI context without explicit flag."""
    # Simulate local CLI environment (no GitHub Actions context)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("G2G_ENABLE_DERIVATION", raising=False)

    cfg = {
        "GERRIT_SSH_USER_G2G": "",
        "OTHER_KEY": "value",
    }

    result = apply_parameter_derivation(cfg, "onap")

    # Should NOT derive parameters without explicit flag
    assert result["GERRIT_SSH_USER_G2G"] == ""
    assert "GERRIT_SSH_USER_G2G_EMAIL" not in result
    assert result["OTHER_KEY"] == "value"


def test_apply_parameter_derivation_local_cli_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that derivation works in local CLI context with explicit flag."""
    # Simulate local CLI environment with derivation enabled
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("G2G_ENABLE_DERIVATION", "true")

    cfg = {
        "GERRIT_SSH_USER_G2G": "",
        "GERRIT_SERVER": "custom.gerrit.com",  # explicit value should be preserved
        "OTHER_KEY": "value",
    }

    result = apply_parameter_derivation(cfg, "o-ran-sc")

    # Should derive missing parameters but preserve explicit ones
    assert result["GERRIT_SSH_USER_G2G"] == "o-ran-sc.gh2gerrit"
    assert result["GERRIT_SSH_USER_G2G_EMAIL"] == "releng+o-ran-sc-gh2gerrit@linuxfoundation.org"
    assert result["GERRIT_SERVER"] == "custom.gerrit.com"  # preserved
    assert result["OTHER_KEY"] == "value"


def test_context_detection_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test GitHub Actions and local CLI context detection."""
    from github2gerrit.config import _is_github_actions_context
    from github2gerrit.config import _is_local_cli_context

    # Test GitHub Actions detection via GITHUB_ACTIONS
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _is_github_actions_context() is True
    assert _is_local_cli_context() is False

    # Test GitHub Actions detection via GITHUB_EVENT_NAME
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    assert _is_github_actions_context() is True
    assert _is_local_cli_context() is False

    # Test local CLI detection
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    assert _is_github_actions_context() is False
    assert _is_local_cli_context() is True

    # Test empty GITHUB_EVENT_NAME is treated as local CLI
    monkeypatch.setenv("GITHUB_EVENT_NAME", "")
    assert _is_github_actions_context() is False
    assert _is_local_cli_context() is True


def test_save_derived_parameters_to_config_new_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test saving derived parameters to a new organization section."""
    from github2gerrit.config import save_derived_parameters_to_config

    config_file = tmp_path / "config.txt"
    config_file.write_text('[default]\nGERRIT_SERVER = "gerrit.example.org"\n', encoding="utf-8")

    # Simulate local CLI environment
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    derived_params = {
        "GERRIT_SSH_USER_G2G": "onap.gh2gerrit",
        "GERRIT_SSH_USER_G2G_EMAIL": "releng+onap-gh2gerrit@linuxfoundation.org",
        "GERRIT_SERVER": "gerrit.onap.org",
    }

    save_derived_parameters_to_config("onap", derived_params, str(config_file))

    # Verify the config file was updated
    updated_content = config_file.read_text(encoding="utf-8")
    assert "[onap]" in updated_content
    assert 'GERRIT_SSH_USER_G2G = "onap.gh2gerrit"' in updated_content
    assert 'GERRIT_SSH_USER_G2G_EMAIL = "releng+onap-gh2gerrit@linuxfoundation.org"' in updated_content
    assert 'GERRIT_SERVER = "gerrit.onap.org"' in updated_content


def test_save_derived_parameters_to_config_existing_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test saving derived parameters to an existing organization section."""
    from github2gerrit.config import save_derived_parameters_to_config

    config_file = tmp_path / "config.txt"
    config_file.write_text(
        "[default]\n"
        'GERRIT_SERVER = "gerrit.example.org"\n\n'
        "[onap]\n"
        'GERRIT_HTTP_USER = "existing_user"\n'
        'GERRIT_SSH_USER_G2G = "existing.user"\n',  # This should not be overwritten
        encoding="utf-8",
    )

    # Simulate local CLI environment
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    derived_params = {
        "GERRIT_SSH_USER_G2G": "onap.gh2gerrit",  # Should not be added (already exists)
        "GERRIT_SSH_USER_G2G_EMAIL": "releng+onap-gh2gerrit@linuxfoundation.org",  # Should be added
        "GERRIT_SERVER": "gerrit.onap.org",  # Should be added
    }

    save_derived_parameters_to_config("onap", derived_params, str(config_file))

    # Verify the config file was updated correctly
    updated_content = config_file.read_text(encoding="utf-8")
    assert 'GERRIT_SSH_USER_G2G = "existing.user"' in updated_content  # Original preserved
    assert 'GERRIT_SSH_USER_G2G_EMAIL = "releng+onap-gh2gerrit@linuxfoundation.org"' in updated_content  # Added
    assert 'GERRIT_SERVER = "gerrit.onap.org"' in updated_content  # Added


def test_apply_parameter_derivation_saves_to_config_local_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that apply_parameter_derivation saves derived parameters to config in local CLI mode."""
    from github2gerrit.config import apply_parameter_derivation

    config_file = tmp_path / "config.txt"
    config_file.write_text("", encoding="utf-8")

    # Simulate local CLI environment with derivation enabled
    # G2G_AUTO_SAVE_CONFIG defaults to true in local CLI mode
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setenv("G2G_ENABLE_DERIVATION", "true")
    monkeypatch.setenv("G2G_CONFIG_PATH", str(config_file))

    cfg = {
        "GERRIT_SSH_USER_G2G": "",
        "OTHER_KEY": "value",
    }

    result = apply_parameter_derivation(cfg, "o-ran-sc", save_to_config=True)

    # Should derive parameters
    assert result["GERRIT_SSH_USER_G2G"] == "o-ran-sc.gh2gerrit"
    assert result["GERRIT_SSH_USER_G2G_EMAIL"] == "releng+o-ran-sc-gh2gerrit@linuxfoundation.org"

    # Should save to config file
    updated_content = config_file.read_text(encoding="utf-8")
    assert "[o-ran-sc]" in updated_content
    assert 'GERRIT_SSH_USER_G2G = "o-ran-sc.gh2gerrit"' in updated_content


def test_apply_parameter_derivation_no_save_github_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that apply_parameter_derivation does not save to config in GitHub Actions by default."""
    from github2gerrit.config import apply_parameter_derivation

    config_file = tmp_path / "config.txt"
    config_file.write_text("", encoding="utf-8")

    # Simulate GitHub Actions environment
    # G2G_AUTO_SAVE_CONFIG defaults to false in GitHub Actions
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("G2G_CONFIG_PATH", str(config_file))

    cfg = {
        "GERRIT_SSH_USER_G2G": "",
    }

    result = apply_parameter_derivation(cfg, "onap", save_to_config=True)

    # Should derive parameters
    assert result["GERRIT_SSH_USER_G2G"] == "onap.gh2gerrit"

    # Should NOT save to config file in GitHub Actions (default behavior)
    updated_content = config_file.read_text(encoding="utf-8")
    assert updated_content == ""  # File should remain empty


def test_apply_parameter_derivation_disabled_auto_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that auto-save can be disabled with G2G_AUTO_SAVE_CONFIG=false."""
    from github2gerrit.config import apply_parameter_derivation

    config_file = tmp_path / "config.txt"
    config_file.write_text("", encoding="utf-8")

    # Simulate local CLI environment with auto-save explicitly disabled
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setenv("G2G_ENABLE_DERIVATION", "true")
    monkeypatch.setenv("G2G_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("G2G_AUTO_SAVE_CONFIG", "false")

    cfg = {
        "GERRIT_SSH_USER_G2G": "",
    }

    result = apply_parameter_derivation(cfg, "onap", save_to_config=True)

    # Should derive parameters
    assert result["GERRIT_SSH_USER_G2G"] == "onap.gh2gerrit"

    # Should NOT save to config file when auto-save is disabled
    updated_content = config_file.read_text(encoding="utf-8")
    assert updated_content == ""  # File should remain empty


def test_apply_parameter_derivation_github_actions_explicit_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that GitHub Actions can explicitly enable auto-save with G2G_AUTO_SAVE_CONFIG=true."""
    from github2gerrit.config import apply_parameter_derivation

    config_file = tmp_path / "config.txt"
    config_file.write_text("", encoding="utf-8")

    # Simulate GitHub Actions environment with auto-save explicitly enabled
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("G2G_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("G2G_AUTO_SAVE_CONFIG", "true")

    cfg = {
        "GERRIT_SSH_USER_G2G": "",
    }

    result = apply_parameter_derivation(cfg, "onap", save_to_config=True)

    # Should derive parameters
    assert result["GERRIT_SSH_USER_G2G"] == "onap.gh2gerrit"

    # Should save to config file when explicitly enabled in GitHub Actions
    updated_content = config_file.read_text(encoding="utf-8")
    assert "[onap]" in updated_content
    assert 'GERRIT_SSH_USER_G2G = "onap.gh2gerrit"' in updated_content
