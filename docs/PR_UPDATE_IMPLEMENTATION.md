<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# PR Update Implementation Summary

## Overview

This document summarizes the implementation of robust PR update handling in GitHub2Gerrit, specifically designed to
support automation tools like Dependabot that update pull requests in place.

## Problem Statement

In the past, when Dependabot (or other automation) updated a PR by rebasing or changing commits, GitHub2Gerrit would
sometimes:

- Create duplicate Gerrit changes instead of updating existing ones
- Fail to reuse Change-IDs after rebases
- Not sync PR metadata changes to Gerrit
- Provide unclear error messages when updates failed

This implementation addresses all these issues systematically.

## Implementation Phases

### Phase 1: Detection & Routing ✅

**Files Modified:**

- `src/github2gerrit/models.py` - Added `PROperationMode` enum
- `src/github2gerrit/cli.py` - Added operation mode detection and routing

**What we added:**

1. `PROperationMode` enum with values:
   - `CREATE` - New PR (opened event)
   - `UPDATE` - PR updated (synchronize event)
   - `EDIT` - PR metadata edited (edited event)
   - `REOPEN` - PR reopened
   - `CLOSE` - PR closed
   - `UNKNOWN` - Unknown or not applicable

2. `GitHubContext.get_operation_mode()` method to detect operation type from event

3. Operation mode detection in `_process()`:
   - Logs detected mode with emoji indicators
   - Stores mode in `G2G_OPERATION_MODE` environment variable
   - Skips duplicate check for UPDATE operations (expects existing change)

**Example log output:**

```text
🔍 Detected PR operation mode: update
📝 PR update (synchronize) event - will update existing Gerrit change
⏩ Skipping duplicate check for UPDATE operation (change expected to exist)
```

### Phase 2: Robust Recovery ✅

**Files Modified:**

- `src/github2gerrit/core.py` - Added change discovery methods

**What we added:**

1. `_find_existing_change_for_pr()` method with four strategies:
   - **Strategy 1**: Topic-based query (`GH-owner-repo-PR#`)
   - **Strategy 2**: GitHub-Hash trailer matching
   - **Strategy 3**: GitHub-PR trailer URL matching
   - **Strategy 4**: Mapping comment parsing from PR comments

2. `_enforce_existing_change_for_update()` method:
   - Calls `_find_existing_change_for_pr()`
   - Raises detailed error if no change found for UPDATE operation
   - Returns Change-IDs that system must reuse

3. Integration in `Orchestrator.execute()`:
   - Detects operation mode
   - For UPDATE/EDIT operations, enforces finding existing changes
   - Forces reuse of discovered Change-IDs (bypasses reconciliation)
   - Logs clear success/failure messages

**Example output:**

```text
🔍 Searching for existing Gerrit change(s) to update...
✅ Found 1 existing change(s) by topic: I61a8381a1ae46414723fde5fa878f6aea9addad0
✅ Will update existing change(s): I61a8381a1ae46414723fde5fa878f6aea9addad0
```

**Error handling:**

```text
❌ UPDATE operation requires existing Gerrit change, but none found.
PR #29 should have an existing change with topic 'GH-lfit-sandbox-29'.
This typically means:
1. GitHub2Gerrit did not process the PR earlier
2. Someone abandoned or deleted the Gerrit change
3. The topic was manually changed in Gerrit
Consider using 'opened' event type or check Gerrit for the change.
```

### Phase 3: Metadata Sync ✅

**Files Modified:**

- `src/github2gerrit/core.py` - Added metadata sync methods

**What we added:**

1. `_get_gerrit_change_details()` method:
   - Queries Gerrit REST API for change details
   - Returns subject, status, revision info

2. `_update_gerrit_change_metadata()` method:
   - Uses Gerrit REST API to update commit message
   - Requires `GERRIT_HTTP_USER` and `GERRIT_HTTP_PASSWORD`
   - Updates via `PUT /changes/{change-id}/message`

3. `_sync_gerrit_change_metadata()` method:
   - Compares PR title with Gerrit subject
   - Updates if different
   - Logs clear success/failure messages

