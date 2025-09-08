# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
#
# Commit Normalization Module
#
# This module provides functionality to normalize commit messages to follow
# conventional commit standards. It analyzes repository configuration and
# history to determine the preferred commit message format and applies
# appropriate transformations to automated PR titles.
#
# Key features:
# - Detects conventional commit preferences from .pre-commit-config.yaml
# - Analyzes .github/release-drafter.yml for commit type patterns
# - Examines git history for existing conventional commit patterns
# - Transforms dependabot/automation PR titles to conventional format
# - Respects repository-specific capitalization preferences

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import yaml


log = logging.getLogger("github2gerrit.commit_normalization")

# Conventional commit types in order of preference
CONVENTIONAL_COMMIT_TYPES = [
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
]

# Patterns for detecting different types of changes
CHANGE_TYPE_PATTERNS = {
    "docs": [
        r"update.*documentation",
        r"add.*documentation",
        r"improve.*docs",
        r"update.*readme",
        r"add.*readme",
        r"add\s+new\s+documentation",
    ],
    "feat": [
        r"add\s+new(?!\s+documentation)",
        r"implement",
        r"introduce",
        r"create\s+new",
    ],
    "fix": [
        r"fix",
        r"resolve",
        r"correct",
        r"repair",
        r"patch",
    ],
    "build": [
        r"update.*dependencies",
        r"upgrade.*dependencies",
        r"bump.*dependencies",
        r"update.*dependency",
        r"upgrade.*dependency",
        r"bump.*dependency",
        r"update.*gradle",
        r"update.*maven",
        r"update.*npm",
        r"update.*pip",
        r"update.*requirements",
    ],
    "ci": [
        r"update.*workflow",
        r"update.*action",
        r"update.*pipeline",
        r"update.*jenkins",
        r"update.*github.*action",
        r"update.*ci",
    ],
    "chore": [
        r"bump",
        r"update",
        r"upgrade",
        r"maintain",
        r"housekeeping",
        r"cleanup",
        r"pre-commit.*autofix",
        r"pre-commit.*autoupdate",
    ],
}

# Dependabot-specific patterns
DEPENDABOT_PATTERNS = [
    r"bump\s+(?P<package>[^\s]+)\s+from\s+(?P<old_version>[^\s]+)\s+to\s+(?P<new_version>[^\s]+)",
    r"update\s+(?P<package>[^\s]+)\s+requirement\s+from\s+(?P<old_version>[^\s]+)\s+to\s+(?P<new_version>[^\s]+)",
    r"upgrade\s+(?P<package>[^\s]+)\s+from\s+(?P<old_version>[^\s]+)\s+to\s+(?P<new_version>[^\s]+)",
]


@dataclass
class ConventionalCommitPreferences:
    """Repository preferences for conventional commits."""

    # Capitalization style: "lower", "title", "sentence"
    capitalization: str = "lower"

    # Preferred commit types found in the repository
    preferred_types: dict[str, str] = field(default_factory=dict)

    # Default type for dependency updates
    dependency_type: str = "chore"

    # Default type for automated fixes
    automation_type: str = "chore"

    # Whether to use scope in commit messages
    use_scope: bool = False

    # Default scope for dependency updates
    dependency_scope: str = "deps"


