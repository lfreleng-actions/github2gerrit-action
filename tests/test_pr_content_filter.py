# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for the extensible PR content filtering module."""

from github2gerrit.pr_content_filter import DependabotConfig
from github2gerrit.pr_content_filter import DependabotRule
from github2gerrit.pr_content_filter import FilterConfig
from github2gerrit.pr_content_filter import PRContentFilter
from github2gerrit.pr_content_filter import PrecommitConfig
from github2gerrit.pr_content_filter import PrecommitRule
from github2gerrit.pr_content_filter import create_default_filter
from github2gerrit.pr_content_filter import filter_pr_body
from github2gerrit.pr_content_filter import should_filter_pr_body


class TestFilterConfig:
    """Test configuration classes."""

    def test_filter_config_defaults(self) -> None:
        """Test default FilterConfig values."""
        config = FilterConfig()

        assert config.enabled is True
        assert config.remove_emoji_codes is True
        assert config.deduplicate_title_in_body is True
        assert config.author_rules == {}
        assert isinstance(config.dependabot_config, DependabotConfig)
        assert isinstance(config.precommit_config, PrecommitConfig)

    def test_dependabot_config_defaults(self) -> None:
        """Test default DependabotConfig values."""
        config = DependabotConfig()

        assert config.enabled is True
        assert config.expand_release_notes is True
        assert config.expand_commits is True
        assert config.remove_compatibility_images is True
        assert config.remove_command_instructions is True
        assert config.truncate_at_commands is True

    def test_precommit_config_defaults(self) -> None:
        """Test default PrecommitConfig values."""
        config = PrecommitConfig()

        assert config.enabled is True


class TestDependabotRule:
    """Test Dependabot filtering rule."""

    def test_matches_dependabot_pr(self) -> None:
        """Test Dependabot PR detection."""
        rule = DependabotRule()

        title = "Bump package from 1.0 to 2.0"
        body = """
<details><summary>Release notes</summary>
Content here
</details>

Dependabot will resolve any conflicts with this PR.
"""

        # With dependabot author
        assert rule.matches(title, body, "dependabot[bot]")

        # Without author but with multiple indicators
        assert rule.matches(title, body, "user123")

    def test_does_not_match_regular_pr(self) -> None:
        """Test that regular PRs are not matched."""
        rule = DependabotRule()

        title = "Fix authentication bug"
        body = "This fixes issue #123 by updating the auth logic."

        assert not rule.matches(title, body, "user123")

    def test_apply_dependabot_filtering(self) -> None:
        """Test Dependabot filtering application."""
        rule = DependabotRule()
        config = DependabotConfig()

        title = "Bump package from 1.0 to 2.0"
        body = """
<details>
<summary>Release notes</summary>

## What's Changed
- Bug fix #123
- New feature added

</details>

<details>
<summary>Commits</summary>
- abc123: Fix issue
- def456: Add feature
</details>

![compatibility](https://camo.githubusercontent.com/example/compatibility_score)

Dependabot will resolve any conflicts with this PR.

### Dependabot commands and options
- `@dependabot rebase` will rebase this PR
"""

        result = rule.apply(title, body, config)

        # Should expand details sections
        assert "## Release notes" in result
        assert "## Commits" in result
        assert "Bug fix #123" in result
        assert "abc123: Fix issue" in result

        # Should remove unwanted content
        assert "camo.githubusercontent.com" not in result
        assert "Dependabot will resolve" not in result
        assert "@dependabot rebase" not in result
        assert "<details>" not in result
        assert "<summary>" not in result

    def test_disabled_dependabot_config(self) -> None:
        """Test that disabled config returns original body."""
        rule = DependabotRule()
        config = DependabotConfig(enabled=False)

        title = "Bump package"
        body = "<details><summary>Test</summary>Content</details>"

        result = rule.apply(title, body, config)
        assert result == body


class TestPrecommitRule:
    """Test pre-commit.ci filtering rule."""

    def test_matches_precommit_pr(self) -> None:
        """Test pre-commit.ci PR detection."""
        rule = PrecommitRule()

        title = "pre-commit autoupdate"
        body = "Updates by pre-commit.ci"

        # With pre-commit author
        assert rule.matches(title, body, "pre-commit-ci[bot]")

        # With indicators in title/body
        assert rule.matches(title, body, "user123")

    def test_does_not_match_regular_pr(self) -> None:
        """Test that regular PRs are not matched."""
        rule = PrecommitRule()

        title = "Fix bug"
        body = "Regular PR content"

        assert not rule.matches(title, body, "user123")


