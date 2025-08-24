# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
#
# PR Content Filtering Module
#
# This module provides an extensible, rule-based system for filtering and cleaning
# GitHub pull request content for Gerrit consumption. It supports multiple automation
# tools (Dependabot, pre-commit.ci, etc.) with author-specific filtering rules.
#
# Key features:
# - Author-specific filtering rules
# - Extensible rule system for different automation tools
# - Configurable filtering options
# - Content deduplication between title and body
# - Emoji and formatting cleanup

from __future__ import annotations

import logging
import re
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from typing import Any


log = logging.getLogger("github2gerrit.pr_content_filter")

# Common patterns used across filters
_HTML_DETAILS_PATTERN = re.compile(
    r"<details[^>]*>\s*<summary[^>]*>(.*?)</summary>\s*(.*?)</details>", re.IGNORECASE | re.DOTALL
)
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_MULTIPLE_NEWLINES_PATTERN = re.compile(r"\n{3,}")
_EMOJI_PATTERN = re.compile(r":[a-z_]+:")  # GitHub emoji codes like :sparkles:


@dataclass
class FilterConfig:
    """Configuration for PR content filtering."""

    # Global options
    enabled: bool = True
    remove_emoji_codes: bool = True
    deduplicate_title_in_body: bool = True

    # Author-specific filtering
    author_rules: dict[str, str] = field(default_factory=dict)

    # Rule-specific configurations
    dependabot_config: DependabotConfig = field(default_factory=lambda: DependabotConfig())
    precommit_config: PrecommitConfig = field(default_factory=lambda: PrecommitConfig())


@dataclass
class DependabotConfig:
    """Configuration for Dependabot PR filtering."""

    enabled: bool = True
    expand_release_notes: bool = True
    expand_commits: bool = True
    remove_compatibility_images: bool = True
    remove_command_instructions: bool = True
    truncate_at_commands: bool = True


@dataclass
class PrecommitConfig:
    """Configuration for pre-commit.ci PR filtering."""

    enabled: bool = True
    # Future: add pre-commit.ci specific options


class FilterRule(ABC):
    """Abstract base class for PR content filtering rules."""

    @abstractmethod
    def matches(self, title: str, body: str, author: str) -> bool:
        """Check if this rule should be applied to the given PR."""

    @abstractmethod
    def apply(self, title: str, body: str, config: Any) -> str:
        """Apply the filtering rule and return the cleaned body."""

    @abstractmethod
    def get_config_key(self) -> str:
        """Return the configuration key for this rule."""


class DependabotRule(FilterRule):
    """Filtering rule for Dependabot PRs."""

    def matches(self, title: str, body: str, author: str) -> bool:
        """Check if this is a Dependabot PR."""
        if not body:
            return False

        dependabot_indicators = [
            "dependabot" in author.lower(),
            "Bumps " in title and " from " in title and " to " in title,
            "Dependabot will resolve any conflicts" in body,
            "<details>" in body and "<summary>" in body,
            "camo.githubusercontent.com" in body,
        ]

        # Require multiple indicators for confidence
        return sum(dependabot_indicators) >= 2

    def get_config_key(self) -> str:
        return "dependabot_config"

    def apply(self, title: str, body: str, config: DependabotConfig) -> str:
        """Apply Dependabot-specific filtering."""
        if not config.enabled:
            return body

        log.info("Applying Dependabot filtering rules")
        filtered = body

        # Step 1: Expand collapsed sections
        if config.expand_release_notes or config.expand_commits:
            filtered = self._expand_html_details(filtered)

        # Step 2: Remove compatibility images
        if config.remove_compatibility_images:
            filtered = self._remove_compatibility_images(filtered)

        # Step 3: Truncate at command instructions
        if config.truncate_at_commands:
            filtered = self._truncate_at_dependabot_commands(filtered)

        return filtered

    def _expand_html_details(self, content: str) -> str:
        """Expand HTML details/summary sections."""

        def replace_details(match: re.Match[str]) -> str:
            summary = match.group(1).strip()
            detail_content = match.group(2).strip()

            if summary:
                return f"## {summary}\n\n{detail_content}\n"
            return f"{detail_content}\n"

        return _HTML_DETAILS_PATTERN.sub(replace_details, content)

    def _remove_compatibility_images(self, content: str) -> str:
        """Remove Dependabot compatibility score images."""
        pattern = re.compile(r"!\[.*?\]\(https://camo\.githubusercontent\.com/[^)]*\)", re.IGNORECASE | re.DOTALL)
        return pattern.sub("", content)

    def _truncate_at_dependabot_commands(self, content: str) -> str:
        """Truncate at Dependabot command instructions."""
        pattern = re.compile(r"Dependabot will resolve any conflicts", re.IGNORECASE | re.MULTILINE)
        match = pattern.search(content)
        if match:
            return content[: match.start()].rstrip()
        return content


