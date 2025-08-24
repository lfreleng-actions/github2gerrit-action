# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for composite action output handling and formatting.

This module validates that outputs are correctly captured, formatted,
and made available for consumption by other workflows.
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


class TestActionOutputs:
    """Test action output definitions and processing."""

    def test_output_definitions(self, action_config):
        """Test that outputs are properly defined in action.yaml."""
        outputs = action_config.get("outputs", {})

        # Expected outputs
        expected_outputs = {
            "gerrit_change_request_url": {
                "description": "Gerrit change URL(s) (newline-separated if "
                "multiple)",
                "value": "${{ steps.capture-outputs.outputs."
                "gerrit_change_request_url }}",
            },
            "gerrit_change_request_num": {
                "description": "Gerrit change number(s) (newline-separated if "
                "multiple)",
                "value": "${{ steps.capture-outputs.outputs."
                "gerrit_change_request_num }}",
            },
            "gerrit_commit_sha": {
                "description": "Patch set commit sha(s) (newline-separated if "
                "multiple)",
                "value": "${{ steps.capture-outputs.outputs."
                "gerrit_commit_sha }}",
            },
        }

        assert len(outputs) == len(expected_outputs)

        for output_name, expected_config in expected_outputs.items():
            assert output_name in outputs
            assert (
                outputs[output_name]["description"]
                == expected_config["description"]
            )
            assert outputs[output_name]["value"] == expected_config["value"]

    def test_output_step_reference(self, action_config):
        """Test that outputs reference the correct step."""
        outputs = action_config.get("outputs", {})

        for output_name, output_config in outputs.items():
            value = output_config["value"]
            # Should reference the capture-outputs step
            assert "steps.capture-outputs.outputs" in value
            assert output_name in value

    def test_multiline_output_support(self, action_config):
        """Test that outputs support multiline content."""
        outputs = action_config.get("outputs", {})

        # All outputs should support newline-separated values
        for _output_name, output_config in outputs.items():
            description = output_config["description"]
            assert (
                "newline-separated" in description or "multiple" in description
            )


