# FOS Audit Remediation Ledger

Audit baseline: `438c748772be96990e4a6b9678a6936e62b98681`

The current repository lineage was independently revalidated before any
behavior change. The named FOS Skill Suite is not installed in this workspace;
that capability gap is recorded in the final report rather than represented as
completed skill execution.

| Finding ID | Severity | Original root owner | Current revalidation | Status | Remaining evidence |
| --- | --- | --- | --- | --- | --- |
| FOS-001 | P1 | Change Review | CONFIRMED: `_resolve_records` shared `_operation_key` across ranges and the save planner created one same-file Block from the first range; repository links did not enforce line containment. | FIXED_VERIFIED | Legacy malformed historical ranges require separate evidence-bound repair. |
| FOS-002 | P1 | Change Review | CONFIRMED: successful inheritance created a candidate LineLink without a candidate `AnalysisBlock`; confirm-only rolling inheritance lost the next-generation block identity. | FIXED_VERIFIED | Full external parser corpus remains pending. |
| FOS-003 | P1 | Runtime Reliability | CONFIRMED: generic `JobRepository.upsert` updated by `job_id` only and runner heartbeat could race terminal persistence. | FIXED_VERIFIED | MariaDB driver-level concurrency evidence remains pending. |
| FOS-004 | P1 | Runtime Reliability | CONFIRMED structural gap / NEEDS_EXTERNAL_EVIDENCE for real dual-connection MariaDB ordering: analysis/review/publication paths lacked a shared project-state lock boundary. | PRODUCTION_ACCEPTANCE_PENDING | Source lock boundary is fixed and tested; disposable MariaDB/production-like concurrency run remains external. |
| FOS-005 | P2 | Change Review | CONFIRMED: affected-file path rebased every `coverage_file_state` row and validated a full authoritative fact aggregate inside the mutation transaction. | FIXED_VERIFIED | Fixed-hardware workload measurement remains pending. |
| FOS-006 | P2 | Change Review | CONFIRMED: `execute` and `executemany` shared one `db_query_count`, while returned and affected rows were not separately named from server-examined rows. | FIXED_VERIFIED | Real driver/session statement count remains pending. |
| FOS-007 | P2 | Change Review | CONFIRMED: inheritance recorded all predecessor active relations even for unready repositories or missing candidate files, then publication replayed the full predecessor set. | FIXED_VERIFIED | Large-scope checkpoint measurement remains pending. |
| FOS-008 | P1 | Change Review | CONFIRMED: orphan query considered only LineLink references and the candidate gate consumed the global count, ignoring historical Block/rejection references. | FIXED_VERIFIED | Historical production data acceptance remains pending. |
| FOS-009 | P2 | Runtime Reliability | CONFIRMED: LRU removal did not remove `_identity_index`; an oversize/disabled store could index a non-existent cache entry. | FIXED_VERIFIED | Long-running RSS workload remains pending. |

UNCLASSIFIED: 0

## Per-finding execution record

The following fields are updated after each root-cause commit:

- Changed Files
- Changed Functions
- Regression Test
- Integration Test
- Browser Test
- Compatibility Check
- Commit SHA
- Status (`FIXED_VERIFIED`, `ALREADY_RESOLVED`, `NOT_REPRODUCIBLE`, `PRODUCTION_ACCEPTANCE_PENDING`, or `BLOCKED`)
- Remaining Evidence

### FOS-001

- Original Severity: P1
- Original Root Cause: content-operation identity was incorrectly reused as a unique selection/range identity, and the domain repository did not enforce physical line containment in a Block.
- Current Revalidation Result: CONFIRMED at the start of Phase 1; both pre-fix regression tests failed (one broad first-range Block and one missing direct line-number conflict).
- Root Owner: Change Review
- Changed Files: `app/services/analysis_service.py`, `app/db/repositories/analysis_domain_repository.py`, `tests/vnext/test_analysis_domain.py`
- Changed Functions: `AnalysisService._resolve_records`, `AnalysisService._save_domain_records`, `AnalysisDomainRepository.create_block`, `create_link`, `create_links_many`
- Regression Test: `test_multi_range_selection_creates_one_exact_block_per_interval`, `test_direct_line_id_rejects_conflicting_client_line_number`; both pass after the fix.
- Integration Test: `python3 -m unittest -v tests.vnext.test_analysis_domain` — PASS (13 tests).
- Browser Test: NOT_RUN; no UI code changed and browser acceptance is not required to prove the persistence invariant.
- Compatibility Check: legacy single-range/manual and rejection/undo domain tests pass; historical malformed data is not rewritten.
- Commit SHA: `a741c1596d656e43e22e3649b052b45fb1e38338`
- Status: FIXED_VERIFIED
- Remaining Evidence: full API/browser multi-select corpus and any historical malformed-block repair remain outside offline source acceptance.

