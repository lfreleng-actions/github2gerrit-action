<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# github2gerrit

Submit a GitHub pull request to a Gerrit repository, implemented in Python.

This action is a drop‑in replacement for the shell‑based
`lfit/github2gerrit` composite action. It mirrors the same inputs,
outputs, environment variables, and secrets so you can adopt it without
changing existing configuration in your organizations.

The tool expects a `.gitreview` file in the repository to derive Gerrit
connection details and the destination project. It uses `git` over SSH
and `git-review` semantics to push to `refs/for/<branch>` and relies on
Gerrit `Change-Id` trailers to create or update changes.

## How it works (high level)

- Discover pull request context and inputs.
- **Detects PR operation mode** (CREATE, UPDATE, EDIT) based on event type.
- Detects and prevents tool runs from creating duplicate changes.
- Reads `.gitreview` for Gerrit host, port, and project.
- When run locally, will pull `.gitreview` from the remote repository.
- Sets up `git` user config and SSH for Gerrit.
- **For UPDATE operations**: Finds and reuses existing Gerrit Change-IDs.
- Prepare commits:
  - one‑by‑one cherry‑pick with `Change-Id` trailers, or
  - squash into a single commit and keep or reuse `Change-Id`.
- Optionally replace the commit message with PR title and body.
- Push with a topic to `refs/for/<branch>` using `git-review` behavior.
- **For UPDATE/EDIT operations**: Syncs PR metadata (title/description) to Gerrit.
- Query Gerrit for the resulting URL, change number, and patchset SHA.
- **Verifies patchset creation** to confirm updates vs. new changes.
- Add a back‑reference comment in Gerrit to the GitHub PR and run URL.
- Comment on the GitHub PR with the Gerrit change URL(s).
- By default, the tool preserves PRs after submission; set `PRESERVE_GITHUB_PRS=false` to close them.

## PR Update Handling (Dependabot Support)

GitHub2Gerrit now **intelligently handles PR updates** from automation tools like Dependabot:

### How PR Updates Work

When a PR updates (e.g., Dependabot rebases or updates dependencies):

1. **Automatic Detection**: The `synchronize` event triggers UPDATE mode
2. **Change-ID Recovery**: Finds existing Gerrit change using four strategies:
   - Topic-based query (`GH-owner-repo-PR#`)
   - GitHub-Hash trailer matching
   - GitHub-PR trailer URL matching
   - Mapping comment parsing
3. **Change-ID Reuse**: Forces reuse of existing Change-ID(s)
4. **New Patchset Creation**: Pushes create a new patchset, not a new change
5. **Metadata Sync**: Updates Gerrit change title/description if PR edits occur
6. **Verification**: Confirms patchset creation and increment

### PR Event Types

| Event         | Action | Behavior                                  |
| ------------- | ------ | ----------------------------------------- |
| `opened`      | CREATE | Creates new Gerrit change(s)              |
| `synchronize` | UPDATE | Updates existing change with new patchset |
| `edited`      | EDIT   | Syncs metadata changes to Gerrit          |
| `reopened`    | REOPEN | Treats as CREATE if no existing change    |
| `closed`      | CLOSE  | Handles PR closure                        |

### Example: Dependabot Workflow

```yaml
on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize, closed]
```

**Typical Dependabot flow:**

1. **Day 1**: Dependabot opens PR #29 → GitHub2Gerrit creates Gerrit change 73940
2. **Day 2**: Dependabot rebases PR #29 → GitHub2Gerrit updates change 73940 (new patchset 2)
3. **Day 3**: Dependabot updates dependencies in PR #29 → change 73940 gets patchset 3
4. **Day 4**: Someone edits PR title → metadata synced to Gerrit change 73940
5. **Day 5**: Change 73940 merged in Gerrit → PR #29 auto-closed in GitHub

### Key Features

- **No Duplicate Changes**: UPDATE mode enforces existing change presence
- **Robust Reconciliation**: Configurable similarity matching with dynamic threshold changes for PR updates
- **Metadata Synchronization**: PR title/description changes sync to Gerrit
- **Patchset Verification**: Confirms updates create new patchsets, not new changes
- **Clear Error Messages**: Helpful guidance when existing change not found

### Error Handling

If UPDATE fails to find existing change:

```text
❌ UPDATE FAILED: Cannot update non-existent Gerrit change
💡 GitHub2Gerrit did not process PR #42.
   To create a new change, trigger the 'opened' workflow action.
```

## Close Merged PRs Feature

GitHub2Gerrit now includes **automatic PR closure** when Gerrit merges changes
and syncs them back to GitHub. This completes the lifecycle for automation PRs
(like Dependabot).

**How it works:**

1. A bot (e.g., Dependabot) creates a GitHub PR
2. GitHub2Gerrit converts it to a Gerrit change with tracking information
3. When the Gerrit change is **merged** and synced to GitHub, the original PR is automatically closed
4. When the Gerrit change is **abandoned**, the tool handles the PR based on `CLOSE_MERGED_PRS`:
   - If `CLOSE_MERGED_PRS=true` (default): The tool closes the PR with an abandoned comment ⛔️
   - If `CLOSE_MERGED_PRS=false`: PR remains open, but receives an abandoned notification comment ⛔️

**Key characteristics:**

- **Enabled by default** via `CLOSE_MERGED_PRS=true`
- **Non-fatal operation** - the tool logs missing or already-closed PRs as
  info, not errors
- Works on `push` events when Gerrit syncs changes to GitHub mirrors
- **Abandoned change handling**: The tool closes PRs or adds comments based on the `CLOSE_MERGED_PRS` setting

**Gerrit change status handling:**

<!-- markdownlint-disable MD013 MD060 -->

| Scenario                    | `CLOSE_MERGED_PRS=true` (default)      | `CLOSE_MERGED_PRS=false`                               |
| --------------------------- | -------------------------------------- | ------------------------------------------------------ |
| Change has MERGED status    | ✅ Closes PR with merged comment       | ⏭️ No action                                           |
| Change has ABANDONED status | ✅ Closes PR with abandoned comment ⛔️ | 💬 Adds abandoned notification comment (PR stays open) |
| Change is NEW/OPEN          | ⚠️ Closes PR with a warning            | ⏭️ No action                                           |
| Status UNKNOWN              | ⚠️ Closes PR with a warning            | ⏭️ No action                                           |

<!-- markdownlint-enable MD013 MD060 -->

**Status reporting examples:**

```text
No GitHub PR URL found in commit abc123de - skipping
GitHub PR #42 is already closed - nothing to do
Gerrit change confirmed as MERGED
SUCCESS: Closed GitHub PR #42
```

**Abandoned change examples:**

With `CLOSE_MERGED_PRS=true`:

```text
Gerrit change ABANDONED; will close PR with abandoned comment
SUCCESS: Closed GitHub PR #42
```

With `CLOSE_MERGED_PRS=false`:

```text
Gerrit change ABANDONED; will add comment (CLOSE_MERGED_PRS=false)
SUCCESS: Added comment to PR #42 (PR remains open)
```

## Automatic Cleanup Features

GitHub2Gerrit includes **automatic cleanup operations** that run after successful
PR processing or when you close PRs. These features help maintain synchronization
between GitHub and Gerrit by cleaning up orphaned or stale changes.

### Cleanup Operations

There are two cleanup operations that run automatically:

#### 1. CLEANUP_ABANDONED (Abandoned Gerrit Changes → Close GitHub PRs)

**What it does:** Scans all open GitHub PRs in the repository and closes those
whose corresponding Gerrit changes Gerrit has abandoned.

