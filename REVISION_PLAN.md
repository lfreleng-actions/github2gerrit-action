<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2025 The Linux Foundation -->

# github2gerrit Revision & Enhancement Plan

This document captures the current state, identified gaps, and a structured
roadmap to elevate the project's maturity, reliability, and bi‑directional
change traceability between GitHub and Gerrit.

---

## 1. Executive Overview

The project has a strong technical baseline:

- Strict typing (mypy / pyright strict modes).
- Cohesive helper modules for GitHub and Gerrit APIs with centralized
  retry/backoff.
- Extensive unit tests for reconciliation, SSH setup, duplicate detection,
  error diagnostics, and commit normalization.

Primary growth areas:

- The orchestration layer (`core.py`) is monolithic and couples diverse
  concerns (SSH, git, reconciliation, REST querying, mapping comments,
  error analysis).

- Bi-directional synchronization is partially implemented:
  - PR → Gerrit: trailers, topic naming, mapping comment.
  - Gerrit → PR: review backref comments (not idempotent, not parsed).

- Orphan (stale) Gerrit changes are now handled via configurable policy with REST
  side-effects (abandon/comment/ignore).

- GitHub Action shell logic lacks tests. (PARTLY ADDRESSED Phase 1)

- Reconciliation uses dual pathways (topic + comment or legacy fallback),
  producing ambiguity and maintenance overhead. (COMPLETED: legacy path removed)
- Bulk (multi PR) mode is sequential; no concurrency or progress feedback.

---

## 2. Current Strengths

- High typing rigor and lint coverage.
- Centralized external API call abstraction with metrics and retry policy.
- Reconciliation strategy supports multi‑pass matching:
  - Change‑Id trailer reuse
  - Subject exact
  - File signature fallback
  - Subject similarity (token ratio)
- Deterministic PR metadata trailers (GitHub-PR, GitHub-Hash).
- Structured mapping comment format with clear markers and digest verification.
- Rich SSH/Gerrit push error classification improving user diagnostics.
- Well isolated git fixture utilities enabling fast test cycles.
- Formal reconciliation planning with orphan policy enforcement via REST API.
- Idempotent backref comments preventing duplication across runs.

---

## 3. Key Weaknesses / Risks

| Area | Issue | Impact |
| ---- | ----- | ------ |
| Architecture | Monolithic orchestrator | Hard to extend securely |
| Traceability | No orphan handling / verification | Drift accumulation |
| Reconciliation | ~~Redundant legacy path~~ | ~~Increased complexity~~ (RESOLVED) |
| Action Layer | Untested shell steps | Hidden regressions |
| Backrefs | Not idempotent | Comment noise / duplication |
| Mapping | Two semi‑authoritative sources | Possible divergence |
| Performance | Sequential bulk processing | Slow large scale sync |
| Security | Topic unsanitized / no dep integrity checks | Hardening gap |
| Observability | Metrics not surfaced externally | Limited insight |
| Concurrency | No parallel PR handling | Latency under load |

---

## 4. Bi‑Directional Traceability: Present vs Desired

| Dimension | Present | Desired |
| --------- | ------- | ------- |
| PR → Gerrit link | Commit trailers + topic | Same + digest validation |
| Gerrit → PR link | Backref comment per patch | Idempotent + structured |
| Mapping authority | Mapping comment + inferred topic | Single canonical mapping |
| Drift detection | None post‑push | Verification phase with digest |
| Orphan lifecycle | Ignored | Policy: abandon / comment / ignore |
| **Change update path** | Re-push approach | Plan-driven differential actions |

---

## 5. Target State Principles

1. **Single Responsibility:** Each pipeline phase isolated and testable.
2. **Determinism:** Same inputs yield identical mapping/digest.
3. **Idempotence:** Repeated runs do not create redundant comments.
4. **Declarative Reconciliation:** Produce a plan before mutating git.
5. **Explicit Orphan Policy:** User-configurable and auditable.
6. **Safe Parallelism:** Bulk PR handling with bounded concurrency.
7. **Transparent Metrics:** Machine-readable summary emitted every run.
8. **Secure Defaults:** Sanitized topics, no silent key usage fallbacks.
9. **Pluggable Verification:** Hook for post‑push integrity checks.
10. **Backward Evolvability:** Legacy paths isolated, then retired.

---

## 6. Architectural Refactor (Proposed Package Layout)

```text
github2gerrit/orchestrator/
  pipeline.py            (phase coordinator / state machine)
  context.py             (immutable run + repo context objects)
  ssh.py                 (SSH agent / key material setup & cleanup)
  git_ops.py             (log extraction, squash, cherry-pick, trailers)
  reconciliation.py      (match strategies + plan builder)  # EXTRACTED (Phase 1)
  mapping.py             (serialize/parse mapping + digest)
  gerrit_push.py         (push + error analysis + reviewer handling)
  backref.py             (idempotent Gerrit review comments)
  verification.py        (post-push validation & drift reporting)
  orphan_policy.py       (abandon / comment / ignore strategies)
```

