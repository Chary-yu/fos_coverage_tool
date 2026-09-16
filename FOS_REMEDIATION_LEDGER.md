# FOS Audit Remediation Ledger

Audit baseline: `438c748772be96990e4a6b9678a6936e62b98681`

The current repository lineage was independently revalidated before any
behavior change. The named FOS Skill Suite is not installed in this workspace;
that capability gap is recorded in the final report rather than represented as
completed skill execution.

| Finding ID | Severity | Original root owner | Current revalidation | Status | Remaining evidence |
| --- | --- | --- | --- | --- | --- |
| FOS-001 | P1 | Change Review | CONFIRMED: `_resolve_records` shared `_operation_key` across ranges and the save planner created one same-file Block from the first range; repository links did not enforce line containment. | FIXED_VERIFIED | Legacy malformed historical ranges require separate evidence-bound repair. |
| FOS-002 | P1 | Change Review | CONFIRMED: successful inheritance creates a candidate LineLink without a candidate `AnalysisBlock`; confirm-only rolling inheritance loses the next-generation block identity. | BLOCKED | Remediation in progress; full external parser corpus remains pending. |
| FOS-003 | P1 | Runtime Reliability | CONFIRMED: generic `JobRepository.upsert` updates by `job_id` only and runner heartbeat can race terminal persistence. | BLOCKED | Remediation in progress; MariaDB concurrency evidence remains pending. |
| FOS-004 | P1 | Runtime Reliability | CONFIRMED structural gap / NEEDS_EXTERNAL_EVIDENCE for real dual-connection MariaDB ordering: analysis/review/publication paths lack a shared project-state lock boundary. | PRODUCTION_ACCEPTANCE_PENDING | Disposable MariaDB/production-like concurrency run must be executed outside this task. |
| FOS-005 | P2 | Change Review | CONFIRMED: affected-file path rebases every `coverage_file_state` row and validates a full authoritative fact aggregate inside the mutation transaction. | BLOCKED | Remediation in progress; fixed-hardware workload measurement remains pending. |
| FOS-006 | P2 | Change Review | CONFIRMED: `execute` and `executemany` share one `db_query_count`, while returned and affected rows are not separately named from server-examined rows. | BLOCKED | Remediation in progress; real driver/session statement count remains pending. |
| FOS-007 | P2 | Change Review | CONFIRMED: inheritance records all predecessor active relations even for unready repositories or missing candidate files, then publication replays the full predecessor set. | BLOCKED | Remediation in progress; large-scope checkpoint measurement remains pending. |
| FOS-008 | P1 | Change Review | CONFIRMED: orphan query considers only LineLink references and the candidate gate consumes the global count, ignoring historical Block/rejection references. | BLOCKED | Remediation in progress; historical production data acceptance remains pending. |
| FOS-009 | P2 | Runtime Reliability | CONFIRMED: LRU removal does not remove `_identity_index`; an oversize/disabled store can index a non-existent cache entry. | BLOCKED | Remediation in progress; long-running RSS workload remains pending. |

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
- Integration Test: `python3 -m unittest -v tests.vnext.test_analysis_domain` — PASS (11 tests).
- Browser Test: NOT_RUN; no UI code changed and browser acceptance is not required to prove the persistence invariant.
- Compatibility Check: legacy single-range/manual and rejection/undo domain tests pass; historical malformed data is not rewritten.
- Commit SHA: recorded after the focused commit below.
- Status: FIXED_VERIFIED
- Remaining Evidence: full API/browser multi-select corpus and any historical malformed-block repair remain outside offline source acceptance.
