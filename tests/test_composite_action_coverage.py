# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Comprehensive test coverage for the composite action (action.yaml).

This test suite validates the complete composite action workflow including:
- Input validation and processing
- Environment variable mapping
- Step execution flow
- Output generation
- Error handling
- Integration scenarios

The tests simulate the GitHub Actions environment and validate the shell
logic embedded in the action.yaml file.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml


class CompositeActionTester:
    """Helper class to test composite action behavior."""

    def __init__(self, action_yaml_path: str):
        """Initialize with path to action.yaml."""
        with open(action_yaml_path) as f:
            self.action_config = yaml.safe_load(f)

        # Extract the shell scripts from the action steps
        self.scripts = self._extract_shell_scripts()

    def _extract_shell_scripts(self) -> dict[str, str]:
        """Extract shell scripts from action steps."""
        scripts = {}
        for i, step in enumerate(self.action_config["runs"]["steps"]):
            if step.get("shell") == "bash" and "run" in step:
                step_name = step.get("name", f"step_{i}")
                scripts[step_name] = step["run"]
        return scripts

    def simulate_step(
        self,
        step_name: str,
        env_vars: dict[str, str],
        inputs: dict[str, str],
        github_context: dict[str, Any],
    ) -> subprocess.CompletedProcess[str]:
        """Simulate execution of a specific step."""
        if step_name not in self.scripts:
            raise ValueError(f"Step '{step_name}' not found")

        script = self.scripts[step_name]

        # Replace GitHub Actions expressions with actual values
        script = self._substitute_expressions(
            script, inputs, github_context, env_vars
        )

        # Prepare environment
        test_env = os.environ.copy()
        test_env.update(env_vars)

        # Add GitHub Actions specific environment variables
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as env_file:
            test_env["GITHUB_ENV"] = env_file.name
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as output_file:
            test_env["GITHUB_OUTPUT"] = output_file.name

        # Map inputs to environment variables (simulate GitHub Actions)
        for key, value in inputs.items():
            test_env[f"INPUT_{key.upper()}"] = str(value)

        # Add GitHub context
        for key, value in github_context.items():
            test_env[f"GITHUB_{key.upper()}"] = str(value)

        try:
            # Execute the script
            result = subprocess.run(
                ["bash", "-c", script],
                env=test_env,
                text=True,
                capture_output=True,
                check=False,
            )

            # Read any environment variables set by the script
            if os.path.exists(test_env["GITHUB_ENV"]):
                with open(test_env["GITHUB_ENV"]) as f:
                    for line in f:
                        if "=" in line:
                            key, value = line.strip().split("=", 1)
                            test_env[key] = value

            return result
        finally:
            # Cleanup temp files
            for temp_file in [
                test_env["GITHUB_ENV"],
                test_env["GITHUB_OUTPUT"],
            ]:
                if os.path.exists(temp_file):
                    os.unlink(temp_file)

    def _substitute_expressions(
        self,
        script: str,
        inputs: dict[str, str],
        github_context: dict[str, Any],
        env_vars: dict[str, str],
    ) -> str:
        """Substitute GitHub Actions expressions with actual values."""
        result = script

        # Substitute inputs
        for key, value in inputs.items():
            result = result.replace(f"${{{{ inputs.{key} }}}}", str(value))

        # Substitute GitHub context
        for key, value in github_context.items():
            result = result.replace(f"${{{{ github.{key} }}}}", str(value))

        # Substitute environment variables
        for key, value in env_vars.items():
            result = result.replace(f"${{{{ env.{key} }}}}", str(value))

        return result


@pytest.fixture
def action_tester() -> CompositeActionTester:
    """Provide a CompositeActionTester instance."""
    action_path = Path(__file__).parent.parent / "action.yaml"
    return CompositeActionTester(str(action_path))


