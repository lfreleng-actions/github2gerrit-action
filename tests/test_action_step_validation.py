# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for composite action step validation and integration.

This module validates the step execution flow, dependencies, and proper
integration between action steps.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
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


class TestActionStepValidation:
    """Test action step validation and execution flow."""

    def test_step_execution_order(self, action_config):
        """Test that steps are in the correct execution order."""
        steps = action_config["runs"]["steps"]
        step_names = [step.get("name", "") for step in steps]

        # Define expected order dependencies
        order_requirements = [
            ("Setup Python", "Setup uv"),
            ("Setup Python", "Install required dependencies"),
            ("Setup uv", "Install required dependencies"),
            ("Checkout repository", "Run github2gerrit Python CLI"),
            ("Install required dependencies", "Run github2gerrit Python CLI"),
            ("Run github2gerrit Python CLI", "Capture outputs (best-effort)"),
        ]

        for before_step, after_step in order_requirements:
            before_idx = next(
                (i for i, name in enumerate(step_names) if before_step in name),
                -1,
            )
            after_idx = next(
                (i for i, name in enumerate(step_names) if after_step in name),
                -1,
            )

            assert before_idx != -1, f"Step '{before_step}' not found"
            assert after_idx != -1, f"Step '{after_step}' not found"
            assert before_idx < after_idx, (
                f"Step '{before_step}' must come before '{after_step}'"
            )

    def test_conditional_step_execution(self, action_config):
        """Test conditional step execution logic."""
        steps = action_config["runs"]["steps"]

        # Find conditional steps
        conditional_steps = [step for step in steps if "if" in step]

        # Should have several conditional steps for Issue ID lookup
        issue_id_steps = [
            step
            for step in conditional_steps
            if "Issue" in step.get("name", "")
            or "lookup" in step.get("name", "").lower()
        ]

        # At least some conditional steps should exist
        assert len(conditional_steps) >= 1, "Should have conditional steps"

        # Validate conditional logic for issue ID steps if they exist
        for step in issue_id_steps:
            if_condition = step["if"]
            assert "inputs.ISSUE_ID == ''" in if_condition
            assert "vars.ISSUE_ID_LOOKUP == 'true'" in if_condition

    def test_action_step_pinning(self, action_config):
        """Test that external actions are pinned to specific versions."""
        steps = action_config["runs"]["steps"]

        for step in steps:
            if "uses" in step:
                uses_value = step["uses"]

                # Skip local actions
                if uses_value.startswith("./"):
                    continue

                # External actions should be pinned
                assert "@" in uses_value, f"Action {uses_value} is not pinned"

                # Extract the version/SHA part
                action_ref = uses_value.split("@")[-1]

                # Should be a SHA (at least 7 characters, all hex), but allow
                # exceptions
                if (
                    action_ref not in ["main", "master"]
                    and "releng-reusable-workflows" not in uses_value
                ):
                    assert len(action_ref) >= 7, (
                        f"SHA too short in {uses_value}"
                    )
                    assert all(
                        c in "0123456789abcdef" for c in action_ref.lower()
                    ), f"Invalid SHA format in {uses_value}"

    def test_step_shell_configuration(self, action_config):
        """Test shell step configuration."""
        steps = action_config["runs"]["steps"]
        shell_steps = [step for step in steps if "shell" in step]

        # All shell steps should use bash
        for step in shell_steps:
            assert step["shell"] == "bash"

            # Should have proper error handling for complex scripts
            if "run" in step:
                script = step["run"]
                step_name = step.get("name", "")
                # Check for error handling in critical scripts
                critical_steps = [
                    "Install required dependencies",
                    "Normalize PR_NUMBER for workflow_dispatch",
                    "Extract PR number and validate context",
                    "Validate PR_NUMBER usage (non-dispatch)",
                    "Set IssueID in GITHUB_ENV",
                    "Run github2gerrit Python CLI",
                    "Capture outputs (best-effort)",
                ]
                is_critical = any(
                    critical in step_name for critical in critical_steps
                )
                if is_critical and len(script.split("\n")) > 2:
                    assert "set -euo pipefail" in script or "set -e" in script

    def test_python_setup_step(self, action_config):
        """Test Python setup step configuration."""
        steps = action_config["runs"]["steps"]
        python_step = next(
            (step for step in steps if step.get("name") == "Setup Python"), None
        )

        assert python_step is not None
        assert python_step["uses"].startswith("actions/setup-python@")

        with_config = python_step.get("with", {})
        assert "python-version-file" in with_config
        assert with_config["python-version-file"] == "pyproject.toml"
        assert with_config.get("cache") == "pip"

    def test_uv_setup_step(self, action_config):
        """Test UV setup step configuration."""
        steps = action_config["runs"]["steps"]
        uv_step = next(
            (step for step in steps if step.get("name") == "Setup uv"), None
        )

        assert uv_step is not None
        assert uv_step["uses"].startswith("astral-sh/setup-uv@")

    def test_checkout_step(self, action_config):
        """Test repository checkout step configuration."""
        steps = action_config["runs"]["steps"]
        checkout_step = next(
            (
                step
                for step in steps
                if step.get("name") == "Checkout repository"
            ),
            None,
        )

        assert checkout_step is not None
        assert checkout_step["uses"].startswith("actions/checkout@")

        with_config = checkout_step.get("with", {})
        assert "fetch-depth" in with_config
        assert with_config["fetch-depth"] == "${{ inputs.FETCH_DEPTH }}"
        assert "ref" in with_config
        assert "github.event.pull_request.head.sha" in with_config["ref"]

    def test_dependency_installation_step(self, action_config):
        """Test dependency installation step."""
        steps = action_config["runs"]["steps"]
        install_step = next(
            (
                step
                for step in steps
                if "Install required dependencies" in step.get("name", "")
            ),
            None,
        )

        assert install_step is not None
        assert install_step["shell"] == "bash"

        script = install_step["run"]
        assert "uv --version" in script
        assert "uv pip install --system ." in script

    def test_cli_execution_step(self, action_config):
        """Test CLI execution step configuration."""
        steps = action_config["runs"]["steps"]
        cli_step = next(
            (
                step
                for step in steps
                if step.get("name") == "Run github2gerrit Python CLI"
            ),
            None,
        )

        assert cli_step is not None
        assert cli_step["id"] == "run-cli"
        assert cli_step["shell"] == "bash"

        # Should have extensive environment configuration
        env_config = cli_step.get("env", {})
        assert len(env_config) > 20  # Many environment variables

        script = cli_step["run"]
        assert "python -m github2gerrit.cli" in script

    def test_output_capture_step(self, action_config):
        """Test output capture step configuration."""
        steps = action_config["runs"]["steps"]
        capture_step = next(
            (
                step
                for step in steps
                if "Capture outputs" in step.get("name", "")
            ),
            None,
        )

        assert capture_step is not None
        assert capture_step["id"] == "capture-outputs"
        assert capture_step["shell"] == "bash"

        script = capture_step["run"]
        # Should use multiline output format
        assert "<<G2G" in script
        assert "GITHUB_OUTPUT" in script
        assert "GERRIT_CHANGE_REQUEST_URL" in script
        assert "GERRIT_CHANGE_REQUEST_NUM" in script
        assert "GERRIT_COMMIT_SHA" in script


