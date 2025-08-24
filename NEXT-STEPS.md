<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2025 The Linux Foundation -->

# NEXT STEPS: Reliable Gerrit Change Updates From GitHub PR Reruns

This document tracks the remaining implementation tasks required to make
rerunning the github2gerrit CLI on an existing GitHub Pull Request reliably
update the correct *existing* Gerrit changes (patch sets) — for both squash
and multi‑commit modes — rather than creating duplicate changes.

Focus is on completing and hardening the phased design already started.

---

## 1. Current State (Implemented So Far)

Phases delivered:

1. Phase 0 – Structural groundwork (no behavior change).
2. Phase 1 – Deterministic metadata trailers injected in **all modes**:
   - `GitHub-PR: <full PR URL>`
   - `GitHub-Hash: <deterministic short hash>`
3. Phase 2 – Emission of a structured mapping PR comment:
   - `<!-- github2gerrit:change-id-map v1 -->` block with ordered Change-Ids.
4. Phase 3 (partial) – Basic reconciliation:
   - Parses *latest* mapping comment.
   - Reuses ordered Change-Ids (index-based) during preparation.

Idempotency safeguards:

- Metadata trailers appended only if missing.
- Reconciliation silently degrades (continues) on failure.

---

## 2. Gaps Blocking “Reliable Update” Goal

The current partial Phase 3 implementation still leaves several reliability
failure modes:

| Area | Current Behavior | Risk / Gap |
|------|------------------|------------|
| Change discovery | Only comment-based reuse | Missing Gerrit-side validation |
| Multi-commit mapping | Pure index pairing | Breaks on reordering, insertion, deletion |
| Topic usage | Set on push | Not queried for reconciliation |
| Trailer usage | Inserted | Not leveraged to short-circuit duplicate detection / mapping |
| Change existence check | None | Could reuse stale Change-Id not in Gerrit |
| Mapping comment mgmt | Always append | Noise + no update-in-place |
| Conflict detection | Not implemented | Silent Change-Id drift if subjects changed radically |
| Partial reuse | All-or-nothing index mapping | Should reuse what matches; create new for extras |
| Mode switching | Squash ↔ multi not handled | Could mismatch reuse logic |
| Error reporting | Silent skip on failures | Hard to debug reconciliation misses |

---

## 3. Target Architecture For Robust Reconciliation

### 3.1 Data Sources (Authoritative → Fallback)

1. Gerrit Query (topic + trailers)
2. Mapping Comment (latest block)
3. Local Commit Graph (current preparation run)
4. Subject / File similarity (last resort)

### 3.2 Proposed Matching Algorithm (Multi-Commit Mode)

1. Query Gerrit:
   - `topic:GH-<repo>-<pr>` (statuses: open, optionally merged if configured).
   - Retrieve: change number, current patch set message, files, Change-Id.
2. Build candidate set `G = {Change-Id -> (subject_normalized, file_sig)}`.
3. Enumerate new local commits (ordered) → build `L = [(idx, subject_norm, file_sig)]`.
4. Matching passes:
   - Pass A: Trailer direct match (if commit already has reused Change-Id).
   - Pass B: Subject exact normalized match (1:1).
   - Pass C: File signature match (same file set hash).
   - Pass D: Subject token Jaccard ≥ threshold (e.g., 0.7).
5. Any unmatched local commits → allocate NEW Change-Ids.
6. Any Gerrit changes unmatched:
   - If local commit count shrank → log as “orphaned (no local counterpart)”.
   - (Optional future) mark for abandon if policy flag set.

### 3.3 Squash Mode Strategy

- Expect exactly one Change-Id.
- If multiple Gerrit changes under topic: choose highest patch set for the
  *most similar* subject; reuse its Change-Id; warn for others.
- If none: proceed with existing single-ID generation.

### 3.4 Integrity / Safety Checks

| Check | Action |
|-------|--------|
| Same Change-Id mapped to >1 local commit | Abort with explicit error |
| Duplicate subjects mapping to different IDs | Warn; still reuse deterministically |
| Trailer hash mismatch (different PR) | Abort (prevent cross‑PR contamination) |
| Mode switch (squash ↔ multi) with existing multi changes | Warn; proceed with best-effort mapping |

---

## 4. Remaining Phases / Work Packages

### Phase 3 (Completion / Hardening)

1. Gerrit topic query (REST) integration.
2. Unified matching pipeline (multi-pass as above).
3. File signature function:
   - Normalize path → lowercase, strip trivial whitespace.
   - Hash sorted list (e.g., sha256 first 12 chars).
