# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for the centralized Gerrit URL builder.
"""

import os
from unittest.mock import patch

from github2gerrit.gerrit_urls import GerritUrlBuilder
from github2gerrit.gerrit_urls import create_gerrit_url_builder


class TestGerritUrlBuilder:
    """Test cases for GerritUrlBuilder class."""

    def test_init_with_explicit_base_path(self) -> None:
        """Test initialization with explicit base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "custom/path")
        assert builder.host == "gerrit.example.com"
        assert builder.base_path == "custom/path"
        assert builder.has_base_path is True

    def test_init_with_empty_base_path(self) -> None:
        """Test initialization with empty base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "")
        assert builder.host == "gerrit.example.com"
        assert builder.base_path == ""
        assert builder.has_base_path is False

    def test_init_with_none_base_path_no_env(self) -> None:
        """Test initialization with None base path and no environment variable."""
        with patch.dict(os.environ, {}, clear=True):
            builder = GerritUrlBuilder("gerrit.example.com", None)
            assert builder.host == "gerrit.example.com"
            assert builder.base_path == ""
            assert builder.has_base_path is False

    def test_init_with_none_base_path_with_env(self) -> None:
        """Test initialization with None base path but environment variable set."""
        with patch.dict(os.environ, {"GERRIT_HTTP_BASE_PATH": "  /r/  "}, clear=True):
            builder = GerritUrlBuilder("gerrit.example.com", None)
            assert builder.host == "gerrit.example.com"
            assert builder.base_path == "r"
            assert builder.has_base_path is True

    def test_base_path_normalization(self) -> None:
        """Test that base paths are properly normalized."""
        # Leading/trailing slashes and whitespace should be stripped
        test_cases = [
            ("  /r/  ", "r"),
            ("/custom/path/", "custom/path"),
            ("  gerrit-api  ", "gerrit-api"),
            ("", ""),
            ("   ", ""),
        ]

        for input_path, expected in test_cases:
            builder = GerritUrlBuilder("gerrit.example.com", input_path)
            assert builder.base_path == expected, f"Input '{input_path}' should normalize to '{expected}'"

    def test_host_normalization(self) -> None:
        """Test that hostnames are properly normalized."""
        builder = GerritUrlBuilder("  gerrit.example.com  ", "r")
        assert builder.host == "gerrit.example.com"

    def test_api_url_with_base_path(self) -> None:
        """Test API URL construction with base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        # Test various endpoint patterns
        assert builder.api_url() == "https://gerrit.example.com/r/"
        assert builder.api_url("") == "https://gerrit.example.com/r/"
        assert builder.api_url("/changes/") == "https://gerrit.example.com/r/changes/"
        assert builder.api_url("changes/") == "https://gerrit.example.com/r/changes/"
        assert builder.api_url("/accounts/self") == "https://gerrit.example.com/r/accounts/self"
        assert builder.api_url("dashboard/self") == "https://gerrit.example.com/r/dashboard/self"

    def test_api_url_without_base_path(self) -> None:
        """Test API URL construction without base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "")

        assert builder.api_url() == "https://gerrit.example.com/"
        assert builder.api_url("/changes/") == "https://gerrit.example.com/changes/"
        assert builder.api_url("accounts/self") == "https://gerrit.example.com/accounts/self"

    def test_api_url_with_override(self) -> None:
        """Test API URL construction with base path override."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        # Override with different base path
        assert builder.api_url("/changes/", "custom") == "https://gerrit.example.com/custom/changes/"

        # Override with empty base path
        assert builder.api_url("/changes/", "") == "https://gerrit.example.com/changes/"

    def test_web_url_with_base_path(self) -> None:
        """Test web URL construction with base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        assert builder.web_url() == "https://gerrit.example.com/r"
        assert builder.web_url("") == "https://gerrit.example.com/r"
        assert builder.web_url("dashboard") == "https://gerrit.example.com/r/dashboard"
        assert builder.web_url("/c/project/+/123") == "https://gerrit.example.com/r/c/project/+/123"
        assert builder.web_url("c/project/+/123") == "https://gerrit.example.com/r/c/project/+/123"

    def test_web_url_without_base_path(self) -> None:
        """Test web URL construction without base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "")

        assert builder.web_url() == "https://gerrit.example.com"
        assert builder.web_url("dashboard") == "https://gerrit.example.com/dashboard"
        assert builder.web_url("c/project/+/123") == "https://gerrit.example.com/c/project/+/123"

    def test_web_url_with_override(self) -> None:
        """Test web URL construction with base path override."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        # Override with different base path
        assert builder.web_url("dashboard", "custom") == "https://gerrit.example.com/custom/dashboard"

        # Override with empty base path
        assert builder.web_url("dashboard", "") == "https://gerrit.example.com/dashboard"

    def test_change_url_with_base_path(self) -> None:
        """Test change URL construction with base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        assert builder.change_url("myproject", 12345) == "https://gerrit.example.com/r/c/myproject/+/12345"

    def test_change_url_without_base_path(self) -> None:
        """Test change URL construction without base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "")

        assert builder.change_url("myproject", 12345) == "https://gerrit.example.com/c/myproject/+/12345"

    def test_change_url_with_special_characters(self) -> None:
        """Test change URL construction with special characters in project name."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        # Project names are passed through as-is (no URL encoding for backward compatibility)
        assert builder.change_url("org/my-project", 123) == "https://gerrit.example.com/r/c/org/my-project/+/123"
        assert builder.change_url("my project", 456) == "https://gerrit.example.com/r/c/my project/+/456"

    def test_change_url_with_override(self) -> None:
        """Test change URL construction with base path override."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        assert builder.change_url("project", 123, "custom") == "https://gerrit.example.com/custom/c/project/+/123"
        assert builder.change_url("project", 123, "") == "https://gerrit.example.com/c/project/+/123"

    def test_hook_url_with_base_path(self) -> None:
        """Test hook URL construction with base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        assert builder.hook_url("commit-msg") == "https://gerrit.example.com/r/tools/hooks/commit-msg"

    def test_hook_url_without_base_path(self) -> None:
        """Test hook URL construction without base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "")

        assert builder.hook_url("commit-msg") == "https://gerrit.example.com/tools/hooks/commit-msg"

    def test_hook_url_with_override(self) -> None:
        """Test hook URL construction with base path override."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        assert builder.hook_url("commit-msg", "custom") == "https://gerrit.example.com/custom/tools/hooks/commit-msg"
        assert builder.hook_url("commit-msg", "") == "https://gerrit.example.com/tools/hooks/commit-msg"

    def test_get_api_url_candidates_with_base_path(self) -> None:
        """Test API URL candidates generation with base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "custom")

        candidates = builder.get_api_url_candidates("/changes/")
        expected = [
            "https://gerrit.example.com/custom/changes/",
        ]
        assert candidates == expected

    def test_get_api_url_candidates_without_base_path(self) -> None:
        """Test API URL candidates generation without base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "")

        candidates = builder.get_api_url_candidates("/changes/")
        expected = [
            "https://gerrit.example.com/changes/",
        ]
        assert candidates == expected

    def test_get_api_url_candidates_with_r_base_path(self) -> None:
        """Test API URL candidates generation when base path is already 'r'."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        candidates = builder.get_api_url_candidates("/changes/")
        expected = [
            "https://gerrit.example.com/r/changes/",
        ]
        assert candidates == expected

    def test_get_hook_url_candidates_with_base_path(self) -> None:
        """Test hook URL candidates generation with base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "custom")

        candidates = builder.get_hook_url_candidates("commit-msg")
        expected = [
            "https://gerrit.example.com/custom/tools/hooks/commit-msg",
        ]
        assert candidates == expected

    def test_get_hook_url_candidates_without_base_path(self) -> None:
        """Test hook URL candidates generation without base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "")

        candidates = builder.get_hook_url_candidates("commit-msg")
        expected = [
            "https://gerrit.example.com/tools/hooks/commit-msg",
        ]
        assert candidates == expected

    def test_get_hook_url_candidates_with_r_base_path(self) -> None:
        """Test hook URL candidates generation when base path is already 'r'."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        candidates = builder.get_hook_url_candidates("commit-msg")
        expected = [
            "https://gerrit.example.com/r/tools/hooks/commit-msg",
        ]
        assert candidates == expected

    def test_get_web_base_path_with_base_path(self) -> None:
        """Test web base path generation with base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        assert builder.get_web_base_path() == "/r/"

    def test_get_web_base_path_without_base_path(self) -> None:
        """Test web base path generation without base path."""
        builder = GerritUrlBuilder("gerrit.example.com", "")

        assert builder.get_web_base_path() == "/"

    def test_get_web_base_path_with_override(self) -> None:
        """Test web base path generation with override."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        assert builder.get_web_base_path("custom") == "/custom/"
        assert builder.get_web_base_path("") == "/"

    def test_repr(self) -> None:
        """Test string representation."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")
        expected = "GerritUrlBuilder(host='gerrit.example.com', base_path='r')"
        assert repr(builder) == expected


def test_get_api_url_candidates_with_infra_base_path() -> None:
    """Test API URL candidates when base path is 'infra'."""
    builder = GerritUrlBuilder("gerrit.example.com", "infra")

    candidates = builder.get_api_url_candidates("/changes/")
    expected = [
        "https://gerrit.example.com/infra/changes/",
    ]
    assert candidates == expected


def test_get_hook_url_candidates_with_infra_base_path() -> None:
    """Test hook URL candidates when base path is 'infra'."""
    builder = GerritUrlBuilder("gerrit.example.com", "infra")

    candidates = builder.get_hook_url_candidates("commit-msg")
    expected = [
        "https://gerrit.example.com/infra/tools/hooks/commit-msg",
    ]
    assert candidates == expected


class TestFactoryFunction:
    """Test cases for the factory function."""

    def test_create_gerrit_url_builder_with_base_path(self) -> None:
        """Test factory function with explicit base path."""
        builder = create_gerrit_url_builder("gerrit.example.com", "r")

        assert isinstance(builder, GerritUrlBuilder)
        assert builder.host == "gerrit.example.com"
        assert builder.base_path == "r"

    def test_create_gerrit_url_builder_without_base_path(self) -> None:
        """Test factory function without base path."""
        with patch.dict(os.environ, {}, clear=True):
            builder = create_gerrit_url_builder("gerrit.example.com")

            assert isinstance(builder, GerritUrlBuilder)
            assert builder.host == "gerrit.example.com"
            assert builder.base_path == ""

    def test_create_gerrit_url_builder_with_env_base_path(self) -> None:
        """Test factory function with environment variable base path."""
        with patch.dict(os.environ, {"GERRIT_HTTP_BASE_PATH": "custom"}, clear=True):
            builder = create_gerrit_url_builder("gerrit.example.com")

            assert isinstance(builder, GerritUrlBuilder)
            assert builder.host == "gerrit.example.com"
            assert builder.base_path == "custom"


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_host(self) -> None:
        """Test initialization with empty host."""
        builder = GerritUrlBuilder("", "r")
        assert builder.host == ""
        # URLs should still be constructed (though invalid)
        assert builder.api_url() == "https:///r/"

    def test_host_with_protocol(self) -> None:
        """Test that host with protocol gets handled correctly."""
        # The builder expects just the hostname, but should handle protocol gracefully
        builder = GerritUrlBuilder("https://gerrit.example.com", "r")
        assert builder.host == "https://gerrit.example.com"
        # This will create an invalid URL, but that's expected behavior
        assert builder.api_url() == "https://https://gerrit.example.com/r/"

    def test_special_characters_in_endpoints(self) -> None:
        """Test handling of special characters in endpoints."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        # URLs should be properly joined even with special characters
        endpoint = "/changes/?q=status:open&o=CURRENT_REVISION"
        result = builder.api_url(endpoint)
        assert result == "https://gerrit.example.com/r/changes/?q=status:open&o=CURRENT_REVISION"

    def test_complex_base_paths(self) -> None:
        """Test handling of complex base paths."""
        builder = GerritUrlBuilder("gerrit.example.com", "gerrit/api/v1")

        assert builder.api_url("/changes/") == "https://gerrit.example.com/gerrit/api/v1/changes/"
        assert builder.web_url("dashboard") == "https://gerrit.example.com/gerrit/api/v1/dashboard"
        assert builder.get_web_base_path() == "/gerrit/api/v1/"

    def test_unicode_in_project_names(self) -> None:
        """Test handling of Unicode characters in project names."""
        builder = GerritUrlBuilder("gerrit.example.com", "r")

        # Unicode characters are passed through as-is (Gerrit handles encoding)
        result = builder.change_url("プロジェクト", 123)
        assert result == "https://gerrit.example.com/r/c/プロジェクト/+/123"
