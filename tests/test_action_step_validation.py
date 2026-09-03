# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for composite action step validation and integration.

This module validates the step execution flow, dependencies, and proper
integration between action steps.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml


# Constants for action validation
ALLOWED_BRANCH_REFS = ["main", "master"]
EXEMPT_WORKFLOW_PATTERN = "releng-reusable-workflows"
MIN_SHA_LENGTH = 7
HEX_CHARACTERS = "0123456789abcdef"


@pytest.fixture
def action_config():
    """Load action.yaml configuration."""
    action_path = Path(__file__).parent.parent / "action.yaml"
    with open(action_path) as f:
        return yaml.safe_load(f)


@pytest.fixture
def reusable_workflow():
    """Load the bundled reusable workflow."""
    path = (
        Path(__file__).parent.parent
        / ".github"
        / "workflows"
        / "github2gerrit.yaml"
    )
    with open(path) as f:
        return yaml.safe_load(f)


class TestReusableWorkflowCheckoutOrder:
    """The seed checkout in the reusable workflow is load-bearing.

    ``actions/checkout`` deletes the entire contents of its target
    directory when that directory is not already a git repository,
    regardless of ``clean``.  Seeding the workspace root with the target
    repository first makes it a repository, so the composite action's
    own push checkout finds a valid repo and leaves ``.g2g-action``
    alone.  Moving or removing this step breaks push runs.
    """

    def _steps(self, reusable_workflow):
        return reusable_workflow["jobs"]["github2gerrit"]["steps"]

    def _index(self, steps, predicate):
        return next(
            (i for i, step in enumerate(steps) if predicate(step)),
            -1,
        )

    def test_seed_checkout_precedes_action_checkout(self, reusable_workflow):
        steps = self._steps(reusable_workflow)

        seed_idx = self._index(
            steps,
            lambda s: (
                str(s.get("uses", "")).startswith("actions/checkout@")
                and "path" not in s.get("with", {})
            ),
        )
        action_idx = self._index(
            steps,
            lambda s: s.get("with", {}).get("path") == ".g2g-action",
        )
        invoke_idx = self._index(
            steps, lambda s: str(s.get("uses", "")) == "./.g2g-action"
        )

        assert seed_idx != -1, (
            "the reusable workflow must seed the workspace root with the "
            "target repository on push events"
        )
        assert action_idx != -1
        assert invoke_idx != -1
        assert seed_idx < action_idx, (
            "the seed checkout must precede the .g2g-action checkout, or "
            "the composite checkout will delete the action mid-run"
        )
        assert action_idx < invoke_idx

    def test_seed_checkout_is_push_gated(self, reusable_workflow):
        """Pull request runs must not check a fork head into the runner."""
        steps = self._steps(reusable_workflow)

        seed = steps[
            self._index(
                steps,
                lambda s: (
                    str(s.get("uses", "")).startswith("actions/checkout@")
                    and "path" not in s.get("with", {})
                ),
            )
        ]

        assert "github.event_name == 'push'" in str(seed.get("if", ""))
        assert "pull_request" not in str(seed.get("with", {}).get("ref", ""))


# Recognised settings a consumer cannot supply through the reusable
# workflow, each with the reason it is deliberately out of reach. See
# TestEveryKnownSettingIsReachable for what this list holds to account.
_SUPPLIED_BY_THE_ACTION = frozenset(
    {
        # The action provides these itself.
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GERRIT_SSH_PRIVKEY_G2G",  # a secret, not an input
        # Derived from another input rather than set directly.
        "SYNC_ALL_OPEN_PRS",  # from PR_NUMBER
        "G2G_VERBOSE",  # from VERBOSE
        "G2G_DRYRUN_DISABLE_NETWORK",  # from G2G_NO_GERRIT
    }
)

_LOCAL_CLI_ONLY = frozenset(
    {
        # Meaningful only when a person runs the tool themselves.
        "G2G_AUTO_SAVE_CONFIG",
        "G2G_RESPECT_USER_SSH",
        "G2G_SHOW_PROGRESS",
    }
)

_TESTING_ONLY = frozenset({"CI_TESTING", "USE_LOCAL_ACTION"})