4. Integration in `Orchestrator.execute()`:
   - Called after query results for UPDATE/EDIT operations
   - Updates all changes in the list

**Example output:**

```text
🔄 Syncing PR metadata to Gerrit change(s)...
📝 PR title differs from Gerrit subject, updating...
✅ Updated Gerrit change metadata
```

The sync mechanism preserves the GitHub2Gerrit metadata block and trailers from the initial commit.

### Phase 4: Verification ✅

**Files Modified:**

- `src/github2gerrit/core.py` - Added patchset verification

**What we added:**

1. `_verify_patchset_creation()` method:
   - Queries Gerrit after push to verify results
   - Checks patchset number (should be > 1 for updates)
   - Warns if patchset = 1 (may show new change instead of update)
   - Checks change status (warns if ABANDONED)
   - Stores verification results for later use

2. Integration in `Orchestrator.execute()`:
   - Called after querying results for UPDATE/EDIT operations
   - Provides clear verification summary

**Example output:**

```text
🔍 Verifying patchset creation...
✅ Verified UPDATE: Change 73940, patchset 2, status=NEW
✅ Verification complete: 1/1 changes verified
```

**Warning examples:**

```text
⚠️  Change 73940 has patchset 1 - may be new creation instead of update
⚠️  Change 73940 has ABANDONED status - update may not be visible
```

### Phase 5: Enhanced Reconciliation ✅

**Files Modified:**

- `src/github2gerrit/orchestrator/reconciliation.py` - Enhanced for rebased commits
- `src/github2gerrit/core.py` - Pass operation mode to reconciliation

**What we added:**

1. `is_update_operation` parameter to `perform_reconciliation()`:
   - Lowers similarity threshold for UPDATE operations
   - Default: 0.7, UPDATE: 0.55 (0.7 - 0.15)
   - Helps match rebased commits with changed metadata

2. Enhanced logging:
   - Shows whether UPDATE mode is active
   - Logs adjusted similarity threshold

**Example output:**

```text
UPDATE operation detected - using relaxed similarity threshold: 0.55
```

### Phase 6: Enhanced Comments & Error Messages ✅

**Files Modified:**

- `src/github2gerrit/core.py` - Enhanced PR comment generation
- `src/github2gerrit/cli.py` - Enhanced error handling

**What we added:**

1. Operation-aware PR comments:
   - "Change raised in Gerrit" (CREATE)
   - "Change updated in Gerrit" (UPDATE)
   - "Change synchronized in Gerrit" (EDIT)

2. Detailed error messages for UPDATE failures:
   - Detects "no existing change found" errors
   - Provides actionable guidance
   - Shows clear emoji indicators

**Example comment:**

```text
Change updated in Gerrit by GitHub2Gerrit: https://gerrit.linuxfoundation.org/infra/c/sandbox/+/73940
```

**Example error:**

```text
❌ UPDATE FAILED: Cannot update non-existent Gerrit change
💡 PR #29 had no earlier processing by GitHub2Gerrit.
   To create a new change, trigger the 'opened' workflow action.
```

### Phase 7: G2G Metadata in Gerrit Commits ✅

**Files Modified:**

- `src/github2gerrit/core.py` - Enhanced metadata block generation
- `tests/test_pr_update_detection.py` - New tests for metadata preservation

**What we added:**

1. `_build_g2g_metadata_block()` method:
   - Generates structured metadata block for commit messages
   - Includes Mode, Topic, Digest, and Change-IDs list
   - Used for reconciliation when changes merge/abandon

2. Enhanced `_build_commit_message_with_trailers()`:
   - New parameters: `include_g2g_metadata`, `g2g_mode`, `g2g_topic`, `g2g_change_ids`
   - Inserts metadata block before trailers
   - Maintains proper ordering: body → metadata → trailers

3. Updated commit preparation methods:
   - `_prepare_squashed_commit()` includes metadata in squash commits
   - `_prepare_single_commits()` includes metadata in each commit
   - All commits now carry reconciliation metadata