`pipeline.py` drives ordered phases:

1. Context & configuration resolution
2. Commit graph extraction
3. Reconciliation plan creation
4. Commit preparation (guided by plan)
5. Push & capture change metadata
6. Mapping + backref sync
7. Verification & orphan handling
8. Final PR status (comment / optional close)

---

## 7. Reconciliation Plan Model (Concept)

A concrete intermediate representation:

```text
ReconciliationPlan:
  reused: list[(local_index, change_id)]
  new: list[local_commit_sha]
  orphan: list[gerrit_change_id]
  mapping_order: ordered list[change_id]
  digest: sha256(first 12 hex)
```

Benefits:

This forms a single source of truth for future push + mapping emission.

- Enables deterministic digest validation post-push.
- Supports orphan policy actions (abandon / comment) cleanly.

---

## 8. Orphan Handling Policy

| Policy | Behavior |
| ------ | -------- |
| abandon | Issue REST abandon with reason referencing PR update |
| comment | Add Gerrit change comment “orphaned by PR update” |
| ignore | Silent (current behavior) |

Config variable: `ORPHAN_POLICY` (default: `comment`).

---

## 9. Verification Phase

Steps:

1. After push, re-query Gerrit by topic.
2. Rebuild ordered Change-Id list.
3. Recompute digest and compare with planned digest.
4. If mismatch:
   - Log structured warning and optionally mark run as unstable (non-zero
     exit code behind feature flag).
5. Emit JSON summary line:

   ```text
   RECONCILE_SUMMARY {
     "planned_digest": "...",
     "observed_digest": "...",
     "reused": N,
     "new": M,
     "orphan_handled": K,
     "policy": "abandon"
   }
   ```

---

## 10. Composite Action Test Coverage Additions

Test matrix (via local Action runner):

- `workflow_dispatch` with `PR_NUMBER=0` (bulk path) (COMPLETED)
- Invalid `PR_NUMBER` (alpha chars) → exit 2 (COMPLETED)
- Missing PR context on non-dispatch event → exit 2 (COMPLETED)
- ISSUE_ID JSON lookup success, fallback, and absence (PENDING)
- DRY_RUN with different PRs verifying outputs formatting (PENDING)
- Environment variable propagation of multi-line change URLs (PENDING)

---

## 11. Performance & Parallelism Enhancements

| Improvement | Detail |
| ----------- | ------ |
| Thread pool bulk mode | Configurable `BULK_PARALLELISM` (default 4) |
| Pre-plan Change-Ids | Avoids second pass re-preparation |
| Adaptive pagination | Set page size = min(50, expected_commits * 2) |
| Quick cutoff | Stop Gerrit queries once all known reused IDs resolved |
| Digest short-circuit | If plan digest unchanged from prior mapping, skip push (future) |

---

## 12. Security / Hardening Actions

| Item | Action |
| ---- | ------ |
| Topic sanitization | Regex restrict; truncate > 80 chars (PENDING) |
| Dependency integrity | Optional hash verification (PENDING) |
| Sensitive logging | Central mask helper (PENDING) |
| SSH key lifecycle | Explicit zeroization (PENDING) |
| **Mapping backref content** | Escape/verify dynamic tokens (PENDING) |

---

## 13. Metrics & Observability

Expose structured log lines (machine parsable):

- `API_METRICS { "github": {...}, "gerrit_rest": {...} }` (PENDING external
  surfacing)
- `RECONCILE_SUMMARY { ... }` (STABILIZED Phase 1, DIGEST ADDED Phase 2)
- `VERIFICATION_SUMMARY { ... }` (ADDED Phase 2.5: digest verification with
  strict mode)
- `ORPHAN_ACTIONS { "abandoned": [...], "commented": [...], "ignored": [...] }`
  (COMPLETED: abandon/comment operations via Gerrit REST API)

Future optional:

- Emit a GitHub Actions step summary (markdown) with table of reused/new/orphan.

---

## 14. Refactor Phasing Roadmap

| Phase | Goal | Deliverables |
| ----- | ---- | ------------ |
| 1 | Stabilize & Test Gaps | Action tests, extract reconciliation module (COMPLETED) |
| 2 | Traceability Maturity | ReconciliationPlan (COMPLETED), mapping digest (COMPLETED), idempotent backrefs (COMPLETED), orphan policy with REST side-effects (COMPLETED), verification phase (COMPLETED) |
| 3 | Performance | Parallel bulk, pre-plan, adaptive pagination |
| 4 | Security | Topic sanitization, integrity checks, masking audit |
| 5 | Observability | Structured metrics & verification digests |
| 6 | Legacy Cleanup | Remove legacy reconciliation path & inlined logic (COMPLETED: dual pathways removed) |

