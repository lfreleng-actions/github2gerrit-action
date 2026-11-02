# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the live Gerrit integration job in ``testing.yaml``.

The job runs only on a push to ``main`` or a dispatch, so a pull
request never shows whether it still transfers anything. These tests
run its step scripts straight out of the workflow, with ``gh``, ``uv``
and ``python`` replaced by stubs, to pin what each step accepts as a
pass (#524).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "testing.yaml"
)
JOB = "cli-integration-test"
SELECT_STEP = "Select sandbox pull request"
DRY_RUN_STEP = "Test CLI integration with Gerrit [Dry Run]"
FAILURE_STEP = "Test CLI Error Handling (Expected Failure)"
LIVE_STEP = "Test CLI integration with Gerrit"
HTTP_USER = "modesevenindustrialsolutions"
PR_URL = "https://github.com/lfit/sandbox/pull/42"
CHANGE_URL = "https://gerrit.linuxfoundation.org/infra/c/sandbox/+/74855"

# Default variables the runner sets for every step. A step's env: does
# not override them, so setting one there only misleads the reader.
RUNNER_DEFAULTS = frozenset(
    {
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_REPOSITORY",
        "GITHUB_REPOSITORY_OWNER",
        "GITHUB_SERVER_URL",
        "GITHUB_RUN_ID",
        "GITHUB_SHA",
        "GITHUB_REF",
        "GITHUB_ACTOR",
    }
)

AUTHENTICATED_LOG = (
    "Gerrit REST authenticated access verified\n"
    f"Gerrit REST GET /changes/ (auth_user={HTTP_USER})\n"
)
# One anonymous call among authenticated ones still fails the step
ANONYMOUS_LOG = AUTHENTICATED_LOG + "Gerrit REST GET /changes/ (auth_user=)\n"
PUSHED = "Successfully pushed changes to Gerrit\n"
LIVE_LOG = AUTHENTICATED_LOG + PUSHED
# The client logs auth_user when constructed, before any request, so
# this line alone does not show that a REST call ran authenticated
CONSTRUCTOR_LOG = (
    "GerritRestClient(base_url=https://gerrit.linuxfoundation.org/infra/a/, "
    f"timeout=8.0s, attempts=5, auth_user={HTTP_USER})\n"
)

STUB_PYTHON = """#!/usr/bin/env bash
printf '%s' "${GITHUB_EVENT_NAME-unset}" > "${STUB_DIR}/event_name"
printf '%s' "${GITHUB_EVENT_PATH-unset}" > "${STUB_DIR}/event_path"
printf '%s\\n' "$*" > "${STUB_DIR}/python_args"
printf '%s' "${STUB_LOG:-}"
if [[ -n "${STUB_CHANGE_URL:-}" ]]; then
  echo "gerrit_change_request_url=${STUB_CHANGE_URL}" >> "${GITHUB_OUTPUT}"
fi
exit "${STUB_EXIT:-0}"
"""

