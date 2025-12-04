<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# Metadata Synchronization Example

This document shows what metadata looks like in both GitHub and Gerrit, and how it stays synchronized.

## Overview

GitHub2Gerrit maintains bidirectional metadata tracking between GitHub PRs and Gerrit changes. This metadata enables:

- Finding existing changes when PRs update
- Closing PRs when Gerrit changes merge/abandon
- Verifying consistency across systems
- Providing audit trails for automation workflows

---

## GitHub PR Comments

### 1. Mapping Comment (Updated in Place)

This comment creates once and **updates in place** on each PR update:

```markdown
<!-- github2gerrit:change-id-map v1 -->
PR: #29
Mode: squash
Topic: GH-sandbox-29
Change-Ids:
  I61a8381a1ae46414723fde5fa878f6aea9addad0
Digest: 36a9a6263d13
GitHub-Hash: e24c5d88ac357ccc

_Note: This metadata is also included in the Gerrit commit message for reconciliation._
<!-- end github2gerrit:change-id-map -->
```

**Fields:**

- `PR`: Pull request number
- `Mode`: `squash` (single commit) or `multi-commit` (2+ commits)
- `Topic`: Gerrit topic used to group related changes
- `Change-Ids`: List of Gerrit Change-IDs (one for squash, 2+ for multi-commit)
- `Digest`: SHA-256 hash (first 12 chars) of Change-ID list for verification
- `GitHub-Hash`: Stable hash of PR (server + repo + PR number) for duplicate detection

### 2. Reference Comments (New Each Time)

A **new comment** appears each time the PR processes:

**Day 1 (PR opened):**

```text
Change raised in Gerrit by GitHub2Gerrit: https://gerrit.linuxfoundation.org/infra/c/sandbox/+/73940
```

**Day 2 (Dependabot updates PR):**

```text
Change updated in Gerrit by GitHub2Gerrit: https://gerrit.linuxfoundation.org/infra/c/sandbox/+/73940
```

**Day 3 (Dependabot updates again):**

```text
Change updated in Gerrit by GitHub2Gerrit: https://gerrit.linuxfoundation.org/infra/c/sandbox/+/73940
```

**Result:** You get a history showing each update, while mapping metadata stays in one comment.

---

## Gerrit Commit Message

### Complete Example

Here's what the Gerrit commit message looks like with all metadata:

```text
Update dependencies from v1.0 to v2.0

This change updates the project dependencies to their latest versions.
The update includes security patches and performance improvements.

GitHub2Gerrit Metadata:
Mode: squash
Topic: GH-sandbox-29
Digest: 36a9a6263d13

Issue-ID: CIMAN-33
Signed-off-by: dependabot[bot] <support@github.com>
Signed-off-by: lfit.gh2gerrit <releng+lfit-gh2gerrit@linuxfoundation.org>
Change-Id: I61a8381a1ae46414723fde5fa878f6aea9addad0
GitHub-PR: https://github.com/lfit/sandbox/pull/29
GitHub-Hash: e24c5d88ac357ccc
```

### Structure Breakdown

```text
┌─────────────────────────────────────────┐
│ SUBJECT (First Line)                    │  ← PR Title
│ Update dependencies from v1.0 to v2.0   │
├─────────────────────────────────────────┤
│ BODY (Description)                      │  ← PR Body
│ This change updates the project...      │
│ The update includes security...         │
├─────────────────────────────────────────┤
│ GITHUB2GERRIT METADATA BLOCK            │  ← Reconciliation Info
│ GitHub2Gerrit Metadata:                 │
│ Mode: squash                            │
│ Topic: GH-sandbox-29                    │
│ Digest: 36a9a6263d13                    │
├─────────────────────────────────────────┤
│ TRAILERS (Key-Value Pairs)              │  ← Git Trailers
│ Issue-ID: CIMAN-33                      │
│ Signed-off-by: ...                      │
│ Change-Id: I61a8381...                  │
│ GitHub-PR: https://github.com/...       │
│ GitHub-Hash: e24c5d88ac357ccc           │
└─────────────────────────────────────────┘
```