4. Add reverse trailer scan (for GitHub-PR / GitHub-Hash) on Gerrit side.
5. Mixed scenario tests (commit reorder, addition, deletion).
6. Introduce `REUSE_STRATEGY` flag:
   - `comment` | `topic` | `topic+comment` (default) | `none`.
7. Enhanced logging (structured summary table):
   - Reused / New / Orphaned / Conflicted.

### Phase 4 (Trailer-Aware Duplicate Detection)

1. Modify duplicate detection to:
   - First search for existing change carrying same `GitHub-Hash:`.
   - If found → treat as *update target*, skip heuristic duplicate failure.
2. Add unit tests verifying short-circuit.

### Phase 5 (PR Comment Management)

1. Locate previous mapping comment.
2. Replace (edit or delete + add) rather than always append.
3. Add `PERSIST_SINGLE_MAPPING_COMMENT` flag.
4. Include mapping digest (sha256 of ordered Change-Ids) for quick diff.

### Phase 6 (Advanced Structural Matching – Optional)

1. Use Damerau–Levenshtein between title tokens as fallback scoring.
2. Introduce partial similarity threshold lowering for pure doc / chore changes.
3. Configurable thresholds (`SIMILARITY_SUBJECT`, `SIMILARITY_FILES`).

### Phase 7 (Robust Failure Handling)

1. Graceful degradation path diagram in logs:
   - `topic_query → mapping_comment → index_fallback`.
2. Abort conditions with actionable guidance.
3. Retry envelope for Gerrit REST (exponential backoff + jitter).
4. Telemetry summary (optional): counts, timings.

---

## 5. Detailed Task Breakdown

| ID | Task | Type | Est |
|----|------|------|-----|
| T01 | Add Gerrit topic query util (JSON fetch + pagination) | Core | M |
| T02 | Extract commit enumeration into reusable provider (multi) | Refactor | S |
| T03 | Implement file signature + subject normalization reuse (shared) | Core | S |
| T04 | Implement multi-pass matcher (A–D) | Core | M |
| T05 | Integrate matcher into reconciliation pre-flight | Core | M |
| T06 | Add trailer hash (GitHub-Hash) validation vs PR number | Safety | S |
| T07 | Add config flags (`REUSE_STRATEGY`, thresholds) | Config | S |
| T08 | Update duplicate detection early trailer short-circuit | Core | S |
| T09 | Mapping comment replace-in-place | Enhancement | S |
| T10 | Unit tests (parser, matcher edges, reorder) | Tests | M |
| T11 | Integration tests (full rerun flows) | Tests | L |
| T12 | Negative tests (conflict, foreign PR hash) | Tests | M |
| T13 | Logging: summary table & structured JSON debug line | DX | S |
| T14 | Documentation updates (README + flow diagrams) | Docs | S |
| T15 | Coverage push for new modules (≥ 85% local) | Tests | M |

Est Legend: S=Small, M=Medium, L=Large.

---

## 6. Test Plan Expansion

### 6.1 Unit Tests

- Trailer builder returns deterministic lines.
- Mapping parser with:
  - Multiple blocks (latest wins).
  - Corrupt interior content (ignored).
  - Mixed whitespace / casing variations (`Change-Ids:` vs `change-ids:`).
- File signature normalizes path variants (`A/B/../C` vs `a/c` canonicalization).
- Matching logic:
  - Exact subject match reuses correct ID.
  - File signature match when subjects differ by version numbers.
  - Partial overlap (one commit added) → new ID only for new commit.
  - Deletion (one original Change-Id missing) → orphan classification.

### 6.2 Integration Tests (Ephemeral Git Repo)

Scenarios:

1. Initial multi-commit push (N=3) → mapping comment.
2. Rerun unchanged → no new Gerrit changes (simulate by checking reused IDs).
3. Rerun with middle commit modified (subject) → same Change-Id (patch set).
4. Rerun with commit order swap:
   - Expect subject/file-based mapping realignment (IDs follow logical commit intent).
5. Rerun after removing one commit:
   - Expect 2 reused, 1 orphan logged.
6. Rerun after adding new commit at end:
   - Expect prior IDs reused + 1 new.
7. Squash initial → switch to multi-commit:
   - Only previously squashed Change-Id reused by first commit; others new (warn).
8. Foreign PR contamination attempt:
   - Insert mapping comment from PR X into PR Y → fail with safety hash mismatch.

### 6.3 Negative / Safety Tests

- Duplicate Change-Id in two local commits → abort.
- Gerrit topic query returns change with mismatched PR hash → excluded.
- Network failure on Gerrit query:
  - Logs warning; falls back to comment mapping.
- All strategies fail → proceed with fresh IDs (explicit log category).

### 6.4 Performance / Scale (Optional)

