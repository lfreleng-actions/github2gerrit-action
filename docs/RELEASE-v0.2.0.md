<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2025 The Linux Foundation -->

# Release Notes - v0.2.0

## Overview

Version 0.2.0 introduces important behavioral changes and improvements to the
GitHub2Gerrit action. This release includes **two breaking changes** to default
settings: `PRESERVE_GITHUB_PRS` now defaults to `true` (was `false`) and
`SIMILARITY_FILES` now defaults to `false` (was `true`). These changes make the
default behavior more aligned with common use cases while improving the handling
of push events and commit reconciliation.

## Breaking Changes

### ⚠️ PRESERVE_GITHUB_PRS Default Changed from `false` to `true`

**Impact:** HIGH - This is a breaking change that affects default workflow behavior

**Previous Behavior (v0.1.x):**

- Default: `PRESERVE_GITHUB_PRS="false"`
- GitHub pull requests closed automatically when the action pushed them to Gerrit
- Users had to explicitly set `PRESERVE_GITHUB_PRS="true"` to keep PRs open

**New Behavior (v0.2.0):**

- Default: `PRESERVE_GITHUB_PRS="true"`
- GitHub pull requests now remain open by default when the action pushes them to Gerrit
- Users must explicitly set `PRESERVE_GITHUB_PRS="false"` to close PRs after submission

**Rationale:**

We changed the default for these reasons:

1. **Common Use Case**: Most projects using this action want to maintain GitHub
   PRs as a reference point even after they submit changes to Gerrit
2. **Safer Default**: Preserving PRs is a non-destructive operation, making it a safer default behavior
3. **Alignment with Documentation**: The README already recommended
   `PRESERVE_GITHUB_PRS=true` as the typical configuration
4. **Two-Way Workflow**: The new `CLOSE_MERGED_PRS` feature (default: `true`)
   closes PRs automatically when maintainers merge Gerrit changes, offering a
   complete bidirectional workflow

**Migration Guide:**

If your workflow relied on the previous default behavior of closing PRs after submission:

```yaml
# Add this to your workflow to maintain v0.1.x behavior
- uses: lfit/github2gerrit-action@v0.2.0
  with:
    PRESERVE_GITHUB_PRS: "false"
    # ... other inputs
```

If you want to use the new recommended workflow:

```yaml
# Use defaults for v0.2.0 - PRs stay open until Gerrit merge
- uses: lfit/github2gerrit-action@v0.2.0
  with:
    PRESERVE_GITHUB_PRS: "true"   # Default, you can omit this line
    CLOSE_MERGED_PRS: "true"       # Default, you can omit this line
    # ... other inputs
```

### ⚠️ SIMILARITY_FILES Default Changed from `true` to `false`

**Impact:** MEDIUM - This affects how the system matches commits during PR
updates and reconciliation

**Previous Behavior (v0.1.x):**

- Default: `SIMILARITY_FILES="true"`
- Reconciliation required exact file signature match between GitHub PR commits and Gerrit changes
- More strict matching that could fail to reconcile when file lists differed slightly

**New Behavior (v0.2.0):**

- Default: `SIMILARITY_FILES="false"`
- Reconciliation uses more flexible matching based on commit subject and other metadata
- File signature matching is no longer required by default
- More lenient matching allows reconciliation even when file lists differ

**Rationale:**

We changed the default for these reasons:

1. **Flexibility**: File-based matching was too strict and could fail to match
   commits that were logically the same but had minor file differences
2. **Better User Experience**: The more lenient matching reduces false negatives in commit reconciliation
3. **Common Use Case**: Most users don't need exact file signature matching for reconciliation to work correctly

**Migration Guide:**

If your workflow requires exact file signature matching for security or compliance reasons:

```yaml
# Add this to restore v0.1.x strict matching behavior
- uses: lfit/github2gerrit-action@v0.2.0
  with:
    SIMILARITY_FILES: "true"
    # ... other inputs
```

If you want to use the new default (recommended):