- **Default:** ✅ Enabled (`CLEANUP_ABANDONED: true`)
- **Configurable:** Set `CLEANUP_ABANDONED: false` in workflow to disable
- **Runs during:** After successful PR processing, push events, and when you close PRs
- **Behavior:**
  - Finds open GitHub PRs with `GitHub-PR` trailers in their associated Gerrit changes
  - Checks if the Gerrit change has `ABANDONED` status
  - Closes the GitHub PR with an appropriate comment explaining the abandonment
  - Respects the `CLOSE_MERGED_PRS` setting for whether to close or just comment

**Example log output:**

```text
Running abandoned PR cleanup...
Found 150 open PRs to check
PR #42 Gerrit change has ABANDONED status - will close
Abandoned PR cleanup complete: closed 1 PR(s)
```

#### 2. CLEANUP_GERRIT (Closed GitHub PRs → Abandon Gerrit Changes)

**What it does:** Scans all open Gerrit changes in the project and abandons those
whose corresponding GitHub PRs you have closed.

- **Default:** ✅ Enabled (`CLEANUP_GERRIT: true`)
- **Configurable:** Set `CLEANUP_GERRIT: false` in workflow to disable
- **Runs during:** After successful PR processing, push events, and when you close PRs
- **Behavior:**
  - Queries all open Gerrit changes in the project
  - Extracts the `GitHub-PR` trailer from each change
  - Checks if the GitHub PR has closed status
  - Abandons the Gerrit change with a message including:
    - PR number and URL
    - Any comments made when closing the PR
    - Automatic attribution to GitHub2Gerrit

**Example abandon message in Gerrit:**

```text
User closed GitHub pull request #34

PR URL: https://github.com/org/repo/pull/34

Comments when closing:

--- Comment 1 ---
Comment by username:
This PR is no longer needed because...
---

GitHub2Gerrit automatically abandoned this change
because user closed the source pull request.
```

**Example log output:**

```text
Running Gerrit cleanup for closed GitHub PRs...
Scanning open Gerrit changes in project-name for closed GitHub PRs
Found 25 open Gerrit change(s) to check
GitHub PR #34 has closed status, will abandon Gerrit change 12345
Abandoned Gerrit change 12345: https://gerrit.example.com/c/project/+/12345
Gerrit cleanup complete: abandoned 1 change(s)
```

### When Cleanup Runs

Cleanup operations run automatically in the following scenarios:

1. **After successful PR processing** - When you open, synchronize, or edit a PR
2. **On push events** - When Gerrit syncs changes back to GitHub (with `CLOSE_MERGED_PRS` enabled)
3. **When you close PRs** - Immediately when the system detects a PR close event

### PR Close Event Handling

When you close a GitHub PR, GitHub2Gerrit performs the following actions in order:

1. **Abandon the specific Gerrit change** for the closed PR
   - Searches for the Gerrit change with matching `GitHub-PR` trailer
   - Captures the last 3 comments from the PR (to preserve closure context)
   - Abandons the Gerrit change with those comments included

2. **Run CLEANUP_ABANDONED** - Close any other GitHub PRs with abandoned Gerrit changes

3. **Run CLEANUP_GERRIT** - Abandon any other Gerrit changes with closed GitHub PRs

**Example workflow:**

```text
🚪 PR closed event - running cleanup operations
Checking for Gerrit change to abandon for PR #34
Found Gerrit change 12345 for PR #34
✅ Abandoned Gerrit change 12345
Running abandoned PR cleanup...
Running Gerrit cleanup for closed GitHub PRs...
✅ Cleanup operations completed for closed PR
```

### Configuration

These cleanup operations **enable by default** but you can control them via
workflow inputs. They remain non-fatal, meaning if cleanup fails, the system logs a warning
but doesn't fail the entire workflow.

**Configuration options:**

- `CLEANUP_ABANDONED` - Default: `true` (shows ☑️ when enabled in configuration output)
- `CLEANUP_GERRIT` - Default: `true` (shows ☑️ when enabled in configuration output)

**Example - Disable cleanup operations:**

```yaml
uses: lfreleng-actions/github2gerrit-action@main
with:
  GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
  CLEANUP_ABANDONED: false  # Don't close GitHub PRs for abandoned changes
  CLEANUP_GERRIT: false     # Don't abandon Gerrit changes for closed PRs
```

**Example - Enable only one cleanup operation:**

```yaml
uses: lfreleng-actions/github2gerrit-action@main
with:
  GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
  CLEANUP_ABANDONED: true   # Close GitHub PRs when Gerrit abandons changes
  CLEANUP_GERRIT: false     # But don't abandon Gerrit changes when PRs close
```

**Dry-run support:** Both cleanup operations respect the `DRY_RUN` setting for testing.

### Notes

- Cleanup operations remain **parallel-safe** - workflow runs won't interfere with each other
- Operations remain **idempotent** - safe to run repeatedly
- The system skips PRs you already closed or changes Gerrit already abandoned (no duplicate actions)
- The system logs errors during cleanup as warnings and doesn't fail the workflow

## Restrict PRs to Automation Tools

GitHub2Gerrit can restrict pull request processing to known automation tools.
Use this for GitHub mirrors where you want contributors to submit changes via
Gerrit, while still accepting automated dependency updates from tools like
Dependabot.

**Configuration:**

Set `AUTOMATION_ONLY=true` (default) to enable, or `AUTOMATION_ONLY=false`
to accept all PRs.

**Recognized automation tools:**

| Tool          | GitHub Username(s)                    |
| ------------- | ------------------------------------- |
| Dependabot    | `dependabot[bot]`, `dependabot`       |
| Pre-commit.ci | `pre-commit-ci[bot]`, `pre-commit-ci` |

**What happens when enabled:**

The tool rejects PRs from non-automation users by:

1. Logging a warning message
2. Closing the PR with this comment:

   ```text
   This GitHub mirror does not accept pull requests.
   Please submit changes to the project's Gerrit server.
   ```

3. Exiting with code 1

**Example:**

```yaml
- uses: lfit/github2gerrit-action@main
  with:
    AUTOMATION_ONLY: "true"  # default, accepts automation PRs
    GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY }}
```

## Requirements

- Repository contains a `.gitreview` file. If you cannot provide it,
  you must pass `GERRIT_SERVER`, `GERRIT_SERVER_PORT`, and
  `GERRIT_PROJECT` via the reusable workflow interface.
- SSH key used to push changes into Gerrit
- The system populates Gerrit known hosts automatically on first run.
- The default `GITHUB_TOKEN` is available for PR metadata and comments.
- The workflow grants permissions required for PR interactions:
  - `pull-requests: write` (to comment on and close PRs)
  - `issues: write` (to create PR comments via the Issues API)
- The workflow runs with `pull_request_target` or via
  `workflow_dispatch` using a valid PR context.

## Error Codes

The `github2gerrit` tool uses standardized exit codes for different failure types. This helps with automation,
debugging, and providing clear feedback to users.

<!-- markdownlint-disable MD013 -->

| Exit Code | Description             | Common Causes                                | Resolution                                              |
| --------- | ----------------------- | -------------------------------------------- | ------------------------------------------------------- |
| **0**     | Success                 | Operation completed                          | N/A                                                     |
| **1**     | General Error           | Unexpected operational failure               | Check logs for details                                  |
| **2**     | Configuration Error     | Missing or invalid configuration parameters  | Verify required inputs and environment variables        |
| **3**     | Duplicate Error         | Duplicate change detected (when not allowed) | Use `--allow-duplicates` flag or check existing changes |
| **4**     | GitHub API Error        | GitHub API access or permission issues       | Verify `GITHUB_TOKEN` has required permissions          |
| **5**     | Gerrit Connection Error | Failed to connect to Gerrit server           | Check SSH keys, server configuration, and network       |
| **6**     | Network Error           | Network connectivity issues                  | Check internet connection and firewall settings         |
| **7**     | Repository Error        | Git repository access or operation failed    | Verify repository permissions and git configuration     |
| **8**     | PR State Error          | Pull request in invalid state for processing | Ensure PR is open and mergeable                         |
| **9**     | Validation Error        | Input validation failed                      | Check parameter values and formats                      |

