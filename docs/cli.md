<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# CLI Reference

The `github2gerrit` Python CLI is the engine behind the composite action. The
primary deployment method is the GitHub Action, but you can run the tool
directly for local development, advanced usage, and debugging.

## Installation

The package publishes to PyPI as `github2gerrit`:

```bash
# Install as a persistent tool
uv tool install github2gerrit

# Run without installing (recommended for one-off or CI/CD usage)
uvx github2gerrit --help

# Install from PyPI
pip install github2gerrit

# Run from a specific branch or fork
uvx --from git+https://github.com/lfreleng-actions/github2gerrit-action@main github2gerrit --help
```

For development with a local checkout:

```bash
uv run github2gerrit --help
```

## Invocation Modes

The CLI accepts a single optional positional argument, `TARGET_URL`, and
selects its mode of operation from the argument type:

| TARGET_URL                                  | Behavior                                                    |
| ------------------------------------------- | ----------------------------------------------------------- |
| GitHub PR URL (`.../owner/repo/pull/123`)   | Create a Gerrit change from the pull request                |
| Gerrit change URL                           | Find and close the source GitHub pull request               |
| GitHub repository URL (`.../owner/repo`)    | Process all open pull requests in the repository            |
| No argument                                 | Environment-driven CI/CD mode (reads `GITHUB_*` variables)  |

Examples:

```bash
# Process a specific pull request
github2gerrit https://github.com/owner/repo/pull/123

# Process all open pull requests in a repository
github2gerrit https://github.com/owner/repo

# Close the GitHub PR that produced a Gerrit change
github2gerrit https://gerrit.example.org/c/project/+/12345

# CI/CD mode: read context from environment variables
github2gerrit
```

## Options

Every option maps to an environment variable, so the action and the CLI share
one configuration model. Command-line flags take precedence over environment
variables. Boolean environment variables accept `true`/`false` strings (as
set by GitHub Actions) and undergo proper boolean parsing.

### Core behavior

<!-- markdownlint-disable MD013 -->

| Flag(s)                                          | Env var                 | Default | Description                                                                    |
| ------------------------------------------------ | ----------------------- | ------- | ------------------------------------------------------------------------------ |
| `--submit-single-commits`                        | `SUBMIT_SINGLE_COMMITS` | `false` | Submit one commit at a time to the Gerrit repository                           |
| `--use-pr-as-commit`                             | `USE_PR_AS_COMMIT`      | `false` | Use PR title and body as the commit message                                    |
| `--dry-run`                                      | `DRY_RUN`               | `false` | Check settings and PR metadata; do not write to Gerrit                         |
| `--normalise-commit/--no-normalise-commit`       | `NORMALISE_COMMIT`      | `true`  | Normalize commit messages to conventional commit format                        |
| `--issue-id TEXT`                                | `ISSUE_ID`              | `""`    | Issue ID to include in the commit message (e.g., `Issue-ID: ABC-123`)          |
| `--issue-id-lookup-json TEXT`                    | `ISSUE_ID_LOOKUP_JSON`  | `""`    | JSON array mapping GitHub actors to Issue IDs for automatic lookup             |
| `--commit-rules TEXT`                            | `COMMIT_RULES_JSON`     | `""`    | JSON object defining commit message rules with per-project/per-actor overrides |
| `--preserve-github-prs/--no-preserve-github-prs` | `PRESERVE_GITHUB_PRS`   | `true`  | Do not close GitHub PRs after pushing to Gerrit                                |
| `--close-merged-prs/--no-close-merged-prs`       | `CLOSE_MERGED_PRS`      | `true`  | Close GitHub PRs when corresponding Gerrit changes merge                       |
| `--force`                                        | `FORCE`                 | `false` | Force PR closure regardless of Gerrit change status (abandoned, etc.)          |
| `--fetch-depth INTEGER`                          | `FETCH_DEPTH`           | `10`    | Git history depth: PR fetch, push checkout, reconciliation window              |
| `--reviewers-email TEXT`                         | `REVIEWERS_EMAIL`       | `""`    | Email address(es) of reviewers (comma-separated)                               |
| `--organization TEXT`                            | `ORGANIZATION`          | unset   | Organization (defaults to `GITHUB_REPOSITORY_OWNER` when unset)                |
| `--github-actor TEXT`                            | `GITHUB_ACTOR`          | `""`    | GitHub actor (username) who triggered the workflow                             |
| `--automation-only/--no-automation-only`         | `AUTOMATION_ONLY`       | `true`  | Accept pull requests from known automation tools                               |
| `--ci-testing/--no-ci-testing`                   | `CI_TESTING`            | `false` | CI testing mode: override `.gitreview`, handle unrelated repositories          |
| `--allow-ghe-urls/--no-allow-ghe-urls`           | `ALLOW_GHE_URLS`        | `false` | Allow non-github.com GitHub Enterprise URLs in direct URL mode                 |