_COMPOSITE_ACTION_ONLY = frozenset(
    {
        # Reconciliation tuning, documented as env-only. The names are
        # unprefixed, so forwarding them from repository variables
        # risks colliding with a variable a project already keeps for
        # something else, and silently changing behaviour on a name
        # clash is worse than the present limitation.
        "REUSE_STRATEGY",
        "SIMILARITY_SUBJECT",
        "SIMILARITY_UPDATE_FACTOR",
        "SIMILARITY_FILES",
        "ALLOW_ORPHAN_CHANGES",
        "PERSIST_SINGLE_MAPPING_COMMENT",
        "LOG_RECONCILE_JSON",
        "VERIFY_DIGEST_STRICT",
    }
)

_OUT_OF_REACH = (
    _SUPPLIED_BY_THE_ACTION
    | _LOCAL_CLI_ONLY
    | _TESTING_ONLY
    | _COMPOSITE_ACTION_ONLY
)


class TestEveryKnownSettingIsReachable:
    """Recognised configuration must be settable through the action.

    ``config.KNOWN_KEYS`` is the registry of settings the tool accepts.
    A key in that set which no consumer can supply is not a missing
    feature but a silent one: the project sets it, sees no warning, and
    gets the default. That failure has now appeared three times — the
    approver sources, the trusted associations, and the settings the
    README told people to pass with ``env:``, which does not cross a
    ``workflow_call`` boundary.

    So the rule is stated once here rather than rediscovered. A key is
    reachable when it is a workflow input, or forwarded from a
    repository variable, or listed above as deliberately out of reach.
    Adding a key forces that choice instead of leaving it to chance.
    """

    def _reachable(self, reusable_workflow) -> set[str]:
        triggers = reusable_workflow.get("on", reusable_workflow.get(True))
        declared = set(triggers["workflow_call"]["inputs"])

        step = next(
            s
            for s in reusable_workflow["jobs"]["github2gerrit"]["steps"]
            if s.get("name") == "Run github2gerrit composite action"
        )
        # Either block reaches the tool: `with` becomes an action
        # input, `env` is inherited by the composite action's steps.
        forwarded = {
            name
            for block in (step.get("with", {}), step.get("env", {}))
            for name, value in block.items()
            if "vars." in str(value)
        }
        return declared | forwarded

    def test_no_setting_is_silently_unreachable(self, reusable_workflow):
        from github2gerrit.config import KNOWN_KEYS

        unreachable = sorted(
            KNOWN_KEYS - self._reachable(reusable_workflow) - _OUT_OF_REACH
        )
        assert not unreachable, (
            f"{unreachable} are recognised configuration but cannot be "
            f"supplied through the reusable workflow, so a project "
            f"setting them gets the defaults and no warning. Add an "
            f"input, forward the repository variable, or record why "
            f"the setting is deliberately out of reach."
        )

    def test_the_exemptions_are_real_settings(self):
        # An exemption for a key that no longer exists hides a genuine
        # gap behind a stale name.
        from github2gerrit.config import KNOWN_KEYS

        assert not sorted(_OUT_OF_REACH - KNOWN_KEYS)

    def test_the_trust_set_is_reachable(self, reusable_workflow):
        # Called out on its own because its failure is the unsafe one:
        # a project tightening the trust set would believe it had, and
        # keep the permissive default.
        assert "G2G_TRUSTED_ASSOCIATIONS" in self._reachable(reusable_workflow)