```yaml
# Use defaults for v0.2.0 - more flexible reconciliation
- uses: lfit/github2gerrit-action@v0.2.0
  with:
    SIMILARITY_FILES: "false"   # Default, you can omit this line
    # ... other inputs
```

**Impact on Reconciliation:**

- With `SIMILARITY_FILES=false` (new default): The system matches commits based
  on subject line similarity and other metadata
- With `SIMILARITY_FILES=true` (old default): Commits require exact file
  signature match plus other criteria
- The `SIMILARITY_UPDATE_FACTOR` setting still controls the threshold for subject line matching (default: 0.75)

## New Features

### Enhanced Push Event Handling

**Improved Documentation and State Management:**

- Push events now explicitly set `PR_NUMBER` to empty string to show intentional absence
- Added inline documentation explaining that push events use `_process_close_merged_prs()` for PR closure
- Downstream steps can now reliably detect push event processing mode

**Technical Details:**

```yaml
# Push events don't need PR_NUMBER (used for closing merged PRs)
# The CLI handles push events specially via _process_close_merged_prs()
if [[ "${{ github.event_name }}" == "push" ]]; then
  echo "Push event detected - will process merged commits for PR closure"
  # Set PR_NUMBER to empty to show this is intentional for push events
  echo "PR_NUMBER=" >> "$GITHUB_ENV"
  exit 0
fi
```

## Code Quality Improvements

### Simplified Exception Handling

**Refactored `_parse_github_target()` function:**

- Removed unnecessary `else` clause from try-except block
- Return statement now executes right after successful PR number parsing
- Improved code readability and maintainability

**Before:**

```python
try:
    pr_number = int(parts[3])
except Exception:
    pr_number = None
else:
    return "github_pr", owner, repo, pr_number
```

**After:**

```python
try:
    pr_number = int(parts[3])
    return "github_pr", owner, repo, pr_number
except Exception:
    pr_number = None
```

### Type-Safe URL Parsing with Discriminated Union

**Refactored URL parsing to use dataclasses:**

- Replaced tuple-based return types with proper dataclasses
- Created `GitHubPRTarget`, `GitHubRepoTarget`, and `GerritChangeTarget` types
- Improved type safety and API clarity
- Pattern matching with `isinstance()` for better code readability

**Before:**

```python
def _parse_target_url(url: str) -> tuple[str, str | None, str | None, int | str | None]:
    """Parse a GitHub or Gerrit URL."""
    if re.match(GERRIT_CHANGE_URL_PATTERN, url):
        return (
            "gerrit_change",
            None,  # owner
            None,  # repo
            url    # change_url
        )
    return _parse_github_target(url)

# Usage
url_type, org, repo, pr_or_change = _parse_target_url(target_url)
if url_type == "gerrit_change":
    # Handle Gerrit URL
```

**After:**

```python
@dataclass(frozen=True)
class GitHubPRTarget:
    owner: str | None
    repo: str | None
    pr_number: int | None

@dataclass(frozen=True)
class GitHubRepoTarget:
    owner: str | None
    repo: str | None

@dataclass(frozen=True)
class GerritChangeTarget:
    change_url: str

TargetURL = GitHubPRTarget | GitHubRepoTarget | GerritChangeTarget

def _parse_target_url(url: str) -> TargetURL:
    """Parse a GitHub or Gerrit URL into a type-safe result."""
    if re.match(GERRIT_CHANGE_URL_PATTERN, url):
        return GerritChangeTarget(change_url=url)
    return _parse_github_target(url)

# Usage
parsed = _parse_target_url(target_url)
if isinstance(parsed, GerritChangeTarget):
    # Handle Gerrit URL with type safety
    change_url = parsed.change_url
```

**Benefits:**

- **Type Safety**: Static type checkers can verify correct usage
- **Clear API**: No ambiguity about what the 4th tuple element contains
- **Better IDE Support**: Autocomplete and type hints work as expected
- **Immutability**: Frozen dataclasses prevent accidental modifications
- **Self-Documenting**: Class names show the URL type

### Improved Progress Tracker Type Safety

**Refactored progress tracker typing:**