### FOS-002

- Original Severity: P1
- Original Root Cause: inherited output did not satisfy the candidate Scan Block contract required by the next inheritance run.
- Current Revalidation Result: CONFIRMED at the start of Phase 1; the new three-generation fixture failed when S2 had no candidate Block.
- Root Owner: Change Review
- Changed Files: `app/inheritance/engine.py`, `tests/vnext/test_inheritance_engine.py`
- Changed Functions: `InheritanceEngine.run`, `_persist_relation_decision`, `_candidate_block_for_relation`, `_iter_source_relation_pages`
- Regression Test: `test_confirm_only_inheritance_retains_block_for_next_scan` — PASS; it runs S1 manual → S2 inheritance → S2 confirm-only → S3 inheritance and verifies candidate scan/file/range/originating record identity.
- Integration Test: `python3 -m unittest -v tests.vnext.test_inheritance_engine` — PASS (40 tests).
- Browser Test: NOT_RUN; no UI code changed.
- Compatibility Check: existing inheritance parser, Git mapping, rejection and publication fixtures pass; source triple/group evidence remains intact and historical rows are not rewritten.
- Commit SHA: `b832178dcb195bba834aea711f39ee9ed093dfbb`
- Status: FIXED_VERIFIED
- Remaining Evidence: full production parser corpus, MariaDB concurrency, and historical incomplete relation repair remain pending.

### FOS-003

- Original Severity: P1
- Original Root Cause: the generic full-row `job_id` update had no lease-owner/state CAS and the runner stopped heartbeat after writing terminal state.
- Current Revalidation Result: CONFIRMED at the start of Phase 2; the new stale-owner/reclaim/terminal test would fail because the required transition API did not exist.
- Root Owner: Runtime Reliability
- Changed Files: `app/db/repositories/job_repository.py`, `app/jobs/service.py`, `app/scan_import/coordinator.py`, `app/scan_import/recovery.py`, `app/bootstrap.py`, `app/api/application.py`, `scripts/upgrade/migration_runner.py`, `tests/vnext/test_jobs.py`, `tests/vnext/test_scan_import_lifecycle.py`
- Changed Functions: `JobRepository.create`, `start`, `heartbeat`, `transition_terminal`, `reclaim`, `restore`, `mark_interrupted`, `VNextBackgroundJobService._save`, `_runner`, `ScanImportRecoveryService.reclaim`, `record_failure`, `ScanImportCoordinator.publish`
- Regression Test: `test_job_transitions_fence_stale_owner_and_preserve_creation_identity`, `test_stale_recovery_failure_cannot_clobber_reclaimed_owner_or_lock` — PASS; stale old-owner writes affect zero rows, terminal state remains terminal, input/created-at identity is immutable, and a late recovery failure cannot change the reclaimed job or release its lock.
- Integration Test: `python3 -m unittest -v tests.vnext.test_jobs tests.vnext.test_migration_runner tests.vnext.test_scan_import_lifecycle` — PASS (44 tests) before the recovery extension; the follow-up `python3 -m unittest -v tests.vnext.test_jobs tests.vnext.test_scan_import_lifecycle tests.vnext.test_current_review_publication_race` passed (25 tests).
- Browser Test: NOT_RUN; job persistence is not a browser-only invariant.
- Compatibility Check: migration paths use explicit `restore`; scan-import creation uses `create`; existing recovery and queue rejection tests pass.
- Commit SHA: `506440cae4bc849eae4e2bf5945c2b9412e4d696`, follow-up `72656c2654a85e5f8027f2a643bf71ee9135ce65`
- Status: FIXED_VERIFIED
- Remaining Evidence: real MariaDB lock/driver interleaving and an external long-running process shutdown remain pending.

### FOS-004

- Original Severity: P1
- Original Root Cause: CURRENT validation, analysis mutation, and publication read-set/CURRENT CAS did not share a project-state linearization boundary.
- Current Revalidation Result: CONFIRMED structurally; before the fix the targeted contract could not find a `lock_for_update` API and the paths used ordinary state reads.
- Root Owner: Runtime Reliability
- Changed Files: `app/db/repositories/project_state_repository.py`, `app/services/analysis_service.py`, `app/services/inheritance_review_service.py`, `app/inheritance/rejections.py`, `app/scan_import/publication.py`, `tests/vnext/test_current_review_publication_race.py`
- Changed Functions: `ProjectStateRepository.lock_for_update`, `advance`, `set_current_scan`, `invalidate`, `mark_ready`, and the analysis/review/reject/undo/publication entry boundaries.
- Regression Test: `test_project_state_lock_uses_for_update_on_mysql_connections`, `test_analysis_and_publication_share_project_state_lock_boundary` — PASS.
- Integration Test: Phase 1 gate command `python3 -m unittest -v tests.vnext.test_analysis_domain tests.vnext.test_inheritance_engine tests.vnext.test_scan_import_lifecycle tests.vnext.test_reliability_repairs tests.vnext.test_current_review_publication_race` — PASS (80 tests), including CURRENT CAS and rejection/undo paths.
- Browser Test: NOT_RUN; no UI code changed.
- Compatibility Check: SQLite omits MySQL-only syntax while its writer transaction remains serialized; existing publication/import/review tests pass.
- Commit SHA: `94a9c15b8c5819d36b98d1b09fa6b0119b03b86a`
- Status: PRODUCTION_ACCEPTANCE_PENDING
- Remaining Evidence: real disposable MariaDB dual-connection barriers for review/edit/reject/undo/read-set/publication, plus production deployment outer serialization, must be executed by an authorized operator.