class TestOutputCaptureStep:
    """Test the output capture step implementation."""

    def test_capture_step_configuration(self, action_config):
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

    def test_capture_step_script(self, action_config):
        """Test output capture step script content."""
        steps = action_config["runs"]["steps"]
        capture_step = next(
            (
                step
                for step in steps
                if "Capture outputs" in step.get("name", "")
            ),
            None,
        )

        script = capture_step["run"]

        # Should use GitHub Actions multiline output format
        assert "<<G2G" in script
        assert "G2G" in script
        assert "GITHUB_OUTPUT" in script

        # Should capture all expected outputs
        expected_vars = [
            "GERRIT_CHANGE_REQUEST_URL",
            "GERRIT_CHANGE_REQUEST_NUM",
            "GERRIT_COMMIT_SHA",
        ]

        for var in expected_vars:
            assert var in script
            assert f"${{{var}:-}}" in script

    def test_output_capture_format(self):
        """Test output capture formatting logic."""
        # Script that mimics the capture step
        capture_script = textwrap.dedent("""
            set -euo pipefail

            # Set test values
            export GERRIT_CHANGE_REQUEST_URL="https://gerrit.example.com/c/123"
            export GERRIT_CHANGE_REQUEST_NUM="123"
            export GERRIT_COMMIT_SHA="abc123def456"

            # Capture outputs using GitHub Actions multiline format
            {
                echo "gerrit_change_request_url<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_URL:-}"
                echo "G2G"
                echo "gerrit_change_request_num<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_NUM:-}"
                echo "G2G"
                echo "gerrit_commit_sha<<G2G"
                printf '%s\\n' "${GERRIT_COMMIT_SHA:-}"
                echo "G2G"
            } >> "$GITHUB_OUTPUT"

            echo "Outputs captured successfully"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                ["bash", "-c", capture_script],
                env={**os.environ, "GITHUB_OUTPUT": output_file},
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode == 0
            assert "Outputs captured successfully" in result.stdout

            # Verify output file content
            with open(output_file) as f:
                output_content = f.read()

            expected_content = [
                "gerrit_change_request_url<<G2G",
                "https://gerrit.example.com/c/123",
                "G2G",
                "gerrit_change_request_num<<G2G",
                "123",
                "G2G",
                "gerrit_commit_sha<<G2G",
                "abc123def456",
                "G2G",
            ]

            for expected_line in expected_content:
                assert expected_line in output_content

        finally:
            os.unlink(output_file)

    def test_empty_output_handling(self):
        """Test handling of empty output values."""
        capture_script = textwrap.dedent("""
            set -euo pipefail

            # Don't set any values (simulate empty outputs)

            # Capture outputs
            {
                echo "gerrit_change_request_url<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_URL:-}"
                echo "G2G"
                echo "gerrit_change_request_num<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_NUM:-}"
                echo "G2G"
                echo "gerrit_commit_sha<<G2G"
                printf '%s\\n' "${GERRIT_COMMIT_SHA:-}"
                echo "G2G"
            } >> "$GITHUB_OUTPUT"

            echo "Empty outputs captured"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                ["bash", "-c", capture_script],
                env={**os.environ, "GITHUB_OUTPUT": output_file},
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode == 0
            assert "Empty outputs captured" in result.stdout

            # Verify output file has proper structure even with empty values
            with open(output_file) as f:
                output_content = f.read()

            # Should have delimiters even with empty content
            assert "gerrit_change_request_url<<G2G" in output_content
            assert (
                output_content.count("G2G") == 6
            )  # 3 outputs * 2 delimiters each

        finally:
            os.unlink(output_file)


class TestMultilineOutputHandling:
    """Test handling of multiline outputs."""

    def test_multiline_url_output(self):
        """Test multiline URL output handling."""
        capture_script = textwrap.dedent("""
            set -euo pipefail

            # Set multiline URL output
            export GERRIT_CHANGE_REQUEST_URL="https://gerrit.example.com/c/123
            https://gerrit.example.com/c/456
            https://gerrit.example.com/c/789"

            # Capture output
            {
                echo "gerrit_change_request_url<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_URL:-}"
                echo "G2G"
            } >> "$GITHUB_OUTPUT"

            echo "Multiline URL output captured"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                ["bash", "-c", capture_script],
                env={**os.environ, "GITHUB_OUTPUT": output_file},
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode == 0

            with open(output_file) as f:
                output_content = f.read()

            # Should contain all URLs
            assert "https://gerrit.example.com/c/123" in output_content
            assert "https://gerrit.example.com/c/456" in output_content
            assert "https://gerrit.example.com/c/789" in output_content

            # Should preserve line breaks within the delimiter
            lines = output_content.split("\n")
            start_idx = next(
                i
                for i, line in enumerate(lines)
                if "gerrit_change_request_url<<G2G" in line
            )
            end_idx = next(
                i
                for i, line in enumerate(lines[start_idx + 1 :], start_idx + 1)
                if line.strip() == "G2G"
            )

            url_lines = lines[start_idx + 1 : end_idx]
            assert len(url_lines) == 3

        finally:
            os.unlink(output_file)

    def test_multiline_change_numbers(self):
        """Test multiline change number output handling."""
        capture_script = textwrap.dedent("""
            set -euo pipefail

            # Set multiline change number output
            export GERRIT_CHANGE_REQUEST_NUM="123
            456
            789"

            # Capture output
            {
                echo "gerrit_change_request_num<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_NUM:-}"
                echo "G2G"
            } >> "$GITHUB_OUTPUT"

            echo "Multiline change numbers captured"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                ["bash", "-c", capture_script],
                env={**os.environ, "GITHUB_OUTPUT": output_file},
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode == 0

            with open(output_file) as f:
                output_content = f.read()

            # Should contain all change numbers
            assert "123" in output_content
            assert "456" in output_content
            assert "789" in output_content

        finally:
            os.unlink(output_file)

    def test_multiline_commit_shas(self):
        """Test multiline commit SHA output handling."""
        capture_script = textwrap.dedent("""
            set -euo pipefail

            # Set multiline commit SHA output
            export GERRIT_COMMIT_SHA="abc123def456
            ghi789jkl012
            mno345pqr678"

            # Capture output
            {
                echo "gerrit_commit_sha<<G2G"
                printf '%s\\n' "${GERRIT_COMMIT_SHA:-}"
                echo "G2G"
            } >> "$GITHUB_OUTPUT"

            echo "Multiline commit SHAs captured"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                ["bash", "-c", capture_script],
                env={**os.environ, "GITHUB_OUTPUT": output_file},
                text=True,
                capture_output=True,
                check=False,
            )

            assert result.returncode == 0

            with open(output_file) as f:
                output_content = f.read()

            # Should contain all SHAs
            assert "abc123def456" in output_content
            assert "ghi789jkl012" in output_content
            assert "mno345pqr678" in output_content

        finally:
            os.unlink(output_file)


class TestOutputErrorHandling:
    """Test error handling in output processing."""

    def test_capture_step_best_effort(self):
        """Test that capture step is best-effort and doesn't fail workflow."""
        # Script that simulates potential issues but continues
        capture_script = textwrap.dedent("""
            set -euo pipefail

            # Simulate CLI that might not set all outputs
            export GERRIT_CHANGE_REQUEST_URL="https://gerrit.example.com/c/123"
            # Don't set GERRIT_CHANGE_REQUEST_NUM or GERRIT_COMMIT_SHA

            # Capture outputs (best-effort)
            {
                echo "gerrit_change_request_url<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_URL:-}"
                echo "G2G"
                echo "gerrit_change_request_num<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_NUM:-}"
                echo "G2G"
                echo "gerrit_commit_sha<<G2G"
                printf '%s\\n' "${GERRIT_COMMIT_SHA:-}"
                echo "G2G"
            } >> "$GITHUB_OUTPUT"

            echo "Best-effort capture completed"
        """).strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                ["bash", "-c", capture_script],
                env={**os.environ, "GITHUB_OUTPUT": output_file},
                text=True,
                capture_output=True,
                check=False,
            )

            # Should succeed even with missing outputs
            assert result.returncode == 0
            assert "Best-effort capture completed" in result.stdout

            with open(output_file) as f:
                output_content = f.read()

            # Should have URL but empty other fields
            assert "https://gerrit.example.com/c/123" in output_content
            assert "gerrit_change_request_num<<G2G" in output_content
            assert "gerrit_commit_sha<<G2G" in output_content

        finally:
            os.unlink(output_file)

    def test_output_file_permissions_error(self):
        """Test handling of output file permission errors."""
        # Create a read-only directory
        with tempfile.TemporaryDirectory() as temp_dir:
            readonly_file = os.path.join(temp_dir, "readonly_output")

            # Create file and make it read-only
            with open(readonly_file, "w") as f:
                f.write("existing content")
            os.chmod(readonly_file, 0o444)  # Read-only

            capture_script = textwrap.dedent("""
                set -euo pipefail

                export
                GERRIT_CHANGE_REQUEST_URL="https://gerrit.example.com/c/123"

                # Try to write to read-only file (should fail)
                {
                    echo "gerrit_change_request_url<<G2G"
                    printf '%s\\n' "${GERRIT_CHANGE_REQUEST_URL:-}"
                    echo "G2G"
                } >> "$GITHUB_OUTPUT" || {
                    echo "Failed to write outputs (permission denied)" >&2
                    exit 1
                }
            """).strip()

            result = subprocess.run(
                ["bash", "-c", capture_script],
                env={**os.environ, "GITHUB_OUTPUT": readonly_file},
                text=True,
                capture_output=True,
                check=False,
            )

            # Should fail due to permission error
            assert result.returncode == 1
            assert (
                "permission denied" in result.stderr.lower()
                or "failed" in result.stderr.lower()
            )


class TestOutputIntegration:
    """Test output integration with workflows."""

    def test_output_consumption_by_subsequent_steps(self):
        """Test that outputs can be consumed by subsequent workflow steps."""
        # Simulate multi-step workflow where outputs are used
        workflow_script = textwrap.dedent("""
            set -euo pipefail

            # Step 1: Capture outputs
            export GERRIT_CHANGE_REQUEST_URL="https://gerrit.example.com/c/123"
            export GERRIT_CHANGE_REQUEST_NUM="123"
            export GERRIT_COMMIT_SHA="abc123def456"

            {
                echo "gerrit_change_request_url<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_URL:-}"
                echo "G2G"
                echo "gerrit_change_request_num<<G2G"
                printf '%s\\n' "${GERRIT_CHANGE_REQUEST_NUM:-}"
                echo "G2G"
                echo "gerrit_commit_sha<<G2G"
                printf '%s\\n' "${GERRIT_COMMIT_SHA:-}"
                echo "G2G"
            } >> "$GITHUB_OUTPUT"

            # Step 2: Simulate reading outputs (in real workflow this would be
            # another step)
            echo "=== Reading outputs ==="

            # Parse the multiline output format
            url_content=$(sed -n '/gerrit_change_request_url<<G2G/,/^G2G$/p' \
                "$GITHUB_OUTPUT" | sed '1d;$d')
            num_content=$(sed -n '/gerrit_change_request_num<<G2G/,/^G2G$/p' \
                "$GITHUB_OUTPUT" | sed '1d;$d')
            sha_content=$(sed -n '/gerrit_commit_sha<<G2G/,/^G2G$/p' \
                "$GITHUB_OUTPUT" | sed '1d;$d')

            echo "URL: $url_content"
            echo "Number: $num_content"
            echo "SHA: $sha_content"

            # Validate outputs
            [ "$url_content" = "https://gerrit.example.com/c/123" ]
            [ "$num_content" = "123" ]
            [ "$sha_content" = "abc123def456" ]

            echo "Output integration test passed"
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
            assert "Output integration test passed" in result.stdout
            assert "URL: https://gerrit.example.com/c/123" in result.stdout
            assert "Number: 123" in result.stdout
            assert "SHA: abc123def456" in result.stdout

        finally:
            os.unlink(output_file)

    def test_output_format_compatibility(self):
        """Test output format compatibility with GitHub Actions."""
        # Test that our output format is compatible with GitHub Actions parsing
        sample_output = textwrap.dedent("""
            gerrit_change_request_url<<G2G
            https://gerrit.example.com/c/123
            https://gerrit.example.com/c/456
            G2G
            gerrit_change_request_num<<G2G
            123
            456
            G2G
            gerrit_commit_sha<<G2G
            abc123def456
            ghi789jkl012
            G2G
        """).strip()

        # Simulate parsing like GitHub Actions would
        def parse_multiline_output(content: str, output_name: str) -> str:
            lines = content.split("\n")
            start_marker = f"{output_name}<<G2G"
            end_marker = "G2G"

            start_idx = None
            for i, line in enumerate(lines):
                if line.strip() == start_marker:
                    start_idx = i + 1
                    break

            if start_idx is None:
                return ""

            end_idx = None
            for i, line in enumerate(lines[start_idx:], start_idx):
                if line.strip() == end_marker:
                    end_idx = i
                    break

            if end_idx is None:
                return ""

            return "\n".join(lines[start_idx:end_idx])

        # Test parsing
        url_output = parse_multiline_output(
            sample_output, "gerrit_change_request_url"
        )
        num_output = parse_multiline_output(
            sample_output, "gerrit_change_request_num"
        )
        sha_output = parse_multiline_output(sample_output, "gerrit_commit_sha")

        # Verify parsed content
        assert (
            url_output
            == "https://gerrit.example.com/c/123\nhttps://gerrit.example.com/c/456"
        )
        assert num_output == "123\n456"
        assert sha_output == "abc123def456\nghi789jkl012"

        # Verify each output can be split into individual values
        urls = url_output.split("\n")
        numbers = num_output.split("\n")
        shas = sha_output.split("\n")

        assert len(urls) == 2
        assert len(numbers) == 2
        assert len(shas) == 2

        assert urls[0] == "https://gerrit.example.com/c/123"
        assert urls[1] == "https://gerrit.example.com/c/456"
        assert numbers[0] == "123"
        assert numbers[1] == "456"
        assert shas[0] == "abc123def456"
        assert shas[1] == "ghi789jkl012"