<!-- markdownlint-enable MD013 -->

### Common Error Messages

#### GitHub API Permission Issues (Exit Code 4)

```text
❌ GitHub API query failed; provide a GITHUB_TOKEN with the required permissions
```

**Common causes:**

- Missing `GITHUB_TOKEN` environment variable
- Token lacks permissions for target repository
- Token expired or invalid
- Cross-repository access without proper token

**Resolution:**

- Configure `GITHUB_TOKEN` with a valid personal access token
- For cross-repository workflows, use a token with access to the target repository
- Grant required permissions: `contents: read`, `pull-requests: write`, `issues: write`

#### Configuration Issues (Exit Code 2)

```text
❌ Configuration validation failed; check required parameters
```

**Common causes:**

- Missing or invalid configuration parameters
- Invalid parameter combinations
- Missing `.gitreview` file without override parameters

**Resolution:**

- Verify all required inputs exist
- Check parameter compatibility (e.g., don't use conflicting options)
- Provide `GERRIT_SERVER`, `GERRIT_PROJECT` if `.gitreview` is missing

#### Gerrit Connection Issues (Exit Code 5)

```text
❌ Gerrit connection failed; check SSH keys and server configuration
```

**Common causes:**

- Invalid SSH private key
- SSH key not added to Gerrit account
- Incorrect Gerrit server configuration
- Network connectivity to Gerrit server

**Resolution:**

- Verify SSH private key is correct and has access to Gerrit
- Check Gerrit server hostname and port
- Ensure network connectivity to Gerrit server

### Integration Test Scenarios

The improved error handling is important for integration tests that run across different repositories.
For example, when testing the `github2gerrit-action` repository but accessing PRs in the `lfit/sandbox`
repository, you need:

1. **Cross-Repository Token Access**: Use `READ_ONLY_GITHUB_TOKEN` instead of the default `GITHUB_TOKEN`
   for workflows that access PRs in different repositories.

2. **Clear Error Messages**: If the token lacks permissions, you'll see:

   ```text
   ❌ GitHub API query failed; provide a GITHUB_TOKEN with the required permissions
   Details: Cannot access repository 'lfit/sandbox' - check token permissions
   ```

3. **Actionable Resolution**: The error message tells you what's needed - configure a token with access
   to the target repository.

### Debugging Workflow

When troubleshooting failures:

1. **Check the Exit Code**: Each failure has a unique exit code to help identify the root cause
2. **Read the Error Message**: Look for the ❌ prefixed message that explains what went wrong
3. **Review Details**: Context appears when available
4. **Check Logs**: Enable verbose logging with `G2G_VERBOSE=true` for detailed debugging information

### Note on sitecustomize.py

This repository includes a sitecustomize.py that is automatically
imported by Python’s site initialization. It exists to make pytest and
coverage runs in CI more robust by:

- assigns a unique COVERAGE_FILE per process to avoid mixing data across runs
- proactively removing stale .coverage artifacts in common base directories.

The logic runs during pytest sessions and is best effort.
It never interferes with normal execution. Maintainers can keep it to
stabilize coverage reporting for parallel/xdist runs.

## Duplicate detection

Duplicate detection uses a scoring-based approach. Instead of relying on a hash
added by this action, the detector compares the first line of the commit message
(subject/PR title), analyzes the body text and the set of files changed, and
computes a similarity score. When the score meets or exceeds a configurable
threshold (default 0.8), the tool treats the change as a duplicate and blocks
submission. This approach aims to remain robust even when similar changes
appeared outside this pipeline.

### Examples of detected duplicates

- Dependency bumps for the same package across close versions
  (e.g., "Bump foo from 1.0 to 1.1" vs "Bump foo from 1.1 to 1.2")
  with overlapping files — high score
- Pre-commit autoupdates that change .pre-commit-config.yaml and hook versions —
  high score
- GitHub Actions version bumps that update .github/workflows/* uses lines —
  medium to high score
- Similar bug fixes with the same subject and significant file overlap —
  strong match

### Allowing duplicates

Use `--allow-duplicates` or set `ALLOW_DUPLICATES=true` to override:

```bash
# CLI usage
github2gerrit --allow-duplicates https://github.com/org/repo

# GitHub Actions
uses: lfreleng-actions/github2gerrit-action@main
with:
  ALLOW_DUPLICATES: 'true'
```

When allowed, duplicates generate warnings but processing continues.
The tool exits with code 3 when it detects duplicates and they are not allowed.

### Configuring duplicate detection scope

By default, the duplicate detector considers changes with status `open` when searching for potential duplicates.
You can customize which Gerrit change states to check using `--duplicate-types` or setting `DUPLICATE_TYPES`:

```bash
# CLI usage - check against open and merged changes
github2gerrit --duplicate-types=open,merged https://github.com/org/repo

# Environment variable
DUPLICATE_TYPES=open,merged,abandoned github2gerrit https://github.com/org/repo

# GitHub Actions
uses: lfreleng-actions/github2gerrit-action@main
with:
  DUPLICATE_TYPES: 'open,merged'
```

Valid change states include `open`, `merged`, and `abandoned`. This setting determines which existing changes
to check when evaluating whether a new change would be a duplicate.

## Commit Message Normalization

The tool includes intelligent commit message normalization that automatically
converts automated PR titles (from tools like Dependabot, pre-commit.ci, etc.)
to follow conventional commit standards. This feature defaults to enabled
and you can control it via the `NORMALISE_COMMIT` setting.

### How it works

1. **Repository Analysis**: The tool analyzes your repository to determine
   preferred conventional commit patterns by examining:
   - `.pre-commit-config.yaml` for commit message formats
   - `.github/release-drafter.yml` for commit type patterns
   - Recent git history for existing conventional commit usage

2. **Smart Detection**: Applies normalization to automated PRs from
   known bots (dependabot[bot], pre-commit-ci[bot], etc.) or PRs with
   automation patterns in the title.

3. **Adaptive Formatting**: Respects your repository's existing conventions:
   - **Capitalization**: Detects whether you use `feat:` or `FEAT:`
   - **Commit Types**: Uses appropriate types (`chore`, `build`, `ci`, etc.)
   - **Dependency Updates**: Converts "Bump package from X to Y" to
     "chore: bump package from X to Y"

### Examples

**Before normalization:**

```text
Bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1
pre-commit autoupdate
Update GitHub Action dependencies
```

**After normalization:**

```text
chore: bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1
chore: pre-commit autoupdate
build: update GitHub Action dependencies
```

### Configuration

Enable or disable commit normalization:

```bash
# CLI usage
github2gerrit --normalise-commit https://github.com/org/repo
github2gerrit --no-normalise-commit https://github.com/org/repo

# Environment variable
NORMALISE_COMMIT=true github2gerrit https://github.com/org/repo
NORMALISE_COMMIT=false github2gerrit https://github.com/org/repo

# GitHub Actions
uses: lfreleng-actions/github2gerrit-action@main
with:
  NORMALISE_COMMIT: 'true'  # default
  # or
  NORMALISE_COMMIT: 'false'  # disable
```

### Repository-specific Configuration

To influence the normalization behavior, configure your repository:

**`.pre-commit-config.yaml`:**

```yaml
ci:
  autofix_commit_msg: |
    Chore: pre-commit autofixes

    Signed-off-by: pre-commit-ci[bot] <pre-commit-ci@users.noreply.github.com>
  autoupdate_commit_msg: |
    Chore: pre-commit autoupdate

    Signed-off-by: pre-commit-ci[bot] <pre-commit-ci@users.noreply.github.com>
```

**`.github/release-drafter.yml`:**

```yaml
autolabeler:
  - label: "chore"
    title:
      - "/chore:/i"
  - label: "feature"
    title:
      - "/feat:/i"
  - label: "bug"
    title:
      - "/fix:/i"
```

The tool will detect the capitalization style from these files and apply
it consistently to normalized commit messages.

### Example Usage in CI/CD

```bash
# Run the tool and handle different exit codes
if github2gerrit "$PR_URL"; then
    echo "✅ Submitted to Gerrit"
elif [ $? -eq 2 ]; then
    echo "❌ Configuration error - check your settings"
    exit 1
elif [ $? -eq 3 ]; then
    echo "⚠️  Duplicate detected - use ALLOW_DUPLICATES=true to override"
    exit 0  # Treat as non-fatal in some workflows
else
    echo "❌ Runtime failure - check logs for details"
    exit 1
fi
```

## Change-ID Reconciliation

The action includes an intelligent reconciliation system that reuses existing
Gerrit Change-IDs when updating pull requests. This prevents creating
duplicate changes in Gerrit when developers rebase, add commits, or amend a PR.

### How It Works

When developers update a PR (e.g., via `synchronize` event), the reconciliation system:

1. **Queries existing Gerrit changes** using the PR's topic (or falls back to GitHub comments)
2. **Matches local commits** to existing changes using these strategies:
   - **Trailer matching**: Reuses Change-IDs already present in commit messages
   - **Exact subject matching**: Matches commits with identical subjects
   - **File signature matching**: Matches commits with identical file changes
   - **Subject similarity matching**: Uses Jaccard similarity on commit subjects
3. **Generates new Change-IDs** for commits that don't match any existing change

### Configuration

The reconciliation behavior can be fine-tuned with these parameters:

**`REUSE_STRATEGY`** (default: `topic+comment`)

- `topic`: Query Gerrit changes by topic
- `comment`: Search GitHub PR comments for Change-IDs
- `topic+comment`: Try topic first, fall back to comments
- `none`: Disable reconciliation (always generate new Change-IDs)

**`SIMILARITY_SUBJECT`** (default: `0.7`)

- Jaccard similarity threshold (0.0-1.0) for subject matching
- Higher values require more similarity between commit subjects
- Example: `0.7` means 70% of words must match

**`SIMILARITY_UPDATE_FACTOR`** (default: `0.75`)

- Multiplier applied to similarity threshold for UPDATE operations
- Allows more lenient matching for rebased/amended commits
- Applied as: `update_threshold = max(0.5, base_threshold × factor)`
- Example: With base `0.7` and factor `0.75`, UPDATE threshold becomes `0.525`
- Floor threshold of `0.5` prevents too-loose matching

**`SIMILARITY_FILES`** (default: `false`)

- Whether to require exact file signature match during reconciliation (Pass C)
- When `true`: Commits must touch the exact same set of files to match (strict mode)
- When `false` (recommended): Skips file signature matching, relies on subject matching
- **Why default is `false`**: File signature matching is too strict for common workflows:
  - Developers add/remove files during PR updates
  - Rebasing shifts file changes between commits
  - Conflict resolution changes which files a commit touches
  - Developers amend commits with more file changes
- **When to use `true`**: Enable this for controlled workflows where file sets never change

**`ALLOW_ORPHAN_CHANGES`** (default: `false`)

- When enabled, unmatched Gerrit changes don't generate warnings
- Useful when you expect to remove changes from the topic

### Why Adjustable Similarity?

PR updates often involve rebasing, which can change commit messages slightly
(e.g., updating references, fixing typos, or resolving conflicts). The
`SIMILARITY_UPDATE_FACTOR` allows the system to recognize these as the same
logical change despite minor message differences:

- **Base threshold** (`SIMILARITY_SUBJECT`): Used for initial PR creation
- **Update threshold** (base × factor): Used for PR synchronize events
- **Percentage-based**: Scales consistently across different base thresholds
- **Floor at 0.5**: Prevents matching unrelated commits

### Example Configurations

```bash
# Strict matching - require 90% similarity, minor relaxation on updates
SIMILARITY_SUBJECT=0.9
SIMILARITY_UPDATE_FACTOR=0.85

# Lenient matching - allow more variation in commit messages
SIMILARITY_SUBJECT=0.6
SIMILARITY_UPDATE_FACTOR=0.7

# Recommended: Flexible matching for most workflows (default settings)
SIMILARITY_SUBJECT=0.7
SIMILARITY_UPDATE_FACTOR=0.75
SIMILARITY_FILES=false  # default - allows file changes in PR updates

# Strict matching - use for controlled workflows
SIMILARITY_SUBJECT=0.9
SIMILARITY_UPDATE_FACTOR=0.85
SIMILARITY_FILES=true  # requires exact file matches

# Disable reconciliation (always create new Change-IDs)
REUSE_STRATEGY=none
```

### Common Pitfalls

**File signature matching can break reconciliation during normal workflows:**

### GitHub Actions Example

```yaml
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    GERRIT_KNOWN_HOSTS: ${{ secrets.GERRIT_KNOWN_HOSTS }}
    GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
    SIMILARITY_SUBJECT: '0.75'
    SIMILARITY_UPDATE_FACTOR: '0.8'
    # SIMILARITY_FILES defaults to 'false' - uncomment to enable strict mode
    # SIMILARITY_FILES: 'true'
```

### CLI Example

```bash
# Custom similarity settings
github2gerrit \
  --similarity-subject 0.75 \
  --similarity-update-factor 0.8 \
  https://github.com/owner/repo/pull/123
```

## Usage

This action runs as part of a workflow that triggers on
`pull_request_target` and also supports manual runs via
`workflow_dispatch`.

Minimal example:

```yaml
name: github2gerrit

on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize]
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  submit-to-gerrit:
    runs-on: ubuntu-latest
    steps:
      - name: Submit PR to Gerrit
        id: g2g
        uses: lfreleng-actions/github2gerrit-action@main
        with:
          SUBMIT_SINGLE_COMMITS: "false"
          USE_PR_AS_COMMIT: "false"
          FETCH_DEPTH: "10"
          GERRIT_KNOWN_HOSTS: ${{ vars.GERRIT_KNOWN_HOSTS }}
          GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
          GERRIT_SSH_USER_G2G: ${{ vars.GERRIT_SSH_USER_G2G }}
          GERRIT_SSH_USER_G2G_EMAIL: ${{ vars.GERRIT_SSH_USER_G2G_EMAIL }}
          ORGANIZATION: ${{ github.repository_owner }}
          REVIEWERS_EMAIL: ""
          ISSUE_ID: ""  # Optional: adds 'Issue-ID: ...' trailer to the commit message
          ISSUE_ID_LOOKUP_JSON: ${{ vars.ISSUE_ID_LOOKUP_JSON }}  # Optional: JSON lookup table for automatic Issue-ID resolution
```

The action reads `.gitreview`. If `.gitreview` is absent, you must
supply Gerrit connection details through a reusable workflow or by
setting the corresponding environment variables before invoking the
action. The shell action enforces `.gitreview` for the composite
variant; this Python action mirrors that behavior for compatibility.

## Command Line Usage and Debugging

### Direct Command Line Usage

You can run the tool directly from the command line to process GitHub pull requests.

**For development (with local checkout):**

```bash
# Process a specific pull request
uv run github2gerrit https://github.com/owner/repo/pull/123

# Process all open pull requests in a repository
uv run github2gerrit https://github.com/owner/repo

# Run in CI mode (reads from environment variables)
uv run github2gerrit
```

**For CI/CD or one-time usage:**

```bash
# Install and run in one command
uvx github2gerrit https://github.com/owner/repo/pull/123

# Install from specific version/source
uvx --from git+https://github.com/lfreleng-actions/github2gerrit-action@main github2gerrit https://github.com/owner/repo/pull/123
```

### Available Options

```bash
# View help (local development)
uv run github2gerrit --help

# View help (CI/CD)
uvx github2gerrit --help
```

The comprehensive [Inputs](#inputs) table above documents all CLI options and shows
alignment between action inputs, environment variables, and CLI flags. All CLI flags
have corresponding environment variables for configuration.

Key options include:

- `--verbose` / `-v`: Enable verbose debug logging (`G2G_VERBOSE`)
- `--dry-run`: Check configuration without making changes (`DRY_RUN`)
- `--submit-single-commits`: Submit each commit individually (`SUBMIT_SINGLE_COMMITS`)
- `--use-pr-as-commit`: Use PR title/body as commit message (`USE_PR_AS_COMMIT`)
- `--issue-id`: Add an Issue-ID trailer (e.g., "Issue-ID: ABC-123") to the commit message (`ISSUE_ID`)
- `--preserve-github-prs`: Don't close GitHub PRs after submission (`PRESERVE_GITHUB_PRS`)
- `--duplicate-types`: Configure which Gerrit change states to check for duplicates (`DUPLICATE_TYPES`)

For a complete list of all available options, see the [Inputs](#inputs) section.

### Debugging and Troubleshooting

When encountering issues, enable verbose logging to see detailed execution:

```bash
# Using the CLI flag
github2gerrit --verbose https://github.com/owner/repo/pull/123

# Using environment variable
G2G_LOG_LEVEL=DEBUG github2gerrit https://github.com/owner/repo/pull/123

# Alternative environment variable
G2G_VERBOSE=true github2gerrit https://github.com/owner/repo/pull/123
```

Debug output includes:

- Git command execution and output
- SSH connection attempts
- Gerrit API interactions
- Branch resolution logic
- Change-Id processing

Common issues and solutions:

1. **Configuration Validation Errors**: The tool provides clear error messages when
   required configuration is missing or invalid. Look for messages starting with
   "Configuration validation failed:" that specify missing inputs like
   `GERRIT_KNOWN_HOSTS`, `GERRIT_SSH_PRIVKEY_G2G`, etc.

2. **SSH Permission Denied**:
   - Ensure `GERRIT_SSH_PRIVKEY_G2G` and `GERRIT_KNOWN_HOSTS` are properly set
   - If you see "Permissions 0644 for 'gerrit_key' are too open", the action will automatically
     try SSH agent authentication
   - For persistent file permission issues, ensure `G2G_USE_SSH_AGENT=true` (default)

3. **Branch Not Found**: Check that the target branch exists in both GitHub and Gerrit
4. **Change-Id Issues**: Enable debug logging to see Change-Id generation and validation
5. **Account Not Found Errors**: If you see "Account '<Email@Domain.com>' not found",
   ensure your Gerrit account email matches your git config email (case-sensitive).
6. **Gerrit API Errors**: Verify Gerrit server connectivity and project permissions

> **Note**: The tool displays configuration errors cleanly without Python tracebacks.
> If you see a traceback in the output, please report it as a bug.

### Environment Variables

The comprehensive [Inputs](#inputs) table above documents all environment variables.
Key variables for CLI usage include:

- `G2G_LOG_LEVEL`: Set to `DEBUG` for verbose output (default: `INFO`)
- `G2G_VERBOSE`: Set to `true` to enable debug logging (same as `--verbose` flag)
- `GERRIT_SSH_PRIVKEY_G2G`: SSH private key content
- `GERRIT_KNOWN_HOSTS`: SSH known hosts entries
- `GERRIT_SSH_USER_G2G`: Gerrit SSH username
- `G2G_USE_SSH_AGENT`: Set to `false` to force file-based SSH (default: `true`)
- `DRY_RUN`: Set to `true` for check mode
- `CI_TESTING`: Set to `true` to ignore `.gitreview` file and use environment variables instead

For a complete list of all supported environment variables, their defaults, and
their corresponding action inputs and CLI flags, see the [Inputs](#inputs) section.

## Advanced usage

### Overriding .gitreview Settings

When `CI_TESTING=true`, the tool ignores any `.gitreview` file in the
repository and uses environment variables instead. This is useful for:

- **Integration testing** against different Gerrit servers
- **Overriding repository settings** when the `.gitreview` points to the wrong server
- **Development and debugging** with custom Gerrit configurations

**Example:**

```bash
export CI_TESTING=true
export GERRIT_SERVER=gerrit.example.org
export GERRIT_PROJECT=sandbox
github2gerrit https://github.com/org/repo/pull/123
```

### SSH Authentication Methods

This action supports two SSH authentication methods:

1. **SSH Agent Authentication (Default)**: More secure, avoids file permission issues in CI
2. **File-based Authentication**: Fallback method that writes keys to temporary files

#### SSH Agent Authentication

By default, the action uses SSH agent to load keys into memory rather than writing them to disk. This is more
secure and avoids the file permission issues commonly seen in CI environments.

To control this behavior:

```yaml
- name: Submit to Gerrit
  uses: your-org/github2gerrit-action@v1
  env:
    G2G_USE_SSH_AGENT: "true"  # Default: enables SSH agent (recommended)
    # G2G_USE_SSH_AGENT: "false"  # Forces file-based authentication
  with:
    GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
    # ... other inputs
```

**Benefits of SSH Agent Authentication:**

- No temporary files written to disk
- Avoids SSH key file permission issues (0644 vs 0600)
- More secure in containerized CI environments
- Automatic cleanup when process exits

#### File-based Authentication (Fallback)

If SSH agent setup fails, the action automatically falls back to writing the SSH key to a temporary file with
secure permissions. This method:

- Creates files in workspace-specific `.ssh-g2g/` directory
- Attempts to set proper file permissions (0600)
- Includes four fallback permission-setting strategies for CI environments

### Custom SSH Configuration

You can explicitly install the SSH key and provide a custom SSH configuration
before invoking this action. This is useful when:

- You want to override the port/host used by SSH
- You need to define host aliases or SSH options
- Your Gerrit instance uses a non-standard HTTP base path (e.g. /r)

Example:

```yaml
name: github2gerrit (advanced)

on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize]
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  submit-to-gerrit:
    runs-on: ubuntu-latest
    steps:


      - name: Submit PR to Gerrit (with explicit overrides)
        id: g2g
        uses: lfreleng-actions/github2gerrit-action@main
        with:
          # Behavior
          SUBMIT_SINGLE_COMMITS: "false"
          USE_PR_AS_COMMIT: "false"
          FETCH_DEPTH: "10"

          # Required SSH/identity
          GERRIT_KNOWN_HOSTS: ${{ vars.GERRIT_KNOWN_HOSTS }}
          GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
          GERRIT_SSH_USER_G2G: ${{ vars.GERRIT_SSH_USER_G2G }}
          GERRIT_SSH_USER_G2G_EMAIL: ${{ vars.GERRIT_SSH_USER_G2G_EMAIL }}

          # Optional overrides when .gitreview is missing or to force values
          GERRIT_SERVER: ${{ vars.GERRIT_SERVER }}
          GERRIT_SERVER_PORT: ${{ vars.GERRIT_SERVER_PORT }}
          GERRIT_PROJECT: ${{ vars.GERRIT_PROJECT }}

          # Optional Gerrit REST base path and credentials (if required)
          # e.g. '/r' for some deployments
          GERRIT_HTTP_BASE_PATH: ${{ vars.GERRIT_HTTP_BASE_PATH }}
          GERRIT_HTTP_USER: ${{ vars.GERRIT_HTTP_USER }}
          GERRIT_HTTP_PASSWORD: ${{ secrets.GERRIT_HTTP_PASSWORD }}

          ORGANIZATION: ${{ github.repository_owner }}
          REVIEWERS_EMAIL: ""
```

Notes:

- The action configures SSH internally using the provided inputs (key,
  known_hosts) and does not use the runner’s SSH agent or ~/.ssh/config.
- Do not add external steps to install SSH keys or edit SSH config; they’re
  unnecessary and may conflict with the action.

## GitHub Enterprise support

- Direct-URL mode accepts enterprise GitHub hosts when explicitly enabled.
  Default: off (use github.com by default). Enable via the CLI flag
  --allow-ghe-urls or by setting ALLOW_GHE_URLS="true".
- In GitHub Actions, this action works with GitHub Enterprise when the
  workflow runs in that enterprise environment and provides a valid
  GITHUB_TOKEN. For direct-URL runs outside Actions, ensure ORGANIZATION
  and GITHUB_REPOSITORY reflect the target repository.

## Inputs

All inputs are strings, matching the composite action. The following table shows
alignment between action inputs, environment variables, and CLI flags:

<!-- markdownlint-disable MD013 -->

| Action Input                | Environment Variable        | CLI Flag                      | Required | Default                          | Description                                                                                |
| --------------------------- | --------------------------- | ----------------------------- | -------- | -------------------------------- | ------------------------------------------------------------------------------------------ |
| `SUBMIT_SINGLE_COMMITS`     | `SUBMIT_SINGLE_COMMITS`     | `--submit-single-commits`     | No       | `"false"`                        | Submit one commit at a time to Gerrit                                                      |
| `USE_PR_AS_COMMIT`          | `USE_PR_AS_COMMIT`          | `--use-pr-as-commit`          | No       | `"false"`                        | Use PR title and body as the commit message                                                |
| `FETCH_DEPTH`               | `FETCH_DEPTH`               | `--fetch-depth`               | No       | `"10"`                           | Fetch depth for checkout                                                                   |
| `PR_NUMBER`                 | `PR_NUMBER`                 | N/A                           | No       | `"0"`                            | Pull request number to process (workflow_dispatch)                                         |
| `GERRIT_KNOWN_HOSTS`        | `GERRIT_KNOWN_HOSTS`        | `--gerrit-known-hosts`        | Yes      | N/A                              | SSH known hosts entries for Gerrit                                                         |
| `GERRIT_SSH_PRIVKEY_G2G`    | `GERRIT_SSH_PRIVKEY_G2G`    | `--gerrit-ssh-privkey-g2g`    | Yes      | N/A                              | SSH private key content for Gerrit authentication                                          |
| `GERRIT_SSH_USER_G2G`       | `GERRIT_SSH_USER_G2G`       | `--gerrit-ssh-user-g2g`       | No¹      | `""`                             | Gerrit SSH username (auto-derived if enabled)                                              |
| `GERRIT_SSH_USER_G2G_EMAIL` | `GERRIT_SSH_USER_G2G_EMAIL` | `--gerrit-ssh-user-g2g-email` | No¹      | `""`                             | Email for Gerrit SSH user (auto-derived if enabled)                                        |
| `ORGANIZATION`              | `ORGANIZATION`              | `--organization`              | No       | `${{ github.repository_owner }}` | GitHub organization/owner                                                                  |
| `REVIEWERS_EMAIL`           | `REVIEWERS_EMAIL`           | `--reviewers-email`           | No       | `""`                             | Comma-separated reviewer emails                                                            |
| `ALLOW_GHE_URLS`            | `ALLOW_GHE_URLS`            | `--allow-ghe-urls`            | No       | `"false"`                        | Allow GitHub Enterprise URLs in direct URL mode                                            |
| `PRESERVE_GITHUB_PRS`       | `PRESERVE_GITHUB_PRS`       | `--preserve-github-prs`       | No       | `"true"`                         | Do not close GitHub PRs after pushing to Gerrit                                            |
| `DRY_RUN`                   | `DRY_RUN`                   | `--dry-run`                   | No       | `"false"`                        | Check settings/PR metadata; do not write to Gerrit                                         |
| `ALLOW_DUPLICATES`          | `ALLOW_DUPLICATES`          | `--allow-duplicates`          | No       | `"false"`                        | Allow submitting duplicate changes without error                                           |
| `CI_TESTING`                | `CI_TESTING`                | `--ci-testing`                | No       | `"false"`                        | Enable CI testing mode (overrides .gitreview)                                              |
| `ISSUE_ID`                  | `ISSUE_ID`                  | `--issue-id`                  | No       | `""`                             | Issue ID to include (e.g., ABC-123)                                                        |
| `ISSUE_ID_LOOKUP_JSON`      | `ISSUE_ID_LOOKUP_JSON`      | `--issue-id-lookup-json`      | No       | `"[]"`                           | JSON array mapping GitHub actors to Issue IDs (automatic lookup if ISSUE_ID not provided)  |
| `G2G_USE_SSH_AGENT`         | `G2G_USE_SSH_AGENT`         | N/A                           | No       | `"true"`                         | Use SSH agent for authentication                                                           |
| `DUPLICATE_TYPES`           | `DUPLICATE_TYPES`           | `--duplicate-types`           | No       | `"open"`                         | Comma-separated Gerrit change states to check for duplicate detection                      |
| `AUTOMATION_ONLY`           | `AUTOMATION_ONLY`           | `--automation-only`           | No       | `"true"`                         | Accept PRs from automation tools only (dependabot, pre-commit-ci); reject human PRs        |
| `CLEANUP_ABANDONED`         | `CLEANUP_ABANDONED`         | N/A                           | No       | `"true"`                         | Close GitHub PRs for abandoned Gerrit changes                                              |
| `CLEANUP_GERRIT`            | `CLEANUP_GERRIT`            | N/A                           | No       | `"true"`                         | Abandon Gerrit changes for closed GitHub PRs closure                                       |
| `REUSE_STRATEGY`            | `REUSE_STRATEGY`            | `--reuse-strategy`            | No       | `"topic+comment"`                | Change-ID reuse strategy: `topic`, `comment`, `topic+comment`, or `none`                   |
| `SIMILARITY_SUBJECT`        | `SIMILARITY_SUBJECT`        | `--similarity-subject`        | No       | `"0.7"`                          | Jaccard similarity threshold (0.0-1.0) for subject matching during reconciliation          |
| `SIMILARITY_UPDATE_FACTOR`  | `SIMILARITY_UPDATE_FACTOR`  | `--similarity-update-factor`  | No       | `"0.75"`                         | Multiplier (0.0-1.0) for similarity threshold on PR UPDATE operations (rebases/amendments) |
| `SIMILARITY_FILES`          | `SIMILARITY_FILES`          | `--similarity-files`          | No       | `"false"`                        | Require exact file signature match for reconciliation (strict mode)                        |
| `ALLOW_ORPHAN_CHANGES`      | `ALLOW_ORPHAN_CHANGES`      | `--allow-orphan-changes`      | No       | `"false"`                        | Keep unmatched Gerrit changes without warning during reconciliation                        |
| `GERRIT_SERVER`             | `GERRIT_SERVER`             | `--gerrit-server`             | No²      | `""`                             | Gerrit server hostname (auto-derived if enabled)                                           |
| `GERRIT_SERVER_PORT`        | `GERRIT_SERVER_PORT`        | `--gerrit-server-port`        | No       | `"29418"`                        | Gerrit SSH port                                                                            |
| `GERRIT_PROJECT`            | `GERRIT_PROJECT`            | `--gerrit-project`            | No²      | `""`                             | Gerrit project name                                                                        |
| `GERRIT_HTTP_BASE_PATH`     | `GERRIT_HTTP_BASE_PATH`     | N/A                           | No       | `""`                             | HTTP base path for Gerrit REST API                                                         |
| `GERRIT_HTTP_USER`          | `GERRIT_HTTP_USER`          | N/A                           | No       | `""`                             | Gerrit HTTP user for REST authentication                                                   |
| `GERRIT_HTTP_PASSWORD`      | `GERRIT_HTTP_PASSWORD`      | N/A                           | No       | `""`                             | Gerrit HTTP password/token for REST authentication                                         |
| N/A                         | `G2G_VERBOSE`               | `--verbose`, `-v`             | No       | `"false"`                        | Enable verbose debug logging                                                               |

<!-- markdownlint-enable MD013 -->

**Notes:**

1. Auto-derived when `G2G_ENABLE_DERIVATION=true` (default: true in all contexts)
2. Optional if `.gitreview` file exists in repository

The format required for the JSON Issue-ID lookup is:

`[{"key": "username", "value": "ISSUE-ID"}]`

### Internal Environment Variables

The following environment variables control internal behavior but are not action inputs:

<!-- markdownlint-disable MD013 -->

| Environment Variable         | Description                                    | Default                                    |
| ---------------------------- | ---------------------------------------------- | ------------------------------------------ |
| `G2G_LOG_LEVEL`              | Logging level (DEBUG, INFO, WARNING, ERROR)    | `"INFO"`                                   |
| `G2G_ENABLE_DERIVATION`      | Enable auto-derivation of Gerrit parameters    | `"true"`                                   |
| `G2G_CONFIG_PATH`            | Path to organization configuration file        | `~/.config/github2gerrit/config.ini`       |
| `G2G_AUTO_SAVE_CONFIG`       | Auto-save derived parameters to config         | `"false"` (GitHub Actions), `"true"` (CLI) |
| `G2G_TARGET_URL`             | Internal flag for direct URL mode              | Set automatically                          |
| `G2G_TMP_BRANCH`             | Temporary branch name for single commits       | `"tmp_branch"`                             |
| `G2G_TOPIC_PREFIX`           | Prefix for Gerrit topic names                  | `"GH"`                                     |
| `G2G_SKIP_GERRIT_COMMENTS`   | Skip posting back-reference comments in Gerrit | `"false"`                                  |
| `G2G_DRYRUN_DISABLE_NETWORK` | Disable network calls in dry-run mode          | `"false"`                                  |
| `SYNC_ALL_OPEN_PRS`          | Process all open PRs (set automatically)       | Set automatically                          |
| `GERRIT_BRANCH`              | Override target branch for Gerrit              | Uses `GITHUB_BASE_REF`                     |
| `GITHUB_TOKEN`               | GitHub API token                               | Provided by GitHub Actions                 |
| `GITHUB_*` context           | GitHub Actions context variables               | Provided by GitHub Actions                 |

<!-- markdownlint-enable MD013 -->

## Outputs

The action provides the following outputs for use in later workflow steps:

<!-- markdownlint-disable MD013 -->

| Output Name                 | Description                                 | Environment Variable        |
| --------------------------- | ------------------------------------------- | --------------------------- |
| `gerrit_change_request_url` | Gerrit change URL(s) (newline-separated)    | `GERRIT_CHANGE_REQUEST_URL` |
| `gerrit_change_request_num` | Gerrit change number(s) (newline-separated) | `GERRIT_CHANGE_REQUEST_NUM` |
| `gerrit_commit_sha`         | Patch set commit SHA(s) (newline-separated) | `GERRIT_COMMIT_SHA`         |

<!-- markdownlint-enable MD013 -->

These outputs export automatically as environment variables and are accessible in
later workflow steps using `${{ steps.<step-id>.outputs.<output-name> }}` syntax.

## Configuration and Parameters

For a complete list of all supported configuration parameters, including action
inputs, environment variables, and CLI flags, see the comprehensive [Inputs](#inputs)
table above.

### Configuration Precedence

The tool follows this precedence order for configuration values:

1. **CLI flags** (highest priority)
2. **Environment variables**
3. **Configuration file values**
4. **Tool defaults** (lowest priority)

### Configuration File Format

Configuration files use INI format with organization-specific sections:

```ini
[default]
GERRIT_SERVER = "gerrit.example.org"
PRESERVE_GITHUB_PRS = "true"

[onap]
ISSUE_ID = "CIMAN-33"
REVIEWERS_EMAIL = "user@example.org"

[opendaylight]
GERRIT_HTTP_USER = "bot-user"
GERRIT_HTTP_PASSWORD = "${ENV:ODL_GERRIT_TOKEN}"
```

The tool loads configuration from `~/.config/github2gerrit/configuration.txt`
by default, or from the path specified in the `G2G_CONFIG_PATH` environment
variable.

**Note**: Unknown configuration keys will generate warnings to help catch typos
and missing functionality.

### Credential Derivation

When `GERRIT_SSH_USER_G2G` and `GERRIT_SSH_USER_G2G_EMAIL` are not explicitly provided,
the tool automatically derives these credentials using a multi-source approach with the
following priority order:

#### Derivation Sources (in priority order)

1. **SSH Config User** (if `G2G_RESPECT_USER_SSH=true` in local mode)
   - Reads from `~/.ssh/config` for the specific Gerrit host
   - Matches host patterns (supports wildcards like `gerrit.*`)
   - Extracts the `User` directive for matching entries

2. **Git User Email** (if `G2G_RESPECT_USER_SSH=true` in local mode)
   - Reads from local git configuration (`git config user.email`)
   - Used as the email address for commits

3. **Organization-based Fallback** (default for GitHub Actions)
   - Derives credentials from the GitHub organization name
   - Generates standardized values

#### Organization-based Pattern

The fallback credentials follow this pattern based on the `ORGANIZATION` value:

- **Gerrit Server**: Derived as `gerrit.{organization}.org` (or from config file)
- **SSH Username**: `{organization}.gh2gerrit`
- **Email Address**: `releng+{organization}-gh2gerrit@linuxfoundation.org`

**Example**: For organization `onap`:

- Server: `gerrit.onap.org`
- Username: `onap.gh2gerrit`
- Email: `releng+onap-gh2gerrit@linuxfoundation.org`

#### Organization Name Source

The tool determines the organization name from GitHub context in the following order:

1. Explicit `ORGANIZATION` parameter (action input or environment variable)
2. `GITHUB_REPOSITORY_OWNER` (automatically set by GitHub Actions to the repository owner)

**Example**: For a repository `onap/releng-builder`:

- Organization: `onap` (from `github.repository_owner`)
- Derived server: `gerrit.onap.org`
- Derived username: `onap.gh2gerrit`
- Derived email: `releng+onap-gh2gerrit@linuxfoundation.org`

The tool normalizes the organization name to lowercase before using it to construct the
Gerrit server hostname and credentials.

#### Local Development Mode

For local CLI usage, set `G2G_RESPECT_USER_SSH=true` to use your personal SSH config
and git config instead of organization-based defaults:

```bash
# Enable personalized credentials from SSH/git config
export G2G_RESPECT_USER_SSH=true
github2gerrit https://github.com/org/repo/pull/123
```

**Example `~/.ssh/config` entry:**

```ssh-config
Host gerrit.*.org
    User alice

Host gerrit.opendaylight.org
    User alice-odl
    Port 29418
```

With this configuration and `G2G_RESPECT_USER_SSH=true`:

- Username will be `alice` (from SSH config)
- Email will be from `git config user.email`
- Falls back to organization-based values if SSH/git config not found

#### GitHub Actions Mode

In GitHub Actions (the default), credentials always use the organization-based fallback
pattern unless explicitly provided via action inputs:

```yaml
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    ORGANIZATION: ${{ github.repository_owner }}  # e.g., "onap"
    # Credentials auto-derived:
    # - GERRIT_SSH_USER_G2G: onap.gh2gerrit
    # - GERRIT_SSH_USER_G2G_EMAIL: releng+onap-gh2gerrit@linuxfoundation.org
```

To override with custom credentials:

```yaml
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    GERRIT_SSH_USER_G2G: ${{ vars.GERRIT_SSH_USER_G2G }}
    GERRIT_SSH_USER_G2G_EMAIL: ${{ vars.GERRIT_SSH_USER_G2G_EMAIL }}
    ORGANIZATION: ${{ github.repository_owner }}
```

#### Disabling Derivation

To disable automatic derivation entirely, set `G2G_ENABLE_DERIVATION=false`. This requires
all Gerrit parameters to be explicitly provided.

### Issue ID Lookup

The action supports automatic Issue ID resolution via JSON lookup when you
omit `ISSUE_ID`. Set the `ISSUE_ID_LOOKUP_JSON` input with a valid JSON array,
and the action will automatically look up the Issue ID based on the GitHub
actor who created the pull request.

```yaml
- uses: lfreleng-actions/github2gerrit-action@v1
  with:
    GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
    # Automatic Issue ID lookup (pass repository variable as input)
    ISSUE_ID_LOOKUP_JSON: ${{ vars.ISSUE_ID_LOOKUP_JSON }}
    # ... other inputs
```

**Setup:**

Set a repository or organization variable named `ISSUE_ID_LOOKUP_JSON` with a
JSON array mapping GitHub usernames to Issue IDs:

**Example JSON format:**

   ```json
   [
     { "key": "dependabot[bot]", "value": "AUTO-123" },
     { "key": "renovate[bot]", "value": "AUTO-456" },
     { "key": "alice", "value": "PROJ-789" },
     { "key": "bob", "value": "PROJ-101" }
   ]
   ```

**Lookup Logic:**

1. If you provide `ISSUE_ID` input → action uses it directly (highest priority)
2. If `ISSUE_ID` is empty AND `ISSUE_ID_LOOKUP_JSON` is valid JSON → action automatically looks up Issue ID using `github.actor`
3. If lookup fails or JSON is invalid → action logs a warning and skips Issue ID

**Validation:**

- If `ISSUE_ID_LOOKUP_JSON` contains invalid JSON, the action displays a warning: `⚠️ Warning: Issue-ID JSON was not valid`
- Invalid JSON will not cause the workflow to fail, but the action will skip adding Issue ID
- The warning appears in both console output and log files

This feature helps organizations automatically tag commits with
project-specific Issue IDs based on who creates the pull request, without
requiring manual configuration per PR or user.

## Behavior details

- Branch resolution
  - Uses `GITHUB_BASE_REF` as the target branch for Gerrit, or defaults
    to `master` when unset, matching the existing workflow.
- Topic naming
  - Uses `GH-<repo>-<pr-number>` where `<repo>` replaces slashes with
    hyphens.
- GitHub Enterprise support
  - Direct URL mode accepts enterprise GitHub hosts when explicitly enabled
    (default: off; use github.com by default). Enable via --allow-ghe-urls or
    ALLOW_GHE_URLS="true". The tool determines the GitHub API base URL from
    GITHUB_API_URL or GITHUB_SERVER_URL/api/v3.
- Change‑Id handling
  - Single commits: the process amends each cherry‑picked commit to include a
    `Change-Id`. The tool collects these values for querying.
  - Squashed: collects trailers from original commits, preserves
    `Signed-off-by`, and reuses the `Change-Id` when PRs reopen or synchronize.
- Reviewers
  - If empty, defaults to the Gerrit SSH user email.
- Comments
  - Adds a back‑reference comment in Gerrit with the GitHub PR and run
    URL. Adds a comment on the GitHub PR with the Gerrit change URL(s).
- Closing PRs
  - By default, PRs are **preserved** after submission (`PRESERVE_GITHUB_PRS=true`).
  - Set `PRESERVE_GITHUB_PRS=false` to close PRs after submission on `pull_request_target` events.

## Security notes

- Do not hardcode secrets or keys. Provide the private key via the
  workflow secrets and known hosts via repository or org variables.
- SSH handling is non-invasive: the tool creates temporary SSH files in
  the workspace without modifying user SSH configuration or keys.
- SSH agent scanning prevention uses `IdentitiesOnly=yes` to avoid
  unintended key usage (e.g., signing keys requiring biometric auth).
- Temporary SSH files are automatically cleaned up after execution.
- All external calls should use retries and clear error reporting.

## Development

- Language and CLI
  - Python 3.11+, the CLI uses Typer.
- Packaging
  - `pyproject.toml` with setuptools backend. Use `uv` to install and run.
- Structure
  - `src/github2gerrit/cli.py` (CLI entrypoint)
  - `src/github2gerrit/core.py` (orchestration)
  - `src/github2gerrit/gitutils.py` (subprocess and git helpers)
- Linting and type checking
  - Ruff and MyPy use settings in `pyproject.toml`.
  - Run from pre‑commit hooks and CI.
- Tests
  - Pytest with coverage targets around 80%.
  - Add unit and integration tests for each feature.

### Local setup

- Install `uv` and run:
  - `uv pip install --system .`
  - `uv run github2gerrit --help`
- Run tests:
  - `uv run pytest -q`
- Lint and type check:
  - `uv run ruff check .`
  - `uv run ruff format .`
  - `uv run mypy src`

### Dependency management

- **Update dependencies**: Use `uv lock --upgrade` to rebuild and update the `uv.lock` file with the latest compatible versions
- **Add new dependencies**: Add to `pyproject.toml` then run `uv lock` to update the lock file
- **Install from lock file**: `uv pip install --system .` will use the exact versions from `uv.lock`

### Local testing and development

Test local builds before releases with commands like:

```bash
# Test against a real PR with dry-run mode
uv run python -m github2gerrit.cli https://github.com/onap/portal-ng-bff/pull/37 --preserve-github-prs --dry-run

# Test with different options
uv run python -m github2gerrit.cli <PR_URL> --help

# Run the CLI directly for development
uv run github2gerrit --help
```

### CI/CD and production usage

For CI/CD pipelines (like GitHub Actions), use `uvx` to install and run without managing virtual environments:

```bash
# Install and run in one command
uvx github2gerrit <PR_URL> --dry-run

# Install from a specific version or source
uvx --from git+https://github.com/lfreleng-actions/github2gerrit-action@main github2gerrit <PR_URL>

# Run with specific Python version
uvx --python 3.11 github2gerrit <PR_URL>
```

**Note**: `uvx` is ideal for CI/CD as it automatically handles dependency isolation and cleanup.

### Notes on parity

- Inputs, outputs, and environment usage match the shell action.
- The action assumes the same GitHub variables and secrets are present.
- Where the shell action uses tools such as `jq` and `gh`, the Python
  version uses library calls and subprocess as appropriate, with retries
  and clear logging.

## License

Apache License 2.0. See `LICENSE` for details.