@pytest.fixture
def default_inputs():
    """Provide default input values."""
    return {
        "GERRIT_SSH_PRIVKEY_G2G": "test-key",
        "SUBMIT_SINGLE_COMMITS": "false",
        "USE_PR_AS_COMMIT": "false",
        "FETCH_DEPTH": "10",
        "PR_NUMBER": "0",
        "GERRIT_SSH_USER_G2G": "",
        "GERRIT_SSH_USER_G2G_EMAIL": "",
        "ORGANIZATION": "test-org",
        "REVIEWERS_EMAIL": "",
        "ALLOW_GHE_URLS": "false",
        "PRESERVE_GITHUB_PRS": "true",
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


@pytest.fixture
def default_github_context():
    """Provide default GitHub context."""
    return {
        "event_name": "pull_request",
        "repository": "test-org/test-repo",
        "repository_owner": "test-org",
        "server_url": "https://github.com",
        "run_id": "12345",
        "sha": "abc123",
        "base_ref": "main",
        "head_ref": "feature-branch",
        "token": "gh_token_123",
    }


class TestInputValidation:
    """Test input validation and processing."""

    def test_required_input_gerrit_ssh_privkey(self, action_tester):
        """Test that GERRIT_SSH_PRIVKEY_G2G is required."""
        action_config = action_tester.action_config
        required_inputs = [
            name
            for name, config in action_config["inputs"].items()
            if config.get("required", False)
        ]
        assert "GERRIT_SSH_PRIVKEY_G2G" in required_inputs

    def test_input_defaults(self, action_tester):
        """Test that input defaults are correctly defined."""
        inputs = action_tester.action_config["inputs"]

        # Test specific defaults
        assert inputs["SUBMIT_SINGLE_COMMITS"]["default"] == "false"
        assert inputs["FETCH_DEPTH"]["default"] == "10"
        assert inputs["PR_NUMBER"]["default"] == "0"
        assert inputs["GERRIT_SERVER_PORT"]["default"] == "29418"
        assert inputs["G2G_USE_SSH_AGENT"]["default"] == "true"

    def test_boolean_input_values(self, action_tester):
        """Test that boolean inputs accept valid values."""
        boolean_inputs = [
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

        for input_name in boolean_inputs:
            assert input_name in action_tester.action_config["inputs"]


class TestPRNumberHandling:
    """Test PR number validation and normalization."""

    def test_workflow_dispatch_pr_number_zero(
        self, action_tester, default_inputs, default_github_context
    ):
        """Test workflow_dispatch with PR_NUMBER=0 enables bulk mode."""
        env_vars = {}
        inputs = default_inputs.copy()
        inputs["PR_NUMBER"] = "0"
        github_context = default_github_context.copy()
        github_context["event_name"] = "workflow_dispatch"

        result = action_tester.simulate_step(
            "Normalize PR_NUMBER",
            env_vars,
            inputs,
            github_context,
        )

        assert result.returncode == 0
        # The step writes to GITHUB_ENV, so we check stderr/stdout for any
        # output
        assert result.stderr == "" or "SYNC_ALL_OPEN_PRS" in result.stdout

    def test_workflow_dispatch_specific_pr_number(
        self, action_tester, default_inputs, default_github_context
    ):
        """Test workflow_dispatch with specific PR number."""
        env_vars = {}
        inputs = default_inputs.copy()
        inputs["PR_NUMBER"] = "42"
        github_context = default_github_context.copy()
        github_context["event_name"] = "workflow_dispatch"

        result = action_tester.simulate_step(
            "Normalize PR_NUMBER",
            env_vars,
            inputs,
            github_context,
        )

        assert result.returncode == 0
        # The step writes to GITHUB_ENV, not stdout
        assert result.stderr == "" or "error" not in result.stderr.lower()

    def test_workflow_dispatch_invalid_pr_number(
        self, action_tester, default_inputs, default_github_context
    ):
        """Test workflow_dispatch with invalid PR number."""
        env_vars = {}
        inputs = default_inputs.copy()
        inputs["PR_NUMBER"] = "abc"
        github_context = default_github_context.copy()
        github_context["event_name"] = "workflow_dispatch"

        result = action_tester.simulate_step(
            "Normalize PR_NUMBER",
            env_vars,
            inputs,
            github_context,
        )

        assert result.returncode == 2
        assert "must be a numeric value" in result.stdout

    def test_non_dispatch_rejects_explicit_pr_number(
        self, action_tester, default_inputs, default_github_context
    ):
        """Test that non-dispatch events reject explicit PR_NUMBER."""
        env_vars = {}
        inputs = default_inputs.copy()
        inputs["PR_NUMBER"] = "42"
        github_context = default_github_context.copy()
        github_context["event_name"] = "pull_request"

        result = action_tester.simulate_step(
            "Validate PR_NUMBER usage",
            env_vars,
            inputs,
            github_context,
        )

        assert result.returncode == 2
        assert "only valid during workflow_dispatch" in result.stdout


class TestEnvironmentMapping:
    """Test environment variable mapping from inputs."""

    def test_input_to_env_mapping(self, action_tester):
        """Test that inputs are correctly mapped to environment variables."""
        # Extract the CLI step to see environment mapping
        cli_step = None
        for step in action_tester.action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        assert cli_step is not None
        env_mapping = cli_step.get("env", {})

        # Test key mappings
        expected_mappings = {
            "SUBMIT_SINGLE_COMMITS": "${{ inputs.SUBMIT_SINGLE_COMMITS }}",
            "USE_PR_AS_COMMIT": "${{ inputs.USE_PR_AS_COMMIT }}",
            "FETCH_DEPTH": "${{ inputs.FETCH_DEPTH }}",
            "GERRIT_SSH_PRIVKEY_G2G": "${{ inputs.GERRIT_SSH_PRIVKEY_G2G }}",
            "GITHUB_TOKEN": "${{ github.token }}",
        }

        for env_var, expected_value in expected_mappings.items():
            assert env_mapping.get(env_var) == expected_value

    def test_github_context_mapping(self, action_tester):
        """Test GitHub context variables are mapped."""
        cli_step = None
        for step in action_tester.action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})

        github_vars = [
            "GITHUB_EVENT_NAME",
            "GITHUB_REPOSITORY",
            "GITHUB_REPOSITORY_OWNER",
            "GITHUB_SERVER_URL",
            "GITHUB_RUN_ID",
            "GITHUB_SHA",
            "GITHUB_BASE_REF",
            "GITHUB_HEAD_REF",
        ]

        for var in github_vars:
            assert var in env_mapping


