# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The push argv routes correctly through the real git-review.

The unit tests for the argv check it against a copy of git-review's
argument grammar. This test hands the argv that ``_push_to_gerrit``
builds to the installed git-review itself, in dry-run mode against a
local repository standing in for Gerrit, and reads back the push it
would perform. A change in git-review's grammar that misroutes the push
or drops a reviewer then fails here (#502).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import RepoNames


STABLE = "stable/vanadium"
REVIEWERS = ["alice@example.org", "bob@example.org"]
_BESIDE_PYTHON = Path(sys.executable).with_name("git-review")
GIT_REVIEW: str = shutil.which("git-review") or (
    str(_BESIDE_PYTHON) if _BESIDE_PYTHON.exists() else ""
)
pytestmark = pytest.mark.skipif(
    not GIT_REVIEW, reason="git-review is not installed"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    """Build a clone whose .gitreview defaults to master, on STABLE."""
    gerrit = tmp_path / "gerrit.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(gerrit))
    _git(tmp_path, "init", "-q", "-b", "master", str(work))
    # The autouse fixture strips the SSH agent, so a developer whose
    # global config signs commits with an agent-held key cannot commit
    # here. Signing adds nothing to what this test checks.
    _git(work, "config", "commit.gpgsign", "false")
    (work / ".gitreview").write_text(
        "[gerrit]\nhost=gerrit.example.org\nproject=example.git\n"
        "defaultbranch=master\n"
    )
    _git(work, "add", ".gitreview")
    _git(work, "commit", "-q", "-m", "Base")
    _git(work, "branch", STABLE)
    _git(work, "remote", "add", "gerrit", str(gerrit))
    _git(work, "push", "-q", "gerrit", "master", STABLE)
    _git(work, "checkout", "-q", STABLE)
    (work / "pom.xml").write_text("<version>1.1</version>\n")
    _git(work, "add", "pom.xml")
    _git(work, "commit", "-q", "-m", "Bump foo from 1.0 to 1.1")
    _git(work, "fetch", "-q", "gerrit")
    hook = work / ".git" / "hooks" / "commit-msg"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    return work


def _push_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Return the git-review argv that _push_to_gerrit runs."""
    monkeypatch.delenv("CI_TESTING", raising=False)
    monkeypatch.setenv("PR_NUMBER", "39")
    monkeypatch.setenv("G2G_TOPIC_PREFIX", "GH")
    recorded: list[list[str]] = []

    def record(cmd: list[str], **_kwargs: Any) -> None:
        recorded.append(list(cmd))

    monkeypatch.setattr("github2gerrit.core.run_cmd", record)
    Orchestrator(workspace=tmp_path)._push_to_gerrit(
        gerrit=GerritInfo(
            host="gerrit.example.org", port=29418, project="example"
        ),
        repo=RepoNames(project_gerrit="example", project_github="example"),
        branch=STABLE,
        reviewers=", ".join(REVIEWERS),
        single_commits=False,
    )
    return next(cmd for cmd in recorded if cmd[:2] == ["git", "review"])


def test_git_review_pushes_to_the_branch_with_every_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _push_argv(tmp_path, monkeypatch)
    work = _repository(tmp_path)

    run = subprocess.run(
        [GIT_REVIEW, "--dry-run", *argv[2:]],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )

    match = re.search(r"HEAD:refs/for/(\S+)", run.stdout)
    assert match, run.stdout
    ref, _, options = match.group(1).partition("%")
    assert ref == STABLE
    assert options.split(",") == [
        "topic=GH-example-39",
        *(f"r={reviewer}" for reviewer in REVIEWERS),
    ]
