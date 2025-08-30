# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for commit normalization functionality."""

import tempfile
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from github2gerrit.commit_normalization import CommitNormalizer
from github2gerrit.commit_normalization import ConventionalCommitPreferences
from github2gerrit.commit_normalization import normalize_commit_title
from github2gerrit.commit_normalization import should_normalize_commit


class TestCommitNormalizer:
    """Test cases for CommitNormalizer class."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.normalizer = CommitNormalizer(self.temp_dir)

    def test_is_conventional_commit_positive_cases(self) -> None:
        """Test detection of existing conventional commits."""
        test_cases = [
            "feat: add new feature",
            "fix: resolve bug issue",
            "docs: update readme",
            "chore(deps): bump package version",
            "feat!: breaking change",
            "Fix: resolve issue",  # Different capitalization
            "CHORE: update dependencies",
        ]

        for title in test_cases:
            assert self.normalizer._is_conventional_commit(title), f"Should detect '{title}' as conventional"

    def test_is_conventional_commit_negative_cases(self) -> None:
        """Test detection of non-conventional commits."""
        test_cases = [
            "Bump package from 1.0 to 2.0",
            "Update dependencies",
            "Add new feature",
            "pre-commit autofixes",
            "Regular commit message",
            "",
        ]

        for title in test_cases:
            assert not self.normalizer._is_conventional_commit(title), f"Should not detect '{title}' as conventional"

    def test_is_automation_pr_by_author(self) -> None:
        """Test detection of automation PRs by author name."""
        automation_authors = [
            "dependabot[bot]",
            "dependabot-preview[bot]",
            "pre-commit-ci[bot]",
            "renovate[bot]",
            "greenkeeper[bot]",
        ]

        for author in automation_authors:
            assert self.normalizer._is_automation_pr("Some title", author), f"Should detect {author} as automation"

    def test_is_automation_pr_by_title_pattern(self) -> None:
        """Test detection of automation PRs by title patterns."""
        test_cases = [
            ("bump package from 1.0 to 2.0", "human"),
            ("update something from x to y", "human"),
            ("upgrade dependency from old to new", "human"),
            ("pre-commit autofix", "human"),
            ("pre-commit autoupdate", "human"),
        ]

        for title, author in test_cases:
            assert self.normalizer._is_automation_pr(title, author), f"Should detect '{title}' as automation pattern"

    def test_should_normalize_already_conventional(self) -> None:
        """Test that conventional commits are not normalized."""
        title = "feat: add new feature"
        author = "dependabot[bot]"

        assert not self.normalizer.should_normalize(title, author)

    def test_should_normalize_automation_pr(self) -> None:
        """Test that automation PRs should be normalized."""
        title = "Bump package from 1.0 to 2.0"
        author = "dependabot[bot]"

        assert self.normalizer.should_normalize(title, author)

    def test_should_normalize_human_pr(self) -> None:
        """Test that human PRs are not normalized by default."""
        title = "Add new feature"
        author = "human-developer"

        assert not self.normalizer.should_normalize(title, author)

    def test_determine_commit_type_dependabot(self) -> None:
        """Test commit type determination for dependabot PRs."""
        test_cases = [
            ("Bump package from 1.0 to 2.0", "dependabot[bot]", "chore"),
            ("Update dependencies", "dependabot[bot]", "chore"),
        ]

        for title, author, expected_type in test_cases:
            result = self.normalizer._determine_commit_type(title, author)
            assert result == expected_type, f"Expected {expected_type} for '{title}' by {author}"

    def test_determine_commit_type_precommit(self) -> None:
        """Test commit type determination for pre-commit PRs."""
        test_cases = [
            ("pre-commit autofix", "pre-commit-ci[bot]", "chore"),
            ("pre-commit autoupdate", "pre-commit-ci[bot]", "chore"),
        ]

        for title, author, expected_type in test_cases:
            result = self.normalizer._determine_commit_type(title, author)
            assert result == expected_type, f"Expected {expected_type} for '{title}' by {author}"

    def test_determine_commit_type_patterns(self) -> None:
        """Test commit type determination by content patterns."""
        test_cases = [
            ("fix security vulnerability", "human", "fix"),
            ("add new documentation", "human", "docs"),
            ("update build dependencies", "human", "build"),
            ("update CI workflow", "human", "ci"),
            ("implement new feature", "human", "feat"),
        ]

        for title, author, expected_type in test_cases:
            result = self.normalizer._determine_commit_type(title, author)
            assert result == expected_type, f"Expected {expected_type} for '{title}'"

    def test_clean_title_dependabot_patterns(self) -> None:
        """Test title cleaning for dependabot patterns."""
        test_cases = [
            (
                "Bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1",
                "bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1",
            ),
            ("Update package requirement from >=1.0 to >=2.0", "bump package from >=1.0 to >=2.0"),
        ]

        for original, expected in test_cases:
            result = self.normalizer._clean_title(original)
            assert result == expected, f"Expected '{expected}' for '{original}'"

    def test_clean_title_markdown_removal(self) -> None:
        """Test title cleaning removes markdown."""
        test_cases = [
            ("[text](url) some title", "text some title"),
            ("Title with trailing...", "title with trailing"),
            ("Title with *bold* and _italic_", "title with bold and italic"),
        ]

        for original, expected in test_cases:
            result = self.normalizer._clean_title(original)
            assert result == expected, f"Expected '{expected}' for '{original}'"

    def test_format_conventional_commit_lowercase(self) -> None:
        """Test conventional commit formatting with lowercase preference."""
        self.normalizer.preferences.capitalization = "lower"

        result = self.normalizer._format_conventional_commit("chore", "update package")
        assert result == "chore: update package"

    def test_format_conventional_commit_title_case(self) -> None:
        """Test conventional commit formatting with title case preference."""
        self.normalizer.preferences.capitalization = "title"

        result = self.normalizer._format_conventional_commit("chore", "update package")
        assert result == "Chore: update package"

    def test_format_conventional_commit_uppercase(self) -> None:
        """Test conventional commit formatting with uppercase preference."""
        self.normalizer.preferences.capitalization = "upper"

        result = self.normalizer._format_conventional_commit("chore", "update package")
        assert result == "CHORE: update package"

    def test_format_conventional_commit_with_scope(self) -> None:
        """Test conventional commit formatting with scope."""
        self.normalizer.preferences.use_scope = True
        self.normalizer.preferences.dependency_scope = "deps"
        self.normalizer.preferences.dependency_type = "chore"

        result = self.normalizer._format_conventional_commit("chore", "update package")
        assert result == "chore(deps): update package"

    def test_check_precommit_config(self) -> None:
        """Test preferences detection from .pre-commit-config.yaml."""
        # Create actual config file with content
        config_file = self.temp_dir / ".pre-commit-config.yaml"
        config_content = """