<!-- markdownlint-enable MD013 -->

### Gerrit connection

<!-- markdownlint-disable MD013 -->

| Flag(s)                              | Env var                     | Default | Description                                                  |
| ------------------------------------ | --------------------------- | ------- | ------------------------------------------------------------ |
| `--gerrit-server TEXT`               | `GERRIT_SERVER`             | `""`    | Gerrit server hostname (optional; `.gitreview` preferred)    |
| `--gerrit-server-port INTEGER`       | `GERRIT_SERVER_PORT`        | `29418` | Gerrit SSH port                                              |
| `--gerrit-project TEXT`              | `GERRIT_PROJECT`            | `""`    | Gerrit project (optional; `.gitreview` preferred)            |
| `--gerrit-ssh-user-g2g TEXT`         | `GERRIT_SSH_USER_G2G`       | `""`    | Gerrit SSH username (e.g., automation bot account)           |
| `--gerrit-ssh-user-g2g-email TEXT`   | `GERRIT_SSH_USER_G2G_EMAIL` | `""`    | Email address for the Gerrit SSH user                        |
| `--gerrit-ssh-privkey-g2g TEXT`      | `GERRIT_SSH_PRIVKEY_G2G`    | `""`    | SSH private key content used to authenticate to Gerrit       |
| `--gerrit-known-hosts TEXT`          | `GERRIT_KNOWN_HOSTS`        | `""`    | Known hosts entries for Gerrit SSH (single or multi-line)    |

<!-- markdownlint-enable MD013 -->

### Duplicates and reconciliation

<!-- markdownlint-disable MD013 -->

| Flag(s)                                                                | Env var                          | Default         | Description                                                                                                                               |
| ---------------------------------------------------------------------- | -------------------------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `--allow-duplicates/--no-allow-duplicates`                             | `ALLOW_DUPLICATES`               | `true`          | Allow submitting duplicate changes without error                                                                                          |
| `--duplicate-types TEXT`                                               | `DUPLICATE_TYPES`                | `open`          | Gerrit change states evaluated for duplicates (comma-separated, e.g., `open,merged,abandoned`)                                            |
| `--allow-orphan-changes/--no-allow-orphan-changes`                     | `ALLOW_ORPHAN_CHANGES`           | `false`         | Keep unmatched Gerrit changes without warning                                                                                             |
| `--create-missing/--no-create-missing`                                 | `CREATE_MISSING`                 | `false`         | Create a Gerrit change when an UPDATE cannot find an existing one; also triggered by an `@github2gerrit create missing change` PR comment |
| `--reuse-strategy TEXT`                                                | `REUSE_STRATEGY`                 | `topic+comment` | Change-ID reuse strategy: `topic`, `comment`, `topic+comment`, or `none`                                                                  |
| `--similarity-subject FLOAT`                                           | `SIMILARITY_SUBJECT`             | `0.7`           | Subject token Jaccard similarity threshold (0.0-1.0)                                                                                      |
| `--similarity-files/--no-similarity-files`                             | `SIMILARITY_FILES`               | `false`         | Require exact file signature match for reconciliation                                                                                     |
| `--similarity-update-factor FLOAT`                                     | `SIMILARITY_UPDATE_FACTOR`       | `0.75`          | Multiplier applied to the similarity threshold on UPDATE operations                                                                       |
| `--log-reconcile-json/--no-log-reconcile-json`                         | `LOG_RECONCILE_JSON`             | `true`          | Emit structured JSON reconciliation summary                                                                                               |
| `--persist-single-mapping-comment/--no-persist-single-mapping-comment` | `PERSIST_SINGLE_MAPPING_COMMENT` | `true`          | Replace the existing mapping comment instead of appending                                                                                 |

