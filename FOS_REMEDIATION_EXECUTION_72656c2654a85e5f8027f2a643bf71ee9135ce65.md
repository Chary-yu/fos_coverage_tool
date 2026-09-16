# FOS Audit Remediation Execution

Audit baseline: 438c748772be96990e4a6b9678a6936e62b98681

This report records the offline repository remediation pass. No production
host, production database, production CURRENT, Nginx, systemd, or production
file was accessed or modified.

The report filename is anchored to the last behavior-changing remediation
commit. The final documentation commit which carries this report is
evidence-only and is reported separately in the final handoff.

## 1. Start Identity

- Branch: main
- START_MAIN_SHA: 1d8648bdd1b517a2d97cd71604f5bcc8f8ecf19b
- START_TREE_SHA: b8ad0d839cf4bf603a77c8f9c35a824b19e90cba
- START_COMMIT_TIME: 2026-09-16T13:48:45+08:00
- START_COMMIT_SUBJECT: feat(r8): add flat production candidate operator
- Audit baseline: 438c748772be96990e4a6b9678a6936e62b98681
- Remote at start and during the pass: origin/main =
  c1e770ed59502d0b8197bcc52ec0e6de84813614
- The workspace contained pre-existing user evidence/operator files and was
  treated as known-dirty. Those files were not staged, edited, or deleted.

## 2. Final Identity

- FINAL_SOURCE_CLOSURE_SHA: 72656c2654a85e5f8027f2a643bf71ee9135ce65
- FINAL_SOURCE_CLOSURE_TREE_SHA: 4f7c5565fb4e21cd2c0b3cc86810a6ad213f20da
- FINAL_SOURCE_CLOSURE_SUBJECT: fix(jobs): fence scan import recovery transitions
- The source closure SHA is the final behavior-changing SHA. The report,
  ledger, and plan completion changes are documentation-only evidence.
- Application source changed relative to the audit baseline because the
  confirmed root causes required source fixes. No Candidate builder workflow
  or builder implementation was changed.
- No schema migration was added by this remediation pass.

## 3. Finding Remediation Matrix

| Finding | Severity | Root owner | Revalidation | Final regression audit | Disposition |
| --- | --- | --- | --- | --- | --- |
| FOS-001 | P1 | Change Review | CONFIRMED | RESOLVED | Exact range/file Block identities and line containment |
| FOS-002 | P1 | Change Review | CONFIRMED | RESOLVED | Inherited candidate Block lineage survives confirm-only rolling scans |
| FOS-003 | P1 | Runtime Reliability | CONFIRMED | RESOLVED | Job create/heartbeat/reclaim/terminal transitions are owner-fenced |
| FOS-004 | P1 | Runtime Reliability | CONFIRMED | PRODUCTION_ACCEPTANCE_PENDING | Shared source lock/CAS boundary fixed; real MariaDB interleaving remains external |
| FOS-005 | P2 | Change Review | CONFIRMED | RESOLVED | Affected-file derived-state reconciliation is scoped |
| FOS-006 | P2 | Change Review | CONFIRMED | RESOLVED | DB API, statement, returned-row, affected-row, and unmeasured scopes separated |
| FOS-007 | P2 | Change Review | CONFIRMED | RESOLVED | Read-set contains only consulted ready source scopes |
| FOS-008 | P1 | Change Review | CONFIRMED | RESOLVED | Historical Block/rejection references are included and gate scope is separated |
| FOS-009 | P2 | Runtime Reliability | CONFIRMED | RESOLVED | Identity auxiliary index follows cache lifetime and budget |

P1_TOTAL=5
P1_RESOLVED=4
P1_REMAINING=1 (FOS-004 production acceptance pending)
P2_TOTAL=4
P2_RESOLVED=4
P2_REMAINING=0
UNCLASSIFIED=0

The FOS-004 source race fix is implemented and covered by structural tests.
The remaining status is deliberately pending because this workspace did not
contain a disposable MariaDB server/driver execution environment and
production access is prohibited.

### FOS-001 — exact multi-range analysis identity

The confirmed root cause was reuse of one operation identity for multiple
selected ranges and creation of a Block from only the first range. The fix
keeps shared content/record identity but creates one exact Block per
operation/range/file interval, resolves the actual line number, and rejects
LineLinks outside the Block range.

Regression:
test_multi_range_selection_creates_one_exact_block_per_interval and
test_direct_line_id_rejects_conflicting_client_line_number.

### FOS-002 — rolling inheritance Block lineage

Successful inheritance now materializes a candidate Scan Block with a cloned
originating record and mapped contiguous line runs. Ambiguous mappings fail
closed. This preserves the Block identity needed by the next generation after
an inherited analysis is confirmed.