- 50–100 commits mapping performance (assert under time budget).
- Matcher early exits when all commits matched in Pass A (fast path).

---

## 7. Logging & Observability Enhancements

Add a single structured DEBUG line after reconciliation:

```json
RECONCILE_SUMMARY json={
  "total_local":3,
  "reused":2,
  "new":1,
  "orphaned":0,
  "strategy_order":"topic,comment,index",
  "passes":{"A":2,"B":0,"C":0,"D":0}
}
```

Human-readable INFO summary:

```text
Reconciliation: reused=2 new=1 orphaned=0 (strategy=topic+comment)
```

---

## 8. Configuration Additions (Planned)

| Variable | Purpose | Default |
|----------|---------|---------|
| REUSE_STRATEGY | `topic`, `comment`, `topic+comment`, `none` | `topic+comment` |
| SIMILARITY_SUBJECT | Subject token Jaccard threshold | `0.7` |
| SIMILARITY_FILES | File signature match requirement (boolean / ratio) | exact |
| ALLOW_ORPHAN_CHANGES | Keep unmatched Gerrit changes without warning | false |
| ABANDON_ORPHANS | Actively abandon unmatched changes | false (future) |
| PERSIST_SINGLE_MAPPING_COMMENT | Replace vs append | true |
| LOG_RECONCILE_JSON | Emit structured JSON line | true |

---

## 9. Risk Register & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Over-aggressive matching merges unrelated commits | Wrong Change-Id reuse | Multi-pass ordering (exact → loose), thresholds |
| Race: Gerrit changes added externally mid-run | Partial mismatch | Re-query before push (optional short second pass) |
| Missing topic (legacy/manual pushes) | No discovery | Fallback to mapping comment only |
| Comment editing disabled / API failure | No reuse | Topic path remains primary |
| Hash collision (very low risk) | Misassociation | Use 16 hex (adequate); could extend to 24 if needed |

---

## 10. Definition of Done (Core Objective)

A rerun of the CLI on a modified PR must:

1. Reuse existing Change-Ids where logical continuity exists.
2. Create only the minimum *new* Change-Ids needed for genuinely new commits.
3. Never reuse a Change-Id that belongs to a different PR (enforced by trailer).
4. Produce a clean, updated mapping comment (single authoritative block).
5. Provide explicit summary logs (reused/new/orphaned).
6. Pass integration tests covering reorder, add, remove, squash ↔ multi.
7. Maintain or improve lint + test coverage thresholds.

---

## 11. Immediate Next Implementation Sequence

1. T01 + T03 (topic query + file signature).
2. T04 (multi-pass matcher).
3. T05 integrate into reconciliation pre-flight (replace current index reuse).
4. T08 trailer short-circuit in duplicate detection.
5. T09 mapping comment replace.
6. T10–T12 test expansion (unit + integration).
7. T13 logging improvements.
8. T07 configuration flags + docs update.

---

## 12. Quick Reference: Matching Pass Semantics

| Pass | Name | Criteria | Exclusivity |
|------|------|----------|-------------|
| A | Trailer / explicit Change-Id present | Direct match | Hard lock |
| B | Exact normalized subject | 1:1 only | Consumes both sides |
| C | File signature match | Same hashed file set | Consumes if unique |
| D | Subject similarity (Jaccard ≥ threshold) | Fallback | Consumes if uncontested |

Remaining unmatched locals → NEW Change-Id(s).

---

## 13. Suggested Additional Helper Modules (Future Refactor)

| Module | Responsibility |
|--------|---------------|
| `reconcile_matcher.py` | Implements passes A–D |
| `gerrit_query.py` | Topic query + pagination + safe parsing |
| `mapping_comment.py` | Serialize / deserialize mapping block |
| `trailers.py` | Common trailer constants + parsing |

This modularization will reduce complexity hotspots in `core.py`.

---

## 14. Coverage Strategy

Target incremental coverage lift:

- New modules (matcher, parser, query) ≥ 90%.
- Existing orchestrator reconciliation path: add focused tests driving
  branches (success, partial reuse, empty, mismatch).
- Use synthetic commit sets (parametrized) for permutation coverage.

---

## 15. Actionable Summary (TL;DR)

Implement topic-based Gerrit discovery + robust multi-pass matching +
configurable strategy + authoritative mapping comment replacement. Back
this with a comprehensive test matrix (reorder/add/remove/squash ↔ multi)
and log a clear reconciliation summary. This completes the core promise:
repeatable, deterministic Gerrit updates per GitHub PR evolution.

---

Prepared as the forward engineering plan to close the reconciliation
feature set with clarity, safety, and testable determinism.