---

## 15. Immediate Quick Wins (High Impact / Low Effort)

1. Add idempotent check for Gerrit backref comment (avoid duplication).
   (COMPLETED Phase 2.5)
2. Introduce `ReconciliationPlan` abstraction and unit tests. (COMPLETED plan;
   unit tests present for plan + orphans)
3. Sanitize topic strings and add length cap. (PENDING)
4. Add composite Action path tests (PR_NUMBER parse, workflow_dispatch). (COMPLETED)
5. Centralize mapping digest computation and include in emitted comment.
   (COMPLETED: digest now in mapping comment)
6. Log a single JSON line for reconciliation summary (stabilize & document).
   (COMPLETED)
7. Add verification phase digest comparison + strict mode. (COMPLETED Phase 2.5)

Completed in Phase 1: 4, 6 plus reconciliation extraction and action PR
normalization test coverage.

Completed in Phase 2: 1, 2, 5, 7 plus orphan policy REST side-effects
(abandon/comment via Gerrit REST API).

Completed Legacy Cleanup: Removed dual reconciliation pathways - both squash and
multi-commit modes now use the modern reconciliation system.

---

## 16. Longer-Term Enhancements

| Enhancement | Rationale |
| ----------- | --------- |
| Gerrit hashtag usage (`gh-pr-<num>`, `g2g-digest-<short>`) | Quick filtering/validation |
| Drift auto-heal mode | Auto-abandon or flag stale changes |
| Partial patchset update detection | Skip redundant push if graph unchanged |
| Structured PR comment (YAML block) alternative | Extensible mapping format |
| Reusable library extraction | Embed in other CI ecosystems |
| Pluggable similarity strategies | Optional semantic/diffstat weighting |

---

## 17. Risk Register & Mitigations

| Risk | Description | Mitigation |
| ---- | ----------- | ---------- |
| Regression during refactor | Large file split may introduce subtle order bugs | Incremental extraction + contract tests |
| **Mapping divergence** | Different sources of truth | Single authoritative plan + digest |
| Orphan buildup | Noise in Gerrit UI | Enforce policy with default=comment |
| Performance cliffs | Large PR or bulk backlog | Parallelization + adaptive queries |
| Security drift | New features bypass masking | Central logging guard + pre‑commit rules |

---

## 18. Example Mapping Evolution Flow (Future Ideal)

1. Extract commit list and compute file/subject signatures.
2. Load previous mapping + Gerrit changes.
3. Build `ReconciliationPlan`.
4. If `plan.digest == previous.digest`:
   - Skip push (fast path).
5. Else:
   - Prepare commits (reuse IDs inline).
   - Push changes.
   - Re-query, verify digest.
   - Emit updated mapping (overwrite comment).
   - Apply orphan policy.
   - Emit structured summary.

---

## 19. Configuration Additions (Proposed)

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| REUSE_STRATEGY | topic+comment | Existing (keep) |
| ORPHAN_POLICY | comment | Orphan handling strategy |
| BULK_PARALLELISM | 4 | Max concurrent PR executions |
| VERIFY_DIGEST | true | Enable post-push digest check |
| ABANDON_MESSAGE_TEMPLATE | Fixed message | Customizable orphan abandon reason |

---

## 20. Acceptance Criteria for “Mature Traceability” Milestone

- A reconciliation plan logs results and includes test coverage.
- Orphan changes handled per configured policy.
- Mapping comment updated in-place (no duplication).
- Gerrit backref comment is idempotent (single reference per patchset).
- Structured JSON summary emitted containing digest + reuse/new counts.
- Composite action logic has automated tests for all control flow branches.
- ✅ Legacy reconciliation path removed and documented as deprecated in release
  notes.

---

## 21. Suggested Next Steps

1. Build `ReconciliationPlan` + unit tests. (NEXT)
2. Extract reconciliation code into `reconciliation.py`. (COMPLETED)
3. Add action-level test harness (expand beyond PR_NUMBER cases). (PENDING)
4. Add topic sanitization + digest emission in mapping comment. (PENDING)
5. Add policy configuration and build `comment` mode. (PENDING)
6. Build idempotent Gerrit backref comment logic. (PENDING)
7. Integrate plan-based mapping digest comparison (verification phase). (PENDING)

After completing the above, schedule a review checkpoint before
proceeding to parallelism and verification hardening.

---

## 22. Change Log Policy (For This Plan)

(Informational; do not include evolution history in README per
project guiding principles. This file stands alone as a planning artifact.)

---

## 23. Summary

The project is well positioned for a refinement cycle focused on
structural clarity, authoritative reconciliation, and full traceability.
By introducing a formal plan abstraction, orphan lifecycle management,
and post-push verification, github2gerrit will achieve deterministic,
auditable synchronization between GitHub and Gerrit, reducing operator
burden and integration risk.

---

Prepared as part of the repository maturation initiative.