### FOS-005

- Original Severity: P2
- Original Root Cause: a bounded analysis/review mutation still rewrote every `coverage_file_state` row and ran a full authoritative fact aggregate before the Ready gate.
- Current Revalidation Result: CONFIRMED at the start of Phase 3; the regression expectation failed before the change because `rebase_scan_version` and an unscoped `scan_summary_from_facts` were both invoked.
- Root Owner: Change Review
- Changed Files: `app/db/repositories/file_state_repository.py`, `app/services/file_state_service.py`, `app/services/analysis_service.py`, `app/services/inheritance_review_service.py`, `app/inheritance/rejections.py`, `tests/vnext/test_reliability_repairs.py`
- Changed Functions: `FileStateRepository.scan_aggregate`, `file_state_completeness`, `pending_conservation`, `scan_summary_from_facts`, `FileStateService.validate_rebuilt`, `rebuild_validate_and_mark_ready_in_transaction`, and the analysis/review/reject/undo callers.
- Regression Test: `test_partial_rebuild_updates_only_affected_files_and_keeps_ready_gate` — PASS; the affected file is rebuilt, the unchanged row retains its previously verified version, the rebase UPDATE is not called, the fact reconciliation is scoped to the affected file, and a ready consumer accepts the mixed (older-but-verified) row versions.
- Integration Test: `python3 -m unittest -v tests.vnext.test_reliability_repairs tests.vnext.test_scan_import_lifecycle` — PASS (25 tests).
- Browser Test: NOT_RUN; this is a persistence/transaction workload invariant and no UI code changed.
- Compatibility Check: full rebuild/import paths remain authoritative and retain full reconciliation; stale/partial previous projections fail closed to the full rebuild path; SQLite and MariaDB SQL remain bounded by parameterized file-id sets.
- Commit SHA: `6a0f3770c08a1ed149869cccac01c5eaa16389e4`, follow-up `e9f52d00a149d049518e61f7cb49ea3a38b21dba`
- Status: FIXED_VERIFIED
- Remaining Evidence: fixed-hardware before/after rows examined/affected, lock wait, transaction and response measurements remain pending; no production performance claim is made.

### FOS-008

- Original Severity: P1
- Original Root Cause: the orphan-record audit treated a record as live only when a current or historical LineLink referenced it, ignoring historical `AnalysisBlock.originating_record_id` and rejection references; its global result could also block an unrelated candidate Scan.
- Current Revalidation Result: CONFIRMED at the start of Phase 1; the new historical Block fixture and scoped orphan fixture both captured the prior false orphan/gate behavior before the fix.
- Root Owner: Change Review
- Changed Files: `app/services/analysis_domain_service.py`, `tests/vnext/test_analysis_domain.py`
- Changed Functions: `AnalysisDomainService.audit_consistency`
- Regression Test: `test_consistency_audit_counts_historical_block_reference_as_live_identity`, `test_scoped_consistency_gate_ignores_unrelated_true_orphan` — PASS; historical graph edges are retained as live references, while a true unrelated orphan remains visible globally without blocking the scoped candidate gate.
- Integration Test: `python3 -m unittest -v tests.vnext.test_analysis_domain` — PASS (13 tests).
- Browser Test: NOT_RUN; this is a domain consistency gate and no UI code changed.
- Compatibility Check: `orphan_records` remains the true-unreferenced compatibility count; new diagnostic counts are additive; Block/rejection rows are not rewritten or deleted.
- Commit SHA: `be07fb1a9542b6a385ed2ba9c3dd2a345eac7660`, follow-up `e97dc60f9062e6ba86cb248cb17fcdc4d9341ac6`
- Status: FIXED_VERIFIED
- Remaining Evidence: historical production data and the full migration/legacy semantic corpus remain production acceptance evidence, not local proof.

### FOS-006