<!-- markdownlint-enable MD013 -->

### Netrc credentials (Gerrit HTTP)

<!-- markdownlint-disable MD013 -->

| Flag(s)                             | Env var              | Default | Description                                                                               |
| ----------------------------------- | -------------------- | ------- | ----------------------------------------------------------------------------------------- |
| `--no-netrc`                        | `G2G_NO_NETRC`       | `false` | Disable `.netrc` credential lookup for Gerrit HTTP authentication                         |
| `--netrc-file PATH`                 | `G2G_NETRC_FILE`     | unset   | Explicit path to a `.netrc` file for Gerrit HTTP credentials (must exist and be readable) |
| `--netrc-optional/--netrc-required` | `G2G_NETRC_OPTIONAL` | `true`  | Whether missing `.netrc` files cause failure (default: optional)                          |

<!-- markdownlint-enable MD013 -->

### Output and debugging

<!-- markdownlint-disable MD013 -->

| Flag(s)                    | Env var             | Default | Description                                                    |
| -------------------------- | ------------------- | ------- | -------------------------------------------------------------- |
| `--verbose`, `-v`          | `G2G_VERBOSE`       | `false` | Verbose output (enables DEBUG logging including Rich displays) |
| `--progress/--no-progress` | `G2G_SHOW_PROGRESS` | `true`  | Show real-time progress updates with Rich formatting           |
| `--version`                | (none)              | `false` | Show version and exit                                          |

<!-- markdownlint-enable MD013 -->

## Environment-Only Variables

Some settings have no CLI flag and use environment variables.

When running the tool as a GitHub Action, how you set one depends on the
integration:

- **Reusable workflow** — set the `G2G_`-prefixed settings below as repository
  or organisation **variables**; the workflow forwards them. Environment
  variables do not cross a `workflow_call` boundary, so `env:` in the caller
  is silently ignored.
- **Composite action** — set any of them with `env:` on the job or step.

The workflow forwards only `G2G_`-prefixed names from repository variables.
The unprefixed reconciliation settings could collide with a variable a project
already keeps for something else, so they remain composite-action only.

<!-- markdownlint-disable MD013 -->