Regression:
test_confirm_only_inheritance_retains_block_for_next_scan covers S1 manual,
S2 inherited pending, S2 confirm-only, and S3 inheritance.

### FOS-003 — fenced background job lifecycle

Durable job creation, start, heartbeat, reclaim, terminal transition, and
restore are separate operations. Live writes use owner/state/heartbeat CAS;
terminal state cannot be revived. Scan-import recovery and failure paths now
use the same fence and cannot release a reclaimed owner's lock.

Regression:
test_job_transitions_fence_stale_owner_and_preserve_creation_identity and
test_stale_recovery_failure_cannot_clobber_reclaimed_owner_or_lock.

### FOS-004 — CURRENT and analysis linearization

Project-state mutation paths now share a project-state row lock boundary,
using FOR UPDATE for non-SQLite connections and the serialized SQLite writer
fallback. Analysis, inheritance review/reject/undo, publication, and direct
state transitions retain the existing CAS checks.

The source contract is covered offline. Real two-connection MariaDB
interleavings for stale review/edit/reject/undo/read-set/publication remain
PRODUCTION_ACCEPTANCE_PENDING; no production claim is made.

### FOS-005 — incremental file-state work

The affected-file path no longer unconditionally rebases every file-state row
or recomputes the full fact aggregate inside the bounded mutation. It rebuilds
selected files and validates aggregate deltas, while retaining a full
reconciliation fallback when previous verified state is unavailable or
ambiguous.

Regression:
test_partial_rebuild_updates_only_affected_files_and_keeps_ready_gate.

### FOS-006 — honest database measurement scopes

Instrumentation distinguishes DB-API calls, direct execute statements,
executemany/executescript statements whose physical expansion is unknown,
returned rows, and affected rows. Network round trips and server rows
examined are explicitly marked NOT_MEASURED rather than inferred.

Regression:
test_db_metrics_distinguish_api_calls_statements_and_row_semantics.

### FOS-007 — scoped inheritance read set

The compact read-set contract is versioned and records only ready source files
with an actual candidate correspondence. Publication replays those exact
scopes. Malformed or old ambiguous compact payloads fail closed; a real
consulted dependency mutation still rejects publication.

Regressions:
test_read_set_excludes_source_files_without_candidate_correspondence,
test_read_set_excludes_unready_repository_source_relations, and the existing
consulted-dependency mutation test.

### FOS-008 — historical reference graph consistency

Record liveness now includes current/historical LineLinks, Block
originating_record_id references, and rejection references. Global health
diagnostics remain available, but the candidate publication gate consumes the
candidate scope rather than an unrelated global orphan count.

Regressions:
test_consistency_audit_counts_historical_block_reference_as_live_identity and
test_scoped_consistency_gate_ignores_unrelated_true_orphan.

### FOS-009 — bounded identity cache index

Identity index entries are removed with their LRU entries, and identities
bypassed because of zero/oversize budgets are not indexed. Additive metrics
expose cache/index state without weakening freshness or identity checks.

Regressions:
test_identity_index_is_evicted_with_lru_entry and
test_identity_entry_removal_cleans_exact_index_mapping.

## 4. Changed Files

Behavior and regression files changed by this remediation:

- app/api/application.py
- app/bootstrap.py
- app/code_detail/vnext_service.py
- app/db/repositories/analysis_domain_repository.py
- app/db/repositories/file_state_repository.py
- app/db/repositories/job_repository.py
- app/db/repositories/project_state_repository.py
- app/inheritance/engine.py
- app/inheritance/read_set.py
- app/inheritance/rejections.py
- app/jobs/service.py
- app/observability/performance.py
- app/scan_import/coordinator.py
- app/scan_import/publication.py
- app/scan_import/recovery.py
- app/services/analysis_domain_service.py
- app/services/analysis_service.py
- app/services/file_state_service.py
- app/services/inheritance_review_service.py
- app/services/progress_service.py
- scripts/upgrade/migration_runner.py
- tests/vnext/test_analysis_domain.py
- tests/vnext/test_current_review_publication_race.py
- tests/vnext/test_identity_cache_index_budget.py
- tests/vnext/test_inheritance_engine.py
- tests/vnext/test_jobs.py
- tests/vnext/test_performance_observability.py
- tests/vnext/test_reliability_repairs.py
- tests/vnext/test_scan_import_lifecycle.py

Evidence and execution records:

- FOS_REMEDIATION_LEDGER.md
- docs/superpowers/plans/2026-09-16-fos-audit-remediation.md
- this report