- Original Severity: P2
- Original Root Cause: the instrumentation collapsed DB-API `execute` and `executemany` into one query counter and used names that could be mistaken for physical statements, network round trips, or server-examined rows.
- Current Revalidation Result: CONFIRMED at the start of Phase 3; the new metric-semantics regression initially failed because the new counters and measurement scope were absent.
- Root Owner: Change Review
- Changed Files: `app/observability/performance.py`, `tests/vnext/test_performance_observability.py`
- Changed Functions: `InstrumentedCursor._query`, `InstrumentedConnection.executescript`, `PerformanceEvidenceCollector.record_db_query`, `record_db_rows_read`, `snapshot`
- Regression Test: `test_db_metrics_distinguish_api_calls_statements_and_row_semantics` — PASS; execute calls, executemany calls, returned rows and affected rows are separated, while round trips and server-examined rows are explicitly `NOT_MEASURED`.
- Integration Test: `python3 -m unittest -v tests.vnext.test_performance_observability` — PASS (9 tests).
- Browser Test: NOT_RUN; this is instrumentation semantics and no UI code changed.
- Compatibility Check: `db_query_count`, `db_rows`, `db_rows_read` and `db_rows_affected` remain available; the evidence schema version remains compatible and the additive metrics have their own version/scope declaration.
- Commit SHA: `c00e0184f4ad1821ecc4a801800447a684304366`
- Status: FIXED_VERIFIED
- Remaining Evidence: actual PyMySQL/MariaDB session statement expansion, server rows examined, and network round trips require a disposable matching driver/database; no production/network-log claim is made.

### FOS-007

- Original Severity: P2
- Original Root Cause: the inheritance read-set was defined as every predecessor active relation, so source files with no candidate correspondence or an unready repository could block publication despite never affecting the candidate decision.
- Current Revalidation Result: CONFIRMED at the start of Phase 2; the missing-candidate and unready-repository fixtures would previously add their source relations to the compact read-set.
- Root Owner: Change Review
- Changed Files: `app/inheritance/read_set.py`, `app/inheritance/engine.py`, `app/scan_import/publication.py`, `tests/vnext/test_inheritance_engine.py`
- Changed Functions: `ReadSetAccumulator.add_source_file`, `to_payload`, `matches`; `InheritanceEngine.run`; `ScanPublicationService._validate_compact_read_set`
- Regression Test: `test_read_set_excludes_source_files_without_candidate_correspondence`, `test_read_set_excludes_unready_repository_source_relations`, and the existing true-dependency mutation test — PASS; only consulted candidate-corresponding source scopes are replayed, while a changed consulted record still returns `READ_SET_CHANGED`.
- Integration Test: `python3 -m unittest -v tests.vnext.test_inheritance_engine tests.vnext.test_scan_import_lifecycle` — PASS (55 tests).
- Browser Test: NOT_RUN; this is an inheritance/publication dependency contract and no UI code changed.
- Compatibility Check: compact read-set format is explicitly upgraded to v2; old v1 payloads fail closed rather than being interpreted as empty dependencies; legacy list-form read-set validation remains unchanged.
- Commit SHA: `f5d4cfb1e5d2a195feb4ffa967fe3c41f06f9007`
- Status: FIXED_VERIFIED
- Remaining Evidence: large production-like source-scope checkpoint size and validation timing require a fixed workload measurement; no production data was used.

### FOS-009

- Original Severity: P2
- Original Root Cause: `_remove_service_entry_locked` released identity cache bytes without removing the matching `_identity_index` entry, and `_identity` indexed a full key even when its cache entry was bypassed by a zero/oversize budget.
- Current Revalidation Result: CONFIRMED at the start of Phase 2; the LRU and exact-removal regressions failed because evicted identity keys remained in the auxiliary index.
- Root Owner: Runtime Reliability
- Changed Files: `app/code_detail/vnext_service.py`, `tests/vnext/test_identity_cache_index_budget.py`
- Changed Functions: `VNextCodeDetailService._remove_service_entry_locked`, `_identity`, `metrics`
- Regression Test: `test_identity_index_is_evicted_with_lru_entry`, `test_identity_entry_removal_cleans_exact_index_mapping` — PASS; index entries remain one-to-one with cached identity entries and cleanup is exact.
- Integration Test: `python3 -m unittest -v tests.vnext.test_identity_cache_index_budget` — PASS (2 tests).
- Browser Test: NOT_RUN; the invariant is internal cache ownership and no UI code changed.
- Compatibility Check: identity cache lookup still uses the existing base/full identity and freshness CAS; only stale auxiliary mappings are removed, and cache metrics are additive.
- Commit SHA: `815a9d186fd53de67d97f8228e99e006c9195e2a`
- Status: FIXED_VERIFIED
- Remaining Evidence: long-running RSS/key-cardinality workload under production-scale identity diversity remains pending; no memory-growth percentage is claimed.
