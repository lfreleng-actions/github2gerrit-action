# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for composite action environment variable mapping and processing.

This module validates that inputs are correctly mapped to environment variables
and that the CLI receives the expected environment configuration.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def action_config():
    """Load action.yaml configuration."""
    action_path = Path(__file__).parent.parent / "action.yaml"
    with open(action_path) as f:
        return yaml.safe_load(f)


class TestActionEnvironmentMapping:
    """Test environment variable mapping from action inputs."""

    def test_input_to_environment_mapping(self, action_config):
        """Test that all inputs are properly mapped to environment variables."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        assert cli_step is not None
        env_mapping = cli_step.get("env", {})

        # Test core input mappings - check that environment variables exist and
        # reference inputs
        expected_inputs = [
            "SUBMIT_SINGLE_COMMITS",
            "USE_PR_AS_COMMIT",
            "FETCH_DEPTH",
            "GERRIT_KNOWN_HOSTS",
            "GERRIT_SSH_PRIVKEY_G2G",
            "GERRIT_SSH_USER_G2G",
            "GERRIT_SSH_USER_G2G_EMAIL",
            "ORGANIZATION",
            "REVIEWERS_EMAIL",
            "ALLOW_GHE_URLS",
            "PRESERVE_GITHUB_PRS",
            "DRY_RUN",
            "ALLOW_DUPLICATES",
            "CI_TESTING",
            "DUPLICATE_TYPES",
            "NORMALISE_COMMIT",
            "G2G_USE_SSH_AGENT",
            "GERRIT_SERVER",
            "GERRIT_SERVER_PORT",
            "GERRIT_PROJECT",
            "GERRIT_HTTP_BASE_PATH",
            "GERRIT_HTTP_USER",
            "GERRIT_HTTP_PASSWORD",
        ]

        for input_name in expected_inputs:
            assert input_name in env_mapping
            # Should reference the corresponding input
            assert f"inputs.{input_name}" in env_mapping[input_name]

    def test_github_context_mapping(self, action_config):
        """Test GitHub context variables are mapped to environment."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        # Test GitHub context mappings
        github_mappings = {
            "GITHUB_EVENT_NAME": "${{ github.event_name }}",
            "GITHUB_REPOSITORY": "${{ github.repository }}",
            "GITHUB_REPOSITORY_OWNER": "${{ github.repository_owner }}",
            "GITHUB_SERVER_URL": "${{ github.server_url }}",
            "GITHUB_RUN_ID": "${{ github.run_id }}",
            "GITHUB_SHA": "${{ github.sha }}",
            "GITHUB_BASE_REF": "${{ github.base_ref }}",
            "GITHUB_HEAD_REF": "${{ github.head_ref }}",
            "GITHUB_TOKEN": "${{ github.token }}",
        }

        for env_var, expected_value in github_mappings.items():
            assert env_mapping.get(env_var) == expected_value

    def test_computed_environment_variables(self, action_config):
        """Test computed environment variables from previous steps."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        # Test environment variables set by previous steps
        computed_vars = [
            "SYNC_ALL_OPEN_PRS",
            "PR_NUMBER",
        ]

        for var in computed_vars:
            assert var in env_mapping
            expected_value = f"${{{{ env.{var} }}}}"
            assert env_mapping[var] == expected_value

    def test_issue_id_conditional_mapping(self, action_config):
        """Test Issue ID conditional environment mapping."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})
        issue_id_env = env_mapping.get("ISSUE_ID", "")

        # Should have conditional logic for ISSUE_ID
        assert "inputs.ISSUE_ID != ''" in issue_id_env
        assert "inputs.ISSUE_ID" in issue_id_env
        assert "env.RESOLVED_ISSUE_ID" in issue_id_env

    def test_test_mode_environment(self, action_config):
        """Test test mode environment variable is set."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        # G2G_TEST_MODE should be explicitly set to false in production
        assert env_mapping.get("G2G_TEST_MODE") == "false"


class TestEnvironmentVariableProcessing:
    """Test environment variable processing logic."""

    def test_pr_number_environment_handling(self):
        """Test PR_NUMBER environment variable processing logic."""
        # Script that mimics the PR_NUMBER extraction logic
        script = textwrap.dedent("""
            set -euo pipefail

            EVENT_NAME="$1"
            INPUT_PR_NUMBER="$2"
            EVENT_PR_NUMBER="${3:-}"

            # Extract PR number and validate context (from action.yaml)
            if [[ "$EVENT_NAME" == "workflow_dispatch" ]]; then
                if [[ -n "${SYNC_ALL_OPEN_PRS:-}" ]]; then
                    echo "Processing all open pull requests via " \
                         "workflow_dispatch"
                else
                    if [[ -z "${PR_NUMBER:-}" || "${PR_NUMBER}" == "null" ]]
                    then
                        echo "Error: provide PR_NUMBER or set 0 to " \
                             "process all PRs" >&2
                        exit 2
                    fi
                    echo "PR_NUMBER=${PR_NUMBER}"
                fi
            else
                PR_NUMBER_EVT="${EVENT_PR_NUMBER}"
                if [[ -z "${PR_NUMBER:-}" ]]; then
                    PR_NUMBER="${PR_NUMBER_EVT}"
                fi
                if [[ -z "${PR_NUMBER}" || "${PR_NUMBER}" == "null" ]]; then
                    echo "Error: PR_NUMBER is empty." >&2
                    echo "This action requires a valid pull request context" >&2
                    echo "Current event: $EVENT_NAME" >&2
                    exit 2
                fi
                echo "PR_NUMBER=${PR_NUMBER}"
            fi
        """).strip()

        def run_script(
            event_name: str,
            input_pr: str,
            event_pr: str = "",
            env_vars: dict[str, str] | None = None,
        ) -> subprocess.CompletedProcess[str]:
            test_env = os.environ.copy()
            if env_vars:
                test_env.update(env_vars)

            return subprocess.run(
                ["bash", "-c", script, "--", event_name, input_pr, event_pr],
                env=test_env,
                text=True,
                capture_output=True,
                check=False,
            )

        # Test workflow_dispatch with bulk mode
        result = run_script(
            "workflow_dispatch", "0", "", {"SYNC_ALL_OPEN_PRS": "true"}
        )
        assert result.returncode == 0
        assert "Processing all open pull requests" in result.stdout

        # Test workflow_dispatch with specific PR
        result = run_script("workflow_dispatch", "42", "", {"PR_NUMBER": "42"})
        assert result.returncode == 0
        assert "PR_NUMBER=42" in result.stdout

        # Test pull_request event with PR from context
        result = run_script("pull_request", "", "123")
        assert result.returncode == 0
        assert "PR_NUMBER=123" in result.stdout

        # Test pull_request event missing context
        result = run_script("pull_request", "", "")
        assert result.returncode == 2
        assert "requires a valid pull request context" in result.stderr

    def test_issue_id_environment_resolution(self):
        """Test Issue ID environment resolution logic."""
        script = textwrap.dedent("""
            set -euo pipefail

            INPUT_ISSUE_ID="$1"
            RESOLVED_ISSUE_ID="${2:-}"

            # Mimic the Issue ID logic from action.yaml
            if [[ -n "$INPUT_ISSUE_ID" ]]; then
                FINAL_ISSUE_ID="$INPUT_ISSUE_ID"
            else
                FINAL_ISSUE_ID="$RESOLVED_ISSUE_ID"
            fi

            echo "ISSUE_ID=${FINAL_ISSUE_ID}"
        """).strip()

        def run_script(
            input_id: str, resolved_id: str = ""
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", "-c", script, "--", input_id, resolved_id],
                text=True,
                capture_output=True,
                check=False,
            )

        # Test input takes priority
        result = run_script("ABC-123", "DEF-456")
        assert result.returncode == 0
        assert "ISSUE_ID=ABC-123" in result.stdout

        # Test resolved is used when input is empty
        result = run_script("", "DEF-456")
        assert result.returncode == 0
        assert "ISSUE_ID=DEF-456" in result.stdout

        # Test empty when both are empty
        result = run_script("", "")
        assert result.returncode == 0
        assert "ISSUE_ID=" in result.stdout


class TestEnvironmentValidation:
    """Test environment variable validation."""

    def test_required_environment_variables(self, action_config):
        """Test that required environment variables are mapped."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        # Essential variables that must be present
        required_vars = [
            "GITHUB_TOKEN",
            "GERRIT_SSH_PRIVKEY_G2G",
            "GITHUB_REPOSITORY",
            "GITHUB_EVENT_NAME",
            "PR_NUMBER",
        ]

        for var in required_vars:
            assert var in env_mapping, (
                f"Required environment variable {var} not mapped"
            )

    def test_boolean_environment_variables(self, action_config):
        """Test boolean environment variables are properly mapped."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        # Boolean input variables
        boolean_vars = [
            "SUBMIT_SINGLE_COMMITS",
            "USE_PR_AS_COMMIT",
            "ALLOW_GHE_URLS",
            "PRESERVE_GITHUB_PRS",
            "DRY_RUN",
            "ALLOW_DUPLICATES",
            "CI_TESTING",
            "G2G_USE_SSH_AGENT",
            "NORMALISE_COMMIT",
        ]

        for var in boolean_vars:
            assert var in env_mapping
            # Should reference inputs
            assert f"inputs.{var}" in env_mapping[var]

    def test_optional_environment_variables(self, action_config):
        """Test optional environment variables are mapped."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        # Optional variables that may be empty
        optional_vars = [
            "GERRIT_KNOWN_HOSTS",
            "GERRIT_SSH_USER_G2G",
            "GERRIT_SSH_USER_G2G_EMAIL",
            "REVIEWERS_EMAIL",
            "GERRIT_SERVER",
            "GERRIT_PROJECT",
            "GERRIT_HTTP_BASE_PATH",
            "GERRIT_HTTP_USER",
            "GERRIT_HTTP_PASSWORD",
        ]

        for var in optional_vars:
            assert var in env_mapping
            # Should reference inputs
            assert f"inputs.{var}" in env_mapping[var]