### Multi-Commit Example

For multi-commit mode, each commit includes the full Change-ID list:

```text
Fix authentication bug

Resolves issue where users couldn't log in with OAuth.

GitHub2Gerrit Metadata:
Mode: multi-commit
Topic: GH-sandbox-30
Digest: def789abc012
Change-Ids: I1234567890abcdef, Ifedcba0987654321, I9876543210fedcba

Issue-ID: CIMAN-34
Signed-off-by: developer@example.com
Change-Id: I1234567890abcdef
GitHub-PR: https://github.com/lfit/sandbox/pull/30
GitHub-Hash: a1b2c3d4e5f6g7h8
```

---

## Synchronization Flow

### Day 1: PR Created (opened event)

**GitHub PR #29:**

- Comment 1 (mapping): Created with initial metadata ✅
- Comment 2 (reference): "Change raised in Gerrit..." ✅

**Gerrit Change 73940 (Patchset 1):**

```text
Update dependencies from v1.0 to v2.0

Initial update of dependencies.

GitHub2Gerrit Metadata:
Mode: squash
Topic: GH-sandbox-29
Digest: 36a9a6263d13

Change-Id: I61a8381a1ae46414723fde5fa878f6aea9addad0
GitHub-PR: https://github.com/lfit/sandbox/pull/29
GitHub-Hash: e24c5d88ac357ccc
```

### Day 2: Dependabot Updates PR (synchronize event)

**GitHub PR #29:**

- Comment 1 (mapping): **Updated in place** with new digest ✏️
- Comment 3 (reference): "Change **updated** in Gerrit..." ✅ (new comment)

**Gerrit Change 73940 (Patchset 2):**

```text
Update dependencies from v1.0 to v2.0

Updated to include more security patches.

GitHub2Gerrit Metadata:
Mode: squash
Topic: GH-sandbox-29
Digest: 36a9a6263d13

Change-Id: I61a8381a1ae46414723fde5fa878f6aea9addad0
GitHub-PR: https://github.com/lfit/sandbox/pull/29
GitHub-Hash: e24c5d88ac357ccc
```

**What Changed:**

- New patchset created (1 → 2)
- Description updated with new PR body
- **Metadata block preserved** (Mode, Topic, Digest stay same)
- **Trailers preserved** (Change-Id, GitHub-PR, GitHub-Hash stay same)

### Day 3: User Edits PR Title (edited event)

**GitHub PR #29:**

- Comment 1 (mapping): **Updated in place** ✏️
- Comment 4 (reference): "Change **synchronized** in Gerrit..." ✅ (new comment)

**Gerrit Change 73940 (Patchset 2 - metadata updated):**

```text
Update dependencies to latest versions with security fixes

Updated to include more security patches.

GitHub2Gerrit Metadata:
Mode: squash
Topic: GH-sandbox-29
Digest: 36a9a6263d13

Change-Id: I61a8381a1ae46414723fde5fa878f6aea9addad0
GitHub-PR: https://github.com/lfit/sandbox/pull/29
GitHub-Hash: e24c5d88ac357ccc
```

**What Changed:**

- Subject (title) updated via REST API
- **Metadata block preserved** during update
- **Trailers preserved** during update
- No new patchset (metadata edit)

---

## Benefits of Dual Storage

### 1. GitHub PR Comment

- **Human-readable** summary visible in PR
- **Historical record** via reference comments
- **Quick reference** for developers

### 2. Gerrit Commit Message

- **Persistent** - survives PR closure
- **Auditable** - part of git history
- **Reconciliation** - enables automated workflows
- **Offline access** - available via git log

### 3. Synchronization

- **Consistency** - metadata matches in both places
- **Reliability** - redundant sources of truth
- **Recovery** - can reconstruct from either side

---