- Replaced `Any` type with proper union types: `G2GProgressTracker | DummyProgressTracker | None`
- Made `None` checks explicit where the parameter accepts `None`
- Removed unnecessary null checks where tracker is always initialized
- Improved type safety for better IDE support and static analysis

**Before:**

```python
def _process_close_merged_prs(data: Inputs, gh: GitHubContext) -> None:
    show_progress = env_bool("G2G_SHOW_PROGRESS", True)
    progress_tracker: Any = None  # Type is too broad

    if show_progress:
        progress_tracker = G2GProgressTracker(target)
    else:
        progress_tracker = DummyProgressTracker("Gerrit PR Closer", target)

    # Unnecessary null check - always assigned above
    if progress_tracker:
        progress_tracker.update_operation("...")
```

**After:**

```python
def _process_close_merged_prs(data: Inputs, gh: GitHubContext) -> None:
    show_progress = env_bool("G2G_SHOW_PROGRESS", True)

    # Explicit union type, no None needed here
    progress_tracker: G2GProgressTracker | DummyProgressTracker
    if show_progress:
        progress_tracker = G2GProgressTracker(target)
    else:
        progress_tracker = DummyProgressTracker("Gerrit PR Closer", target)

    # Direct call - no null check needed
    progress_tracker.update_operation("...")
```

**For functions accepting tracker as parameter:**

```python
def _process_single(
    data: Inputs,
    gh: GitHubContext,
    progress_tracker: G2GProgressTracker | DummyProgressTracker | None = None,
) -> tuple[bool, SubmissionResult]:
    # None check where parameter can accept None
    if progress_tracker:
        progress_tracker.update_operation("...")
```

**Benefits:**

- Better static type checking with mypy/pyright
- Intent is explicit: shows when None is possible
- Removes dead code from unnecessary null checks
- Improved IDE autocomplete and refactoring support

### Respect User Flags for Gerrit Change URLs

**Fixed flag override issue:**

- When the user provided a Gerrit change URL, the code unconditionally set `CLOSE_MERGED_PRS` to `true`
- The action now respects the user's explicit `--close-merged-prs` flag value
- Provides better user control over PR closure behavior

**Behavior with flag:**

- `--close-merged-prs=true` (default): Closes the GitHub PR when processing Gerrit change
- `--close-merged-prs=false`: Adds a comment to the PR but leaves it open

**Before:**

```python
if isinstance(parsed, GerritChangeTarget):
    # Always forced to true, ignoring user preference
    os.environ["CLOSE_MERGED_PRS"] = "true"
    os.environ["G2G_GERRIT_CHANGE_URL"] = parsed.change_url
```

**After:**

```python
if isinstance(parsed, GerritChangeTarget):
    # Respects user's --close-merged-prs flag set earlier
    # CLOSE_MERGED_PRS was already set based on user flag at line 812
    os.environ["G2G_GERRIT_CHANGE_URL"] = parsed.change_url
    log.debug(
        "Gerrit change URL mode with CLOSE_MERGED_PRS=%s",
        os.environ.get("CLOSE_MERGED_PRS", "true")
    )
```

**Use cases enabled:**

```bash
# Close PR when processing Gerrit change (default behavior)
github2gerrit https://gerrit.example.com/c/project/+/12345

# Add comment to PR without closing it (new capability)
# Useful for notification workflows or testing
github2gerrit --close-merged-prs=false https://gerrit.example.com/c/project/+/12345
```

**Benefits:**

- User intent controls behavior across all code paths
- Consistent behavior with other CLI flags
- More flexible workflow options (close vs. comment)
- Useful for notification workflows
- Better debugging and testing capabilities

### Improved Gerrit URL Pattern Matching

**More specific and secure regex pattern:**

- Replaced greedy `(?:.*/)?` pattern with explicit `(?:[\w-]+/)*`
- Better security: prevents unintended matches and path traversal attempts
- More maintainable: documents what characters we accept in subpaths
- Comprehensive documentation with examples added to `constants.py`

**Before:**

