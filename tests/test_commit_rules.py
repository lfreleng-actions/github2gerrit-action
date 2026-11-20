# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""Comprehensive tests for the commit_rules module."""

from __future__ import annotations

import json

import pytest

from github2gerrit.commit_rules import CommitRule
from github2gerrit.commit_rules import CommitRulesConfig
from github2gerrit.commit_rules import ResolvedCommitRules
from github2gerrit.commit_rules import apply_body_rules
from github2gerrit.commit_rules import apply_trailer_rules
from github2gerrit.commit_rules import parse_commit_rules_json
from github2gerrit.commit_rules import resolve_rules


# ---------------------------------------------------------------------------
# CommitRule dataclass
# ---------------------------------------------------------------------------


class TestCommitRule:
    """Tests for the CommitRule dataclass."""

    def test_defaults(self) -> None:
        rule = CommitRule(key="Type", value="ci")
        assert rule.location == "trailer"
        assert rule.separator == "blank_line"

    def test_format_line(self) -> None:
        rule = CommitRule(key="Issue-ID", value="CIMAN-33")
        assert rule.format_line() == "Issue-ID: CIMAN-33"

    def test_format_line_body(self) -> None:
        rule = CommitRule(key="Type", value="fix", location="body")
        assert rule.format_line() == "Type: fix"

    def test_frozen(self) -> None:
        rule = CommitRule(key="Type", value="ci")
        with pytest.raises(AttributeError):
            rule.key = "Other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ResolvedCommitRules
# ---------------------------------------------------------------------------


class TestResolvedCommitRules:
    """Tests for the ResolvedCommitRules helper."""

    def test_empty(self) -> None:
        r = ResolvedCommitRules()
        assert not r.has_rules
        assert r.get_trailer_value("Issue-ID") is None
        assert r.get_body_value("Type") is None

    def test_has_rules_trailer(self) -> None:
        r = ResolvedCommitRules(
            trailer_rules=[CommitRule(key="Issue-ID", value="X-1")]
        )
        assert r.has_rules
        assert r.get_trailer_value("Issue-ID") == "X-1"
        assert r.get_trailer_value("Missing") is None

    def test_has_rules_body(self) -> None:
        r = ResolvedCommitRules(
            body_rules=[CommitRule(key="Type", value="ci", location="body")]
        )
        assert r.has_rules
        assert r.get_body_value("Type") == "ci"
        assert r.get_body_value("Missing") is None


# ---------------------------------------------------------------------------
# parse_commit_rules_json — valid inputs
# ---------------------------------------------------------------------------


