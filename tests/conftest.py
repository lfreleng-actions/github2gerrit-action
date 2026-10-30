# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Test Configuration and Environment Isolation
═════════════════════════════════════════════

This conftest.py provides critical test environment isolation to ensure:
1. Deterministic test execution across all environments
2. Prevention of cross-test contamination
3. Consistent behavior in local, CI, and pre-commit contexts

Key Isolation Strategies:
─────────────────────────

1. **Git Environment Isolation** (isolate_git_environment fixture)
   - Clears SSH agent state to prevent host SSH key usage
   - Sets consistent git identity for all tests
   - Configures non-interactive SSH for git operations
   - **REQUIRED**: Without this, pre-commit pytest runs fail randomly

2. **GitHub CI Mode Isolation** (disable_github_ci_mode fixture)
   - Disables GitHub Actions detection during tests
   - Ensures config loading works normally in CI environments
   - Prevents tests from behaving differently in GitHub Actions

3. **Coverage Data Isolation** (pytest_sessionstart)
   - Removes stale coverage files to prevent data mixing
   - Uses unique coverage files per test run
   - Prevents branch/statement coverage conflicts

4. **Config File Isolation** (pytest_sessionstart)
   - Uses temporary, empty config files for tests
   - Prevents tests from reading user's ~/.config/github2gerrit/
   - Ensures hermetic test execution

Common Issues Prevented:
────────────────────────

✓ Random test failures in pre-commit hooks (SSH agent pollution)
✓ Tests passing locally but failing in CI (environment differences)
✓ Coverage data mixing errors (parallel test runs)
✓ Tests reading/writing real user configuration files
✓ Git operations using host SSH keys instead of test keys
✓ Inconsistent git identity across test runs

For test authors:
─────────────────

All fixtures with autouse=True are INTENTIONALLY global. Do not disable them
unless you have a specific need and understand the implications. If you need
custom configuration, override specific environment variables within your test
rather than skipping the fixture.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest


# Ensure src directory is on sys.path so tests can import the package without
# installation
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Isolate coverage output to a unique temp file to avoid data mixing across runs
if not os.getenv("COVERAGE_FILE"):
    import tempfile
    import uuid

    cov_file = (
        Path(tempfile.gettempdir())
        / f".coverage.pytest.{os.getpid()}.{uuid.uuid4().hex}"
    )
    os.environ["COVERAGE_FILE"] = str(cov_file)


def _remove_path(p: Path) -> None:
    try:
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)
        elif p.exists() and p.is_dir():
            shutil.rmtree(p)
    except Exception as exc:
        # Do not fail test collection/session start if cleanup cannot proceed.
        print(
            f"[conftest] Warning: failed to remove {p}: {exc}", file=sys.stderr
        )


def _remove_coverage_files(bases: Iterable[Path]) -> None:
    for base in bases:
        base_path = base
        with suppress(Exception):
            base_path = base_path.resolve()

        # Standard coverage data file
        cov_file = base_path / ".coverage"
        if cov_file.exists():
            _remove_path(cov_file)

        # Any leftover parallel/previous data artifacts like
        # .coverage.hostname.pid.*
        for child in base_path.iterdir():
            name = child.name
            if name.startswith(".coverage.") and child.is_file():
                _remove_path(child)

    # Respect explicit COVERAGE_FILE override if set
    cov_env = os.getenv("COVERAGE_FILE", "").strip()
    if cov_env:
        p = Path(cov_env)
        if p.exists():
            _remove_path(p)


def pytest_sessionstart(session: Any) -> None:
    """
    Ensure clean coverage data by removing any pre-existing coverage files
    that could cause branch/statement data mixing errors when combining.
    """
    bases: set[Path] = {Path.cwd()}
    rootpath = getattr(session.config, "rootpath", None)
    if isinstance(rootpath, Path):
        bases.add(rootpath)
    # Some pytest versions expose 'invocation_params.dir'
    inv_params = getattr(session.config, "invocation_params", None)
    if inv_params is not None:
        inv_dir = getattr(inv_params, "dir", None)
        if isinstance(inv_dir, Path):
            bases.add(inv_dir)

    # Force hermetic config path and token for tests
    try:
        import tempfile

        cfg_tmp = (
            Path(tempfile.gettempdir()) / f"g2g-empty-config-{os.getpid()}.ini"
        )
        # Ensure the file exists and is empty
        cfg_tmp.write_text("", encoding="utf-8")
        os.environ["G2G_CONFIG_PATH"] = str(cfg_tmp)
    except Exception:
        # Fallback: use a non-existent path to disable config loading entirely
        os.environ["G2G_CONFIG_PATH"] = "/dev/null/nonexistent-config.ini"

    # Provide a dummy token so any incidental GitHub client construction
    # succeeds
    os.environ.setdefault("GITHUB_TOKEN", "dummy")

    # Ensure tests don't write to real GitHub output files
    if "GITHUB_OUTPUT" not in os.environ:
        os.environ["GITHUB_OUTPUT"] = "/dev/null"

    # Disable GitHub CI mode detection during tests to ensure config loading works
    # This prevents _is_github_ci_mode() from returning True during test execution
    if "GITHUB_ACTIONS" in os.environ:
        del os.environ["GITHUB_ACTIONS"]
    if "GITHUB_EVENT_NAME" in os.environ:
        del os.environ["GITHUB_EVENT_NAME"]

    _remove_coverage_files(bases)


