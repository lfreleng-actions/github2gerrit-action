# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests for composite action PR_NUMBER handling logic (Phase 1 gap closure).

We emulate the shell logic contained in action.yaml that normalizes and
validates PR_NUMBER for workflow_dispatch and non-dispatch events.

Scenarios covered:
- workflow_dispatch with PR_NUMBER=0 (bulk mode)
- workflow_dispatch with specific numeric PR_NUMBER
- workflow_dispatch with invalid non-numeric PR_NUMBER
- Non-dispatch event rejecting explicit PR_NUMBER input
- Non-dispatch event deriving PR number from event context
- Non-dispatch event missing PR context (error)
"""

from __future__ import annotations

import subprocess
import textwrap


# ---------------------------------------------------------------------------
# Shell snippet replicating the relevant action.yaml steps
# (trimmed and adapted for test determinism)
# ---------------------------------------------------------------------------

_ACTION_SNIPPET = textwrap.dedent(
    """
    set -euo pipefail

    EVENT_NAME="$1"
    INPUT_PR_NUMBER="$2"
    EVENT_PR_NUMBER="${3:-}"  # Simulates extraction from event payload

    # Step: Validate PR_NUMBER usage on non-dispatch events
    if [ "${EVENT_NAME}" != "workflow_dispatch" ] && \
       [ -n "${INPUT_PR_NUMBER}" ] && \
       [ "${INPUT_PR_NUMBER}" != "0" ]; then
        echo "Error: PR_NUMBER only valid during workflow_dispatch events." >&2
        exit 2
    fi

    # Step: Normalize PR_NUMBER for workflow_dispatch
    if [ "${EVENT_NAME}" = "workflow_dispatch" ]; then
        pr_in="${INPUT_PR_NUMBER}"
        if [ -z "${pr_in}" ] || [ "${pr_in}" = "null" ]; then
            pr_in="0"
        fi
        if ! echo "${pr_in}" | grep -Eq '^[0-9]+$'; then
            echo "Error: PR_NUMBER must be a numeric value" >&2
            exit 2
        fi
        if [ "${pr_in}" = "0" ]; then
            SYNC_ALL_OPEN_PRS=true
            echo "SYNC_ALL_OPEN_PRS=true"
        else
            PR_NUMBER="${pr_in}"
            echo "PR_NUMBER=${PR_NUMBER}"
        fi
        exit 0
    fi

    # Non-dispatch event: derive PR_NUMBER if not set
    if [ -z "${PR_NUMBER:-}" ]; then
        PR_NUMBER="${EVENT_PR_NUMBER}"
    fi
    if [ -z "${PR_NUMBER}" ] || [ "${PR_NUMBER}" = "null" ]; then
        echo "Error: PR_NUMBER is empty." >&2
        echo "This action requires a valid pull request context." >&2
        echo "Current event: ${EVENT_NAME}" >&2
        exit 2
    fi
    echo "PR_NUMBER=${PR_NUMBER}"
    """
).strip()


def _run_action_logic(
    event_name: str,
    pr_input: str,
    event_pr_number: str | None = "",
) -> subprocess.CompletedProcess[str]:
    """
    Execute the embedded action logic snippet and return the process result.
    """
    cmd = [
        "bash",
        "-c",
        _ACTION_SNIPPET,
        "--",
        event_name,
        pr_input,
        event_pr_number or "",
    ]
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_workflow_dispatch_bulk_all() -> None:
    """PR_NUMBER=0 on workflow_dispatch enables bulk mode."""
    res = _run_action_logic("workflow_dispatch", "0")
    assert res.returncode == 0, res.stderr
    assert "SYNC_ALL_OPEN_PRS=true" in res.stdout
    assert "PR_NUMBER=" not in res.stdout


def test_workflow_dispatch_specific_pr() -> None:
    """Specific numeric PR_NUMBER is preserved."""
    res = _run_action_logic("workflow_dispatch", "17")
    assert res.returncode == 0, res.stderr
    assert "PR_NUMBER=17" in res.stdout
    assert "SYNC_ALL_OPEN_PRS" not in res.stdout


def test_workflow_dispatch_invalid_alpha_pr() -> None:
    """Alpha PR_NUMBER value causes an exit code 2."""
    res = _run_action_logic("workflow_dispatch", "abc")
    assert res.returncode == 2
    assert "must be a numeric value" in res.stderr


def test_non_dispatch_rejects_explicit_pr_number() -> None:
    """
    Non-dispatch event must reject explicit PR_NUMBER input per action rules.
    """
    res = _run_action_logic("pull_request_target", "12")
    assert res.returncode == 2
    assert "only valid during workflow_dispatch" in res.stderr


def test_non_dispatch_derives_pr_number_success() -> None:
    """Non-dispatch event derives PR number from event payload."""
    res = _run_action_logic("pull_request_target", "", "42")
    assert res.returncode == 0, res.stderr
    assert "PR_NUMBER=42" in res.stdout


def test_non_dispatch_missing_pr_context_error() -> None:
    """Missing PR context on non-dispatch event triggers error."""
    res = _run_action_logic("pull_request_target", "", "")
    assert res.returncode == 2
    assert "requires a valid pull request context" in res.stderr