4. Enhanced `_update_gerrit_change_metadata()`:
   - Preserves existing G2G metadata block when updating
   - Preserves existing trailers (Change-Id, GitHub-PR, etc.)
   - Updates title/description, keeps metadata intact

**Example commit message structure:**

```text
Update dependencies from v1.0 to v2.0

This change updates the project dependencies to their latest versions.

GitHub2Gerrit Metadata:
Mode: squash
Topic: GH-sandbox-29
Digest: 36a9a6263d13

Issue-ID: CIMAN-33
Signed-off-by: dependabot[bot] <support@github.com>
Change-Id: I61a8381a1ae46414723fde5fa878f6aea9addad0
GitHub-PR: https://github.com/lfit/sandbox/pull/29
GitHub-Hash: e24c5d88ac357ccc
```

**Benefits:**

- Metadata visible in both GitHub (PR comment) and Gerrit (commit message)
- Reconciliation when changes merge or abandon
- Digest helps verify Change-ID mapping consistency
- Topic and Mode provide context for automation workflows
- All information needed for lifecycle management is in Gerrit

## Testing

**Files Created:**

- `tests/test_pr_update_detection.py` - Comprehensive test suite

**Test Coverage:**

1. **Operation Mode Detection Tests** (8 tests)
   - All event types detected properly
   - Test unknown events return UNKNOWN
   - Test non-PR events return UNKNOWN

2. **Change Discovery Tests** (2 tests)
   - Verify topic-based discovery
   - Verify empty result when not found

3. **Enforcement Tests** (2 tests)
   - Verify error raised when no change found
   - Verify Change-IDs returned when found

4. **Verification Tests** (2 tests)
   - Verify success logging for patchset > 1
   - Verify warning for patchset = 1

5. **Metadata Sync Tests** (2 tests)
   - Verify update when titles differ
   - Verify skip when titles match

6. **G2G Metadata Block Tests** (3 tests)
   - Verify metadata included in squash commits
   - Verify metadata included in multi-commits
   - Verify metadata preserved during sync

**Total: 19 new test cases** (16 from Phases 1-6, 3 new for G2G metadata)

## Configuration

No new configuration required! The implementation uses existing inputs:

- `GERRIT_HTTP_USER` - Used for metadata updates (optional)
- `GERRIT_HTTP_PASSWORD` - Used for metadata updates (optional)
- `PRESERVE_GITHUB_PRS` - Existing behavior preserved

## Workflow Triggers

The existing workflow triggers already support all necessary events:

```yaml
on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize, closed]
```

- `opened` → CREATE mode
- `synchronize` → UPDATE mode (Dependabot rebases/updates)
- `edited` → EDIT mode (title/description changes)
- `reopened` → REOPEN mode
- `closed` → CLOSE mode

## Architecture Improvements

### Before

```text
PR synchronize event
  ↓
Process same as opened
  ↓
Reconciliation (may fail)
  ↓
Hope system finds Change-ID
  ↓
Push (may create duplicate)
```

### After

```text
PR synchronize event
  ↓
Detect UPDATE mode
  ↓
Find existing change (ENFORCED)
  ↓
Force Change-ID reuse
  ↓
Push with existing Change-ID
  ↓
Verify patchset created
  ↓
Sync metadata if needed
```

## Error Recovery

The implementation provides four fallback strategies:

1. **Topic query fails** → Try GitHub-Hash trailer matching
2. **GitHub-Hash fails** → Try GitHub-PR URL matching
3. **URL matching fails** → Try mapping comment parsing
4. **All strategies fail** → Clear error with actionable guidance

## Performance Impact

Minimal performance impact:

- 1-2 extra Gerrit REST queries for UPDATE operations
- Queries are parallelizable and fast (< 1 second typically)
- No impact on CREATE operations
- Verification is optional and non-blocking

## Backward Compatibility

✅ **Fully backward compatible:**

- CREATE operations work the same as before
- No new required inputs
- No breaking changes to existing workflows
- Enhanced behavior is opt-in via event type

## Known Limitations