class PrecommitRule(FilterRule):
    """Filtering rule for pre-commit.ci PRs."""

    def matches(self, title: str, body: str, author: str) -> bool:
        """Check if this is a pre-commit.ci PR."""
        if not body:
            return False

        precommit_indicators = [
            "pre-commit-ci" in author.lower(),
            "pre-commit" in title.lower(),
            "pre-commit.ci" in body,
            "autoupdate" in title.lower(),
        ]

        return sum(precommit_indicators) >= 2

    def get_config_key(self) -> str:
        return "precommit_config"

    def apply(self, title: str, body: str, config: PrecommitConfig) -> str:
        """Apply pre-commit.ci specific filtering."""
        if not config.enabled:
            return body

        log.info("Applying pre-commit.ci filtering rules")
        # Future: implement pre-commit.ci specific filtering
        return body


class PRContentFilter:
    """Main PR content filtering engine."""

    def __init__(self, config: FilterConfig | None = None):
        """Initialize the filter with configuration."""
        self.config = config or FilterConfig()
        self.rules: list[FilterRule] = [
            DependabotRule(),
            PrecommitRule(),
        ]

    def should_filter(self, title: str, body: str, author: str) -> bool:
        """Determine if PR content should be filtered."""
        if not self.config.enabled or not body:
            return False

        # Check author-specific rules first
        if author in self.config.author_rules:
            rule_name = self.config.author_rules[author]
            return any(rule.__class__.__name__.lower().startswith(rule_name.lower()) for rule in self.rules)

        # Check if any rule matches
        return any(rule.matches(title, body, author) for rule in self.rules)

    def filter_content(self, title: str, body: str, author: str) -> str:
        """Filter PR content based on configured rules."""
        if not self.should_filter(title, body, author):
            log.debug("No filtering rules matched for author: %s", author)
            return body

        filtered_body = body

        # Apply global pre-processing first (including title deduplication)
        filtered_body = self._pre_process(title, filtered_body)

        # Apply matching rules
        for rule in self.rules:
            if rule.matches(title, body, author):
                config_key = rule.get_config_key()
                rule_config = getattr(self.config, config_key, None)
                if rule_config:
                    filtered_body = rule.apply(title, filtered_body, rule_config)

        # Apply global post-processing
        filtered_body = self._post_process(title, filtered_body)

        return filtered_body.strip()

    def _pre_process(self, title: str, body: str) -> str:
        """Apply global pre-processing rules."""
        processed = body

        # Remove title duplication first, before other processing
        if self.config.deduplicate_title_in_body:
            processed = self._remove_title_duplication(title, processed)

        return processed

    def _post_process(self, title: str, body: str) -> str:
        """Apply global post-processing rules."""
        processed = body

        # Remove emoji codes
        if self.config.remove_emoji_codes:
            processed = self._remove_emoji_codes(processed)

        # Clean HTML and markdown
        processed = self._clean_html_and_markdown(processed)

        # Clean up whitespace
        processed = self._clean_whitespace(processed)

        return processed

    def _remove_emoji_codes(self, content: str) -> str:
        """Remove GitHub emoji codes like :sparkles: and :bug:."""
        # First handle emoji codes inside HTML tags to prevent leading spaces
        # e.g., "<h3>:sparkles: New features</h3>" -> "<h3>New features</h3>"
        content = re.sub(r"(<[^>]*>)\s*:[a-z_]+:\s*", r"\1", content)

        # Remove emoji codes while preserving line structure
        lines = content.splitlines()
        cleaned_lines = []

        for line in lines:
            # Remove emoji codes from each line
            cleaned_line = _EMOJI_PATTERN.sub("", line)
            # Clean up multiple spaces that might result from emoji removal
            cleaned_line = re.sub(r"  +", " ", cleaned_line)

            # Fix lines that started with emoji codes and now have leading space
            if cleaned_line.startswith(" ") and not line.startswith(" "):
                # This line originally started with an emoji, remove the leading space
                cleaned_line = cleaned_line.lstrip()

            # Fix markdown headers that lost their emoji but kept leading space
            # e.g., "### :sparkles: New features" -> "### New features" not "###  New features"
            if cleaned_line.startswith("### "):
                # Ensure exactly one space after ###
                header_text = cleaned_line[4:].lstrip()  # Remove ### and any spaces
                cleaned_line = f"### {header_text}" if header_text else "###"
            elif cleaned_line.startswith("## "):
                # Same for ## headers
                header_text = cleaned_line[3:].lstrip()
                cleaned_line = f"## {header_text}" if header_text else "##"
            elif cleaned_line.startswith("# "):
                # Same for # headers
                header_text = cleaned_line[2:].lstrip()
                cleaned_line = f"# {header_text}" if header_text else "#"

            # Strip trailing whitespace but preserve the line
            cleaned_lines.append(cleaned_line.rstrip())

        # Post-process to add missing line breaks after non-markdown headings
        final_lines = []
        for i, line in enumerate(cleaned_lines):
            final_lines.append(line)

            # If this line looks like a heading (not starting with #) and should have
            # a blank line after it, add one
            if (
                i < len(cleaned_lines) - 1
                and line.strip()
                and not line.startswith("#")
                and not line.startswith("-")
                and not line.startswith(" ")
                and not line.startswith("@")
                and cleaned_lines[i + 1].strip()
                and
                # Check if this looks like a heading (common patterns)
                (
                    line.endswith(("Changed", "Contributors"))
                    or "features" in line.lower()
                    or "fixes" in line.lower()
                    or "upgrades" in line.lower()
                    or "documentation" in line.lower()
                )
            ):
                # Add blank line regardless of what follows
                final_lines.append("")

        return "\n".join(final_lines)

    def _clean_html_and_markdown(self, content: str) -> str:
        """Clean HTML tags and simplify markdown links."""
        # Remove HTML tags
        cleaned = _HTML_TAG_PATTERN.sub("", content)

        # Simplify markdown links to just the text
        cleaned = _MARKDOWN_LINK_PATTERN.sub(r"\1", cleaned)

        return cleaned

    def _remove_title_duplication(self, title: str, body: str) -> str:
        """Remove duplication of title in body content."""
        if not title or not body:
            return body

        lines = body.splitlines()
        if not lines:
            return body

        # Find the first non-empty line
        first_content_line_idx = None
        first_content_line = ""

        for i, line in enumerate(lines):
            if line.strip():
                first_content_line_idx = i
                first_content_line = line.strip()
                break

        if first_content_line_idx is None:
            return body  # No content found

        # Clean both title and first content line for comparison
        title_clean = self._clean_for_comparison(title)
        first_line_clean = self._clean_for_comparison(first_content_line)

        # Handle common variations:
        # - Exact match after cleaning
        # - Body starts with "Bumps ..." when title is "Bump ..."
        is_duplicate = first_line_clean == title_clean or (
            title_clean.startswith("Bump ")
            and first_line_clean.startswith("Bumps ")
            and title_clean[5:] == first_line_clean[6:]
        )

        if is_duplicate:
            # Remove the duplicate line and any immediately following empty lines
            remaining_lines = lines[first_content_line_idx + 1 :]
            while remaining_lines and not remaining_lines[0].strip():
                remaining_lines = remaining_lines[1:]
            return "\n".join(remaining_lines)

        return body

    def _clean_for_comparison(self, text: str) -> str:
        """Clean text for comparison by removing markdown and punctuation."""
        # Remove markdown links
        cleaned = _MARKDOWN_LINK_PATTERN.sub(r"\1", text)
        # Remove trailing periods and normalize spacing
        cleaned = cleaned.strip().rstrip(".")
        return cleaned

    def _clean_whitespace(self, content: str) -> str:
        """Clean up excessive whitespace."""
        # Strip trailing whitespace from each line
        lines = [line.rstrip() for line in content.splitlines()]
        cleaned = "\n".join(lines)

        # Reduce multiple newlines to at most 2
        cleaned = _MULTIPLE_NEWLINES_PATTERN.sub("\n\n", cleaned)

        return cleaned

    def add_rule(self, rule: FilterRule) -> None:
        """Add a custom filtering rule."""
        self.rules.append(rule)

    def set_author_rule(self, author: str, rule_name: str) -> None:
        """Set a specific rule for an author."""
        self.config.author_rules[author] = rule_name


