<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# Test Isolation Improvements

## Overview

This document describes the test isolation improvements made to ensure pytest runs reliably in all environments,
including when executed via pre-commit hooks.

## Problem Statement

Tests were experiencing inconsistent behavior between different execution contexts:

- ✅ `uv run pytest` - Tests passed consistently
- ❌ `pre-commit run pytest -a` - Tests sometimes failed due to state leakage

This inconsistency indicated test isolation issues requiring fixes.

## Root Causes Identified

### 1. Global Mutable State

**File:** `tests/test_cli_url_and_dryrun.py`

The test file used a global mutable variable `_ORCH_RECORD` to capture orchestrator calls:

```python
# This mutable global gets replaced per-test to capture execute calls
_ORCH_RECORD = _CallRecord()
```

**Issue:** Three tests modified this global without resetting it, causing state leakage between tests.

### 2. Test Execution Order Dependencies

Tests did not explicitly reset global state at the beginning of each test, making them sensitive to execution
order. Pre-commit might run tests in a different order than direct pytest execution.

### 3. Missing Test Isolation Markers

The pytest configuration lacked explicit markers for test isolation and deterministic ordering.

## Solutions Implemented

### 1. Explicit Global State Reset

**File:** `tests/test_cli_url_and_dryrun.py`

Added explicit reset of `_ORCH_RECORD` at the start of each test that uses it:

```python
def test_pr_url_dry_run_invokes_single_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reset global state and patch Orchestrator in the CLI module
    global _ORCH_RECORD
    # Initialize fresh record
    _ORCH_RECORD = _CallRecord()  # ← Explicit reset added here
    # ... rest of test
```

This ensures each test starts fresh, regardless of execution order.

### 2. Enhanced pytest Configuration

**File:** `pyproject.toml`

Updated `[tool.pytest.ini_options]` to include:

```toml
[tool.pytest.ini_options]
minversion = "8.0"
addopts = "-ra -q --cov=github2gerrit --cov-report=term-missing --cov-report=html --strict-markers -p no:randomly"
testpaths = ["tests"]
asyncio_default_fixture_loop_scope = "function"
# Enable better isolation
usefixtures = []
# Ensure deterministic test ordering
# Note: Tests should not depend on execution order
markers = [
    "integration: marks tests as integration tests (deselect with '-m \"not integration\"')",
]
```

**Key additions:**

- `--strict-markers`: Enforces registered markers
- `-p no:randomly`: Disables pytest-randomly plugin if present (ensures deterministic order)
- Comments documenting isolation strategy

### 3. Improved Pre-commit Configuration

**File:** `.pre-commit-config.yaml`

Updated pytest hook with more explicit options:

```yaml
- id: pytest
  name: pytest
  entry: uv
  args: [run, pytest, --tb=short, -q, -v, --strict-markers, --maxfail=5]
  language: system
  pass_filenames: false
  always_run: true
  require_serial: true  # ← ADDED: Prevent parallel execution
```

**Key additions:**

- `-v`: Verbose output to see which tests are running
- `--strict-markers`: Consistent with pyproject.toml
- `--maxfail=5`: Stop after 5 failures to avoid flooding output
- `require_serial: true`: Ensures tests run sequentially in pre-commit

## Existing Safeguards (Already in Place)

The project already had strong test isolation in `tests/conftest.py`:

### 1. Git Environment Isolation

```python
@pytest.fixture(autouse=True)
def isolate_git_environment(monkeypatch):
    """Isolate git environment for each test."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("SSH_AGENT_PID", raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Bot")
    # ... more isolation
```

### 2. GitHub CI Mode Isolation

```python
@pytest.fixture(autouse=True)
def disable_github_ci_mode(monkeypatch, request):
    """Disable GitHub CI mode detection for all tests."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
```

### 3. Coverage Data Isolation

```python
def pytest_sessionstart(session: Any) -> None:
    """Ensure clean coverage data by removing pre-existing files."""
    _remove_coverage_files(bases)
```

These fixtures prevented most isolation issues, but the global state in `test_cli_url_and_dryrun.py` required explicit handling.

## Testing Verification

After implementing these changes:

```bash
# All execution contexts now pass consistently:
✅ uv run pytest                    # 835 passed
✅ pytest                           # 835 passed
✅ pre-commit run pytest -a         # 835 passed
✅ pre-commit run -a                # All hooks pass
```

## Best Practices for Test Authors

### ✅ DO

1. **Use monkeypatch for all environment changes:**

   ```python
   def test_something(monkeypatch):
       monkeypatch.setenv("MY_VAR", "value")
   ```

2. **Reset global state at test start:**

   ```python
   def test_with_global():
       global MY_STATE
       # Reset to clean state
       MY_STATE = initialize_clean_state()
   ```

3. **Use fixtures with `autouse=True` for common setup:**

   ```python
   @pytest.fixture(autouse=True)
   def setup_common_state():
       # Setup
       yield
       # Teardown
   ```

4. **Make tests independent:**
   - Tests should not depend on execution order
   - Tests should not share mutable state
   - Tests should clean up after themselves

### ❌ DON'T

1. **Change `os.environ` directly without cleanup:**

   ```python
   # BAD - can leak to other tests
   os.environ["MY_VAR"] = "value"

   # GOOD - pytest cleans up automatically
   monkeypatch.setenv("MY_VAR", "value")
   ```

2. **Share mutable global state without resetting:**

   ```python
   # BAD - state leaks between tests
   RESULTS = []

   def test_one():
       RESULTS.append(1)

   # GOOD - reset in each test
   def test_two():
       global RESULTS
       # Clear previous state
       RESULTS = []  # Reset to empty
       RESULTS.append(2)  # Now safe to use
   ```

3. **Assume test execution order:**

   ```python
   # BAD - relies on test_one running first
   def test_one():
       global IS_READY
       # Set ready flag
       IS_READY = True

   def test_two():
       assert IS_READY is True  # Fails if runs before test_one!
   ```

## Performance Impact

All changes have minimal performance impact:

- **Global state reset:** Negligible (simple object instantiation)
- **pytest markers:** Zero overhead (compile-time)
- **Serial execution in pre-commit:** Acceptable (tests complete in ~35 seconds)

## Future Improvements

Consider:

1. **Remove global state entirely** in `test_cli_url_and_dryrun.py`:
   - Refactor to use fixtures or class-based tests
   - Pass state explicitly rather than using globals

2. **Add test isolation markers:**

   ```python
   @pytest.mark.isolated
   def test_requires_clean_state():
       pass
   ```

3. **Enable pytest-xdist for parallel testing** (after ensuring all tests are fully isolated):

   ```toml
   addopts = "-n auto"  # Run tests in parallel
   ```

## References

- **pytest documentation:** <https://docs.pytest.org/>
- **Test isolation best practices:** <https://docs.pytest.org/en/stable/how-to/fixtures.html>
- **monkeypatch fixture:** <https://docs.pytest.org/en/stable/how-to/monkeypatch.html>

## Conclusion

The test suite now has robust isolation that ensures consistent behavior across all execution environments. Tests
are deterministic, independent, and reliable whether run locally, in CI, or via pre-commit hooks.