```python
# Too greedy - could match unexpected URLs
GERRIT_CHANGE_URL_PATTERN = r"https?://([^/]+)/(?:.*/)?c/[^+]+/\+/(\d+)"
```

**After:**

```python
# More specific - matches word characters and hyphens in subpaths
# Pattern breakdown documented inline
GERRIT_CHANGE_URL_PATTERN = r"https?://([^/]+)/(?:[\w-]+/)*c/[^+]+/\+/(\d+)"
```

**Valid URLs matched:**

```python
# Standard format
https://gerrit.example.com/c/project/+/12345

# With subpath (e.g., /infra/, /r/)
https://gerrit.example.com/infra/c/releng/lftools/+/123

# Nested project names
https://gerrit.example.com/c/nested/project/name/+/99999

# Subpaths at different levels
https://gerrit.example.com/sub1/sub2/c/project/+/789
```

**Invalid URLs rejected:**

```python
# Missing /c/ indicator
https://gerrit.example.com/project/+/123  # ✗

# Path traversal attempts
https://gerrit.example.com/../../c/proj/+/123  # ✗

# Spaces in subpath
https://gerrit.example.com/bad path/c/proj/+/123  # ✗
```

**Benefits:**

- Reduced false positive matches
- Better security posture
- Intent is explicit through the pattern
- Comprehensive inline documentation
- All existing tests continue to pass

### Improved G2G_TARGET_URL Environment Variable

**Enhanced internal flag storage:**

- The code used to set `G2G_TARGET_URL` to `"1"` as a boolean flag when in direct URL mode
- Now stores the actual URL string (e.g., `"https://github.com/owner/repo/pull/123"`)
- All downstream code uses truthy/falsy checks, so behavior remains the same
- Provides better debugging and logging capabilities

**Technical Details:**

```python
# Now stores actual URL instead of "1"
os.environ["G2G_TARGET_URL"] = target_url  # e.g., "https://github.com/..."
os.environ["G2G_TARGET_URL_TYPE"] = url_type

# All usages remain compatible (truthy checks)
if os.getenv("G2G_TARGET_URL"):  # Works with any non-empty string
    # ... code
```

**Benefits:**

- Better debugging: Can see the actual URL in logs and environment dumps
- Future extensibility: Code can now access the original URL if needed
- No breaking changes: All boolean checks continue to work as before

## Behavior Summary Table

<!-- markdownlint-disable MD013 -->

| Feature               | v0.1.x Default | v0.2.0 Default | Notes                   |
| --------------------- | -------------- | -------------- | ----------------------- |
| `PRESERVE_GITHUB_PRS` | `"false"`      | `"true"`       | **BREAKING CHANGE**     |
| `SIMILARITY_FILES`    | `"true"`       | `"false"`      | **BREAKING CHANGE**     |
| `CLOSE_MERGED_PRS`    | `"true"`       | `"true"`       | No change               |
| Push event handling   | Basic          | Enhanced       | Better state management |

<!-- markdownlint-enable MD013 -->

## Recommended Workflow Patterns

### Pattern 1: Preserve PRs, Close on Merge (Recommended)

```yaml
on:
  pull_request_target:
    types: [opened, synchronize, reopened]
  push:
    branches: [main, master]

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: lfit/github2gerrit-action@v0.2.0
        with:
          GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
          # Defaults handle the rest:
          # - PRESERVE_GITHUB_PRS: "true" keeps PRs open
          # - CLOSE_MERGED_PRS: "true" closes PRs when merged in Gerrit
```

### Pattern 2: Close PRs After Submission

```yaml
on:
  pull_request_target:
    types: [opened, synchronize, reopened]

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: lfit/github2gerrit-action@v0.2.0
        with:
          GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
          PRESERVE_GITHUB_PRS: "false"
          # No need for push events in this pattern
```

### Pattern 3: Keep All PRs Open (Reference)

```yaml
on:
  pull_request_target:
    types: [opened, synchronize, reopened]

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: lfit/github2gerrit-action@v0.2.0
        with:
          GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
          PRESERVE_GITHUB_PRS: "true"
          CLOSE_MERGED_PRS: "false"
```