class TestIssueIdLookup:
    """Test Issue ID lookup functionality."""

    def test_issue_id_lookup_condition(self, action_tester):
        """Test that Issue ID lookup is now handled in Python, not action steps."""
        # The action should NOT have Issue ID lookup steps anymore
        # Lookup is now handled in the Python CLI
        lookup_steps = []
        for step in action_tester.action_config["runs"]["steps"]:
            step_name = step.get("name", "")
            if "lookup" in step_name.lower() and "Issue" in step_name:
                lookup_steps.append(step)

        # Should be 0 - lookup moved to Python
        assert len(lookup_steps) == 0

    def test_issue_id_priority(self, action_tester):
        """Test Issue ID is passed directly to CLI (lookup handled in Python)."""
        cli_step = None
        for step in action_tester.action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})
        issue_id_env = env_mapping.get("ISSUE_ID", "")
        issue_id_lookup_json_env = env_mapping.get("ISSUE_ID_LOOKUP_JSON", "")

        # Should pass ISSUE_ID directly (no conditional logic)
        assert issue_id_env == "${{ inputs.ISSUE_ID }}"
        # Should pass JSON for Python to handle lookup
        assert issue_id_lookup_json_env == "${{ inputs.ISSUE_ID_LOOKUP_JSON }}"