class CommitNormalizer:
    """Normalizes commit messages to conventional commit format."""

    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace or Path.cwd()
        self.preferences = ConventionalCommitPreferences()
        self._detected_preferences = False

    def should_normalize(self, title: str, author: str) -> bool:
        """Check if a commit title should be normalized."""
        if not title:
            return False

        # Check if already in conventional commit format
        if self._is_conventional_commit(title):
            return False

        # Check for automation patterns
        return self._is_automation_pr(title, author)

    def normalize_commit_title(self, title: str, author: str) -> str:
        """Normalize a commit title to conventional commit format."""
        if not self.should_normalize(title, author):
            return title

        # Detect preferences if not already done
        if not self._detected_preferences:
            self._detect_preferences()
            self._detected_preferences = True

        # Determine the appropriate conventional commit type
        commit_type = self._determine_commit_type(title, author)

        # Clean and normalize the title
        normalized_title = self._clean_title(title)

        # Apply conventional commit format
        return self._format_conventional_commit(commit_type, normalized_title)

    def _is_conventional_commit(self, title: str) -> bool:
        """Check if title is already in conventional commit format."""
        pattern = r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\(.+?\))?\s*!?\s*:\s*.+"
        return bool(re.match(pattern, title, re.IGNORECASE))

    def _is_automation_pr(self, title: str, author: str) -> bool:
        """Check if this is an automation PR that should be normalized."""
        # Check for known automation authors
        automation_authors = [
            "dependabot[bot]",
            "dependabot-preview[bot]",
            "pre-commit-ci[bot]",
            "renovate[bot]",
            "greenkeeper[bot]",
        ]

        if any(author.lower().startswith(bot.lower()) for bot in automation_authors):
            return True

        # Check for automation patterns in title
        automation_patterns = [
            r"^bump\s+",
            r"^update\s+.*\s+from\s+.*\s+to\s+",
            r"^upgrade\s+.*\s+from\s+.*\s+to\s+",
            r"pre-commit.*autofix",
            r"pre-commit.*autoupdate",
        ]

        return any(re.search(pattern, title, re.IGNORECASE) for pattern in automation_patterns)

    def _detect_preferences(self) -> None:
        """Detect repository preferences for conventional commits."""
        log.debug("Detecting conventional commit preferences for workspace: %s", self.workspace)

        # Check .pre-commit-config.yaml
        self._check_precommit_config()

        # Check .github/release-drafter.yml
        self._check_release_drafter_config()

        # Analyze git history
        self._analyze_git_history()

        log.info("Detected commit preferences: %s", self.preferences)

    def _check_precommit_config(self) -> None:
        """Check .pre-commit-config.yaml for commit message patterns."""
        config_file = self.workspace / ".pre-commit-config.yaml"
        if not config_file.exists():
            return

        try:
            with config_file.open("r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            ci_config = config.get("ci", {})

            # Check autofix commit message
            autofix_msg = ci_config.get("autofix_commit_msg", "")
            if autofix_msg:
                self._extract_preferences_from_message(autofix_msg)

            # Check autoupdate commit message
            autoupdate_msg = ci_config.get("autoupdate_commit_msg", "")
            if autoupdate_msg:
                self._extract_preferences_from_message(autoupdate_msg)

        except Exception as e:
            log.debug("Failed to parse .pre-commit-config.yaml: %s", e)

    def _check_release_drafter_config(self) -> None:
        """Check .github/release-drafter.yml for commit type patterns."""
        config_paths = [
            self.workspace / ".github" / "release-drafter.yml",
            self.workspace / ".github" / "release-drafter.yaml",
        ]

        for config_file in config_paths:
            if not config_file.exists():
                continue

            try:
                with config_file.open("r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)

                autolabeler = config.get("autolabeler", [])
                for rule in autolabeler:
                    titles = rule.get("title", [])

                    for title_pattern in titles:
                        # Extract conventional commit type from pattern
                        if title_pattern.startswith("/") and title_pattern.endswith("/i"):
                            pattern = title_pattern[1:-2]  # Remove /pattern/i
                            if ":" in pattern:
                                commit_type = pattern.split(":")[0]
                                if commit_type in CONVENTIONAL_COMMIT_TYPES:
                                    self.preferences.preferred_types[commit_type] = self._get_capitalization(
                                        commit_type
                                    )

                break  # Use first found config

            except Exception as e:
                log.debug("Failed to parse release-drafter config: %s", e)

    def _analyze_git_history(self) -> None:
        """Analyze recent git history for conventional commit patterns."""
        try:
            # Get recent commit messages
            git_cmd = shutil.which("git")
            if not git_cmd:
                log.debug("git command not found in PATH")
                return

            result = subprocess.run(  # noqa: S603
                [git_cmd, "log", "--pretty=format:%s", "-50"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            if result.returncode != 0:
                log.debug("Failed to get git history")
                return

            commit_messages = result.stdout.strip().split("\n")

            # Analyze conventional commit patterns
            type_counts: dict[str, int] = {}
            capitalization_examples: dict[str, str] = {}

            for message in commit_messages:
                if self._is_conventional_commit(message):
                    match = re.match(r"^([a-zA-Z]+)", message)
                    if match:
                        commit_type = match.group(1).lower()
                        type_counts[commit_type] = type_counts.get(commit_type, 0) + 1

                        # Track capitalization
                        if commit_type not in capitalization_examples:
                            capitalization_examples[commit_type] = match.group(1)

            # Update preferences based on analysis
            if type_counts:
                # Determine most common capitalization
                if capitalization_examples:
                    sample_type = next(iter(capitalization_examples.values()))
                    if sample_type.isupper():
                        self.preferences.capitalization = "upper"
                    elif sample_type.istitle():
                        self.preferences.capitalization = "title"
                    else:
                        self.preferences.capitalization = "lower"

                # Update preferred types
                for commit_type in type_counts:
                    if commit_type in CONVENTIONAL_COMMIT_TYPES:
                        self.preferences.preferred_types[commit_type] = self._apply_capitalization(commit_type)

        except Exception as e:
            log.debug("Failed to analyze git history: %s", e)

    def _extract_preferences_from_message(self, message: str) -> None:
        """Extract preferences from a commit message."""
        if self._is_conventional_commit(message):
            match = re.match(r"^([a-zA-Z]+)", message)
            if match:
                commit_type = match.group(1)
                self.preferences.automation_type = commit_type.lower()

                # Detect capitalization
                if commit_type.isupper():
                    self.preferences.capitalization = "upper"
                elif commit_type.istitle():
                    self.preferences.capitalization = "title"
                else:
                    self.preferences.capitalization = "lower"

    def _determine_commit_type(self, title: str, author: str) -> str:
        """Determine the appropriate conventional commit type."""
        title_lower = title.lower()

        # Check for dependabot patterns first
        if "dependabot" in author.lower() or any(re.search(pattern, title_lower) for pattern in DEPENDABOT_PATTERNS):
            return self.preferences.dependency_type

        # Check for pre-commit.ci patterns
        if "pre-commit" in author.lower() or "pre-commit" in title_lower:
            return self.preferences.automation_type

        # Pattern-based detection
        for commit_type, patterns in CHANGE_TYPE_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, title_lower):
                    return commit_type

        # Default to chore for unrecognized automation
        return self.preferences.automation_type

    def _clean_title(self, title: str) -> str:
        """Clean and normalize the title text."""
        # Remove markdown links
        title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)

        # Remove trailing ellipsis
        title = re.sub(r"\s*[.]{3,}.*$", "", title)

        # Remove markdown formatting
        title = re.sub(r"[*_`]", "", title)

        # For dependabot titles, extract the essential information
        for pattern in DEPENDABOT_PATTERNS:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                package = match.group("package")
                old_version = match.group("old_version")
                new_version = match.group("new_version")
                return f"bump {package} from {old_version} to {new_version}"

        # Remove common prefixes if not already handled
        prefixes_to_remove = [
            r"^bump\s+",
            r"^update\s+",
            r"^upgrade\s+",
        ]

        for prefix in prefixes_to_remove:
            title = re.sub(prefix, "", title, flags=re.IGNORECASE).strip()

        # Ensure first letter is lowercase (will be adjusted by capitalization later)
        if title and title[0].isupper():
            title = title[0].lower() + title[1:]

        return title.strip()

    def _format_conventional_commit(self, commit_type: str, title: str) -> str:
        """Format a conventional commit message."""
        # Apply capitalization preference
        formatted_type = self._apply_capitalization(commit_type)

        # Add scope if preferred for dependency updates
        scope = ""
        if commit_type == self.preferences.dependency_type and self.preferences.use_scope:
            scope = f"({self.preferences.dependency_scope})"

        return f"{formatted_type}{scope}: {title}"

    def _apply_capitalization(self, commit_type: str) -> str:
        """Apply the preferred capitalization to a commit type."""
        if self.preferences.capitalization == "upper":
            return commit_type.upper()
        elif self.preferences.capitalization == "title":
            return commit_type.title()
        else:
            return commit_type.lower()

    def _get_capitalization(self, text: str) -> str:
        """Determine the capitalization style of text."""
        if text.isupper():
            return "upper"
        elif text.istitle():
            return "title"
        else:
            return "lower"


def normalize_commit_title(title: str, author: str, workspace: Path | None = None) -> str:
    """
    Normalize a commit title to conventional commit format.

    Args:
        title: The original commit title
        author: The author of the commit/PR
        workspace: Path to the git repository workspace

    Returns:
        Normalized commit title in conventional commit format
    """
    normalizer = CommitNormalizer(workspace)
    return normalizer.normalize_commit_title(title, author)


def should_normalize_commit(title: str, author: str, workspace: Path | None = None) -> bool:
    """
    Check if a commit title should be normalized.

    Args:
        title: The original commit title
        author: The author of the commit/PR
        workspace: Path to the git repository workspace

    Returns:
        True if the commit should be normalized
    """
    normalizer = CommitNormalizer(workspace)
    return normalizer.should_normalize(title, author)