## Upgrade Checklist

When upgrading from v0.1.x to v0.2.0:

- [ ] Review your workflow files for `PRESERVE_GITHUB_PRS` settings
- [ ] Decide if you want the new default behavior (preserve PRs)
- [ ] If you want v0.1.x behavior, explicitly set
  `PRESERVE_GITHUB_PRS: "false"`
- [ ] Review your workflow files for `SIMILARITY_FILES` settings
- [ ] Decide if you need exact file signature matching for reconciliation
- [ ] If you require strict file-based matching, explicitly set
  `SIMILARITY_FILES: "true"`
- [ ] Consider enabling push event triggers if using `CLOSE_MERGED_PRS`
- [ ] Update any documentation or runbooks referencing PR closure behavior
- [ ] Test in a non-production environment first

## Notes

### Why Two Options?

- **`PRESERVE_GITHUB_PRS`**: Controls whether the action closes PRs right after
  pushing to Gerrit (on `pull_request_target` events)
- **`CLOSE_MERGED_PRS`**: Controls whether the action closes PRs when maintainers
  merge corresponding Gerrit changes (on `push` events)

These options work together to provide flexible workflow patterns:

- Both `true`: PRs stay open during review, close when merged ✅ **Recommended**
- `PRESERVE=false`, `CLOSE=true`: The action closes PRs right away (v0.1.x default behavior)
- `PRESERVE=true`, `CLOSE=false`: The action never closes PRs automatically
- Both `false`: The action closes PRs right away (same as `PRESERVE=false`)

## Test Infrastructure Improvements

### Enhanced Test Environment Isolation

**Comprehensive documentation and fixes for test stability:**

- Documented why `isolate_git_environment` fixture uses `autouse=True`
- Added explanation of pre-commit hook test failures without isolation
- Provided module-level documentation in `tests/conftest.py`
- Included examples for overriding fixture behavior when needed

**Key isolation features:**

```python
@pytest.fixture(autouse=True)
def isolate_git_environment(monkeypatch):
    """
    ⚠️  IMPORTANT: autouse=True ensures test suite stability

    Why this fixture applies globally:
    1. Pre-commit Hook Failures: Without this, pytest running from
       pre-commit hooks resulted in random test failures due to SSH
       agent state pollution from the host environment.

    2. Cross-Test Contamination: Git operations in one test can affect
       later tests through shared environment variables.

    3. Non-Deterministic Behavior: Tests can fail if they execute
       git commands internally or depend on code that does.

    4. CI/CD Consistency: Ensures tests behave identically whether run
       locally, in GitHub Actions, or via pre-commit hooks.
    """
```

**What's protected:**

- SSH agent state isolation (no `SSH_AUTH_SOCK` or `SSH_AGENT_PID` inheritance)
- Consistent git identity across all tests (`Test Bot <test-bot@example.org>`)
- Non-interactive SSH configuration for git operations
- Coverage data isolation (prevents data mixing across test runs)
- Config file isolation (uses temporary files, not user's config)
- GitHub CI mode detection disabled during tests

**Common issues prevented:**

- ✓ Random test failures in pre-commit hooks (SSH agent pollution)
- ✓ Tests passing locally but failing in CI (environment differences)
- ✓ Coverage data mixing errors (parallel test runs)
- ✓ Tests reading/writing real user configuration files
- ✓ Git operations using host SSH keys instead of test keys

**For test authors:**
All fixtures with `autouse=True` are intentionally global for test stability.
If you need custom configuration, override specific environment variables
within your test rather than skipping the fixture.

## Feedback and Support

If you encounter issues or have questions about this release:

- Open an issue: [GitHub Issues](https://github.com/lfit/releng-lftools/issues)
- Review documentation: [README.md](../README.md)
- Check examples: [.github/workflows/](../.github/workflows/)

## Contributors

Thank you to all contributors who helped make this release possible!

---

**Full Changelog**: [v0.1.0...v0.2.0](https://github.com/lfit/releng-lftools/compare/v0.1.0...v0.2.0)