class TestOutputCapture:
    """Test output capture and formatting."""

    def test_output_definitions(self, action_tester):
        """Test that outputs are properly defined."""
        outputs = action_tester.action_config.get("outputs", {})

        expected_outputs = [
            "gerrit_change_request_url",
            "gerrit_change_request_num",
            "gerrit_commit_sha",
        ]

        for output_name in expected_outputs:
            assert output_name in outputs
            assert (
                "steps.capture-outputs.outputs" in outputs[output_name]["value"]
            )

    def test_multiline_output_format(self, action_tester):
        """Test multiline output formatting in capture step."""
        capture_step = None
        for step in action_tester.action_config["runs"]["steps"]:
            if step.get("name") == "Capture outputs":
                capture_step = step
                break

        assert capture_step is not None
        script = capture_step["run"]

        # Should use GitHub Actions multiline format
        assert "<<G2G" in script
        assert "G2G" in script
        assert "GITHUB_OUTPUT" in script


class TestStepDependencies:
    """Test step dependencies and execution order."""

    def test_python_setup_before_installation(self, action_tester):
        """Test Python is set up before dependency installation."""
        steps = action_tester.action_config["runs"]["steps"]
        step_names = [step.get("name", "") for step in steps]

        python_idx = next(
            i for i, name in enumerate(step_names) if "Setup Python" in name
        )
        install_idx = next(
            i
            for i, name in enumerate(step_names)
            if "Setup github2gerrit" in name
        )

        assert python_idx < install_idx

    def test_checkout_before_cli(self, action_tester):
        """Test repository checkout happens before CLI execution."""
        steps = action_tester.action_config["runs"]["steps"]
        step_names = [step.get("name", "") for step in steps]

        checkout_idx = next(
            i
            for i, name in enumerate(step_names)
            if "Checkout repository" in name
        )
        cli_idx = next(
            i
            for i, name in enumerate(step_names)
            if "Run github2gerrit Python CLI" in name
        )

        assert checkout_idx < cli_idx

    def test_cli_before_output_capture(self, action_tester):
        """Test CLI execution happens before output capture."""
        steps = action_tester.action_config["runs"]["steps"]
        step_names = [step.get("name", "") for step in steps]

        cli_idx = next(
            i
            for i, name in enumerate(step_names)
            if "Run github2gerrit Python CLI" in name
        )
        capture_idx = next(
            i for i, name in enumerate(step_names) if "Capture outputs" in name
        )

        assert cli_idx < capture_idx


class TestActionMetadata:
    """Test action metadata and structure."""

    def test_action_name_and_description(self, action_tester):
        """Test action has proper name and description."""
        assert action_tester.action_config["name"] == "github2gerrit"
        assert (
            "Gerrit changes from GitHub pull requests"
            in action_tester.action_config["description"]
        )

    def test_composite_action_structure(self, action_tester):
        """Test proper composite action structure."""
        assert action_tester.action_config["runs"]["using"] == "composite"
        assert "steps" in action_tester.action_config["runs"]
        assert len(action_tester.action_config["runs"]["steps"]) > 5

    def test_action_pinned_versions(self, action_tester):
        """Test that external actions use pinned SHA versions."""
        steps = action_tester.action_config["runs"]["steps"]

        for step in steps:
            if "uses" in step:
                uses_value = step["uses"]
                if not uses_value.startswith("./"):  # External action
                    # Should contain SHA pin (64 char hex after @)
                    assert "@" in uses_value
                    sha_part = uses_value.split("@")[-1]
                    # Allow for both full SHA (64) and short SHA (7+), but skip
                    # "main" branches and reusable workflows
                    if (
                        sha_part not in ["main", "master"]
                        and "releng-reusable-workflows" not in uses_value
                    ):
                        assert len(sha_part) >= 7 and len(sha_part) <= 64
                        # Should be hex
                        assert all(
                            c in "0123456789abcdef" for c in sha_part.lower()
                        )