No .github workflow, Candidate builder, production operator, or production
configuration was modified during the remediation commits.

## 5. Commit List

The focused remediation lineage after the initial plan/ledger commit is:

- 165a92bd9a2fa161f160c7a76c42e67ed0bc4f3a docs(audit): establish remediation plan and ledger
- a741c1596d656e43e22e3649b052b45fb1e38338 fix(analysis): preserve exact multi-range block identity
- b832178dcb195bba834aea711f39ee9ed093dfbb fix(inheritance): preserve candidate block lineage
- 506440cae4bc849eae4e2bf5945c2b9412e4d696 fix(jobs): fence stale worker state transitions
- 94a9c15b8c5819d36b98d1b09fa6b0119b03b86a fix(current): serialize analysis and publication boundary
- be07fb1a9542b6a385ed2ba9c3dd2a345eac7660 fix(domain): preserve historical block references in orphan audit
- 6a0f3770c08a1ed149869cccac01c5eaa16389e4 fix(performance): scope file-state mutation reconciliation
- c00e0184f4ad1821ecc4a801800447a684304366 fix(observability): distinguish database measurement scopes
- f5d4cfb1e5d2a195feb4ffa967fe3c41f06f9007 fix(inheritance): scope publication read-set dependencies
- 815a9d186fd53de67d97f8228e99e006c9195e2a fix(runtime): bound identity cache index lifetime
- e9f52d00a149d049518e61f7cb49ea3a38b21dba fix(current): keep incrementally verified file-state ready
- e97dc60f9062e6ba86cb248cb17fcdc4d9341ac6 fix(domain): separate scoped and global consistency status
- 72656c2654a85e5f8027f2a643bf71ee9135ce65 fix(jobs): fence scan import recovery transitions

The last commit is a follow-up to FOS-003 after a source review found direct
scan-import recovery/failure paths that still needed the same fencing
contract.

## 6. Targeted Test Evidence

No full repository test suite was run. Final phase-gate commands all passed:

- Phase 1 correctness gate: 81 tests, PASS
- Phase 2 reliability gate: 49 tests, PASS
- Phase 3 performance/read-set gate: 72 tests, PASS
- Phase 4/5 runtime, architecture, API, compatibility, and release checks:
  82 tests, PASS

These commands contain overlapping modules; the total is 284 test
invocations, not a count of unique tests. Targeted test failures after the
last fix: 0.

Additional offline checks:

- py_compile for all changed Python source and test files: PASS
- changed_test_selection.py against the remediation range: PASS
- frontend_vnext_api_contract_audit.py: PASS
- canonical_ownership_audit.py: PASS
- skill_capability_manifest.py at the source closure SHA: PASS
- repository skill_drift_audit.py self-check with the exact capability
  manifest: PASS, violations empty
- task_manifest_audit.py in a clean clone: PASS
- final_source_review.py in a clean clone at the source closure SHA: PASS
- git diff --check: PASS

The existing architecture test also reports that the legacy retirement gate
is INCOMPLETE because external retirement/usage telemetry is not present.
That test result is recorded as an evidence gap, not converted into PASS.

## 7. Integration Evidence

The phase gates exercise the domain repositories, analysis and inheritance
services, publication/CURRENT contracts, scan-import recovery, job lifecycle,
observability, API/runtime wiring, and release-governance checks together.

The clean-clone source review found all repository capability checks passing
at 72656c2654a85e5f8027f2a643bf71ee9135ce65. The review itself identifies
external validation still required for production process/service identity,
target database/runtime fingerprint, and traffic-closed read-only
verification.

FOS-004 was intentionally not promoted to real-concurrency PASS: no local
MariaDB server/container or matching command-line server was available, and
production access is forbidden.

## 8. Browser Evidence

No P1/P2 UI root cause was confirmed in the current revalidation, and no UI
file was changed. Browser-only acceptance was not substituted with a mocked
API result. Production browser behavior, cross-client refresh, all task
center states, and visual matrices remain PRODUCTION_ACCEPTANCE_PENDING or
NOT_VERIFIED where applicable.

## 9. Compatibility Evidence

- Existing single-range analysis, rejection, undo, inheritance, publication,
  queue, recovery, API, and legacy projection tests pass.
- SQLite uses a serialized writer fallback; MySQL-only FOR UPDATE syntax is
  selected only for non-SQLite connections.
- Old ambiguous compact inheritance read-set payloads fail closed.
- Full rebuild and import paths retain full reconciliation fallback.
- Historical records, blocks, links, and rejection rows are not rewritten or
  deleted by these fixes.
- Compatibility metric aliases remain available; new metric scopes are
  additive.