class TestEnvironmentDefaults:
    """Test environment variable defaults."""

    def test_default_values_in_inputs(self, action_config):
        """Test default values are properly set in inputs."""
        inputs = action_config["inputs"]

        expected_defaults = {
            "SUBMIT_SINGLE_COMMITS": "false",
            "USE_PR_AS_COMMIT": "false",
            "FETCH_DEPTH": "10",
            "PR_NUMBER": "0",
            "GERRIT_SSH_USER_G2G": "",
            "GERRIT_SSH_USER_G2G_EMAIL": "",
            "ORGANIZATION": "${{ github.repository_owner }}",
            "REVIEWERS_EMAIL": "",
            "ALLOW_GHE_URLS": "false",
            "PRESERVE_GITHUB_PRS": "false",
            "DRY_RUN": "false",
            "ALLOW_DUPLICATES": "false",
            "CI_TESTING": "false",
            "ISSUE_ID": "",
            "G2G_USE_SSH_AGENT": "true",
            "DUPLICATE_TYPES": "open",
            "NORMALISE_COMMIT": "false",
            "GERRIT_SERVER": "",
            "GERRIT_SERVER_PORT": "29418",
            "GERRIT_PROJECT": "",
            "GERRIT_HTTP_BASE_PATH": "",
            "GERRIT_HTTP_USER": "",
            "GERRIT_HTTP_PASSWORD": "",
        }

        for input_name, expected_default in expected_defaults.items():
            assert input_name in inputs
            actual_default = inputs[input_name].get("default", "")
            assert actual_default == expected_default, (
                f"Input {input_name}: expected default '{expected_default}', "
                f"got '{actual_default}'"
            )

    def test_github_context_defaults(self, action_config):
        """Test GitHub context variable defaults."""
        inputs = action_config["inputs"]

        # ORGANIZATION should default to repository owner
        org_input = inputs["ORGANIZATION"]
        assert org_input["default"] == "${{ github.repository_owner }}"

    def test_port_number_default(self, action_config):
        """Test Gerrit port number has correct default."""
        inputs = action_config["inputs"]

        port_input = inputs["GERRIT_SERVER_PORT"]
        assert port_input["default"] == "29418"
        # Description may not contain the port number explicitly
        assert (
            "port" in port_input["description"].lower()
            or "tcp" in port_input["description"].lower()
        )


class TestEnvironmentSecrets:
    """Test environment variables containing secrets."""

    def test_secret_environment_variables(self, action_config):
        """Test that secret variables are properly handled."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        # Secret variables that should reference inputs or context
        secret_vars = {
            "GERRIT_SSH_PRIVKEY_G2G": "inputs.GERRIT_SSH_PRIVKEY_G2G",
            "GITHUB_TOKEN": "github.token",
            "GERRIT_HTTP_PASSWORD": "inputs.GERRIT_HTTP_PASSWORD",
        }

        for var, expected_ref in secret_vars.items():
            assert var in env_mapping
            assert expected_ref in env_mapping[var]

    def test_no_hardcoded_secrets(self, action_config):
        """Test that no secrets are hardcoded in environment mapping."""
        cli_step = None
        for step in action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        # Check that no environment values contain hardcoded secrets
        for var, value in env_mapping.items():
            # Should not contain obvious secret patterns
            assert not any(
                pattern in value.lower()
                for pattern in [
                    "password123",
                    "secret123",
                    "key123",
                    "token123",
                ]
            )

            # Should use GitHub Actions syntax for references
            if "PRIVKEY" in var or "PASSWORD" in var or "TOKEN" in var:
                assert "${{" in value and "}}" in value