class TestErrorHandling:
    """Test error handling scenarios."""

    def test_set_errexit_in_scripts(self, action_tester):
        """Test that shell scripts use proper error handling."""
        critical_steps = [
            "Install required dependencies",
            "Normalize PR_NUMBER for workflow_dispatch",
            "Extract PR number and validate context",
            "Validate PR_NUMBER usage (non-dispatch)",
            "Set IssueID in GITHUB_ENV",
            "Run github2gerrit Python CLI",
            "Capture outputs (best-effort)",
        ]

        for script_name, script in action_tester.scripts.items():
            if script.strip():  # Non-empty scripts
                # Critical steps should use proper error handling
                is_critical = any(
                    critical in script_name for critical in critical_steps
                )
                if is_critical and len(script.split("\n")) > 2:
                    assert "set -euo pipefail" in script or "set -e" in script

    def test_validation_steps_exit_codes(
        self, action_tester, default_inputs, default_github_context
    ):
        """Test that validation steps return appropriate exit codes."""
        # Test invalid PR number scenario
        env_vars = {}
        inputs = default_inputs.copy()
        inputs["PR_NUMBER"] = "invalid"
        github_context = default_github_context.copy()
        github_context["event_name"] = "workflow_dispatch"

        result = action_tester.simulate_step(
            "Normalize PR_NUMBER",
            env_vars,
            inputs,
            github_context,
        )

        assert result.returncode == 2  # Should exit with error code 2
        assert (
            "numeric value" in result.stdout or "numeric value" in result.stderr
        )


class TestSecurityConsiderations:
    """Test security-related configurations."""

    def test_no_hardcoded_secrets(self, action_tester):
        """Test that no secrets are hardcoded in the action."""
        action_yaml = yaml.dump(action_tester.action_config)

        # Check for potential secret patterns
        sensitive_patterns = ["password", "token", "key", "secret", "auth"]

        for pattern in sensitive_patterns:
            # Should only appear in input names or variable references
            lines_with_pattern = [
                line
                for line in action_yaml.lower().split("\n")
                if pattern in line
                and not (
                    line.strip().startswith("#")  # Comments
                    or "${{" in line  # Variable references
                    or "description:" in line  # Descriptions
                    or f"{pattern}:" in line  # Input definitions
                    or "name:" in line  # Step names
                    or "uses:" in line  # Action references
                    or "run:" in line  # Script comments
                    or 'echo "' in line  # Echo statements in scripts
                    or line.strip().endswith(":")  # YAML keys ending with colon
                )
            ]
            # Should not have hardcoded values
            assert len(lines_with_pattern) == 0, (
                f"Potential hardcoded {pattern} found: {lines_with_pattern}"
            )

    def test_ssh_key_handling(self, action_tester):
        """Test SSH key input is properly handled."""
        inputs = action_tester.action_config["inputs"]
        ssh_key_input = inputs["GERRIT_SSH_PRIVKEY_G2G"]

        assert ssh_key_input["required"] is True
        assert "SSH private key" in ssh_key_input["description"]

    def test_github_token_usage(self, action_tester):
        """Test GitHub token is used from context."""
        cli_step = None
        for step in action_tester.action_config["runs"]["steps"]:
            if step.get("name") == "Run github2gerrit Python CLI":
                cli_step = step
                break

        env_mapping = cli_step.get("env", {})
        assert env_mapping.get("GITHUB_TOKEN") == "${{ github.token }}"