@pytest.fixture(autouse=True)
def disable_github_ci_mode(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """
    Automatically disable GitHub CI mode detection for all tests.

    This ensures that config loading and file operations work normally
    during test execution, even when running in GitHub Actions CI.
    """
    # Clear GitHub Actions environment variables that trigger CI mode
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    # Also clear other GitHub-related vars that might interfere with tests
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY_OWNER", raising=False)
    # Pull request context. Tests that drive the CLI's URL handling
    # write these straight into os.environ, and a leaked PR_NUMBER
    # would put every later .gitreview read into untrusted pull request
    # mode, where the local file is declined.
    monkeypatch.delenv("PR_NUMBER", raising=False)
    monkeypatch.delenv("PR_HEAD_REPO", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    # Gerrit target. CLI tests export derived values straight into
    # os.environ through apply_config_to_env, and a leaked
    # GERRIT_PROJECT reads as operator intent to every later test.
    monkeypatch.delenv("GERRIT_PROJECT", raising=False)
    monkeypatch.delenv("GERRIT_SERVER", raising=False)

    # Ensure consistent test environment
    monkeypatch.setenv("G2G_ENABLE_DERIVATION", "true")

    # Only set G2G_AUTO_SAVE_CONFIG if the test doesn't explicitly control it
    # Check if this is the specific test that needs to control auto-save behavior
    if (
        "test_apply_parameter_derivation_saves_to_config_local_cli"
        not in request.node.name
    ):
        monkeypatch.setenv("G2G_AUTO_SAVE_CONFIG", "false")


@pytest.fixture(autouse=True)
def reset_gerrit_base_path_cache() -> Iterable[None]:
    """Clear the process-wide Gerrit base-path cache around every test.

    ⚠️  IMPORTANT: autouse=True is REQUIRED for test suite stability

    ``gerrit_urls._BASE_PATH_CACHE`` is a module-level dict keyed by
    host.  Tests that exercise base-path discovery populate it for
    ``gerrit.example.org`` — the fixture host used by roughly twenty
    test modules — and nothing reset it between tests.  Any later test
    building a URL for that host then received an unexpected ``/r/``
    segment, typically surfacing in
    ``test_query_gerrit_results_success_base_path``.

    The leak is order-dependent, so whether it bites depends on which
    modules run first.  Adding, removing or renaming test files can
    expose it without any change to the code under test, which makes it
    hard to attribute when it does appear.

    Clearing on both sides of the test keeps a dirty cache from reaching
    a test and stops one leaving a cache behind.
    """
    from github2gerrit import gerrit_urls

    gerrit_urls._BASE_PATH_CACHE.clear()
    yield
    gerrit_urls._BASE_PATH_CACHE.clear()


@pytest.fixture(autouse=True)
def reset_derived_key_provenance() -> Iterable[None]:
    """Clear the derived-config-key record around every test.

    ⚠️  IMPORTANT: autouse=True is REQUIRED for test suite stability

    ``config.mark_derived_keys`` records provenance in the
    ``G2G_DERIVED_KEYS`` environment variable, and is deliberately
    additive so repeated derivation passes accumulate.  Any test that
    calls ``apply_parameter_derivation`` therefore leaves a record
    behind, and it outlives ``monkeypatch`` teardown whenever the test
    reached ``os.environ`` directly rather than through the fixture.

    A stale record makes ``config.is_derived_key`` answer ``True`` for a
    key a later test set explicitly, which is exactly the distinction
    duplicate detection relies on -- so the leak would surface as a
    short-circuit that silently stopped happening, in an unrelated
    module, depending on test order.

    Clearing on both sides keeps a dirty record from reaching a test and
    stops one leaving a record behind.
    """
    from github2gerrit.config import DERIVED_KEYS_ENV
    from github2gerrit.config import GUESSED_KEYS_ENV

    os.environ.pop(DERIVED_KEYS_ENV, None)
    os.environ.pop(GUESSED_KEYS_ENV, None)
    yield
    os.environ.pop(DERIVED_KEYS_ENV, None)
    os.environ.pop(GUESSED_KEYS_ENV, None)


@pytest.fixture(autouse=True)
def restore_bool_option_environment() -> Iterable[None]:
    """Restore the configurable boolean variables after every test.

    The CLI writes each of them to ``os.environ`` once the configuration
    file is applied, so any test that runs ``main`` or
    ``_load_effective_inputs`` would otherwise leave a value behind that
    reads as an explicit setting to every later test.
    """
    from github2gerrit.cli import _CONFIGURABLE_BOOL_OPTIONS

    names = [opt.env_var for opt in _CONFIGURABLE_BOOL_OPTIONS]
    saved = {name: os.environ.get(name) for name in names}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def reset_gerrit_project_listings() -> Iterable[None]:
    """Empty the per-process Gerrit project-listing memo around every test.

    ``project_names`` remembers each ``/projects/?p=<prefix>`` answer by
    host and prefix for the life of the process, so bulk runs ask once.
    Tests stub that endpoint with different answers for the same host,
    and one test's answer must not reach the next.
    """
    from github2gerrit import project_names

    project_names._LISTINGS.clear()
    yield
    project_names._LISTINGS.clear()


@pytest.fixture(autouse=True)
def isolate_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Isolate git environment for each test to prevent cross-test contamination.

    ⚠️  IMPORTANT: autouse=True is REQUIRED for test suite stability

    Why this fixture is globally applied:
    ────────────────────────────────────────────────────────────────

    1. **Pre-commit Hook Failures**: Without this fixture, pytest running from
       pre-commit hooks resulted in random test failures due to SSH agent state
       pollution from the host environment.

    2. **Cross-Test Contamination**: Git operations in one test can affect
       subsequent tests through shared environment variables (SSH_AUTH_SOCK,
       SSH_AGENT_PID, git identity configuration).

    3. **Non-Deterministic Behavior**: Tests that don't explicitly need git
       isolation can still be affected if they execute git commands internally
       or depend on code that does.

    4. **CI/CD Consistency**: Ensures tests behave identically whether run
       locally, in GitHub Actions, or via pre-commit hooks.

    What this fixture provides:
    ────────────────────────────────────────────────────────────────

    - Clean SSH agent state (no SSH_AUTH_SOCK or SSH_AGENT_PID)
    - Consistent git identity across all tests (Test Bot <test-bot@example.org>)
    - Non-interactive SSH configuration for git operations
    - Prevention of unintended SSH key usage from host environment

    Overriding this fixture:
    ────────────────────────────────────────────────────────────────

    If a specific test needs different git configuration:

    ```python
    def test_custom_git_config(monkeypatch, isolate_git_environment):
        # The fixture still runs, but you can override specific vars
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Custom Author")
        # ... rest of test
    ```

    Or use a marker to skip the fixture (requires pytest configuration):

    ```python
    @pytest.mark.no_git_isolation
    def test_with_host_git_config():
        # This test would need special handling in conftest.py
        pass
    ```

    Performance Impact:
    ────────────────────────────────────────────────────────────────

    Minimal - only sets/unsets environment variables via monkeypatch,
    which is automatically cleaned up by pytest after each test.
    """
    # Clear SSH-related environment variables to ensure clean state
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("SSH_AGENT_PID", raising=False)

    # Clear Git repository location variables to prevent cross-repo contamination
    # This ensures test repos don't accidentally reference the main repo's objects
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    monkeypatch.delenv("GIT_OBJECT_DIRECTORY", raising=False)
    monkeypatch.delenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", raising=False)

    # Set consistent git identity for all tests
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Bot")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test-bot@example.org")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test Bot")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test-bot@example.org")

    # Configure non-interactive SSH for git operations
    monkeypatch.setenv(
        "GIT_SSH_COMMAND",
        "ssh -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes "
        "-o PreferredAuthentications=publickey -o StrictHostKeyChecking=yes "
        "-o PasswordAuthentication=no -o ConnectTimeout=5",
    )