class TestParseCommitRulesJson:
    """Tests for parse_commit_rules_json."""

    def test_empty_string(self) -> None:
        assert parse_commit_rules_json("") is None

    def test_whitespace_only(self) -> None:
        assert parse_commit_rules_json("   ") is None

    def test_invalid_json(self) -> None:
        assert parse_commit_rules_json("{not json}") is None

    def test_non_object_json(self) -> None:
        assert parse_commit_rules_json("[1,2,3]") is None

    def test_empty_object(self) -> None:
        config = parse_commit_rules_json("{}")
        assert config is not None
        assert config.defaults == []
        assert config.projects == {}
        assert config.actors == {}

    def test_defaults_only(self) -> None:
        data = {
            "defaults": [
                {"key": "Issue-ID", "value": "CIMAN-33"},
                {"key": "Type", "value": "ci", "location": "body"},
            ]
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert len(config.defaults) == 2
        assert config.defaults[0].key == "Issue-ID"
        assert config.defaults[0].value == "CIMAN-33"
        assert config.defaults[0].location == "trailer"
        assert config.defaults[1].key == "Type"
        assert config.defaults[1].location == "body"

    def test_projects(self) -> None:
        data = {
            "projects": {
                "vpp": [
                    {
                        "key": "Type",
                        "value": "ci",
                        "location": "body",
                        "separator": "blank_line",
                    }
                ],
                "csit": [],
            }
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert "vpp" in config.projects
        assert len(config.projects["vpp"]) == 1
        assert config.projects["vpp"][0].key == "Type"
        assert config.projects["vpp"][0].separator == "blank_line"
        # Empty arrays produce no entry
        assert "csit" not in config.projects

    def test_actors(self) -> None:
        data = {
            "actors": {
                "dependabot[bot]": [
                    {"key": "Issue-ID", "value": "CIMAN-33"},
                    {"key": "Type", "value": "ci", "location": "body"},
                ]
            }
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert "dependabot[bot]" in config.actors
        assert len(config.actors["dependabot[bot]"]) == 2

    def test_full_document(self) -> None:
        data = {
            "defaults": [
                {"key": "Issue-ID", "value": "CIMAN-33", "location": "trailer"}
            ],
            "projects": {
                "vpp": [
                    {
                        "key": "Type",
                        "value": "ci",
                        "location": "body",
                        "separator": "blank_line",
                    },
                    {"key": "Issue-ID", "value": "CIMAN-33"},
                ],
                "hicn": [{"key": "Type", "value": "ci", "location": "body"}],
            },
            "actors": {
                "dependabot[bot]": [
                    {"key": "Type", "value": "ci", "location": "body"},
                    {"key": "Issue-ID", "value": "CIMAN-33"},
                ],
                "renovate[bot]": [
                    {"key": "Issue-ID", "value": "CIMAN-44"},
                ],
            },
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert len(config.defaults) == 1
        assert len(config.projects) == 2
        assert len(config.actors) == 2


# ---------------------------------------------------------------------------
# parse_commit_rules_json — invalid/malformed entries
# ---------------------------------------------------------------------------


class TestParseCommitRulesJsonMalformed:
    """Tests for graceful handling of malformed rule entries."""

    def test_non_dict_rule_entry(self) -> None:
        data = {"defaults": ["not-a-dict", 42, None]}
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.defaults == []

    def test_missing_key(self) -> None:
        data = {"defaults": [{"value": "ci"}]}
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.defaults == []

    def test_missing_value(self) -> None:
        data = {"defaults": [{"key": "Type"}]}
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.defaults == []

    def test_empty_key(self) -> None:
        data = {"defaults": [{"key": "", "value": "ci"}]}
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.defaults == []

    def test_non_string_value(self) -> None:
        data = {"defaults": [{"key": "Type", "value": 42}]}
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.defaults == []

    def test_unknown_location_defaults_to_trailer(self) -> None:
        data = {
            "defaults": [{"key": "Type", "value": "ci", "location": "header"}]
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert len(config.defaults) == 1
        assert config.defaults[0].location == "trailer"

    def test_unknown_separator_defaults_to_blank_line(self) -> None:
        data = {
            "defaults": [
                {
                    "key": "Type",
                    "value": "ci",
                    "location": "body",
                    "separator": "tab",
                }
            ]
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.defaults[0].separator == "blank_line"

    def test_projects_not_object(self) -> None:
        data = {"projects": [1, 2, 3]}
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.projects == {}

    def test_actors_not_object(self) -> None:
        data = {"actors": "not-an-object"}
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.actors == {}

    def test_project_rules_not_array(self) -> None:
        data = {"projects": {"vpp": "not-an-array"}}
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.projects == {}

    def test_mixed_valid_invalid(self) -> None:
        """Valid entries survive alongside invalid ones."""
        data = {
            "defaults": [
                {"key": "Type", "value": "ci", "location": "body"},
                "garbage",
                {"key": "", "value": "nope"},
                {"key": "Ticket", "value": "VPP-123"},
            ]
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert len(config.defaults) == 2
        assert config.defaults[0].key == "Type"
        assert config.defaults[1].key == "Ticket"


# ---------------------------------------------------------------------------
# resolve_rules — precedence
# ---------------------------------------------------------------------------


class TestResolveRules:
    """Tests for the resolve_rules precedence engine."""

    def test_none_config(self) -> None:
        result = resolve_rules(None)
        assert not result.has_rules

    def test_empty_config(self) -> None:
        result = resolve_rules(CommitRulesConfig())
        assert not result.has_rules

    def test_defaults_only(self) -> None:
        config = CommitRulesConfig(
            defaults=[
                CommitRule(key="Issue-ID", value="X-1"),
                CommitRule(key="Type", value="ci", location="body"),
            ]
        )
        result = resolve_rules(config)
        assert len(result.trailer_rules) == 1
        assert len(result.body_rules) == 1
        assert result.trailer_rules[0].key == "Issue-ID"
        assert result.body_rules[0].key == "Type"

    def test_project_overrides_defaults(self) -> None:
        config = CommitRulesConfig(
            defaults=[
                CommitRule(key="Issue-ID", value="DEFAULT-1"),
                CommitRule(key="Type", value="docs", location="body"),
            ],
            projects={
                "vpp": [
                    CommitRule(key="Type", value="ci", location="body"),
                ]
            },
        )
        result = resolve_rules(config, gerrit_project="vpp")
        # Type should be overridden to "ci"
        assert result.get_body_value("Type") == "ci"
        # Issue-ID should still come from defaults
        assert result.get_trailer_value("Issue-ID") == "DEFAULT-1"

    def test_actor_overrides_project_and_defaults(self) -> None:
        config = CommitRulesConfig(
            defaults=[
                CommitRule(key="Issue-ID", value="DEFAULT-1"),
            ],
            projects={
                "vpp": [
                    CommitRule(key="Issue-ID", value="VPP-1"),
                    CommitRule(key="Type", value="fix", location="body"),
                ]
            },
            actors={
                "dependabot[bot]": [
                    CommitRule(key="Issue-ID", value="CIMAN-33"),
                    CommitRule(key="Type", value="ci", location="body"),
                ]
            },
        )
        result = resolve_rules(
            config, gerrit_project="vpp", github_actor="dependabot[bot]"
        )
        assert result.get_trailer_value("Issue-ID") == "CIMAN-33"
        assert result.get_body_value("Type") == "ci"

    def test_unmatched_project_uses_defaults(self) -> None:
        config = CommitRulesConfig(
            defaults=[
                CommitRule(key="Issue-ID", value="DEFAULT-1"),
            ],
            projects={
                "vpp": [
                    CommitRule(key="Type", value="ci", location="body"),
                ]
            },
        )
        # csit has no project-specific rules
        result = resolve_rules(config, gerrit_project="csit")
        assert result.get_trailer_value("Issue-ID") == "DEFAULT-1"
        assert result.get_body_value("Type") is None

    def test_unmatched_actor_uses_project(self) -> None:
        config = CommitRulesConfig(
            projects={
                "vpp": [
                    CommitRule(key="Type", value="ci", location="body"),
                ]
            },
            actors={
                "dependabot[bot]": [
                    CommitRule(key="Type", value="make", location="body"),
                ]
            },
        )
        # Unknown actor — project rules should apply
        result = resolve_rules(
            config, gerrit_project="vpp", github_actor="human-user"
        )
        assert result.get_body_value("Type") == "ci"

    def test_empty_project_and_actor_strings(self) -> None:
        """Empty strings should not match any project or actor entries."""
        config = CommitRulesConfig(
            defaults=[CommitRule(key="Issue-ID", value="DEFAULT-1")],
            projects={
                "vpp": [CommitRule(key="Type", value="ci", location="body")]
            },
            actors={"bot": [CommitRule(key="Issue-ID", value="BOT-1")]},
        )
        result = resolve_rules(config, gerrit_project="", github_actor="")
        assert result.get_trailer_value("Issue-ID") == "DEFAULT-1"
        assert result.get_body_value("Type") is None

    def test_location_can_change_between_layers(self) -> None:
        """A project can move a key from trailer to body (or vice versa)."""
        config = CommitRulesConfig(
            defaults=[
                CommitRule(key="Ticket", value="VPP-100", location="trailer"),
            ],
            projects={
                "vpp": [
                    CommitRule(key="Ticket", value="VPP-200", location="body"),
                ]
            },
        )
        result = resolve_rules(config, gerrit_project="vpp")
        # Ticket should now be in body, not trailer
        assert result.get_body_value("Ticket") == "VPP-200"
        assert result.get_trailer_value("Ticket") is None

    def test_multiple_rules_different_keys(self) -> None:
        config = CommitRulesConfig(
            defaults=[
                CommitRule(key="Issue-ID", value="X-1"),
                CommitRule(key="Type", value="ci", location="body"),
                CommitRule(
                    key="Ticket",
                    value="VPP-99",
                    location="body",
                    separator="none",
                ),
            ]
        )
        result = resolve_rules(config)
        assert len(result.trailer_rules) == 1
        assert len(result.body_rules) == 2
        assert result.body_rules[0].key == "Type"
        assert result.body_rules[1].key == "Ticket"


# ---------------------------------------------------------------------------
# resolve_rules — FD.io scenario
# ---------------------------------------------------------------------------


class TestResolveRulesFdioScenario:
    """Integration-style tests modelling the FD.io use case."""

    @pytest.fixture()
    def fdio_config(self) -> CommitRulesConfig:
        data = {
            "defaults": [
                {"key": "Issue-ID", "value": "CIMAN-33", "location": "trailer"}
            ],
            "projects": {
                "vpp": [
                    {
                        "key": "Type",
                        "value": "ci",
                        "location": "body",
                        "separator": "blank_line",
                    },
                    {
                        "key": "Issue-ID",
                        "value": "CIMAN-33",
                        "location": "trailer",
                    },
                ],
                "hicn": [
                    {
                        "key": "Type",
                        "value": "ci",
                        "location": "body",
                        "separator": "blank_line",
                    },
                ],
            },
            "actors": {
                "dependabot[bot]": [
                    {"key": "Type", "value": "ci", "location": "body"},
                    {
                        "key": "Issue-ID",
                        "value": "CIMAN-33",
                        "location": "trailer",
                    },
                ],
                "renovate[bot]": [
                    {
                        "key": "Issue-ID",
                        "value": "CIMAN-44",
                        "location": "trailer",
                    },
                ],
            },
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        return config

    def test_vpp_dependabot(self, fdio_config: CommitRulesConfig) -> None:
        result = resolve_rules(
            fdio_config, gerrit_project="vpp", github_actor="dependabot[bot]"
        )
        assert result.get_body_value("Type") == "ci"
        assert result.get_trailer_value("Issue-ID") == "CIMAN-33"

    def test_vpp_renovate(self, fdio_config: CommitRulesConfig) -> None:
        result = resolve_rules(
            fdio_config, gerrit_project="vpp", github_actor="renovate[bot]"
        )
        # renovate overrides Issue-ID but has no Type rule →
        # project-level Type should still apply
        assert result.get_body_value("Type") == "ci"
        assert result.get_trailer_value("Issue-ID") == "CIMAN-44"

    def test_vpp_human(self, fdio_config: CommitRulesConfig) -> None:
        result = resolve_rules(
            fdio_config, gerrit_project="vpp", github_actor="some-human"
        )
        assert result.get_body_value("Type") == "ci"
        assert result.get_trailer_value("Issue-ID") == "CIMAN-33"

    def test_csit_dependabot(self, fdio_config: CommitRulesConfig) -> None:
        """CSIT has no project rules — falls back to defaults."""
        result = resolve_rules(
            fdio_config, gerrit_project="csit", github_actor="dependabot[bot]"
        )
        # Actor rules override, so Type=ci and Issue-ID=CIMAN-33
        assert result.get_body_value("Type") == "ci"
        assert result.get_trailer_value("Issue-ID") == "CIMAN-33"

    def test_csit_human(self, fdio_config: CommitRulesConfig) -> None:
        """CSIT + unknown actor → only defaults apply."""
        result = resolve_rules(
            fdio_config, gerrit_project="csit", github_actor="human"
        )
        assert result.get_body_value("Type") is None
        assert result.get_trailer_value("Issue-ID") == "CIMAN-33"

    def test_hicn_human(self, fdio_config: CommitRulesConfig) -> None:
        """HICN has Type but no Issue-ID in project rules — gets default."""
        result = resolve_rules(
            fdio_config, gerrit_project="hicn", github_actor="human"
        )
        assert result.get_body_value("Type") == "ci"
        assert result.get_trailer_value("Issue-ID") == "CIMAN-33"


# ---------------------------------------------------------------------------
# apply_body_rules
# ---------------------------------------------------------------------------


class TestApplyBodyRules:
    """Tests for apply_body_rules."""

    def test_no_rules(self) -> None:
        body = "Subject line\n\nBody text."
        result = apply_body_rules(body, ResolvedCommitRules())
        assert result == body.rstrip()

    def test_single_rule_blank_line_separator(self) -> None:
        body = "Subject line\n\nBody text."
        rules = ResolvedCommitRules(
            body_rules=[
                CommitRule(
                    key="Type",
                    value="ci",
                    location="body",
                    separator="blank_line",
                )
            ]
        )
        result = apply_body_rules(body, rules)
        assert "Type: ci" in result
        # Should have blank line before
        assert "\n\nType: ci" in result

    def test_single_rule_no_separator(self) -> None:
        body = "Subject line\n\nBody text."
        rules = ResolvedCommitRules(
            body_rules=[
                CommitRule(
                    key="Type",
                    value="ci",
                    location="body",
                    separator="none",
                )
            ]
        )
        result = apply_body_rules(body, rules)
        assert result.endswith("Body text.\nType: ci")

    def test_multiple_body_rules(self) -> None:
        body = "Subject line\n\nBody text."
        rules = ResolvedCommitRules(
            body_rules=[
                CommitRule(
                    key="Type",
                    value="ci",
                    location="body",
                    separator="blank_line",
                ),
                CommitRule(
                    key="Ticket",
                    value="VPP-100",
                    location="body",
                    separator="none",
                ),
            ]
        )
        result = apply_body_rules(body, rules)
        assert "Type: ci" in result
        assert "Ticket: VPP-100" in result
        # Type comes before Ticket
        assert result.index("Type: ci") < result.index("Ticket: VPP-100")

    def test_skip_duplicate(self) -> None:
        body = "Subject line\n\nType: ci"
        rules = ResolvedCommitRules(
            body_rules=[CommitRule(key="Type", value="ci", location="body")]
        )
        result = apply_body_rules(body, rules)
        # Should NOT duplicate
        assert result.count("Type: ci") == 1

    def test_empty_body(self) -> None:
        rules = ResolvedCommitRules(
            body_rules=[
                CommitRule(
                    key="Type",
                    value="ci",
                    location="body",
                    separator="blank_line",
                )
            ]
        )
        result = apply_body_rules("", rules)
        assert "Type: ci" in result

    def test_body_with_trailing_newlines(self) -> None:
        body = "Subject\n\nBody text.\n\n\n"
        rules = ResolvedCommitRules(
            body_rules=[
                CommitRule(
                    key="Type",
                    value="fix",
                    location="body",
                    separator="blank_line",
                )
            ]
        )
        result = apply_body_rules(body, rules)
        assert "Type: fix" in result
        # Should not produce excessive blank lines
        assert "\n\n\n\n" not in result

    def test_substring_match_does_not_skip_insertion(self) -> None:
        """Dedup uses line-based check, not substring match."""
        body = "Subject line\n\nWe require Type: ci for this project."
        rules = ResolvedCommitRules(
            body_rules=[
                CommitRule(
                    key="Type",
                    value="ci",
                    location="body",
                    separator="blank_line",
                )
            ]
        )
        result = apply_body_rules(body, rules)
        # The rule line should be inserted as a standalone line
        # even though "Type: ci" appears as a substring
        assert "Type: ci" in result.splitlines()


# ---------------------------------------------------------------------------
# apply_trailer_rules
# ---------------------------------------------------------------------------


class TestApplyTrailerRules:
    """Tests for apply_trailer_rules."""

    def test_no_rules(self) -> None:
        trailers: list[str] = ["Signed-off-by: Test <t@t.com>"]
        result = apply_trailer_rules(trailers, ResolvedCommitRules())
        assert result == ["Signed-off-by: Test <t@t.com>"]

    def test_single_trailer_rule(self) -> None:
        trailers: list[str] = []
        rules = ResolvedCommitRules(
            trailer_rules=[CommitRule(key="Issue-ID", value="CIMAN-33")]
        )
        result = apply_trailer_rules(trailers, rules)
        assert "Issue-ID: CIMAN-33" in result

    def test_prepends_before_existing(self) -> None:
        trailers = ["Signed-off-by: Test <t@t.com>"]
        rules = ResolvedCommitRules(
            trailer_rules=[CommitRule(key="Ticket", value="VPP-100")]
        )
        result = apply_trailer_rules(trailers, rules)
        assert result[0] == "Ticket: VPP-100"
        assert result[1] == "Signed-off-by: Test <t@t.com>"

    def test_issue_id_override_skips_rule(self) -> None:
        trailers: list[str] = []
        rules = ResolvedCommitRules(
            trailer_rules=[CommitRule(key="Issue-ID", value="RULE-1")]
        )
        result = apply_trailer_rules(
            trailers, rules, issue_id_override="EXPLICIT-99"
        )
        assert "Issue-ID: RULE-1" not in result

    def test_issue_id_no_override(self) -> None:
        trailers: list[str] = []
        rules = ResolvedCommitRules(
            trailer_rules=[CommitRule(key="Issue-ID", value="RULE-1")]
        )
        result = apply_trailer_rules(trailers, rules, issue_id_override="")
        assert "Issue-ID: RULE-1" in result

    def test_skip_existing_trailer(self) -> None:
        trailers: list[str] = []
        rules = ResolvedCommitRules(
            trailer_rules=[CommitRule(key="Issue-ID", value="X-1")]
        )
        existing = {"Issue-ID": ["X-1"]}
        result = apply_trailer_rules(
            trailers, rules, existing_trailers=existing
        )
        assert "Issue-ID: X-1" not in result

    def test_allow_same_key_different_value(self) -> None:
        """Dedup checks key+value, not just key."""
        trailers: list[str] = []
        rules = ResolvedCommitRules(
            trailer_rules=[CommitRule(key="Issue-ID", value="NEW-1")]
        )
        existing = {"Issue-ID": ["OLD-1"]}
        result = apply_trailer_rules(
            trailers, rules, existing_trailers=existing
        )
        assert "Issue-ID: NEW-1" in result

    def test_skip_already_in_ordered_list(self) -> None:
        trailers = ["Issue-ID: X-1"]
        rules = ResolvedCommitRules(
            trailer_rules=[CommitRule(key="Issue-ID", value="X-1")]
        )
        result = apply_trailer_rules(trailers, rules)
        assert result.count("Issue-ID: X-1") == 1

    def test_multiple_trailer_rules_ordering(self) -> None:
        trailers: list[str] = []
        rules = ResolvedCommitRules(
            trailer_rules=[
                CommitRule(key="Type", value="ci"),
                CommitRule(key="Issue-ID", value="X-1"),
            ]
        )
        result = apply_trailer_rules(trailers, rules)
        assert result == ["Type: ci", "Issue-ID: X-1"]

    def test_non_issue_id_not_affected_by_override(self) -> None:
        """issue_id_override only suppresses Issue-ID rules."""
        trailers: list[str] = []
        rules = ResolvedCommitRules(
            trailer_rules=[
                CommitRule(key="Issue-ID", value="RULE-1"),
                CommitRule(key="Ticket", value="VPP-100"),
            ]
        )
        result = apply_trailer_rules(
            trailers, rules, issue_id_override="EXPLICIT-99"
        )
        assert "Issue-ID: RULE-1" not in result
        assert "Ticket: VPP-100" in result


# ---------------------------------------------------------------------------
# End-to-end integration: parse → resolve → apply
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Integration tests simulating the full pipeline."""

    @staticmethod
    def _build_commit(
        subject: str,
        body: str = "",
        trailers: list[str] | None = None,
    ) -> str:
        parts = [subject]
        if body:
            parts.append("")
            parts.append(body)
        if trailers:
            parts.append("")
            parts.extend(trailers)
        return "\n".join(parts)

    def test_fdio_vpp_full_pipeline(self) -> None:
        """Simulates VPP dependabot commit with Type + Issue-ID."""
        rules_json = json.dumps(
            {
                "defaults": [
                    {
                        "key": "Issue-ID",
                        "value": "CIMAN-33",
                        "location": "trailer",
                    }
                ],
                "projects": {
                    "vpp": [
                        {
                            "key": "Type",
                            "value": "ci",
                            "location": "body",
                            "separator": "blank_line",
                        },
                        {"key": "Issue-ID", "value": "CIMAN-33"},
                    ]
                },
            }
        )

        config = parse_commit_rules_json(rules_json)
        assert config is not None

        resolved = resolve_rules(
            config, gerrit_project="vpp", github_actor="dependabot[bot]"
        )

        # Apply body rules
        body = "gha: update actions/checkout from v3 to v4"
        body = apply_body_rules(body, resolved)
        assert "Type: ci" in body

        # Apply trailer rules
        trailers: list[str] = []
        trailers = apply_trailer_rules(trailers, resolved)
        assert "Issue-ID: CIMAN-33" in trailers

        # Simulate final message
        final = body + "\n\n" + "\n".join(trailers)
        assert "Type: ci" in final
        assert "Issue-ID: CIMAN-33" in final
        # Type should be in body (before trailer block)
        type_pos = final.index("Type: ci")
        issue_pos = final.index("Issue-ID: CIMAN-33")
        assert type_pos < issue_pos

    def test_fdio_csit_no_type(self) -> None:
        """CSIT doesn't need Type — only defaults apply."""
        rules_json = json.dumps(
            {
                "defaults": [{"key": "Issue-ID", "value": "CIMAN-33"}],
                "projects": {
                    "vpp": [
                        {"key": "Type", "value": "ci", "location": "body"},
                    ]
                },
            }
        )

        config = parse_commit_rules_json(rules_json)
        resolved = resolve_rules(config, gerrit_project="csit")

        body = "csit: update dependencies"
        body = apply_body_rules(body, resolved)
        assert "Type:" not in body

        trailers: list[str] = []
        trailers = apply_trailer_rules(trailers, resolved)
        assert "Issue-ID: CIMAN-33" in trailers

    def test_onap_issue_id_only(self) -> None:
        """ONAP projects typically just need Issue-ID."""
        rules_json = json.dumps(
            {
                "actors": {
                    "dependabot[bot]": [
                        {"key": "Issue-ID", "value": "CIMAN-33"}
                    ]
                }
            }
        )

        config = parse_commit_rules_json(rules_json)
        resolved = resolve_rules(
            config,
            gerrit_project="sdc",
            github_actor="dependabot[bot]",
        )

        body = "chore: bump dependencies"
        body = apply_body_rules(body, resolved)
        assert "Issue-ID" not in body  # No body rules

        trailers: list[str] = []
        trailers = apply_trailer_rules(trailers, resolved)
        assert "Issue-ID: CIMAN-33" in trailers

    def test_explicit_issue_id_overrides_rule(self) -> None:
        """ISSUE_ID input takes precedence over commit-rules Issue-ID."""
        rules_json = json.dumps(
            {"defaults": [{"key": "Issue-ID", "value": "RULE-DEFAULT"}]}
        )

        config = parse_commit_rules_json(rules_json)
        resolved = resolve_rules(config, gerrit_project="myproject")

        trailers: list[str] = []
        trailers = apply_trailer_rules(
            trailers, resolved, issue_id_override="EXPLICIT-99"
        )
        assert "Issue-ID: RULE-DEFAULT" not in trailers

    def test_body_rule_not_duplicated_when_present(self) -> None:
        """If body already contains the rule line, don't add it again."""
        rules_json = json.dumps(
            {"defaults": [{"key": "Type", "value": "ci", "location": "body"}]}
        )

        config = parse_commit_rules_json(rules_json)
        resolved = resolve_rules(config)

        body = "Subject\n\nType: ci"
        body = apply_body_rules(body, resolved)
        assert body.count("Type: ci") == 1

    def test_no_rules_json_is_noop(self) -> None:
        """Empty COMMIT_RULES_JSON should not affect commit messages."""
        config = parse_commit_rules_json("")
        resolved = resolve_rules(config)
        assert not resolved.has_rules

        body = "Subject\n\nOriginal body."
        assert apply_body_rules(body, resolved) == body.rstrip()

        trailers = ["Signed-off-by: T <t@t.com>"]
        assert apply_trailer_rules(trailers, resolved) == trailers


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_key_and_value_whitespace_stripped(self) -> None:
        data = {
            "defaults": [
                {"key": "  Type  ", "value": "  ci  ", "location": "body"}
            ]
        }
        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert config.defaults[0].key == "Type"
        assert config.defaults[0].value == "ci"

    def test_same_key_different_projects(self) -> None:
        """Different projects can have different values for the same key."""
        config = CommitRulesConfig(
            projects={
                "vpp": [CommitRule(key="Type", value="ci", location="body")],
                "hicn": [CommitRule(key="Type", value="fix", location="body")],
            }
        )
        vpp = resolve_rules(config, gerrit_project="vpp")
        hicn = resolve_rules(config, gerrit_project="hicn")
        assert vpp.get_body_value("Type") == "ci"
        assert hicn.get_body_value("Type") == "fix"

    def test_actor_adds_new_key_not_in_project(self) -> None:
        """Actor can add keys not present in project or defaults."""
        config = CommitRulesConfig(
            projects={
                "vpp": [CommitRule(key="Type", value="ci", location="body")]
            },
            actors={
                "bot": [
                    CommitRule(key="Fixes", value="abc123", location="body")
                ]
            },
        )
        result = resolve_rules(config, gerrit_project="vpp", github_actor="bot")
        # Both Type (from project) and Fixes (from actor) should be present
        assert result.get_body_value("Type") == "ci"
        assert result.get_body_value("Fixes") == "abc123"

    def test_large_json_document(self) -> None:
        """Handles a document with many projects and actors."""
        data: dict[str, object] = {
            "defaults": [{"key": "Issue-ID", "value": "DEFAULT"}],
            "projects": {},
            "actors": {},
        }
        projects: dict[str, list[dict[str, str]]] = {}
        actors: dict[str, list[dict[str, str]]] = {}
        for i in range(50):
            projects[f"project-{i}"] = [
                {"key": "Type", "value": f"type-{i}", "location": "body"}
            ]
            actors[f"actor-{i}"] = [{"key": "Issue-ID", "value": f"ID-{i}"}]
        data["projects"] = projects
        data["actors"] = actors

        config = parse_commit_rules_json(json.dumps(data))
        assert config is not None
        assert len(config.projects) == 50
        assert len(config.actors) == 50

        # Spot-check resolution
        result = resolve_rules(
            config, gerrit_project="project-42", github_actor="actor-7"
        )
        assert result.get_body_value("Type") == "type-42"
        assert result.get_trailer_value("Issue-ID") == "ID-7"

    @pytest.mark.parametrize(
        "json_str",
        [
            pytest.param("1" * 5000, id="integer-over-digit-limit"),
            pytest.param("[" * 100_000, id="nesting-too-deep"),
        ],
    )
    def test_unparsable_json_beyond_decoder_errors(self, json_str: str) -> None:
        """Inputs json.loads rejects without JSONDecodeError return None."""
        assert parse_commit_rules_json(json_str) is None