# Convenience functions for backward compatibility and simple usage
def create_default_filter() -> PRContentFilter:
    """Create a filter with default configuration."""
    config = FilterConfig()

    # Set up default author mappings
    config.author_rules.update(
        {
            "dependabot[bot]": "dependabot",
            "dependabot": "dependabot",
            "pre-commit-ci[bot]": "precommit",
            "pre-commit-ci": "precommit",
        }
    )

    return PRContentFilter(config)


def filter_pr_body(title: str, body: str | None, author: str | None = None) -> str:
    """
    Main entry point for PR body filtering with default configuration.

    Args:
        title: PR title
        body: PR body
        author: PR author

    Returns:
        Filtered body, or original body if no filtering needed
    """
    if not body:
        return body or ""

    filter_engine = create_default_filter()
    return filter_engine.filter_content(title, body, author or "")


# Legacy compatibility functions
def should_filter_pr_body(title: str, body: str | None, author: str | None = None) -> bool:
    """Legacy function for checking if filtering should be applied."""
    if not body:
        return False

    filter_engine = create_default_filter()
    return filter_engine.should_filter(title, body, author or "")


def filter_dependabot_pr_body(body: str | None) -> str:
    """Legacy function for Dependabot-specific filtering."""
    if not body:
        return ""

    # Create a Dependabot-only filter
    config = FilterConfig()

    # Force apply Dependabot rule
    rule = DependabotRule()
    return rule.apply("", str(body), config.dependabot_config)
