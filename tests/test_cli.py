# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from github2gerrit.cli import app


runner = CliRunner()


def test_conflicting_options_error_message_in_stderr(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["SUBMIT_SINGLE_COMMITS"] = "true"
    env["USE_PR_AS_COMMIT"] = "true"

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 2
    assert "Configuration validation failed" in result.stderr
    assert "cannot be enabled at the same time" in result.stderr


def test_missing_required_input_error_message_in_stderr(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    # Remove a required input to trigger a validation error path
    env.pop("GERRIT_SSH_PRIVKEY_G2G", None)

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 2
    assert "Configuration validation failed" in result.stderr
    assert "Missing required input: gerrit_ssh_privkey_g2g" in result.stderr


def test_configuration_error_no_traceback_in_stderr(tmp_path: Path) -> None:
    """Verify that configuration errors don't expose Python tracebacks to users."""
    env = _base_env(tmp_path)
    # Remove a required input to trigger a validation error path
    env.pop("GERRIT_SSH_PRIVKEY_G2G", None)

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 2
    # Should have clear error message
    assert "Configuration validation failed" in result.stderr
    # Should NOT have Python traceback elements
    assert "Traceback" not in result.stderr
    assert "click.exceptions.BadParameter" not in result.stderr
    assert "typer.BadParameter" not in result.stderr
    assert 'File "' not in result.stderr


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Return a baseline environment with required inputs set."""
    event_path = tmp_path / "event.json"
    # Default to an event with a PR number
    event = {"action": "opened", "pull_request": {"number": 7}}
    event_path.write_text(json.dumps(event), encoding="utf-8")

    return {
        # Required inputs
        "GERRIT_KNOWN_HOSTS": "example.com ssh-rsa AAAAB3Nza...",
        "GERRIT_SSH_PRIVKEY_G2G": "-----BEGIN KEY-----\nabc\n-----END KEY-----",
        "GERRIT_SSH_USER_G2G": "gerrit-bot",
        "GERRIT_SSH_USER_G2G_EMAIL": "gerrit-bot@example.org",
        # Optional inputs
        "ORGANIZATION": "example",
        "REVIEWERS_EMAIL": "",
        # Boolean flags
        "DRY_RUN": "false",
        "PRESERVE_GITHUB_PRS": "false",
        "ALLOW_DUPLICATES": "false",
        "CI_TESTING": "false",
        # GitHub context
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_REPOSITORY": "example/repo",
        "GITHUB_REPOSITORY_OWNER": "example",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_SHA": "deadbeef",
        "GITHUB_BASE_REF": "main",
        "GITHUB_HEAD_REF": "feature",
        "G2G_TEST_MODE": "true",
    }


def test_conflicting_options_exits_2(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["SUBMIT_SINGLE_COMMITS"] = "true"
    env["USE_PR_AS_COMMIT"] = "true"

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 2
    assert (
        "cannot be enabled at the same time" in result.stdout or "cannot be enabled at the same time" in result.stderr
    )


def test_missing_required_inputs_exits_2(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    # Remove one required input to trigger validation error
    env.pop("GERRIT_SSH_PRIVKEY_G2G", None)

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 2
    assert "Missing required input" in (result.stdout + result.stderr)


def test_parses_pr_number_and_returns_zero(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    # Ensure non-conflicting options and sane defaults
    env["SUBMIT_SINGLE_COMMITS"] = "false"
    env["USE_PR_AS_COMMIT"] = "false"
    env["FETCH_DEPTH"] = "10"

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 0
    # The CLI currently only validates and exits cleanly
    assert "Validation complete. Ready to execute submission pipeline." in (result.stdout + result.stderr)


def test_no_pr_context_exits_2(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    # Overwrite event to remove PR number
    event_path = Path(env["GITHUB_EVENT_PATH"])
    event_path.write_text(json.dumps({}), encoding="utf-8")
    env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    # Disable test mode to ensure non-zero exit on missing PR context
    env.pop("G2G_TEST_MODE", None)
    # Force non-bulk path to avoid GitHub API token requirement
    env["SYNC_ALL_OPEN_PRS"] = "false"

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 2
    assert "requires a valid pull request context" in (result.stdout + result.stderr)


def test_validation_with_parameter_derivation_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that validation passes when Gerrit parameters can be derived from organization."""
    # Clear environment first
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)

    env = _base_env(tmp_path)
    # Remove the derived parameters to test derivation
    env.pop("GERRIT_SSH_USER_G2G", None)
    env.pop("GERRIT_SSH_USER_G2G_EMAIL", None)
    env.pop("GERRIT_SERVER", None)
    # Keep ORGANIZATION so derivation can work
    env["ORGANIZATION"] = "onap"
    # Use empty config file to avoid interference from real config
    empty_config = tmp_path / "config.txt"
    empty_config.write_text("", encoding="utf-8")
    env["G2G_CONFIG_PATH"] = str(empty_config)

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 0
    assert "Validation complete" in (result.stdout + result.stderr)


def test_validation_fails_when_no_organization_and_missing_gerrit_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that validation fails when organization is missing and Gerrit params can't be derived."""
    # Clear environment first
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)

    env = _base_env(tmp_path)
    # Remove the derived parameters and all sources of organization
    env.pop("GERRIT_SSH_USER_G2G", None)
    env.pop("GERRIT_SSH_USER_G2G_EMAIL", None)
    env.pop("ORGANIZATION", None)
    env.pop("GITHUB_REPOSITORY_OWNER", None)
    env.pop("GITHUB_REPOSITORY", None)
    # Remove GitHub Actions context to simulate local CLI usage
    env.pop("GITHUB_EVENT_NAME", None)
    # Disable test mode to ensure validation errors are properly caught
    env.pop("G2G_TEST_MODE", None)
    # Use empty config file to avoid interference from real config
    empty_config = tmp_path / "config.txt"
    empty_config.write_text("", encoding="utf-8")
    env["G2G_CONFIG_PATH"] = str(empty_config)

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 2
    assert "Missing required input" in (result.stdout + result.stderr)