class TestStepIntegration:
    """Test integration between action steps."""

    def test_environment_variable_flow(self):
        """Test environment variable flow between steps."""
        # Script that mimics environment variable setting and reading
        script = textwrap.dedent("""
            set -euo pipefail

            # Simulate step that sets environment variables
            echo "Setting environment variables..."
            echo "PR_NUMBER=42" >> "$GITHUB_ENV"
            echo "SYNC_ALL_OPEN_PRS=false" >> "$GITHUB_ENV"

            # Simulate reading in subsequent step
            echo "Reading environment variables..."
            echo "PR_NUMBER: ${PR_NUMBER:-not_set}"
            echo "SYNC_ALL_OPEN_PRS: ${SYNC_ALL_OPEN_PRS:-not_set}"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False
        ) as f:
            env_file = f.name

        try:
            # Run the script with GITHUB_ENV pointing to our temp file
            result = subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "GITHUB_ENV": env_file},
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode == 0
            assert "Setting environment variables..." in result.stdout
            assert "Reading environment variables..." in result.stdout

            # Check that variables were written to the file
            with open(env_file) as f:
                env_content = f.read()
            assert "PR_NUMBER=42" in env_content
            assert "SYNC_ALL_OPEN_PRS=false" in env_content

        finally:
            os.unlink(env_file)

    def test_github_output_flow(self):
        """Test GitHub output flow between steps."""
        script = textwrap.dedent("""
            set -euo pipefail

            # Simulate setting outputs
            {
                echo "test_output<<EOF"
                echo "line1"
                echo "line2"
                echo "EOF"
            } >> "$GITHUB_OUTPUT"

            echo "Output set successfully"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "GITHUB_OUTPUT": output_file},
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode == 0
            assert "Output set successfully" in result.stdout

            # Check output file content
            with open(output_file) as f:
                output_content = f.read()
            assert "test_output<<EOF" in output_content
            assert "line1" in output_content
            assert "line2" in output_content
            assert "EOF" in output_content

        finally:
            os.unlink(output_file)

    def test_step_failure_propagation(self):
        """Test that step failures are properly propagated."""
        # Script that fails
        failing_script = textwrap.dedent("""
            set -euo pipefail
            echo "Before failure"
            exit 1
            echo "After failure"  # Should not execute
        """).strip()

        result = subprocess.run(
            ["bash", "-c", failing_script],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 1
        assert "Before failure" in result.stdout
        assert "After failure" not in result.stdout


class TestActionValidationScripts:
    """Test validation scripts embedded in action steps."""

    def test_pr_number_validation_script(self):
        """Test PR number validation logic."""
        # Extract validation logic from action
        validation_script = textwrap.dedent("""
            set -euo pipefail

            EVENT_NAME="$1"
            INPUT_PR_NUMBER="$2"

            # Validate PR_NUMBER usage (non-dispatch)
            if [ "${EVENT_NAME}" != "workflow_dispatch" ] && \
               [ -n "${INPUT_PR_NUMBER}" ] && \
               [ "${INPUT_PR_NUMBER}" != "0" ]; then
                echo "Error: PR_NUMBER only valid during workflow_dispatch " \
                     "events" >&2
                exit 2
            fi

            echo "Validation passed"
        """).strip()

        def run_validation(
            event_name: str, pr_number: str
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", "-c", validation_script, "--", event_name, pr_number],
                text=True,
                capture_output=True,
                check=False,
            )

        # Valid cases
        result = run_validation("workflow_dispatch", "42")
        assert result.returncode == 0

        result = run_validation("pull_request", "0")
        assert result.returncode == 0

        result = run_validation("pull_request", "")
        assert result.returncode == 0

        # Invalid case
        result = run_validation("pull_request", "42")
        assert result.returncode == 2
        assert "only valid during workflow_dispatch" in result.stderr

    def test_pr_number_normalization_script(self):
        """Test PR number normalization logic."""
        normalization_script = textwrap.dedent("""
            set -euo pipefail

            INPUT_PR_NUMBER="$1"

            # Normalize PR_NUMBER for workflow_dispatch
            pr_in="${INPUT_PR_NUMBER}"
            if [ -z "${pr_in}" ] || [ "${pr_in}" = "null" ]; then
                pr_in="0"
            fi
            if ! echo "${pr_in}" | grep -Eq '^[0-9]+$'; then
                echo "Error: PR_NUMBER must be a numeric value" >&2
                exit 2
            fi
            if [ "${pr_in}" = "0" ]; then
                echo "SYNC_ALL_OPEN_PRS=true"
            else
                echo "PR_NUMBER=${pr_in}"
            fi
        """).strip()

        def run_normalization(
            pr_number: str,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", "-c", normalization_script, "--", pr_number],
                text=True,
                capture_output=True,
                check=False,
            )

        # Test bulk mode
        result = run_normalization("0")
        assert result.returncode == 0
        assert "SYNC_ALL_OPEN_PRS=true" in result.stdout

        # Test specific PR
        result = run_normalization("42")
        assert result.returncode == 0
        assert "PR_NUMBER=42" in result.stdout

        # Test null/empty normalization
        result = run_normalization("")
        assert result.returncode == 0
        assert "SYNC_ALL_OPEN_PRS=true" in result.stdout

        result = run_normalization("null")
        assert result.returncode == 0
        assert "SYNC_ALL_OPEN_PRS=true" in result.stdout

        # Test invalid input
        result = run_normalization("abc")
        assert result.returncode == 2
        assert "must be a numeric value" in result.stderr

    def test_pr_context_extraction_script(self):
        """Test PR context extraction logic."""
        extraction_script = textwrap.dedent("""
            set -euo pipefail

            EVENT_NAME="$1"
            EVENT_PR_NUMBER="${2:-}"

            # Extract PR number and validate context
            if [ "${EVENT_NAME}" != "workflow_dispatch" ]; then
                # Honor PR_NUMBER if previously set
                if [ -z "${PR_NUMBER:-}" ]; then
                    PR_NUMBER="${EVENT_PR_NUMBER}"
                fi
                if [ -z "${PR_NUMBER}" ] || [ "${PR_NUMBER}" = "null" ]; then
                    echo "Error: PR_NUMBER is empty." >&2
                    echo "This action requires a valid pull request context" >&2
                    echo "Current event: ${EVENT_NAME}" >&2
                    exit 2
                fi
                echo "PR_NUMBER=${PR_NUMBER}"
            else
                echo "Skipping for workflow_dispatch"
            fi
        """).strip()

        def run_extraction(
            event_name: str, event_pr: str, existing_pr: str = ""
        ) -> subprocess.CompletedProcess[str]:
            env = os.environ.copy()
            if existing_pr:
                env["PR_NUMBER"] = existing_pr

            return subprocess.run(
                ["bash", "-c", extraction_script, "--", event_name, event_pr],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        # Test successful extraction
        result = run_extraction("pull_request", "123")
        assert result.returncode == 0
        assert "PR_NUMBER=123" in result.stdout

        # Test honors existing PR_NUMBER
        result = run_extraction("pull_request", "456", "123")
        assert result.returncode == 0
        assert "PR_NUMBER=123" in result.stdout

        # Test missing context error
        result = run_extraction("pull_request", "")
        assert result.returncode == 2
        assert "requires a valid pull request context" in result.stderr
        assert "Current event: pull_request" in result.stderr

        # Test workflow_dispatch skip
        result = run_extraction("workflow_dispatch", "")
        assert result.returncode == 0
        assert "Skipping for workflow_dispatch" in result.stdout


class TestActionErrorHandling:
    """Test error handling in action steps."""

    def test_script_error_handling(self):
        """Test that scripts properly handle errors."""
        # Test script with proper error handling
        good_script = textwrap.dedent("""
            set -euo pipefail

            echo "Step 1: Success"
            true  # This succeeds
            echo "Step 2: Success"
        """).strip()

        result = subprocess.run(
            ["bash", "-c", good_script],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        assert "Step 1: Success" in result.stdout
        assert "Step 2: Success" in result.stdout

        # Test script that fails fast
        bad_script = textwrap.dedent("""
            set -euo pipefail

            echo "Step 1: Success"
            false  # This fails
            echo "Step 2: Should not execute"
        """).strip()

        result = subprocess.run(
            ["bash", "-c", bad_script],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 1
        assert "Step 1: Success" in result.stdout
        assert "Step 2: Should not execute" not in result.stdout

    def test_undefined_variable_handling(self):
        """Test handling of undefined variables."""
        # Script that tries to use undefined variable
        script = textwrap.dedent("""
            set -euo pipefail

            echo "Using undefined variable: $UNDEFINED_VAR"
        """).strip()

        result = subprocess.run(
            ["bash", "-c", script],
            text=True,
            capture_output=True,
            check=False,
        )

        # Should fail due to 'set -u'
        assert result.returncode != 0

    def test_pipeline_failure_handling(self):
        """Test handling of pipeline failures."""
        # Script with failing pipeline
        script = textwrap.dedent("""
            set -euo pipefail

            false | echo "This should still fail"
        """).strip()

        result = subprocess.run(
            ["bash", "-c", script],
            text=True,
            capture_output=True,
            check=False,
        )

        # Should fail due to 'set -o pipefail'
        assert result.returncode != 0


class TestActionIntegrationScenarios:
    """Test complete action integration scenarios."""

    def test_full_workflow_simulation(self):
        """Test simulation of full workflow execution."""
        # Simulate the key steps of the action
        workflow_script = textwrap.dedent("""
            set -euo pipefail

            echo "=== Step 1: Setup Python ==="
            python3 --version

            echo "=== Step 2: Setup UV ==="
            # Simulate UV check (skip actual installation)
            echo "UV would be installed here"

            echo "=== Step 3: Checkout Repository ==="
            # Simulate checkout
            echo "Repository would be checked out here"

            echo "=== Step 4: Install Dependencies ==="
            # Simulate dependency installation
            echo "Dependencies would be installed here"

            echo "=== Step 5: Validate PR Number ==="
            EVENT_NAME="pull_request"
            PR_NUMBER="123"
            if [ -n "${PR_NUMBER}" ]; then
                echo "PR_NUMBER=${PR_NUMBER}"
            fi

            echo "=== Step 6: Run CLI ==="
            # Simulate CLI execution (dry run)
            echo "CLI would execute here"
            export GERRIT_CHANGE_REQUEST_URL="https://gerrit.example.com/c/123"
            export GERRIT_CHANGE_REQUEST_NUM="123"
            export GERRIT_COMMIT_SHA="abc123"

            echo "=== Step 7: Capture Outputs ==="
            # Simulate output capture
            {
                echo "gerrit_change_request_url<<G2G"
                echo "${GERRIT_CHANGE_REQUEST_URL}"
                echo "G2G"
                echo "gerrit_change_request_num<<G2G"
                echo "${GERRIT_CHANGE_REQUEST_NUM}"
                echo "G2G"
                echo "gerrit_commit_sha<<G2G"
                echo "${GERRIT_COMMIT_SHA}"
                echo "G2G"
            } >> "$GITHUB_OUTPUT"

            echo "Workflow completed successfully"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                ["bash", "-c", workflow_script],
                env={**os.environ, "GITHUB_OUTPUT": output_file},
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode == 0
            assert "Workflow completed successfully" in result.stdout
            assert "PR_NUMBER=123" in result.stdout

            # Verify outputs were written
            with open(output_file) as f:
                output_content = f.read()
            assert "gerrit_change_request_url<<G2G" in output_content
            assert "https://gerrit.example.com/c/123" in output_content

        finally:
            os.unlink(output_file)

    def test_error_scenario_simulation(self):
        """Test error scenario in workflow execution."""
        error_script = textwrap.dedent("""
            set -euo pipefail

            echo "=== Starting workflow ==="

            echo "=== Step 1: Success ==="
            echo "This step succeeds"

            echo "=== Step 2: Failure ==="
            echo "This step will fail" >&2
            exit 1

            echo "=== Step 3: Should not execute ==="
            echo "This should never be seen"
        """).strip()

        result = subprocess.run(
            ["bash", "-c", error_script],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 1
        assert "Starting workflow" in result.stdout
        assert "This step succeeds" in result.stdout
        assert "This step will fail" in result.stderr
        assert "Should not execute" not in result.stdout