class TestPRContentFilter:
    """Test the main filtering engine."""

    def test_filter_disabled(self) -> None:
        """Test that disabled filter returns original content."""
        config = FilterConfig(enabled=False)
        filter_engine = PRContentFilter(config)

        title = "Bump package from 1.0 to 2.0"
        body = "<details><summary>Test</summary>Content</details>"
        author = "dependabot[bot]"

        result = filter_engine.filter_content(title, body, author)
        assert result == body

    def test_dependabot_filtering_with_author_rules(self) -> None:
        """Test filtering with author-specific rules."""
        config = FilterConfig()
        config.author_rules["dependabot[bot]"] = "dependabot"
        filter_engine = PRContentFilter(config)

        title = "Bump package from 1.0 to 2.0"
        body = """
<details><summary>Release notes</summary>
Content here
</details>

Dependabot will resolve any conflicts.
"""

        result = filter_engine.filter_content(title, body, "dependabot[bot]")

        assert "## Release notes" in result
        assert "Content here" in result
        assert "Dependabot will resolve" not in result

    def test_emoji_removal(self) -> None:
        """Test emoji code removal."""
        config = FilterConfig()
        filter_engine = PRContentFilter(config)

        body = """
## What's Changed

### :sparkles: New features
- Feature A

### :bug: Bug fixes
- Fix B

### :arrow_up: Dependencies
- Update C
"""

        result = filter_engine._remove_emoji_codes(body)

        assert ":sparkles:" not in result
        assert ":bug:" not in result
        assert ":arrow_up:" not in result
        assert "New features" in result
        assert "Bug fixes" in result
        assert "Dependencies" in result

    def test_title_duplication_removal(self) -> None:
        """Test removal of title duplication in body."""
        config = FilterConfig()
        filter_engine = PRContentFilter(config)

        # Test exact duplication
        title = "Bump package from 1.0 to 2.0"
        body = "Bump package from 1.0 to 2.0\n\nAdditional content here."

        result = filter_engine._remove_title_duplication(title, body)
        assert result == "Additional content here."

        # Test Bump/Bumps variation
        title = "Bump package from 1.0 to 2.0"
        body = "Bumps package from 1.0 to 2.0.\n\nRelease notes here."

        result = filter_engine._remove_title_duplication(title, body)
        assert result == "Release notes here."

        # Test no duplication
        title = "Bump package from 1.0 to 2.0"
        body = "Different content here."

        result = filter_engine._remove_title_duplication(title, body)
        assert result == "Different content here."

    def test_full_dependabot_filtering(self) -> None:
        """Test complete Dependabot filtering with all features."""
        config = FilterConfig()
        config.author_rules["dependabot[bot]"] = "dependabot"
        filter_engine = PRContentFilter(config)

        title = "Bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1"
        body = """
Bumps [net.logstash.logback:logstash-logback-encoder](https://github.com/logfellow/repo) from 7.4 to 8.1.

<details>
<summary>Release notes</summary>

### :sparkles: New features and improvements
- Update LoggingEventJsonPatternParser

### :bug: Bug fixes
- Always build source jar

</details>

<details>
<summary>Commits</summary>
- [a998591](https://github.com/logfellow/repo/commit/a998591) prepare release
- [16714f1](https://github.com/logfellow/repo/commit/16714f1) release
</details>

![Dependabot compatibility score](https://camo.githubusercontent.com/example/compatibility_score)

Dependabot will resolve any conflicts with this PR.

### Dependabot commands and options
- `@dependabot rebase` will rebase this PR
"""

        result = filter_engine.filter_content(title, body, "dependabot[bot]")

        # Should remove title duplication
        assert not result.startswith("Bumps net.logstash.logback")

        # Should expand details sections
        assert "## Release notes" in result
        assert "## Commits" in result

        # Should remove emoji codes
        assert ":sparkles:" not in result
        assert ":bug:" not in result

        # Should clean up content
        assert "New features and improvements" in result
        assert "prepare release" in result
        assert "github.com" not in result
        assert "camo.githubusercontent.com" not in result
        assert "Dependabot will resolve" not in result
        assert "@dependabot rebase" not in result

    def test_add_custom_rule(self) -> None:
        """Test adding custom filtering rules."""
        filter_engine = PRContentFilter()

        # Create a simple custom rule
        class TestRule(DependabotRule):
            def matches(self, title: str, body: str, author: str) -> bool:
                return "test-bot" in author.lower()

        custom_rule = TestRule()
        filter_engine.add_rule(custom_rule)

        assert len(filter_engine.rules) == 3  # 2 default + 1 custom
        assert custom_rule in filter_engine.rules

    def test_set_author_rule(self) -> None:
        """Test setting author-specific rules."""
        filter_engine = PRContentFilter()

        filter_engine.set_author_rule("custom-bot[bot]", "dependabot")

        assert filter_engine.config.author_rules["custom-bot[bot]"] == "dependabot"


