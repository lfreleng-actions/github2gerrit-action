# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for the utils module containing common utilities."""

import logging
import os
import tempfile

from _pytest.logging import LogCaptureFixture
from _pytest.monkeypatch import MonkeyPatch

from github2gerrit.utils import append_github_output
from github2gerrit.utils import env_bool
from github2gerrit.utils import env_str
from github2gerrit.utils import is_verbose_mode
from github2gerrit.utils import log_exception_conditionally
from github2gerrit.utils import parse_bool_env


class TestEnvBool:
    """Test the env_bool function."""

    def test_env_bool_true_values(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_bool correctly identifies true values."""
        true_values = ["1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"]
        for value in true_values:
            monkeypatch.setenv("TEST_VAR", value)
            assert env_bool("TEST_VAR") is True

    def test_env_bool_false_values(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_bool correctly identifies false values."""
        false_values = ["0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF", ""]
        for value in false_values:
            monkeypatch.setenv("TEST_VAR", value)
            assert env_bool("TEST_VAR") is False

    def test_env_bool_unset_default_false(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_bool returns False for unset variables by default."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env_bool("TEST_VAR") is False

    def test_env_bool_unset_custom_default(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_bool returns custom default for unset variables."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env_bool("TEST_VAR", default=True) is True

    def test_env_bool_whitespace_handling(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_bool handles whitespace correctly."""
        monkeypatch.setenv("TEST_VAR", "  true  ")
        assert env_bool("TEST_VAR") is True

        monkeypatch.setenv("TEST_VAR", "\tfalse\n")
        assert env_bool("TEST_VAR") is False


class TestEnvStr:
    """Test the env_str function."""

    def test_env_str_existing_value(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_str returns existing environment variable value."""
        monkeypatch.setenv("TEST_VAR", "test_value")
        assert env_str("TEST_VAR") == "test_value"

    def test_env_str_empty_value(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_str returns empty string when variable is empty."""
        monkeypatch.setenv("TEST_VAR", "")
        assert env_str("TEST_VAR") == ""

    def test_env_str_unset_default_empty(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_str returns empty string for unset variables by default."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env_str("TEST_VAR") == ""

    def test_env_str_unset_custom_default(self, monkeypatch: MonkeyPatch) -> None:
        """Test that env_str returns custom default for unset variables."""
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert env_str("TEST_VAR", default="custom_default") == "custom_default"


class TestParseBoolEnv:
    """Test the parse_bool_env function."""

    def test_parse_bool_env_true_values(self) -> None:
        """Test that parse_bool_env correctly identifies true values."""
        true_values = ["1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"]
        for value in true_values:
            assert parse_bool_env(value) is True

    def test_parse_bool_env_false_values(self) -> None:
        """Test that parse_bool_env correctly identifies false values."""
        false_values = ["0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF", ""]
        for value in false_values:
            assert parse_bool_env(value) is False

    def test_parse_bool_env_none(self) -> None:
        """Test that parse_bool_env returns False for None."""
        assert parse_bool_env(None) is False

    def test_parse_bool_env_whitespace_handling(self) -> None:
        """Test that parse_bool_env handles whitespace correctly."""
        assert parse_bool_env("  true  ") is True
        assert parse_bool_env("\tfalse\n") is False


class TestIsVerboseMode:
    """Test the is_verbose_mode function."""

    def test_is_verbose_mode_true_values(self, monkeypatch: MonkeyPatch) -> None:
        """Test that is_verbose_mode correctly identifies verbose mode."""
        true_values = ["true", "1", "yes"]
        for value in true_values:
            monkeypatch.setenv("G2G_VERBOSE", value)
            assert is_verbose_mode() is True

    def test_is_verbose_mode_false_values(self, monkeypatch: MonkeyPatch) -> None:
        """Test that is_verbose_mode correctly identifies non-verbose mode."""
        false_values = ["false", "0", "no", "off", ""]
        for value in false_values:
            monkeypatch.setenv("G2G_VERBOSE", value)
            assert is_verbose_mode() is False

    def test_is_verbose_mode_unset(self, monkeypatch: MonkeyPatch) -> None:
        """Test that is_verbose_mode returns False when G2G_VERBOSE is unset."""
        monkeypatch.delenv("G2G_VERBOSE", raising=False)
        assert is_verbose_mode() is False

    def test_is_verbose_mode_case_sensitivity(self, monkeypatch: MonkeyPatch) -> None:
        """Test that is_verbose_mode is case insensitive."""
        monkeypatch.setenv("G2G_VERBOSE", "TRUE")
        assert is_verbose_mode() is True

        monkeypatch.setenv("G2G_VERBOSE", "True")
        assert is_verbose_mode() is True


class TestLogExceptionConditionally:
    """Test the log_exception_conditionally function."""

    def test_log_exception_conditionally_verbose_mode(
        self, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
    ) -> None:
        """Test that log_exception_conditionally logs exception in verbose mode."""
        monkeypatch.setenv("G2G_VERBOSE", "true")

        logger = logging.getLogger("test")

        def _raise_test_error() -> None:
            msg = "Test error"
            raise ValueError(msg)

        with caplog.at_level(logging.DEBUG):
            try:
                _raise_test_error()
            except ValueError:
                log_exception_conditionally(logger, "Test message: %s", "arg1")

        # Should have logged with exception traceback
        assert "Test message: arg1" in caplog.text
        assert "ValueError: Test error" in caplog.text

    def test_log_exception_conditionally_non_verbose_mode(
        self, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
    ) -> None:
        """Test that log_exception_conditionally logs error without traceback in non-verbose mode."""
        monkeypatch.setenv("G2G_VERBOSE", "false")

        logger = logging.getLogger("test")

        def _raise_test_error() -> None:
            msg = "Test error"
            raise ValueError(msg)

        with caplog.at_level(logging.ERROR):
            try:
                _raise_test_error()
            except ValueError:
                log_exception_conditionally(logger, "Test message: %s", "arg1")

        # Should have logged error but without full traceback
        assert "Test message: arg1" in caplog.text
        # In non-verbose mode, the full traceback should not be present
        assert caplog.records[0].levelname == "ERROR"


class TestAppendGithubOutput:
    """Test the append_github_output function."""

    def test_append_github_output_no_github_output_env(self, monkeypatch: MonkeyPatch) -> None:
        """Test that append_github_output does nothing when GITHUB_OUTPUT is not set."""
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        # Should not raise any exceptions
        append_github_output({"key": "value"})

    def test_append_github_output_single_line_values(self, monkeypatch: MonkeyPatch) -> None:
        """Test append_github_output with single-line values."""
        with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmp_file:
            monkeypatch.setenv("GITHUB_OUTPUT", tmp_file.name)

            outputs = {
                "key1": "value1",
                "key2": "value2",
                "empty_key": "",  # Should be skipped
                "key3": "value3",
            }

            append_github_output(outputs)

            # Read the file content
            with open(tmp_file.name) as f:
                content = f.read()

            expected_lines = [
                "key1=value1",
                "key2=value2",
                "key3=value3",
            ]

            for line in expected_lines:
                assert line in content

            # Empty values should not be written
            assert "empty_key" not in content

            # Clean up
            os.unlink(tmp_file.name)

    def test_append_github_output_multiline_values_github_actions(self, monkeypatch: MonkeyPatch) -> None:
        """Test append_github_output with multiline values in GitHub Actions."""
        with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmp_file:
            monkeypatch.setenv("GITHUB_OUTPUT", tmp_file.name)
            monkeypatch.setenv("GITHUB_ACTIONS", "true")

            outputs = {
                "multiline_key": "line1\nline2\nline3",
                "single_key": "single_value",
            }

            append_github_output(outputs)

            # Read the file content
            with open(tmp_file.name) as f:
                content = f.read()

            # Check heredoc format for multiline values
            assert "multiline_key<<G2G" in content
            assert "line1\nline2\nline3" in content
            assert "G2G" in content

            # Single line values should use normal format
            assert "single_key=single_value" in content

            # Clean up
            os.unlink(tmp_file.name)

    def test_append_github_output_multiline_values_non_github_actions(self, monkeypatch: MonkeyPatch) -> None:
        """Test multiline values in non-GitHub Actions environment."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_file:
            monkeypatch.setenv("GITHUB_OUTPUT", tmp_file.name)
            monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

            outputs = {
                "multiline_key": "line1\nline2\nline3",
            }

            append_github_output(outputs)

            # Read the file content
            with open(tmp_file.name) as f:
                content = f.read()

            # Should use heredoc format for multiline values regardless of GITHUB_ACTIONS
            assert "multiline_key<<G2G" in content
            assert "line1\nline2\nline3" in content
            assert "G2G" in content

            # Clean up
            os.unlink(tmp_file.name)

    def test_append_github_output_file_error(self, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture) -> None:
        """Test append_github_output handles file errors gracefully."""
        # Set GITHUB_OUTPUT to a non-existent directory
        monkeypatch.setenv("GITHUB_OUTPUT", "/non/existent/path/output.txt")

        outputs = {"key": "value"}

        with caplog.at_level(logging.DEBUG):
            # Should not raise an exception
            append_github_output(outputs)

        # Should log the error
        assert "Failed to write GITHUB_OUTPUT" in caplog.text

    def test_append_github_output_appends_to_existing_file(self, monkeypatch: MonkeyPatch) -> None:
        """Test that append_github_output appends to existing files."""
        with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmp_file:
            # Write some initial content
            tmp_file.write("existing_key=existing_value\n")
            tmp_file.flush()

            monkeypatch.setenv("GITHUB_OUTPUT", tmp_file.name)

            outputs = {"new_key": "new_value"}
            append_github_output(outputs)

            # Read the full file content
            with open(tmp_file.name) as f:
                content = f.read()

            # Should contain both existing and new content
            assert "existing_key=existing_value" in content
            assert "new_key=new_value" in content

            # Clean up
            os.unlink(tmp_file.name)