class TestReusableWorkflowForwardsItsInputs:
    """Every declared input must reach the composite action.

    An input the workflow accepts but never passes on is worse than an
    absent one: the caller sets it, sees no error, and gets the
    default. That is how the approver settings were unusable through
    this interface when first written, and how
    ``G2G_TRUSTED_ASSOCIATIONS`` still is (#420).

    Stated as an invariant over whatever the workflow declares, so a
    future input cannot repeat it.
    """

    ACTION_STEP = "Run github2gerrit composite action"

    _INPUT_REFERENCE = re.compile(r"inputs\.([A-Za-z_][A-Za-z0-9_]*)")

    @staticmethod
    def _workflow_call(reusable_workflow):
        # YAML 1.1 resolves a bare `on:` key to the boolean True, so
        # the mapping is not reachable under the string "on".
        triggers = reusable_workflow.get("on", reusable_workflow.get(True))
        assert triggers is not None, "workflow declares no triggers"
        return triggers["workflow_call"]

    def _action_step(self, reusable_workflow):
        return next(
            step
            for step in reusable_workflow["jobs"]["github2gerrit"]["steps"]
            if step.get("name") == self.ACTION_STEP
        )

    def _forwarded_names(self, reusable_workflow) -> set[str]:
        """Return the input names the action step actually references.

        Whole names, extracted by pattern, rather than a substring
        test: `"inputs.GERRIT_SERVER" in text` is satisfied by a
        forwarded `inputs.GERRIT_SERVER_PORT`, so dropping the shorter
        mapping would go unnoticed. This interface has two such pairs.
        """
        step = self._action_step(reusable_workflow)
        text = "\n".join(
            [*step.get("with", {}).values(), *step.get("env", {}).values()]
        )
        return set(self._INPUT_REFERENCE.findall(text))

    def test_every_input_is_forwarded(self, reusable_workflow):
        declared = set(self._workflow_call(reusable_workflow)["inputs"])
        forwarded = self._forwarded_names(reusable_workflow)

        missing = sorted(declared - forwarded)
        assert not missing, (
            f"the reusable workflow declares {missing} but never passes "
            f"them to the action, so a caller setting them silently gets "
            f"the defaults"
        )

    def test_secrets_are_forwarded(self, reusable_workflow):
        declared = set(self._workflow_call(reusable_workflow)["secrets"])
        forwarded = "\n".join(
            self._action_step(reusable_workflow).get("with", {}).values()
        )
        missing = sorted(
            name for name in declared if f"secrets.{name}" not in forwarded
        )
        assert not missing, f"secrets declared but not forwarded: {missing}"


