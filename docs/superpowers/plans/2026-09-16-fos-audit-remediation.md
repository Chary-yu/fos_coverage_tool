# FOS Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Revalidate and remediate every currently applicable P1/P2 finding in `FOS_FULL_SYSTEM_AUDIT_438c748772be96990e4a6b9678a6936e62b98681.md`, preserving legacy compatibility and production safety, then commit a focused, evidence-backed `main` state.

**Architecture:** Apply minimal root-cause fixes at the domain/persistence boundaries: exact analysis range/block identities and inheritance lineage, project-state serialization, owner-fenced job transitions, scoped inheritance read sets and historical reference auditing, honest database/performance evidence, incremental derived-state rebuilds, and bounded identity-cache indexes. Use targeted regression tests before each implementation change, and keep production-only acceptance explicitly pending.

**Tech Stack:** Python 3-compatible application code, SQLite-focused repository tests, optional local MariaDB contract tests when available, unittest, existing release/audit scripts, Git on `main`.

**Spec:** `docs/FOS_FULL_SYSTEM_AUDIT_438c748772be96990e4a6b9678a6936e62b98681.md`

## Global Constraints

- Never access or modify production, vfoswind, production databases, CURRENT, nginx, systemd, or production files.
- Do not change application payload/source for reasons unrelated to a finding; keep fixes minimal and legacy-compatible.
- Do not run the full test suite, create patch files, rebuild a Candidate, or create a persistent branch.
- Revalidate each finding at the current HEAD, add a root-cause regression test first, implement the smallest safe fix, run affected tests, review the diff, and commit coherent root causes independently.
- Record every finding in `FOS_REMEDIATION_LEDGER.md`; preserve `PRODUCTION_ACCEPTANCE_PENDING` for evidence that requires real MariaDB/production/browser/deployment validation.
- The FOS-specific maintainer/domain skills named by the request are unavailable in this workspace; do not fabricate their use. Record the capability gap and run only the repository’s available offline drift tooling if its required suite is present.

---

## Task 1 — Lock identities and establish the remediation ledger

- [ ] Reconfirm branch, HEAD/tree/time, remote synchronization, audit baseline, clean/known-dirty worktree, and no application-source changes since the audit baseline.
- [ ] Parse all nine formal P1/P2 findings and create the initial ledger with no unclassified findings, including current revalidation evidence and production-only gaps.
- [ ] Add focused test-run helpers/fixtures only where needed; commit the plan/ledger separately from behavior fixes.

## Task 2 — Phase 1 correctness: exact analysis range/block identity (FOS-001)

- [ ] Add failing tests for multi-range and sparse selections, block boundaries, actual line links, and direct line-id/line-number identity.
- [ ] Preserve one content/record operation while assigning distinct range/file block identities; enforce link containment in the repository.
- [ ] Run the analysis-domain and coordinator gates, review legacy/API compatibility, and commit the root-cause fix.

## Task 3 — Phase 1 correctness: rolling inheritance lineage (FOS-002)

- [ ] Add the S1 manual-confirm → S2 inherited-pending → S2 confirm → S3 inheritance regression test.
- [ ] Materialize verified candidate inheritance blocks with originating-record lineage and map contiguous ranges safely; preserve source group evidence.
- [ ] Run inheritance/domain/publication focused gates and commit.

## Task 4 — Phase 1 correctness: CURRENT/analysis publication serialization (FOS-004)

- [ ] Add a deterministic dual-connection transaction test (SQLite structural fallback and real local MariaDB only if available) for stale analysis/review versus publication and inheritance read-set invalidation.
- [ ] Introduce a common project-state row lock/CAS boundary for analysis, review/reject/undo, publication, and direct state transitions without holding unrelated long I/O locks.
- [ ] Run focused lifecycle tests; mark real MariaDB or production acceptance pending when unavailable; commit.

## Task 5 — Phase 1 correctness: historical reference graph gate (FOS-008)

- [ ] Add tests proving historical blocks/rejections keep records referenced, true orphans remain visible, and unrelated historical orphans do not block a candidate scan.
- [ ] Include block-originating and rejection references, expose global versus candidate-scope health, and make the publication gate use the candidate scope.
- [ ] Run consistency/coordinator gates and commit.

## Task 6 — Phase 1 gate

