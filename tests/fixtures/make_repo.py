# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Small helper to initialize a temporary git repository for integration-like tests.

This module avoids any network or third-party tool usage. It shells out to
the local `git` executable to set up a minimal repo with a configurable
default branch, test user identity, and a few convenience helpers to write
files and create commits/branches.

Typical usage in a test:

    from pathlib import Path
    from tests.fixtures.make_repo import init_repo

    def test_something(tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "repo", default_branch="main")
        repo.write("README.md", "# Hello\\n")
        repo.add_commit("Add README", ["README.md"])

        repo.create_branch("feature/test", checkout=True)
        repo.write("f.txt", "data\\n")
        repo.add_commit("Add f.txt", ["f.txt"])

        # Now you can run your code under test pointing to repo.path

Notes:
- The default user identity is set to a deterministic test identity to avoid
  relying on global git config in CI.
- The default branch is "main" (configurable).
- No remote is configured.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


def _to_str(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    full_cmd: list[str] = ["git", *args]
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    # Force agent-less, non-interactive SSH for tests
    merged_env["SSH_AUTH_SOCK"] = ""
    merged_env["SSH_AGENT_PID"] = ""
    merged_env.setdefault(
        "GIT_SSH_COMMAND",
        "ssh -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes "
        "-o PreferredAuthentications=publickey -o StrictHostKeyChecking=yes "
        "-o PasswordAuthentication=no -o ConnectTimeout=5",
    )
    # Ensure identity for non-interactive commits in tests
    merged_env.setdefault("GIT_AUTHOR_NAME", "Test Bot")
    merged_env.setdefault("GIT_AUTHOR_EMAIL", "test-bot@example.org")
    merged_env.setdefault("GIT_COMMITTER_NAME", "Test Bot")
    merged_env.setdefault("GIT_COMMITTER_EMAIL", "test-bot@example.org")
    proc = subprocess.run(  # noqa: S603
        full_cmd,
        cwd=str(cwd),
        capture_output=True,
        text=False,
        check=False,
        env=merged_env,
        shell=False,  # inputs are controlled in tests
    )
    if check and proc.returncode != 0:
        raise RuntimeError()
    return proc


@dataclass(frozen=True)
class Repo:
    """
    Lightweight handle for a test repository.
    """

    path: Path

    def git(self, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return _run_git(args, cwd=self.path, check=check)

    def write(self, relpath: str | Path, content: str, *, mkdirs: bool = True) -> Path:
        p = self.path / Path(relpath)
        if mkdirs:
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def add(self, paths: Iterable[str | Path]) -> None:
        str_paths = [str(self.path / Path(p)) if not str(p).startswith(str(self.path)) else str(p) for p in paths]
        # Convert to repo-relative
        rels = [str(Path(s).relative_to(self.path)) for s in str_paths]
        self.git(["add", *rels])

    def add_commit(self, message: str, paths: Iterable[str | Path]) -> None:
        self.add(paths)
        self.git(["commit", "-m", message])

    def current_branch(self) -> str:
        cp = self.git(["rev-parse", "--abbrev-ref", "HEAD"])
        return _to_str(cp.stdout).strip()

    def create_branch(self, name: str, *, checkout: bool = False) -> None:
        self.git(["branch", name])
        if checkout:
            self.git(["checkout", name])

    def set_default_branch(self, name: str) -> None:
        """
        Set the default branch name for the repo. This is only safe to call
        prior to the first commit or if HEAD can be moved. Tests should
        generally specify the default at init time instead.
        """
        self.git(["symbolic-ref", "HEAD", f"refs/heads/{name}"])


def _configure_test_identity(repo: Repo) -> None:
    # Use a deterministic identity for commits in tests
    repo.git(["config", "user.name", "Test Bot"])
    repo.git(["config", "user.email", "test-bot@example.org"])
    # Disable signing to avoid interactive prompts (e.g., TouchID/GPG)
    repo.git(["config", "commit.gpgsign", "false"])
    repo.git(["config", "tag.gpgsign", "false"])


def _initial_commit(repo: Repo, *, default_branch: str) -> None:
    # Ensure HEAD points to the expected branch before first commit
    repo.set_default_branch(default_branch)
    # Create a minimal initial commit
    repo.write(".gitignore", "# test fixture\n")
    repo.git(["add", ".gitignore"])
    repo.git(["commit", "-m", "Initial commit"])


def init_repo(
    path: Path,
    *,
    default_branch: str = "main",
    configure_user: bool = True,
) -> Repo:
    """
    Initialize a new git repository at 'path' with a first commit.

    Args:
      path: Location for the repository (created if missing).
      default_branch: The desired default branch for initial HEAD (default: 'main').
      configure_user: Whether to set a deterministic test identity (default: True).

    Returns:
      Repo handle for convenience operations.
    """
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    # Initialize repository
    _run_git(["init"], cwd=path)
    repo = Repo(path=path)
    if configure_user:
        _configure_test_identity(repo)
    _initial_commit(repo, default_branch=default_branch)
    return repo


def create_basic_history(
    path: Path,
    *,
    base_branch: str = "main",
    feature_branch: str = "feature/test",
    base_commits: int = 1,
    feature_commits: int = 1,
) -> Repo:
    """
    Initialize a repo and create a simple history across a base and a feature branch.

    - Start with an initial commit on base_branch.
    - Add 'base_commits' commits on base_branch touching base_N.txt files.
    - Create 'feature_branch' from base and add 'feature_commits' touching feat_N.txt.

    Returns:
      Repo handle pointing at the feature_branch (checked out).
    """
    repo = init_repo(path, default_branch=base_branch)
    # Add commits on base
    for i in range(1, max(0, base_commits) + 1):
        p = repo.write(f"base_{i}.txt", f"base {i}\n")
        repo.add_commit(f"base commit {i}", [p])

    # Create feature branch and switch
    repo.create_branch(feature_branch, checkout=True)
    for i in range(1, max(0, feature_commits) + 1):
        p = repo.write(f"feat_{i}.txt", f"feat {i}\n")
        repo.add_commit(f"feature commit {i}", [p])

    return repo


def write_gitreview(
    repo: Repo,
    *,
    host: str = "gerrit.example.org",
    port: int = 29418,
    project: str = "example/project",
) -> Path:
    """
    Write a minimal .gitreview file into the repository for tests that need it.
    """
    content = f"""[gerrit]
host={host}
port={port}
project={project}.git
"""
    p = repo.write(".gitreview", content)
    # Stage but do not commit by default; tests can choose when to commit
    return p