ci:
  autofix_commit_msg: |
    Chore: pre-commit autofixes

    Signed-off-by: pre-commit-ci[bot] <pre-commit-ci@users.noreply.github.com>
  autoupdate_commit_msg: |
    Chore: pre-commit autoupdate

    Signed-off-by: pre-commit-ci[bot] <pre-commit-ci@users.noreply.github.com>
"""
        config_file.write_text(config_content)

        self.normalizer._check_precommit_config()

        # Should detect title case from "Chore:"
        assert self.normalizer.preferences.capitalization == "title"
        assert self.normalizer.preferences.automation_type == "chore"

    def test_check_release_drafter_config(self) -> None:
        """Test preferences detection from .github/release-drafter.yml."""
        # Create actual directory and file with content
        github_dir = self.temp_dir / ".github"
        github_dir.mkdir()
        config_file = github_dir / "release-drafter.yml"
        config_content = """
autolabeler:
  - label: "chore"
    title:
      - "/chore:/i"
  - label: "feature"
    title:
      - "/feat:/i"
  - label: "bug"
    title:
      - "/fix:/i"
"""
        config_file.write_text(config_content)

        self.normalizer._check_release_drafter_config()

        # Should detect conventional commit types
        expected_types = {"chore", "feat", "fix"}
        detected_types = set(self.normalizer.preferences.preferred_types.keys())
        assert expected_types.issubset(detected_types)

    @patch("subprocess.run")
    def test_analyze_git_history(self, mock_run: Mock) -> None:
        """Test git history analysis for conventional commit patterns."""
        # Mock git log output with conventional commits
        mock_run.return_value = Mock(
            returncode=0, stdout="feat: add new feature\nfix: resolve bug\nchore: update deps\nregular commit\n"
        )

        self.normalizer._analyze_git_history()

        # Should detect lowercase preference and conventional types
        assert self.normalizer.preferences.capitalization == "lower"
        expected_types = {"feat", "fix", "chore"}
        detected_types = set(self.normalizer.preferences.preferred_types.keys())
        assert expected_types.issubset(detected_types)

    def test_full_normalization_dependabot(self) -> None:
        """Test full normalization flow for dependabot PR."""
        title = "Bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1"
        author = "dependabot[bot]"

        # Force preferences detection to have run
        self.normalizer._detected_preferences = True

        result = self.normalizer.normalize_commit_title(title, author)
        expected = "chore: bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1"
        assert result == expected

    def test_full_normalization_precommit(self) -> None:
        """Test full normalization flow for pre-commit PR."""
        title = "pre-commit autofix"
        author = "pre-commit-ci[bot]"

        # Force preferences detection to have run
        self.normalizer._detected_preferences = True

        result = self.normalizer.normalize_commit_title(title, author)
        expected = "chore: pre-commit autofix"
        assert result == expected

    def test_full_normalization_already_conventional(self) -> None:
        """Test that conventional commits pass through unchanged."""
        title = "feat: add new feature"
        author = "dependabot[bot]"

        result = self.normalizer.normalize_commit_title(title, author)
        assert result == title  # Should be unchanged


class TestPublicFunctions:
    """Test cases for public module functions."""

    def test_normalize_commit_title_function(self) -> None:
        """Test the public normalize_commit_title function."""
        title = "Bump package from 1.0 to 2.0"
        author = "dependabot[bot]"

        result = normalize_commit_title(title, author)
        # Should start with some form of chore (could be "chore:", "Chore:", etc.)
        assert result.lower().startswith("chore:")
        assert "bump package from 1.0 to 2.0" in result

    def test_should_normalize_commit_function(self) -> None:
        """Test the public should_normalize_commit function."""
        # Should normalize automation PR
        assert should_normalize_commit("Bump package from 1.0 to 2.0", "dependabot[bot]")

        # Should not normalize conventional commit
        assert not should_normalize_commit("feat: add new feature", "dependabot[bot]")

        # Should not normalize human PR
        assert not should_normalize_commit("Add new feature", "human-developer")


class TestConventionalCommitPreferences:
    """Test cases for ConventionalCommitPreferences dataclass."""

    def test_default_preferences(self) -> None:
        """Test default preference values."""
        prefs = ConventionalCommitPreferences()

        assert prefs.capitalization == "lower"
        assert prefs.dependency_type == "chore"
        assert prefs.automation_type == "chore"
        assert prefs.use_scope is False
        assert prefs.dependency_scope == "deps"
        assert prefs.preferred_types == {}
