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
- Detects and prevents tool runs from creating duplicate changes.
- Reads `.gitreview` for Gerrit host, port, and project.
- When run locally, will pull `.gitreview` from the remote repository.
- Sets up `git` user config and SSH for Gerrit.
- Prepare commits:
  - one‑by‑one cherry‑pick with `Change-Id` trailers, or
  - squash into a single commit and keep or reuse `Change-Id`.
- Optionally replace the commit message with PR title and body.
- Push with a topic to `refs/for/<branch>` using `git-review` behavior.
- Query Gerrit for the resulting URL, change number, and patchset SHA.
- Add a back‑reference comment in Gerrit to the GitHub PR and run URL.
- Comment on the GitHub PR with the Gerrit change URL(s).
- Optionally close the PR (mirrors the shell action policy).

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
uses: onap/github2gerrit@main
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
uses: onap/github2gerrit@main
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
uses: onap/github2gerrit@main
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

## Exit Codes

The CLI tool uses specific exit codes to signal different types of failures,
making it easier to handle errors in automation and CI/CD pipelines:

| Exit Code | Description | Common Causes |
|-----------|-------------|---------------|
| `0` | Success | Operation completed |
| `1` | General runtime failure | Submission pipeline errors, Git operations failed, network issues, unhandled exceptions |
| `2` | Configuration or validation error | Invalid configuration parameters, missing required settings, empty PR context |
| `3` | Duplicate detection blocked submission | Similar change already exists in Gerrit and `ALLOW_DUPLICATES` is `false` |

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
   - If you see "Permissions 0644 for 'gerrit_key' are too open", the action will automatically try SSH agent
     authentication
   - For persistent file permission issues, ensure `G2G_USE_SSH_AGENT=true` (default)

3. **Branch Not Found**: Check that the target branch exists in both GitHub and Gerrit
4. **Change-Id Issues**: Enable debug logging to see Change-Id generation and validation
5. **Gerrit API Errors**: Verify Gerrit server connectivity and project permissions

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

| Action Input | Environment Variable | CLI Flag | Required | Default | Description |
|-------------|---------------------|----------|----------|---------|-------------|
| `SUBMIT_SINGLE_COMMITS` | `SUBMIT_SINGLE_COMMITS` | `--submit-single-commits` | No | `"false"` | Submit one commit at a time to Gerrit |
| `USE_PR_AS_COMMIT` | `USE_PR_AS_COMMIT` | `--use-pr-as-commit` | No | `"false"` | Use PR title and body as the commit message |
| `FETCH_DEPTH` | `FETCH_DEPTH` | `--fetch-depth` | No | `"10"` | Fetch depth for checkout |
| `PR_NUMBER` | `PR_NUMBER` | N/A | No | `"0"` | Pull request number to process (workflow_dispatch) |
| `GERRIT_KNOWN_HOSTS` | `GERRIT_KNOWN_HOSTS` | `--gerrit-known-hosts` | Yes | N/A | SSH known hosts entries for Gerrit |
| `GERRIT_SSH_PRIVKEY_G2G` | `GERRIT_SSH_PRIVKEY_G2G` | `--gerrit-ssh-privkey-g2g` | Yes | N/A | SSH private key content for Gerrit authentication |
| `GERRIT_SSH_USER_G2G` | `GERRIT_SSH_USER_G2G` | `--gerrit-ssh-user-g2g` | No¹ | `""` | Gerrit SSH username (auto-derived if enabled) |
| `GERRIT_SSH_USER_G2G_EMAIL` | `GERRIT_SSH_USER_G2G_EMAIL` | `--gerrit-ssh-user-g2g-email` | No¹ | `""` | Email for Gerrit SSH user (auto-derived if enabled) |
| `ORGANIZATION` | `ORGANIZATION` | `--organization` | No | `${{ github.repository_owner }}` | GitHub organization/owner |
| `REVIEWERS_EMAIL` | `REVIEWERS_EMAIL` | `--reviewers-email` | No | `""` | Comma-separated reviewer emails |
| `ALLOW_GHE_URLS` | `ALLOW_GHE_URLS` | `--allow-ghe-urls` | No | `"false"` | Allow GitHub Enterprise URLs in direct URL mode |
| `PRESERVE_GITHUB_PRS` | `PRESERVE_GITHUB_PRS` | `--preserve-github-prs` | No | `"false"` | Do not close GitHub PRs after pushing to Gerrit |
| `DRY_RUN` | `DRY_RUN` | `--dry-run` | No | `"false"` | Check settings/PR metadata; do not write to Gerrit |
| `ALLOW_DUPLICATES` | `ALLOW_DUPLICATES` | `--allow-duplicates` | No | `"false"` | Allow submitting duplicate changes without error |
| `CI_TESTING` | `CI_TESTING` | `--ci-testing` | No | `"false"` | Enable CI testing mode (overrides .gitreview) |
| `ISSUE_ID` | `ISSUE_ID` | `--issue-id` | No | `""` | Issue ID to include (e.g., ABC-123) |
| `G2G_USE_SSH_AGENT` | `G2G_USE_SSH_AGENT` | N/A | No | `"true"` | Use SSH agent for authentication |
| `DUPLICATE_TYPES` | `DUPLICATE_TYPES` | `--duplicate-types` | No | `"open"` | Comma-separated Gerrit change states to check for duplicate detection |
| `GERRIT_SERVER` | `GERRIT_SERVER` | `--gerrit-server` | No² | `""` | Gerrit server hostname (auto-derived if enabled) |
| `GERRIT_SERVER_PORT` | `GERRIT_SERVER_PORT` | `--gerrit-server-port` | No | `"29418"` | Gerrit SSH port |
| `GERRIT_PROJECT` | `GERRIT_PROJECT` | `--gerrit-project` | No² | `""` | Gerrit project name |
| `GERRIT_HTTP_BASE_PATH` | `GERRIT_HTTP_BASE_PATH` | N/A | No | `""` | HTTP base path for Gerrit REST API |
| `GERRIT_HTTP_USER` | `GERRIT_HTTP_USER` | N/A | No | `""` | Gerrit HTTP user for REST authentication |
| `GERRIT_HTTP_PASSWORD` | `GERRIT_HTTP_PASSWORD` | N/A | No | `""` | Gerrit HTTP password/token for REST authentication |
| N/A | `G2G_VERBOSE` | `--verbose`, `-v` | No | `"false"` | Enable verbose debug logging |