class TestConvenienceFunctions:
    """Test convenience functions for backward compatibility."""

    def test_create_default_filter(self) -> None:
        """Test default filter creation."""
        filter_engine = create_default_filter()

        assert isinstance(filter_engine, PRContentFilter)
        assert filter_engine.config.enabled is True

        # Check default author mappings
        assert "dependabot[bot]" in filter_engine.config.author_rules
        assert "pre-commit-ci[bot]" in filter_engine.config.author_rules

    def test_filter_pr_body_function(self) -> None:
        """Test main filter_pr_body function."""
        # Dependabot PR should be filtered
        title = "Bump package from 1.0 to 2.0"
        body = """
Bumps package from 1.0 to 2.0.

<details><summary>Release notes</summary>
Content
</details>

Dependabot will resolve any conflicts.
"""

        result = filter_pr_body(title, body, "dependabot[bot]")
        assert "## Release notes" in result
        assert "Dependabot will resolve" not in result
        assert not result.startswith("Bumps package")

        # Regular PR should not be filtered significantly
        regular_title = "Fix bug"
        regular_body = "This fixes the issue."

        result = filter_pr_body(regular_title, regular_body, "user123")
        assert result == regular_body

    def test_should_filter_pr_body_function(self) -> None:
        """Test should_filter_pr_body function."""
        # Dependabot PR
        assert should_filter_pr_body(
            "Bump package from 1.0 to 2.0",
            "<details><summary>Release notes</summary>Content</details>",
            "dependabot[bot]",
        )

        # Regular PR
        assert not should_filter_pr_body("Fix bug", "Regular content", "user123")

    def test_empty_inputs(self) -> None:
        """Test handling of empty inputs."""
        assert filter_pr_body("title", "", "author") == ""
        assert filter_pr_body("title", None, "author") == ""

        assert not should_filter_pr_body("title", "", "author")
        assert not should_filter_pr_body("title", None, "author")


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_malformed_html(self) -> None:
        """Test handling of malformed HTML."""
        config = FilterConfig()
        filter_engine = PRContentFilter(config)

        body = """
<details>
<summary>Release notes
Missing closing tag content
</details>

<summary>Orphaned summary</summary>
"""

        # Should not crash
        result = filter_engine.filter_content("title", body, "dependabot[bot]")
        assert isinstance(result, str)

    def test_unicode_content(self) -> None:
        """Test handling of unicode content."""
        config = FilterConfig()
        filter_engine = PRContentFilter(config)

        body = """
Bumps 📦 package from 1.0 to 2.0.

<details>
<summary>Release notes 🚀</summary>
Unicode content: ✨ 🐛 🆙
</details>

Dependabot will resolve any conflicts.
"""

        result = filter_engine.filter_content("Bump 📦 package", body, "dependabot[bot]")
        assert "📦" in result
        assert "🚀" in result
        assert "✨" in result
        assert "Dependabot will resolve" not in result

    def test_no_matching_rules(self) -> None:
        """Test content with no matching rules."""
        config = FilterConfig()
        filter_engine = PRContentFilter(config)

        title = "Regular PR"
        body = "Regular content with no automation indicators."
        author = "regular-user"

        result = filter_engine.filter_content(title, body, author)

        # Should still apply post-processing (emoji removal, etc.)
        # but no rule-specific filtering
        assert result == body  # No emojis to remove in this case