## Use Cases

### Use Case 1: Finding Changes for PR Update

When Dependabot updates PR #29:

1. GitHub2Gerrit detects `synchronize` event
2. Queries Gerrit by topic: `GH-sandbox-29`
3. Finds existing change 73940
4. Reuses Change-ID: `I61a8381a1ae46414723fde5fa878f6aea9addad0`
5. Creates new patchset with updated code

### Use Case 2: Closing PR After Merge

When Gerrit change 73940 merges:

1. Gerrit syncs commit to GitHub mirror
2. GitHub2Gerrit detects `push` event
3. Extracts metadata from commit:
   - `GitHub-PR: https://github.com/lfit/sandbox/pull/29`
   - `Topic: GH-sandbox-29`
4. Finds and closes PR #29
5. Adds comment: "Merged in Gerrit"

### Use Case 3: Abandoned Change Handling

When Gerrit change 73940 abandons:

1. GitHub2Gerrit detects via reconciliation
2. Uses metadata to find PR #29
3. Based on `CLOSE_MERGED_PRS` setting:
   - `true`: Closes PR with "Abandoned" comment
   - `false`: Adds comment but keeps PR open

### Use Case 4: Digest Verification

After successful push:

1. GitHub2Gerrit queries Gerrit for topic changes
2. Extracts Change-IDs from results
3. Computes digest of Change-ID list
4. Compares with expected digest
5. Logs verification summary (match/mismatch)

---

## Metadata Fields Reference

| Field | Location | Purpose | Stable? |
|-------|----------|---------|---------|
| **PR Number** | Both | Identifies source PR | Yes (per PR) |
| **Mode** | Both | Submission strategy | Yes (per PR) |
| **Topic** | Both | Groups related changes | Yes (per PR) |
| **Change-Ids** | Both | Gerrit change identifiers | Yes (unless reconciliation changes) |
| **Digest** | Both | Verification hash | Changes if Change-IDs change |
| **GitHub-Hash** | Both | PR fingerprint | Yes (per PR) |
| **GitHub-PR URL** | Gerrit | Link back to source | Yes (per PR) |
| **Change-Id** | Gerrit | Individual change ID | Yes (per change) |
| **Issue-ID** | Gerrit | Jira/tracking ticket | Yes (if configured) |
| **Signed-off-by** | Gerrit | Author attestation | Yes (preserved from commits) |

---

## Troubleshooting

### Metadata Missing in Gerrit

**Symptom:** Commit message has trailers but no `GitHub2Gerrit Metadata:` block

**Cause:** Commit created before this feature implementation

**Solution:**

- For new patchsets, metadata adds automatically
- For existing changes, metadata sync during `edited` event preserves existing structure

### Metadata Out of Sync

**Symptom:** GitHub comment shows different Change-IDs than Gerrit commit

**Cause:** Manual edits to either side, or reconciliation issues

**Solution:**

1. Check Gerrit change history for manual edits
2. Check reconciliation logs for errors
3. Trigger `synchronize` event to resync
4. Use `workflow_dispatch` with `ALLOW_DUPLICATES=false` to force reconciliation

### Missing Digest

**Symptom:** `Digest` field not present in metadata

**Cause:** Reconciliation plan not available during commit preparation

**Solution:**

- Non-critical - digest is optional
- Appears if reconciliation runs properly
- Used for verification, not required for functionality

---

## Conclusion

By maintaining synchronized metadata in both GitHub and Gerrit, GitHub2Gerrit enables reliable bidirectional
automation workflows. The metadata provides all information needed for:

- ✅ Finding existing changes during PR updates
- ✅ Creating new patchsets instead of duplicate changes
- Closing PRs when changes merge/abandon
- ✅ Verifying consistency across systems
- ✅ Auditing automation workflows
- ✅ Recovering from failures

This dual-storage approach ensures robustness and enables the complete Dependabot → GitHub → Gerrit → GitHub automation lifecycle.
