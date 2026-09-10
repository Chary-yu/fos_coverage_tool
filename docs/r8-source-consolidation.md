# R8 Source Consolidation

This repository state consolidates the production-release source fixes reviewed for the `vfoswind` upgrade path.

The release controller now preserves Candidate startup failure evidence before teardown, records validation process ownership before HTTP readiness, separates validation and teardown status, uses MariaDB 5.5-compatible scoped Candidate privilege cleanup with grant conservation, persists terminal upgrade states atomically, and keeps production evidence credential-safe.

Release-gate applicability is now classified from current Scan/report/repository identity at runtime. For an explicitly reviewed `legacy_migrated + LEGACY_STATIC + LEGACY_REPORT_COMPATIBLE` release with incomplete historical LCOV/repository identity, Path Mapping is recorded as `DEFERRED_LEGACY_IDENTITY_GAP` and the VNext report gate as `DEFERRED_LEGACY_REPORT_MODE`; neither deferment is represented as `PASSED`. Native/VNext identity gaps and reviewed-vs-runtime classification drift remain fail-closed.

R8-SRC-022 closes the pre-runtime-v3 report schema compatibility gap. When `coverage_reports.report_mode` is absent, the release-gate classifier inspects the existing report identity tuple instead of querying a non-existent column; incomplete historical rows remain `LEGACY_STATIC`, while only an already-complete VNext identity tuple may classify as `VNEXT_ARTIFACT_READY`. The classification source and `report_mode` column presence are emitted into release evidence.

The active `r8-airgapped-release-consolidation` line adds a production-safe air-gapped validation path. A real operator Chrome/Edge session observes the immutable Candidate through the authenticated Candidate Gateway, while independently measured exact-revision Chromium performance artifacts are produced outside `vfoswind` and transferred in with SHA256 integrity evidence. The production host joins these artifacts with Python 3.6-compatible code; it does not require public-network access, Chromium, Playwright, npm, or Node.js.

The exact-revision performance contract uses the same immutable benchmark harness against separate clean Git checkouts of the production baseline and Candidate. Each source artifact binds its exact commit/tree and production browser asset hashes and is classified as `release_performance_revision` / `single_revision`. Only the later A/B combiner may produce `release_performance_ab`; same-run synthetic DOM comparisons remain ineligible for release evidence.

The `vfoswind` production example is bound to the observed application database principal `coverage_user@localhost`. Air-gapped browser/auth/performance evidence for one validation attempt shares the immutable `publish_root/validation_evidence/{attempt_id}` namespace. Historical legacy report identity gaps retain their reviewed DEFERRED semantics and are never promoted to PASS.

The targeted R8 repair validation includes release-gate regression cases, production evidence evaluator tests, Candidate lifecycle/cleanup tests, Python 3.6 grammar checks, old-Git compatibility tests, MariaDB compatibility tests, air-gapped evidence-join tests, and exact-revision browser-performance evidence generation. Production deployment remains a separate gated operation and is not implied by this source state.

Repository branch policy during consolidation: `r8-airgapped-release-consolidation` is an active temporary source branch. It must not be described as a production release or merged/deleted until the final R8 source SHA/tree are locked and the directly relevant source/CI acceptance evidence is green. Branch cleanup is a post-consolidation repository-maintenance action, not release evidence.