- [ ] Rerun all FOS-001/FOS-002/FOS-004/FOS-008 targeted tests plus pending-conservation and historical compatibility checks.
- [ ] Confirm no stale worker/CURRENT/inheritance/data-integrity high-risk regression before Phase 2.

## Task 7 — Phase 2 reliability: lease-fenced job state transitions (FOS-003)

- [ ] Add failing tests for stale heartbeat after reclaim, stale terminal write after completion, immutable creation time/input identity, and runner heartbeat shutdown ordering.
- [ ] Split durable create/heartbeat/terminal CAS operations, route service/recovery paths safely, and reject stale writes without reviving terminal state.
- [ ] Run job/recovery/bootstrap focused tests and commit.

## Task 8 — Phase 2 gate

- [ ] Rerun all job lifecycle/recovery tests including queued/running/completed/failed/interrupted/superseded and scan-import compatibility.
- [ ] Confirm zero stale-worker overwrite paths remain.

## Task 9 — Phase 3 performance/correctness: incremental file state (FOS-005)

- [ ] Add failing fixture asserting only affected files rebuild/rebase and that readiness validates the full aggregate from an affected delta.
- [ ] Replace all-row version rebasing/full-fact recomputation on the affected path with selected-file rebuild plus aggregate delta validation; retain full reconciliation fallback.
- [ ] Capture workload/query/rows/timing evidence where measurable without claiming production performance; commit.

## Task 10 — Phase 3 performance evidence semantics (FOS-006)

- [ ] Add failing metric tests distinguishing DB API calls, directly executed statements, executemany unknown physical statements, returned rows, affected rows, and unverified server rows examined.
- [ ] Update instrumentation schema/documentation while preserving compatibility aliases; commit after focused observability tests.

## Task 11 — Phase 3 dependency read-set scope (FOS-007)

- [ ] Add failing tests for unrelated source repositories/files and candidate-gateway-like coexistence: only ready source scopes with actual target correspondence enter the read set.
- [ ] Version/validate compact read sets with explicit scopes and replay only those scopes; fail closed for ambiguous legacy payloads.
- [ ] Run inheritance/publication tests and commit.

## Task 12 — Phase 3 gate

- [ ] Rerun FOS-005/FOS-006/FOS-007 tests and all correctness gates; review measured evidence and avoid unsupported performance claims.

## Task 13 — Phase 4 UI/UX revalidation

- [ ] Revalidate the audit’s UI domains against current code and existing local/browser fixtures; no UI modification is authorized without a reproducible current root cause.
- [ ] Record `ALREADY_RESOLVED`, `NOT_REPRODUCIBLE`, or `PRODUCTION_BROWSER_ACCEPTANCE_PENDING` with evidence; do not substitute mocked API checks for integration proof.

## Task 14 — Phase 5 maintainability and bounded identity cache (FOS-009)

- [ ] Add failing tests for LRU eviction/index cleanup, zero-byte identity budget, and bounded index metrics.
- [ ] Remove index entries with cache entries, never index bypassed values, expose bounded metrics, run focused code-detail/API tests, and commit.

## Task 15 — Phase 5 gate and skill drift

- [ ] Rerun all Phase 5 tests and repository architecture/compatibility checks.
- [ ] Run `scripts/audit_skill_drift.py` with explicit repository/suite roots if the required FOS suite exists; otherwise record the unavailable-suite limitation and affected pending evidence.

## Task 16 — Final regression and remediation regression audit

- [ ] Re-run each finding’s original trigger and root-cause tests, shared contracts, legacy compatibility, Python/MariaDB/browser checks available offline, and phase gates; no full suite.
- [ ] Produce the final finding matrix with P1/P2 totals and statuses `FIXED_VERIFIED`, `ALREADY_RESOLVED`, `NOT_REPRODUCIBLE`, `PRODUCTION_ACCEPTANCE_PENDING`, or `BLOCKED`, with `UNCLASSIFIED=0`.
- [ ] Create `FOS_REMEDIATION_EXECUTION_<FINAL_SHA>.md` only after the final SHA exists, including identity, commits, tests, compatibility, performance evidence, skill drift, pending evidence, risks, rollback, and next action.
- [ ] Review `git diff`, verify application source and builder unchanged, fetch/check remote lineage, commit the report/ledger, and push normally only if remote remains unchanged.