1. **Metadata sync requires REST credentials**
   - If `GERRIT_HTTP_USER`/`GERRIT_HTTP_PASSWORD` not set, title/description sync skips
   - System logs this as a warning, not an error
   - G2G metadata is still included in initial commits via git push

2. **2+ changes per PR**
   - If PR has 2+ commits (multi-commit mode), all get updated
   - Verification checks all changes but warns, doesn't fail

3. **Manual Gerrit changes**
   - If Change-ID is manually edited in Gerrit, UPDATE may fail
   - Error message guides user to check Gerrit
   - System preserves manual edits to G2G metadata block in Gerrit during updates

## Future Enhancements

Potential improvements for future versions:

1. **File change validation**
   - Compare files changed in PR vs. Gerrit patchset
   - Warn if mismatch detected

2. **Reviewer sync**
   - Update reviewers in Gerrit when inputs change
   - Add/remove reviewers via REST API

3. **Digest verification strictness**
   - Make digest verification mandatory for UPDATE operations
   - Fail if digest mismatch detected

4. **Dry-run for updates**
   - Extend dry-run mode to simulate UPDATE operations
   - Show what would get updated without making changes

## Success Metrics

The implementation addresses all gaps identified in the analysis:

✅ Explicit UPDATE path detection

- ✅ Change-ID recovery with four strategies
✅ Gerrit change metadata synchronization
✅ Patchset verification and logging
✅ Enhanced reconciliation for rebased commits
✅ Clear error messages and guidance
✅ Comprehensive test coverage
✅ Backward compatibility maintained
✅ G2G metadata in Gerrit commits for reconciliation
✅ Metadata synchronized between GitHub and Gerrit

## Deployment

No special deployment steps required:

1. Merge implementation to main branch
2. Tag new release version
3. Update workflows to use new version

- ✅ Existing workflows gain improvements automatically

## Example Scenarios

### Scenario 1: Dependabot Updates Dependencies

```text
Day 1: Dependabot creates PR #29
  Event: opened
  Mode: CREATE
  Result: Gerrit change 73940 created

Day 2: Dependabot rebases PR #29
  Event: synchronize
  Mode: UPDATE
  Result: Change 73940 updated, patchset 2 created

Day 3: Dependabot updates dependencies in PR #29
  Event: synchronize
  Mode: UPDATE
  Result: Change 73940 updated, patchset 3 created

Day 4: Human edits PR title
  Event: edited
  Mode: EDIT
  Result: Change 73940 metadata synced (title updated)

Day 5: Change merged in Gerrit
  Event: push (Gerrit → GitHub sync)
  Result: PR #29 auto-closed
```

### Scenario 2: Manual PR Creation

```text
Human creates PR #30
  Event: opened
  Mode: CREATE
  Result: Gerrit change 73941 created

Human force-pushes to PR #30
  Event: synchronize
  Mode: UPDATE
  Result: Change 73941 updated, patchset 2 created
```

### Scenario 3: Error Recovery

```text
New PR #31 created
  Event: synchronize (incorrect trigger)
  Mode: UPDATE
  Result: ERROR - No existing change found
  Error Message: "PR #31 had no earlier processing. Use 'opened' event."
```

## Documentation Updates

Updated documentation in:

- `README.md` - Added "PR Update Handling" section with examples
- `PR_UPDATE_IMPLEMENTATION.md` - This comprehensive summary
- Inline code comments - Enhanced throughout implementation

## Conclusion

This implementation provides robust, production-ready support for PR updates from automation tools like Dependabot. It
ensures that PR updates create new patchsets in existing Gerrit changes rather than duplicate changes, synchronizes
metadata properly, and provides clear feedback at every step.

**Key Innovation:** By embedding GitHub2Gerrit metadata (Mode, Topic, Digest) directly in Gerrit commit messages, the
system can perform bidirectional reconciliation. When a Gerrit change gets merged or abandoned, the metadata in the
commit helps identify and close the corresponding GitHub PR, completing the automation lifecycle.

The implementation is fully backward compatible, well-tested, and ready for deployment.