| Variable                      | Default                                     | Description                                                                      |
| ----------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------- |
| `G2G_CONFIG_PATH`             | `~/.config/github2gerrit/configuration.txt` | Path to the per-organization configuration file                                  |
| `G2G_LOG_LEVEL`               | `WARNING`                                   | Logging level; set `DEBUG` for verbose output                                    |
| `G2G_TOPIC_PREFIX`            | `GH`                                        | Prefix used when generating Gerrit topics                                        |
| `G2G_SKIP_GERRIT_COMMENTS`    | `false`                                     | Skip posting back-reference comments on Gerrit changes                           |
| `G2G_TRUSTED_ASSOCIATIONS`    | `OWNER,MEMBER,COLLABORATOR`                 | Author associations trusted to issue `@github2gerrit` comment directives         |
| `G2G_ENABLE_DERIVATION`       | `true`                                      | Enable automatic derivation of Gerrit parameters from organization defaults      |
| `G2G_AUTO_SAVE_CONFIG`        | context-dependent                           | Save derived parameters back to the configuration file                           |
| `G2G_RESPECT_USER_SSH`        | `false` (`true` in direct URL mode)         | Use the local user's SSH configuration and keys instead of provided key material |
| `G2G_USE_SSH_AGENT`           | `true`                                      | Use SSH agent authentication; set `false` to force file-based SSH keys           |
| `G2G_NO_GERRIT`               | `false`                                     | Run the full pipeline without contacting Gerrit (forces dry-run behavior)        |
| `G2G_DISABLED`                | `false`                                     | Exit immediately with success without processing anything                        |
| `CLEANUP_ABANDONED`           | `true`                                      | Close GitHub PRs whose Gerrit changes have status abandoned                      |
| `CLEANUP_GERRIT`              | `true`                                      | Abandon Gerrit changes for closed GitHub PRs                                     |
| `PR_NUMBER`                   | unset                                       | Pull request number to process in CI/CD mode (`0` means bulk mode)               |
| `SYNC_ALL_OPEN_PRS`           | `false`                                     | Process all open PRs in the repository (bulk mode)                               |
| `GERRIT_HTTP_USER`            | unset                                       | Username for the Gerrit REST API (when required)                                 |
| `GERRIT_HTTP_PASSWORD`        | unset                                       | HTTP password/token for the Gerrit REST API                                      |
| `GERRIT_HTTP_BASE_PATH`       | unset                                       | Gerrit REST base path for non-standard deployments (e.g., `/r`)                  |
| `GITHUB_TOKEN`                | unset                                       | GitHub API token; required for PR queries, comments, and closing PRs             |
| `GH_TOKEN`                    | unset                                       | Fallback source for `GITHUB_TOKEN` when the latter is unset                      |
| `GERRIT_CHANGE_URL`           | unset                                       | Gerrit change URL for Gerrit-event dispatch mode (close the source PR)           |
| `GERRIT_EVENT_TYPE`           | unset                                       | Gerrit event type for dispatch mode (`change-merged` or `change-abandoned`)      |
| `GERRIT_BRANCH`               | unset                                       | Gerrit branch for dispatch mode change queries                                   |
| `G2G_DRYRUN_DISABLE_NETWORK`  | `false`                                     | Skip DNS/network checks in dry-run; set automatically by `G2G_NO_GERRIT`         |
| `G2G_ANON_SUPERSEDE_FALLBACK` | `true`                                      | Allow anonymous supersession queries when Gerrit credentials are unavailable     |
| `VERIFY_DIGEST_STRICT`        | `true`                                      | Fail (rather than warn) on reconciliation digest mismatch after push             |

<!-- markdownlint-enable MD013 -->

In CI/CD mode (no `TARGET_URL`), the tool also reads the standard GitHub
Actions context variables: `GITHUB_REPOSITORY`, `GITHUB_REPOSITORY_OWNER`,
`GITHUB_EVENT_NAME`, and related `GITHUB_*` values.

## Exit Codes

Exit codes come from `src/github2gerrit/error_codes.py`:

<!-- markdownlint-disable MD013 -->

| Code | Meaning                     | Common causes / resolution                                                                                 |
| ---- | --------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 0    | Success                     | Operation completed                                                                                        |
| 1    | General error               | Unexpected operational failure; check logs for details                                                     |
| 2    | Configuration error         | Missing/invalid parameters; verify required inputs and environment variables                               |
| 3    | Duplicate error             | Duplicate change detected when duplicates disallowed; check existing changes or adjust `--duplicate-types` |
| 4    | GitHub API error            | Permission or authentication issues; verify `GITHUB_TOKEN` permissions                                     |
| 5    | Gerrit connection error     | SSH/authentication failure; check keys, server configuration, and network                                  |
| 6    | Network error               | Connectivity issues; check internet connection and firewall settings                                       |
| 7    | Repository error            | Git repository access or operation failed; verify permissions and git config                               |
| 8    | PR state error              | Pull request in invalid state; ensure the PR is open and mergeable                                         |
| 9    | Validation error            | Input validation failed; check parameter values and formats                                                |
| 10   | Gerrit change already final | The Gerrit change is already merged or abandoned                                                           |

<!-- markdownlint-enable MD013 -->

## Debugging and Troubleshooting

Enable verbose logging to see git command execution, SSH connection attempts,
Gerrit API interactions, branch resolution, and Change-Id processing:

```bash
# CLI flag
github2gerrit --verbose https://github.com/owner/repo/pull/123

# Environment variables (same effect)
G2G_LOG_LEVEL=DEBUG github2gerrit https://github.com/owner/repo/pull/123
G2G_VERBOSE=true github2gerrit https://github.com/owner/repo/pull/123
```

When troubleshooting failures:

