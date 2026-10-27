<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Development Guide

This guide covers local setup, testing, and contribution workflows for the
github2gerrit action and its Python CLI.

## Project Overview

- **Language and CLI**: Python 3.11+; the CLI uses Typer.
- **Packaging**: `pyproject.toml` with a setuptools backend; use `uv` to
  install and run.
- **Structure**:
  - `src/github2gerrit/cli.py` — CLI entrypoint
  - `src/github2gerrit/core.py` — orchestration
  - `src/github2gerrit/gitutils.py` — subprocess and git helpers
  - `action.yaml` — the composite GitHub Action
- **Linting and type checking**: Ruff and MyPy with settings in
  `pyproject.toml`, run from [prek](https://github.com/j178/prek) hooks and
  CI. prek is a faster, Rust-based drop-in replacement for pre-commit that
  reads the existing `.pre-commit-config.yaml` unchanged.
- **Tests**: pytest with coverage targets around 80%. Add unit and
  integration tests for each feature.

## Local Setup

Install `uv`, then:

```bash
# Install the package
uv pip install --system .

# Verify the CLI works
uv run github2gerrit --help

# Install prek hooks
uv tool install prek && prek install -f
```

Run checks:

```bash
# All checks (including tests)
prek run --all-files

# Tests only
uv run pytest -q

# Lint and type check
uv run ruff check .
uv run ruff format .
uv run mypy src
```

Integration tests carry the `integration` marker; deselect them with
`uv run pytest -m "not integration"`.

## Dependency Management

- **Update dependencies**: `uv lock --upgrade` rebuilds `uv.lock` with the
  latest compatible versions.
- **Add new dependencies**: add to `pyproject.toml`, then run `uv lock`.
- **Install from lock file**: `uv pip install --system .` uses the exact
  versions from `uv.lock`.

## Local Testing

Test builds against real pull requests without side effects:

```bash
# Dry-run against a real PR
uv run python -m github2gerrit.cli https://github.com/owner/repo/pull/123 --preserve-github-prs --dry-run

# Run the CLI directly during development
uv run github2gerrit --help
```

For CI/CD pipelines, `uvx` installs and runs without managing virtual
environments:

```bash
uvx github2gerrit <PR_URL> --dry-run
uvx --from git+https://github.com/lfreleng-actions/github2gerrit-action@main github2gerrit <PR_URL>
uvx --python 3.11 github2gerrit <PR_URL>
```

## Testing the Composite Action

A dedicated test suite validates `action.yaml` itself, across four modules
in `tests/`:

- `test_composite_action_coverage.py` — input validation, defaults,
  PR number handling (dispatch modes, bulk mode with `PR_NUMBER=0`,
  invalid values), and Issue ID lookup logic
- `test_action_environment_mapping.py` — input-to-environment variable
  mapping, GitHub context mapping, defaults, and secret handling
- `test_action_step_validation.py` — step execution order, conditional
  execution, external action version pinning, embedded script logic, and
  error handling (`set -euo pipefail`, failure propagation)
- `test_action_outputs.py` — output definitions, capture step behavior,
  multiline output handling, and GitHub Actions output format compliance

The suite uses a `CompositeActionTester` helper that parses `action.yaml`,
extracts the embedded shell scripts, simulates the GitHub Actions runtime
(`GITHUB_ENV`, `GITHUB_OUTPUT`, `INPUT_*` and `GITHUB_*` variables,
`${{ }}` expression substitution), and executes step scripts in isolation.
It also checks security aspects: no hardcoded secrets, proper SSH key input
handling, and token usage from context.

Run the action tests:

```bash
# All action tests
uv run pytest tests/test_composite_action_coverage.py tests/test_action_environment_mapping.py \
  tests/test_action_step_validation.py tests/test_action_outputs.py -v

# A specific category
uv run pytest tests/test_composite_action_coverage.py::TestPRNumberHandling -v

# Without coverage (these tests exercise action.yaml, not Python source)
uv run pytest tests/test_composite_action_coverage.py --no-cov -v
```

When modifying `action.yaml`, update the corresponding tests, add tests for
new functionality, and verify assertions match actual action behavior. The
test suite serves as both validation and documentation of the action's
expected behavior.

### Testing action changes in forks and branches

The action installs the tool from its own checkout, so pointing a
caller at a fork or branch runs that code, with no further input:

```yaml
- uses: my-fork/github2gerrit-action@my-branch
  with:
    # ... inputs
```

The action ignores the deprecated `USE_LOCAL_ACTION` input, which once
selected this over the PyPI release.

## Notes on Parity

- Inputs, outputs, and environment usage match the original shell-based
  action.
- The action assumes the same GitHub variables and secrets are present.
- Where the shell action used tools such as `jq` and `gh`, the Python
  version uses library calls and subprocess as appropriate, with retries
  and clear logging.

## Note on sitecustomize.py

The repository includes a `sitecustomize.py` that Python's site
initialization imports automatically. It makes pytest and coverage runs in
CI more robust by assigning a unique `COVERAGE_FILE` per process and
removing stale `.coverage` artifacts. The logic runs during pytest sessions
on a best-effort basis and never interferes with normal execution; it
stabilizes coverage reporting for parallel/xdist runs.