- Python compilation succeeds for all changed modules.
- No production schema migration was executed or added in this pass.
- A real MariaDB 5.5 dual-connection acceptance run and production data
  compatibility audit remain external evidence, not local claims.

## 10. Performance Before/After

No production-scale speed, query, rows-examined, or RSS improvement is
claimed.

The code-level changes are:

- FOS-005: the bounded affected-file path no longer performs unconditional
  all-file version rebasing/full fact aggregation; it uses selected file
  deltas and a fail-closed full fallback.
- FOS-006: measurements now state what is observed and explicitly leave
  physical statement expansion, network round trips, and server rows examined
  unmeasured.
- FOS-007: publication validation replays only consulted dependency scopes.
- FOS-009: auxiliary identity keys have the same lifetime and budget boundary
  as cache entries.

Fixed-hardware workload identity, before/after elapsed time, database
statement/row profiles, lock waits, and peak RSS are still required before
any capacity or percentage claim.

## 11. Skill Drift Result

The requested FOS Skill Suite files were not available in this workspace and
there is no callable fos-coverage-maintainer or domain-owner skill package
in the available skill catalog. This limitation was recorded in the ledger;
no FOS skill execution was fabricated.

Available repository diagnostics were run instead. The repository capability
manifest and its self-consistency drift audit both passed with no violations.
The five external FOS helper/fixture behavior suites remain NOT_VERIFIED.
No Skill file was changed.

## 12. Remaining NOT_VERIFIED Coverage

The audit coverage ledger remains complete with UNCLASSIFIED=0. The following
19 audit domains still require evidence not available offline:

1. Algorithm complexity across all entry points
2. Process memory/materialization at production scale
4. Database runtime resources, EXPLAIN, lock waits, and transaction timing
6. Full parser/AST/context corpus
13. Production backup migration semantic-loss reconciliation
14. Same-session release identity, configuration, assets, process, and DB
16. Full FOS Skill helper/fixture behavior regression
28. Full search/sort/filter/pagination browser matrix
29. Cross-client refresh after review
32. Task-center state/retry/cancel UI matrix
33. UX task-efficiency and keyboard path
34. Desktop visual 1366x768, 1920x1080, zoom, and long-content matrix
35. UI loading/empty/error/disabled/review-state matrix
36. Full security and supply-chain input/action/artifact review
39. Complete incident event-chain reconstruction
40. Production backup/cutover/rollback rehearsal
41. Complete functional entry-point inventory
42. Complete cache inventory across all runtime entry points
44. Complete historical finding-to-fix-to-regression mapping

## 13. Production Acceptance Pending

The following must be completed by an authorized operator or matching
controlled environment:

- MariaDB dual-connection FOS-004 ordering and lock behavior
- production process, service, database, release identity, asset, and Nginx
  same-session identity verification
- historical production AnalysisRecord/AnalysisBlock/LineLink/Rejection graph
  acceptance
- fixed-hardware performance and long-running RSS measurements
- production browser and cross-client state acceptance
- backup, cutover, migration, and rollback rehearsal
- external FOS Skill Suite helper/fixture behavior regression

This list is not a request for Codex to access production.

## 14. Known Risks

- FOS-004 remains a P1 production-acceptance item until real MariaDB
  interleavings prove the source lock boundary under the deployed driver and
  isolation settings.
- Legacy malformed or incomplete historical Block data is not silently
  rewritten.
- Performance improvements are structural only until fixed workload metrics
  exist.
- The external FOS Skill Suite was unavailable, so its helper-level contract
  coverage could not be independently executed.
- Production deployment identity and release/cutover state remain outside
  this offline closure.

## 15. Rollback Considerations

No production mutation occurred and no schema change was introduced by this
pass. The source changes are isolated in focused commits and can be reverted
through normal forward Git history if a controlled validation environment
finds a regression.

For a future production release, rollback must continue to use the existing
immutable release/CURRENT/service contract. The release operator must bind
the exact prior publication identity, database backup identity, failed phase,
and post-rollback verification. This execution did not perform or claim a
production rollback rehearsal.

## 16. Next Release Action

Run the pending controlled MariaDB, production-identity, historical-data,
performance, browser, backup/cutover, and external Skill Suite acceptance
checks. Only after those checks pass should an authorized release operator
build/reuse the Candidate and proceed with the separate production release
workflow. Codex must not perform those production-side actions.

SOURCE_REMEDIATION_COMPLETE=YES
PRODUCTION_READY=NO
PRODUCTION_MODIFIED=NO
PRODUCTION_DATABASE_MODIFIED=NO
CURRENT_MODIFIED_IN_PRODUCTION=NO