1. Check the exit code to identify the failure category (table above).
2. Read the error message; failures print a clear message prefixed with `❌`.
3. Review any details printed with the error for context.
4. Enable verbose logging for full execution traces.

Common issues:

- **Configuration validation errors**: messages starting with
  "Configuration validation failed:" name the missing inputs, such as
  `GERRIT_KNOWN_HOSTS` or `GERRIT_SSH_PRIVKEY_G2G`.
- **SSH permission denied**: ensure `GERRIT_SSH_PRIVKEY_G2G` and
  `GERRIT_KNOWN_HOSTS` have values. If you see "Permissions 0644 for
  'gerrit_key' are too open", the tool automatically tries SSH agent
  authentication; keep `G2G_USE_SSH_AGENT=true` (the default).
- **GitHub API failures (exit code 4)**: configure `GITHUB_TOKEN` with
  access to the target repository and grant `contents: read`,
  `pull-requests: write`, and `issues: write`. Cross-repository workflows
  need a token with access to the target repository.
- **Branch not found**: check that the target branch exists in both GitHub
  and Gerrit.
- **Account not found**: if you see "Account '<Email@Domain.com>' not
  found", ensure your Gerrit account email matches your git config email
  (case-sensitive).
- **Gerrit API errors**: verify Gerrit server connectivity and project
  permissions.

The tool displays configuration errors cleanly without Python tracebacks. If
you see a traceback in the output, report it as a bug.

## Advanced Usage

### Overriding .gitreview settings

When `CI_TESTING=true`, the tool ignores any `.gitreview` file in the
repository and uses environment variables instead. This helps with:

- Integration testing against different Gerrit servers
- Overriding repository settings when `.gitreview` points to the wrong server
- Development and debugging with custom Gerrit configurations

```bash
export CI_TESTING=true
export GERRIT_SERVER=gerrit.example.org
export GERRIT_PROJECT=sandbox
github2gerrit https://github.com/org/repo/pull/123
```

### SSH authentication methods

The tool supports two SSH authentication methods:

1. **SSH agent authentication (default)**: loads keys into memory rather
   than writing them to disk. This avoids the file permission issues common
   in CI environments, leaves no temporary files, and cleans up
   automatically when the process exits.
2. **File-based authentication (fallback)**: if SSH agent setup fails, the
   tool writes the key to a workspace-specific `.ssh-g2g/` directory,
   attempting to set 0600 permissions with fallback strategies for
   restrictive CI environments.

Set `G2G_USE_SSH_AGENT=false` to force file-based authentication.

When running in direct URL mode (outside GitHub Actions), the tool sets
`G2G_RESPECT_USER_SSH=true` by default, so your local SSH configuration and
keys apply. Set `G2G_RESPECT_USER_SSH=false` to disable this.

### Custom SSH configuration

You can supply explicit Gerrit connection values instead of relying on
`.gitreview` or SSH config discovery. This helps when:

- You need to override the port or host used by SSH
- Your Gerrit instance uses a non-standard HTTP base path (e.g., `/r`)

Provide `GERRIT_SERVER`, `GERRIT_SERVER_PORT`, `GERRIT_PROJECT`, and, when
the REST API requires them, `GERRIT_HTTP_BASE_PATH`, `GERRIT_HTTP_USER`, and
`GERRIT_HTTP_PASSWORD`. As an option, `.netrc` files supply Gerrit HTTP
credentials (see the netrc options above).

Note: when running as a GitHub Action, the action configures SSH internally
from the provided inputs and does not use the runner's SSH agent or
`~/.ssh/config`. Do not add external steps to install SSH keys; they may
conflict with the action.

## GitHub Enterprise Support

- Direct URL mode accepts enterprise GitHub hosts when explicitly enabled
  via `--allow-ghe-urls` or `ALLOW_GHE_URLS=true`. Default: off
  (github.com URLs).
- In GitHub Actions, the action works with GitHub Enterprise when the
  workflow runs in that enterprise environment with a valid `GITHUB_TOKEN`.
- For direct URL runs outside Actions, ensure `ORGANIZATION` and
  `GITHUB_REPOSITORY` reflect the target repository.