**Notes:**

1. Auto-derived when `G2G_ENABLE_DERIVATION=true` (default: false in GitHub Actions, true in CLI)
2. Optional if `.gitreview` file exists in repository

### Internal Environment Variables

The following environment variables control internal behavior but are not action inputs:

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `G2G_LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) | `"INFO"` |
| `G2G_ENABLE_DERIVATION` | Enable auto-derivation of Gerrit parameters | `"false"` (GitHub Actions), `"true"` (CLI) |
| `G2G_CONFIG_PATH` | Path to organization configuration file | `~/.config/github2gerrit/config.ini` |
| `G2G_AUTO_SAVE_CONFIG` | Auto-save derived parameters to config | `"false"` (GitHub Actions), `"true"` (CLI) |
| `G2G_TARGET_URL` | Internal flag for direct URL mode | Set automatically |
| `G2G_TMP_BRANCH` | Temporary branch name for single commits | `"tmp_branch"` |
| `G2G_TOPIC_PREFIX` | Prefix for Gerrit topic names | `"GH"` |
| `G2G_SKIP_GERRIT_COMMENTS` | Skip posting back-reference comments in Gerrit | `"false"` |
| `G2G_DRYRUN_DISABLE_NETWORK` | Disable network calls in dry-run mode | `"false"` |
| `SYNC_ALL_OPEN_PRS` | Process all open PRs (set automatically) | Set automatically |
| `GERRIT_BRANCH` | Override target branch for Gerrit | Uses `GITHUB_BASE_REF` |
| `GITHUB_TOKEN` | GitHub API token | Provided by GitHub Actions |
| `GITHUB_*` context | GitHub Actions context variables | Provided by GitHub Actions |

## Outputs

The action provides the following outputs for use in later workflow steps:

| Output Name | Description | Environment Variable |
|-------------|-------------|---------------------|
| `gerrit_change_request_url` | Gerrit change URL(s) (newline-separated) | `GERRIT_CHANGE_REQUEST_URL` |
| `gerrit_change_request_num` | Gerrit change number(s) (newline-separated) | `GERRIT_CHANGE_REQUEST_NUM` |
| `gerrit_commit_sha` | Patch set commit SHA(s) (newline-separated) | `GERRIT_COMMIT_SHA` |

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
  - On `pull_request_target`, the workflow may close the PR after submission to
    match the shell action’s behavior.

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