def test_validation_partial_derivation_with_explicit_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that validation works when some Gerrit params are explicit and others derived."""
    # Clear environment first
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)

    env = _base_env(tmp_path)
    # Keep one explicit value, remove others for derivation
    env["GERRIT_SSH_USER_G2G"] = "custom.bot"
    env.pop("GERRIT_SSH_USER_G2G_EMAIL", None)
    env.pop("GERRIT_SERVER", None)
    env["ORGANIZATION"] = "o-ran-sc"
    # Use empty config file to avoid interference from real config
    empty_config = tmp_path / "config.txt"
    empty_config.write_text("", encoding="utf-8")
    env["G2G_CONFIG_PATH"] = str(empty_config)

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 0
    assert "Validation complete" in (result.stdout + result.stderr)


def test_validation_local_cli_requires_derivation_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that local CLI usage requires G2G_ENABLE_DERIVATION=true for parameter derivation."""
    # Clear environment first
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)

    env = _base_env(tmp_path)
    # Remove Gerrit parameters to test derivation
    env.pop("GERRIT_SSH_USER_G2G", None)
    env.pop("GERRIT_SSH_USER_G2G_EMAIL", None)
    # Remove GitHub Actions context to simulate local CLI
    env.pop("GITHUB_EVENT_NAME", None)
    env["ORGANIZATION"] = "onap"
    # Disable test mode to see validation behavior
    env.pop("G2G_TEST_MODE", None)
    # Use empty config file to avoid interference from real config
    empty_config = tmp_path / "config.txt"
    empty_config.write_text("", encoding="utf-8")
    env["G2G_CONFIG_PATH"] = str(empty_config)

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 2
    assert "Missing required input" in (result.stdout + result.stderr)


def test_validation_local_cli_with_derivation_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that local CLI usage works when G2G_ENABLE_DERIVATION=true is set."""
    # Clear environment first
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)

    env = _base_env(tmp_path)
    # Remove Gerrit parameters to test derivation
    env.pop("GERRIT_SSH_USER_G2G", None)
    env.pop("GERRIT_SSH_USER_G2G_EMAIL", None)
    # Remove GitHub Actions context to simulate local CLI
    env.pop("GITHUB_EVENT_NAME", None)
    env["ORGANIZATION"] = "onap"
    env["G2G_ENABLE_DERIVATION"] = "true"
    # Use empty config file to avoid interference from real config
    empty_config = tmp_path / "config.txt"
    empty_config.write_text("", encoding="utf-8")
    env["G2G_CONFIG_PATH"] = str(empty_config)

    result = runner.invoke(app, [], env=env)
    assert result.exit_code == 0
    assert "Validation complete" in (result.stdout + result.stderr)


@pytest.mark.parametrize("exception_class", [RuntimeError, ValueError, TypeError])  # type: ignore[misc]
def test_unexpected_exception_exits_with_code_1(tmp_path: Path, exception_class: type[Exception]) -> None:
    """Test that unexpected exceptions during processing exit with code 1."""
    from unittest.mock import patch

    env = _base_env(tmp_path)

    # Mock _process to raise an unexpected exception
    with patch("github2gerrit.cli._process", side_effect=exception_class("Unexpected error")):
        result = runner.invoke(app, [], env=env)
        assert result.exit_code == 1