class TestExtractStepEventHandling:
    """The shipped extract script, executed rather than replicated.

    ``tests/test_action_pr_number_handling.py`` exercises a copy of
    this logic, which cannot notice the real script changing.  These
    run the script straight out of ``action.yaml``.
    """

    STEP_NAME = "Extract PR number, validate context"
    NORMALIZE_STEP = "Normalize PR_NUMBER"

    def _script(self, action_config, step_name=None):
        wanted = step_name or self.STEP_NAME
        step = next(
            s for s in action_config["runs"]["steps"] if s.get("name") == wanted
        )
        return step["run"]

    def _run(self, action_config, tmp_path, event_name, step_name=None, **env):
        output = tmp_path / "github_output"
        output.touch()
        environment = {
            **os.environ,
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_OUTPUT": str(output),
            "EVENT_PR_NUMBER": "",
            "DISPATCH_PR_NUMBER": "",
            "DISPATCH_SYNC_ALL": "",
            "INPUT_PR_NUMBER": "",
            **env,
        }
        result = subprocess.run(
            ["bash", "-c", self._script(action_config, step_name)],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        return result, output.read_text()

    def test_push_needs_no_pull_request(self, action_config, tmp_path):
        result, output = self._run(action_config, tmp_path, "push")
        assert result.returncode == 0, result.stderr
        assert "pr_number=" in output
        assert "sync_all" not in output

    def test_comment_uses_the_issue_number(self, action_config, tmp_path):
        result, output = self._run(
            action_config,
            tmp_path,
            "issue_comment",
            EVENT_PR_NUMBER="29",
        )
        assert result.returncode == 0, result.stderr
        assert "pr_number=29" in output

    @pytest.mark.parametrize("value", ["029", "0029", "01"])
    def test_non_canonical_dispatch_numbers_are_refused(
        self, action_config, tmp_path, value
    ):
        # The concurrency key uses this string raw, so '029' would key
        # on '029' while naming pull request 29 and race the events
        # for it. GitHub expressions cannot normalise it, so refuse it
        # here: a run that cannot start cannot race.
        result, _output = self._run(
            action_config,
            tmp_path,
            "workflow_dispatch",
            step_name=self.NORMALIZE_STEP,
            INPUT_PR_NUMBER=value,
        )
        assert result.returncode == 2
        assert "no leading zeros" in result.stdout

    def test_canonical_dispatch_number_is_accepted(
        self, action_config, tmp_path
    ):
        result, output = self._run(
            action_config,
            tmp_path,
            "workflow_dispatch",
            step_name=self.NORMALIZE_STEP,
            INPUT_PR_NUMBER="29",
        )
        assert result.returncode == 0, result.stderr
        assert "pr_number=29" in output

    def test_missing_pull_request_context_still_errors(
        self, action_config, tmp_path
    ):
        # Only the events that genuinely have no pull request may skip
        # this; everything else must keep failing loudly.
        result, _output = self._run(
            action_config, tmp_path, "pull_request_target"
        )
        assert result.returncode == 2
        assert "requires a valid pull request context" in result.stdout


def _open_command_phrases() -> list[str]:
    """Return every phrase anybody may use to direct the tool.

    Derived from the registry so that a workflow condition or a README
    example naming these phrases cannot fall behind a new alias.

    The mention is checked separately by callers, because the parser
    accepts any whitespace between it and the phrase; requiring one
    literal space would silently skip a supported directive.
    """
    from github2gerrit.pr_commands import COMMAND_REGISTRY

    phrases = [
        phrase
        for command in COMMAND_REGISTRY
        if not command.requires_trust
        for phrase in command.all_phrases()
    ]
    assert phrases, "no open command to admit"
    return phrases


def _readme_comment_guards() -> list[str]:
    """Return the comment-guard lines the README shows to readers."""
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    guards = [
        line
        for line in readme.splitlines()
        if "contains(github.event.comment.body" in line
    ]
    assert guards, "README shows no comment guard to check"
    return guards


class TestReusableWorkflowConcurrency:
    """The concurrency group must tell pull requests apart.

    GitHub keeps at most one running and one pending run per group and
    discards the rest.  A group that cannot distinguish two pull
    requests therefore does not merely serialise them, it silently
    drops runs — which for a trigger used to re-evaluate the fork
    approval gate means an approval that never takes effect.

    The invariant is expressed against the job's own ``if`` condition
    rather than a hard-coded list of events, so adding a trigger that
    admits a new payload shape fails here until the group can key on
    it.
    """

    _PR_NUMBER_ACCESSOR = re.compile(r"github\.event\.\w+\.number")

    def _job(self, reusable_workflow):
        return reusable_workflow["jobs"]["github2gerrit"]

    def test_group_keys_on_every_admitted_payload_shape(
        self, reusable_workflow
    ):
        job = self._job(reusable_workflow)
        admitted = set(self._PR_NUMBER_ACCESSOR.findall(job["if"]))
        assert admitted, "job condition admits no pull request payload"

        group = job["concurrency"]["group"]
        missing = sorted(a for a in admitted if a not in group)
        assert not missing, (
            f"the job admits runs via {missing} but the concurrency "
            f"group cannot key on them, so those runs would share one "
            f"group: {group}"
        )

    def test_dispatched_single_pr_shares_the_pull_request_lock(
        self, reusable_workflow
    ):
        # A single-PR workflow_dispatch carries neither payload
        # accessor and names its target in an input instead. Without
        # this it would sit in the event-name group and could transfer
        # concurrently with a comment-triggered run for the same PR.
        group = self._job(reusable_workflow)["concurrency"]["group"]
        assert "inputs.PR_NUMBER" in group
        # Used raw, because GitHub expressions have no arithmetic to
        # canonicalise it; the action rejects a non-canonical value
        # such as '029' instead, so a run that would key on the wrong
        # group never starts. '0' is a bulk sweep and '' is unset, so
        # both fall through to the event name rather than collapsing
        # every run into one group.
        assert "inputs.PR_NUMBER != '0'" in group
        assert "inputs.PR_NUMBER != ''" in group

    def test_ordinary_comments_do_not_enter_the_group(self, reusable_workflow):
        # GitHub keeps one running and one pending run per group and
        # cancels the older pending one, so an unrelated comment
        # admitted here can evict a re-check that somebody asked for.
        condition = self._job(reusable_workflow)["if"]
        assert "github.event.comment.body" in condition

    @pytest.mark.parametrize(
        "event_name", ["pull_request_review", "pull_request_review_comment"]
    )
    def test_review_events_do_not_take_the_lock(
        self, reusable_workflow, event_name
    ):
        # Neither can transfer anything, but both payloads carry
        # pull_request.number, so admitting them would take a slot in
        # the per-PR group and could evict a pending re-check.
        condition = self._job(reusable_workflow)["if"]
        assert f"github.event_name != '{event_name}'" in condition

    def test_fork_pull_request_events_do_not_take_the_lock(
        self, reusable_workflow
    ):
        # GitHub denies these secrets, so the run stops at
        # _skip_unprivileged_fork_run — but only after taking a slot in
        # the per-PR group, where it could evict a real re-check.
        condition = self._job(reusable_workflow)["if"]
        assert "github.event_name != 'pull_request'" in condition
        assert (
            "github.event.pull_request.head.repo.full_name == "
            "github.repository" in condition
        )

    def test_keyless_modes_survive_the_fork_exclusion(self, reusable_workflow):
        # G2G_NO_GERRIT and DRY_RUN never reach Gerrit, and this
        # repository's own testing workflow calls this one with
        # G2G_NO_GERRIT on every trigger. Excluding fork pull_request
        # unconditionally would drop that coverage entirely.
        condition = self._job(reusable_workflow)["if"]
        assert "inputs.G2G_NO_GERRIT" in condition
        assert "inputs.DRY_RUN" in condition
        # The action step falls back to the repository variable, so
        # this condition has to as well or a project setting only the
        # variable is skipped before the action ever sees it.
        assert "vars.G2G_NO_GERRIT == 'true'" in condition

    def test_fork_pull_request_target_is_still_admitted(
        self, reusable_workflow
    ):
        # The privileged trigger applies the gate, so excluding fork
        # heads there would disable the feature entirely. The head
        # test must be scoped to pull_request alone.
        condition = self._job(reusable_workflow)["if"]
        assert "github.event_name != 'pull_request_target'" not in condition

    def test_readme_examples_reject_closed_pull_requests(self):
        # The bundled workflow guards this; a reader copying the
        # composite-action example gets no such condition unless the
        # example carries it.
        for guard in _readme_comment_guards():
            assert "github.event.issue.state == 'open'" in guard, (
                "a README comment guard admits closed pull requests, so "
                "anyone could start a run on a merged one"
            )

    def test_comments_on_closed_pull_requests_are_not_admitted(
        self, reusable_workflow
    ):
        # A closed pull request has no gate left to lift, so a run
        # there could only fail. Any commenter could otherwise put a
        # failing check on it at will.
        condition = self._job(reusable_workflow)["if"]
        assert "github.event.issue.state == 'open'" in condition

    def test_every_open_command_phrase_is_admitted(self, reusable_workflow):
        # The condition names the command phrases, which duplicates the
        # registry in YAML. Without this, adding an alias would leave a
        # documented directive that silently never starts a run.
        condition = self._job(reusable_workflow)["if"]
        missing = [
            f"'{phrase}'"
            for phrase in _open_command_phrases()
            if f"'{phrase}'" not in condition
        ]
        assert not missing, (
            f"the job condition does not admit {missing}, so those "
            f"directives would never start a run"
        )

    def test_the_mention_is_tested_separately(self, reusable_workflow):
        # The parser accepts any whitespace between the mention and
        # the phrase, and GitHub's mention autocomplete inserts a
        # trailing space, so '@github2gerrit  check' is both supported
        # and likely. Joining them into one literal would skip it.
        condition = self._job(reusable_workflow)["if"]
        assert "'@github2gerrit'" in condition
        for phrase in _open_command_phrases():
            assert f"'@github2gerrit {phrase}'" not in condition

    def test_readme_examples_admit_the_same_phrases(self):
        # Readers copy these guards verbatim, so an example that omits
        # an alias hands them a directive that silently does nothing.
        guards = _readme_comment_guards()
        joined = "\n".join(guards)
        assert "'@github2gerrit'" in joined
        missing = [
            f"'{phrase}'"
            for phrase in _open_command_phrases()
            if f"'{phrase}'" not in joined
        ]
        assert not missing, (
            f"the README comment guards omit {missing}, so a reader "
            f"copying them would find those directives ignored"
        )

    def test_group_falls_back_to_the_event_name(self, reusable_workflow):
        # Non-pull-request runs (push and dispatch) get one
        # group each rather than colliding on an empty operand.
        group = self._job(reusable_workflow)["concurrency"]["group"]
        assert "github.event_name" in group
        assert group.split("||")[-1].strip().startswith("github.event_name")

    def test_in_flight_runs_are_queued_not_cancelled(self, reusable_workflow):
        # An interrupted run may already have pushed to Gerrit.
        concurrency = self._job(reusable_workflow)["concurrency"]
        assert concurrency["cancel-in-progress"] is False


class TestActionStepValidation:
    """Test action step validation and execution flow."""

    def test_step_execution_order(self, action_config):
        """Test that steps are in the correct execution order."""
        steps = action_config["runs"]["steps"]
        step_names = [step.get("name", "") for step in steps]

        # Define expected order dependencies
        order_requirements = [
            ("Setup Python", "Setup uv"),
            ("Setup Python", "Setup github2gerrit"),
            ("Setup uv", "Setup github2gerrit"),
            # Push reconciliation reads commit trailers from the working
            # directory, so the checkout must precede the CLI.
            ("Checkout repository (push events)", "Run github2gerrit"),
            ("Setup github2gerrit", "Run github2gerrit Python CLI"),
            ("Run github2gerrit Python CLI", "Capture outputs"),
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
            assert "inputs.ISSUE_ID_LOOKUP == 'true'" in if_condition

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
                    action_ref not in ALLOWED_BRANCH_REFS
                    and EXEMPT_WORKFLOW_PATTERN not in uses_value
                ):
                    assert len(action_ref) >= MIN_SHA_LENGTH, (
                        f"SHA too short in {uses_value}"
                    )
                    assert all(
                        c in HEX_CHARACTERS for c in action_ref.lower()
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
        assert (
            with_config["python-version-file"]
            == "${{ github.action_path }}/pyproject.toml"
        )
        # No pip caching since we use uv for dependency management
        assert "cache" not in with_config

    def test_uv_setup_step(self, action_config):
        """Test UV setup step configuration."""
        steps = action_config["runs"]["steps"]
        uv_step = next(
            (step for step in steps if step.get("name") == "Setup uv"), None
        )

        assert uv_step is not None
        assert uv_step["uses"].startswith("astral-sh/setup-uv@")

    def test_repository_checkout_restricted_to_push(self, action_config):
        """No checkout may place pull request head content in the runner.

        The tool fetches ``refs/pull/<N>/head`` into a private temporary
        directory itself, so a runner-level checkout is redundant for
        pull requests.  It is also actively harmful: under
        ``pull_request_target`` a checkout of a fork PR head is refused
        by ``actions/checkout``, and any file it leaves in the working
        directory (notably ``.gitreview``) becomes fork-controlled input
        to Gerrit target resolution.

        Push runs still check out, because merged-PR reconciliation
        reads commit trailers from the working directory.
        """
        steps = action_config["runs"]["steps"]

        checkout_steps = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]

        # Push reconciliation reads commit trailers from the working
        # directory, so the push checkout is required, not merely
        # tolerated. Assert presence first so the loop below cannot pass
        # vacuously if the step is ever removed.
        assert len(checkout_steps) == 1, (
            "expected exactly one checkout step (push events); "
            f"found: {[s.get('name') for s in checkout_steps]}"
        )

        for step in checkout_steps:
            condition = str(step.get("if", ""))
            assert "github.event_name == 'push'" in condition, (
                f"checkout step {step.get('name')!r} must be gated to "
                "push events"
            )

            ref = str(step.get("with", {}).get("ref", ""))
            assert "pull_request" not in ref, (
                f"checkout step {step.get('name')!r} must not check out "
                "pull request head content"
            )

            # The reusable workflow places this action at .g2g-action
            # before invoking it. A cleaning checkout would delete it
            # mid-run, which is what SKIP_CHECKOUT used to guard.
            assert step.get("with", {}).get("clean") is False, (
                f"checkout step {step.get('name')!r} must set clean: false "
                "so it cannot delete caller-placed workspace content"
            )

    def test_dependency_installation_step(self, action_config):
        """Test dependency installation step."""
        steps = action_config["runs"]["steps"]
        install_step = next(
            (
                step
                for step in steps
                if "Setup github2gerrit" in step.get("name", "")
            ),
            None,
        )

        assert install_step is not None
        assert install_step["shell"] == "bash"

        script = install_step["run"]
        assert "uv --version" in script
        # Inputs/context are passed via env (not inline ${{ }}) to avoid
        # template injection; the script branches on env/built-in vars.
        assert "GITHUB_REPOSITORY" in script
        assert "=~ lfreleng-actions/github2gerrit-action" in script
        assert 'uv pip install --system "${GITHUB_ACTION_PATH}"' in script
        assert "uvx will install GitHub2Gerrit from PyPI" in script
        # USE_LOCAL_ACTION is provided through the step env block.
        assert install_step.get("env", {}).get("USE_LOCAL_ACTION") == (
            "${{ inputs.USE_LOCAL_ACTION }}"
        )

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
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("PR_NUMBER", "SYNC_ALL_OPEN_PRS")
            }
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