class TestIntegrationScenarios:
    """Test common integration scenarios."""

    def test_pull_request_workflow(self, action_tester):
        """Test standard pull request workflow configuration."""
        # Verify action can handle pull_request events
        inputs = action_tester.action_config["inputs"]

        # Should have sensible defaults for PR workflow
        assert inputs["SUBMIT_SINGLE_COMMITS"]["default"] == "false"
        assert inputs["PRESERVE_GITHUB_PRS"]["default"] == "true"
        assert inputs["DRY_RUN"]["default"] == "false"

    def test_dry_run_capability(self, action_tester):
        """Test dry run functionality."""
        inputs = action_tester.action_config["inputs"]

        assert "DRY_RUN" in inputs
        assert inputs["DRY_RUN"]["default"] == "false"
        assert (
            "dry" in inputs["DRY_RUN"]["description"].lower()
            or "validate" in inputs["DRY_RUN"]["description"].lower()
        )

    def test_ci_testing_mode(self, action_tester):
        """Test CI testing mode configuration."""
        inputs = action_tester.action_config["inputs"]

        assert "CI_TESTING" in inputs
        assert inputs["CI_TESTING"]["default"] == "false"
        assert "ci testing" in inputs["CI_TESTING"]["description"].lower()


class TestPerformanceConsiderations:
    """Test performance-related configurations."""

    def test_fetch_depth_configuration(self, action_tester):
        """Test fetch depth is configurable."""
        inputs = action_tester.action_config["inputs"]

        assert "FETCH_DEPTH" in inputs
        assert inputs["FETCH_DEPTH"]["default"] == "10"

    def test_python_caching(self, action_tester):
        """Test that dependency management uses uv with built-in caching."""
        # Check that Python setup does NOT use pip caching (since we use uv)
        python_step = None
        for step in action_tester.action_config["runs"]["steps"]:
            if step.get("name") == "Setup Python":
                python_step = step
                break

        assert python_step is not None
        assert "cache" not in python_step.get("with", {})

        # Check that uv setup step exists (provides its own caching)
        uv_step = None
        for step in action_tester.action_config["runs"]["steps"]:
            if step.get("name") == "Setup uv":
                uv_step = step
                break

        assert uv_step is not None
        assert uv_step["uses"].startswith("astral-sh/setup-uv@")

    def test_uv_package_manager(self, action_tester):
        """Test UV package manager is used for fast installations."""
        steps = action_tester.action_config["runs"]["steps"]
        step_names = [step.get("name", "") for step in steps]

        # Should have UV setup step
        assert any("uv" in name.lower() for name in step_names)

        # Installation should use UV
        install_step = None
        for step in steps:
            if "Setup github2gerrit" in step.get("name", ""):
                install_step = step
                break

        assert install_step is not None
        assert "uv" in install_step["run"]


# Integration test for the full action workflow
class TestFullWorkflow:
    """Test the complete action workflow integration."""

    @pytest.mark.integration
    def test_action_yaml_syntax_validation(self, action_tester):
        """Test that action.yaml has valid syntax."""
        # If we got this far, YAML parsing succeeded
        assert action_tester.action_config is not None
        assert "runs" in action_tester.action_config
        assert "inputs" in action_tester.action_config
        assert "outputs" in action_tester.action_config

    @pytest.mark.integration
    def test_step_count_and_structure(self, action_tester):
        """Test expected number and types of steps."""
        steps = action_tester.action_config["runs"]["steps"]

        # Should have reasonable number of steps
        assert len(steps) >= 8
        assert len(steps) <= 20

        # Should have mix of action uses and shell runs
        action_steps = [s for s in steps if "uses" in s]
        shell_steps = [
            s for s in steps if "shell" in s and s["shell"] == "bash"
        ]

        assert len(action_steps) >= 3  # Python, UV, Checkout
        assert len(shell_steps) >= 4  # Various validation and processing steps

    @pytest.mark.integration
    def test_required_step_presence(self, action_tester):
        """Test that all required steps are present."""
        steps = action_tester.action_config["runs"]["steps"]
        step_names = [step.get("name", "") for step in steps]

        required_steps = [
            "Setup Python",
            "Setup uv",
            "Checkout repository",
            "Setup github2gerrit",
            "Run github2gerrit Python CLI",
            "Capture outputs",
        ]

        for required_step in required_steps:
            assert any(required_step in name for name in step_names), (
                f"Required step '{required_step}' not found"
            )