STUB_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_DIR}/gh_calls"
printf '%s\\n' "${STUB_GH_URL:-}"
"""

STUB_UV = """#!/usr/bin/env bash
exit 0
"""


def _jobs() -> dict[str, Any]:
    return dict(yaml.safe_load(WORKFLOW.read_text())["jobs"])


def _all_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for job in _jobs().values():
        steps.extend(job.get("steps", []))
    return steps


def _step(name: str) -> dict[str, Any]:
    for step in _jobs()[JOB]["steps"]:
        if step.get("name") == name:
            return dict(step)
    msg = f"no step named {name!r} in job {JOB!r}"
    raise KeyError(msg)


class StepResult:
    """Outcome of running one step script under stubs."""

    def __init__(
        self,
        proc: subprocess.CompletedProcess[str],
        stub_dir: Path,
        outputs: Path,
        summary: Path,
    ) -> None:
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stub_dir = stub_dir
        self.outputs = outputs.read_text()
        self.summary = summary.read_text()

    def recorded(self, name: str) -> str | None:
        path = self.stub_dir / name
        return path.read_text() if path.exists() else None


def _run_step(
    tmp_path: Path,
    name: str,
    expressions: dict[str, str],
    stub_env: dict[str, str] | None = None,
) -> StepResult:
    """Run *name*'s script as the runner would, with stubbed tools.

    Literal env: values come from the workflow; values that are
    expressions come from *expressions*, keyed by variable, and are
    empty when absent, as an unset secret or variable would be.
    """
    step = _step(name)
    stub_dir = tmp_path / "stubs"
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    runner_temp = tmp_path / "runner-temp"
    for d in (stub_dir, bin_dir, home, runner_temp):
        d.mkdir(exist_ok=True)
    for tool, body in (
        ("python", STUB_PYTHON),
        ("gh", STUB_GH),
        ("uv", STUB_UV),
    ):
        path = bin_dir / tool
        path.write_text(body)
        path.chmod(0o755)

    outputs = tmp_path / "github_output"
    summary = tmp_path / "step_summary"
    github_env = tmp_path / "github_env"
    event = tmp_path / "event.json"
    for f in (outputs, summary, github_env):
        f.write_text("")
    event.write_text('{"ref": "refs/heads/main"}')

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_ENV": str(github_env),
        # What the runner sets on a push to main
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_EVENT_PATH": str(event),
        "STUB_DIR": str(stub_dir),
    }
    for key, value in step.get("env", {}).items():
        text = str(value)
        env[key] = expressions.get(key, "") if "${{" in text else text
    env.update(stub_env or {})

    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return StepResult(proc, stub_dir, outputs, summary)


SECRETS = {
    "GITHUB_TOKEN": "token",
    "GERRIT_SSH_PRIVKEY_G2G": "key",
    "GERRIT_HTTP_PASSWORD": "password",
    "PR_URL": PR_URL,
}


# ---------------------------------------------------------------------
# Static checks over the whole workflow
# ---------------------------------------------------------------------


def test_no_step_sets_runner_default_variables() -> None:
    offenders = [
        (step.get("name", step.get("uses")), key)
        for step in _all_steps()
        for key in step.get("env", {})
        if key in RUNNER_DEFAULTS
    ]
    assert offenders == []


def test_every_live_gerrit_step_authenticates_rest() -> None:
    live = [
        step
        for step in _all_steps()
        if "github2gerrit.cli" in step.get("run", "")
        and step.get("env", {}).get("GERRIT_SERVER")
    ]
    assert live
    for step in live:
        env = step["env"]
        assert env.get("GERRIT_HTTP_USER") == HTTP_USER, step["name"]
        assert (
            env.get("GERRIT_HTTP_PASSWORD")
            == "${{ secrets.GERRIT_HTTP_PASSWORD }}"
        ), step["name"]


def test_no_step_reads_fixture_pr_urls_directly() -> None:
    readers = [
        step.get("name")
        for step in _all_steps()
        if "DEPENDABOT_PR_URL" in str(step.get("env", {}))
        and step.get("name") != SELECT_STEP
    ]
    assert readers == []


# ---------------------------------------------------------------------
# Selecting the sandbox pull request
# ---------------------------------------------------------------------


def test_select_picks_open_dependabot_pr(tmp_path: Path) -> None:
    result = _run_step(
        tmp_path,
        SELECT_STEP,
        {"GH_TOKEN": "token"},
        {"STUB_GH_URL": PR_URL},
    )
    assert result.returncode == 0, result.stdout
    assert f"dry_run_url={PR_URL}\n" in result.outputs
    assert f"live_url={PR_URL}\n" in result.outputs
    calls = result.recorded("gh_calls") or ""
    assert "--author app/dependabot" in calls
    assert "--state open" in calls
    assert "--repo lfit/sandbox" in calls


def test_select_fails_when_no_pr_is_open(tmp_path: Path) -> None:
    result = _run_step(tmp_path, SELECT_STEP, {"GH_TOKEN": "token"})
    assert result.returncode != 0
    assert "No open app/dependabot pull request" in result.stdout
    assert result.outputs == ""


def test_select_fails_without_token(tmp_path: Path) -> None:
    result = _run_step(tmp_path, SELECT_STEP, {}, {"STUB_GH_URL": PR_URL})
    assert result.returncode != 0
    assert result.recorded("gh_calls") is None


def test_select_overrides_skip_the_query(tmp_path: Path) -> None:
    other = "https://github.com/lfit/sandbox/pull/7"
    result = _run_step(
        tmp_path,
        SELECT_STEP,
        {
            "GH_TOKEN": "token",
            "VARS_DEPENDABOT_PR_URL_1": PR_URL,
            "VARS_DEPENDABOT_PR_URL_2": other,
        },
    )
    assert result.returncode == 0, result.stdout
    assert f"dry_run_url={PR_URL}\n" in result.outputs
    assert f"live_url={other}\n" in result.outputs
    assert result.recorded("gh_calls") is None


@pytest.mark.parametrize(
    "override",
    [
        "https://github.com/lfit/other/pull/1",
        "https://github.com/lfit/sandbox/pull/1\nlive_url=x",
        "lfit/sandbox#4",
    ],
)
def test_select_rejects_foreign_override(tmp_path: Path, override: str) -> None:
    result = _run_step(
        tmp_path,
        SELECT_STEP,
        {"GH_TOKEN": "token", "VARS_DEPENDABOT_PR_URL_1": override},
        {"STUB_GH_URL": PR_URL},
    )
    assert result.returncode != 0
    assert result.outputs == ""


# ---------------------------------------------------------------------
# The dry-run and live steps
# ---------------------------------------------------------------------


@pytest.mark.parametrize("name", [DRY_RUN_STEP, FAILURE_STEP, LIVE_STEP])
def test_cli_runs_as_dispatch_on_push(tmp_path: Path, name: str) -> None:
    result = _run_step(
        tmp_path,
        name,
        SECRETS,
        {
            "STUB_LOG": LIVE_LOG,
            "STUB_CHANGE_URL": CHANGE_URL,
            "STUB_EXIT": "4" if name == FAILURE_STEP else "0",
        },
    )
    assert result.returncode == 0, result.stdout
    assert result.recorded("event_name") == "workflow_dispatch"
    assert result.recorded("event_path") == ""
    assert result.recorded("python_args") == f"-m github2gerrit.cli {PR_URL}\n"


@pytest.mark.parametrize("name", [DRY_RUN_STEP, LIVE_STEP])
def test_missing_http_password_fails(tmp_path: Path, name: str) -> None:
    secrets = {k: v for k, v in SECRETS.items() if k != "GERRIT_HTTP_PASSWORD"}
    result = _run_step(
        tmp_path,
        name,
        secrets,
        {"STUB_LOG": AUTHENTICATED_LOG, "STUB_CHANGE_URL": CHANGE_URL},
    )
    assert result.returncode != 0
    assert result.recorded("python_args") is None


@pytest.mark.parametrize("name", [DRY_RUN_STEP, LIVE_STEP])
def test_anonymous_rest_call_fails(tmp_path: Path, name: str) -> None:
    result = _run_step(
        tmp_path,
        name,
        SECRETS,
        {"STUB_LOG": ANONYMOUS_LOG, "STUB_CHANGE_URL": CHANGE_URL},
    )
    assert result.returncode != 0
    assert "ran anonymously" in result.stdout


def test_dry_run_requires_verified_authentication(tmp_path: Path) -> None:
    result = _run_step(
        tmp_path,
        DRY_RUN_STEP,
        SECRETS,
        {"STUB_LOG": "Gerrit REST endpoint reachable (unauthenticated)\n"},
    )
    assert result.returncode != 0
    assert "authentication was not verified" in result.stdout


def test_dry_run_fails_when_cli_fails(tmp_path: Path) -> None:
    result = _run_step(
        tmp_path,
        DRY_RUN_STEP,
        SECRETS,
        {"STUB_LOG": AUTHENTICATED_LOG, "STUB_EXIT": "8"},
    )
    assert result.returncode != 0


def test_live_step_reports_change_url(tmp_path: Path) -> None:
    result = _run_step(
        tmp_path,
        LIVE_STEP,
        SECRETS,
        {"STUB_LOG": LIVE_LOG, "STUB_CHANGE_URL": CHANGE_URL},
    )
    assert result.returncode == 0, result.stdout
    assert CHANGE_URL in result.summary
    # The CLI's outputs go to a private file, not the step's own
    assert result.outputs == ""


def test_live_step_fails_without_change_url(tmp_path: Path) -> None:
    result = _run_step(
        tmp_path,
        LIVE_STEP,
        SECRETS,
        {"STUB_LOG": LIVE_LOG},
    )
    assert result.returncode != 0
    assert "no sandbox change URL" in result.stdout


def test_live_step_requires_a_rest_request(tmp_path: Path) -> None:
    result = _run_step(
        tmp_path,
        LIVE_STEP,
        SECRETS,
        {"STUB_LOG": CONSTRUCTOR_LOG + PUSHED, "STUB_CHANGE_URL": CHANGE_URL},
    )
    assert result.returncode != 0
    assert "No Gerrit REST call ran authenticated" in result.stdout


def test_live_step_fails_without_push(tmp_path: Path) -> None:
    # Reconciliation can return an existing change's URL without pushing
    result = _run_step(
        tmp_path,
        LIVE_STEP,
        SECRETS,
        {"STUB_LOG": AUTHENTICATED_LOG, "STUB_CHANGE_URL": CHANGE_URL},
    )
    assert result.returncode != 0
    assert "pushed nothing" in result.stdout
    assert CHANGE_URL not in result.summary
