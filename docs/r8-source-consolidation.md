# R8 Source Consolidation

This repository state consolidates the production-release source fixes reviewed for the `vfoswind` upgrade path.

The release controller now preserves Candidate startup failure evidence before teardown, records validation process ownership before HTTP readiness, separates validation and teardown status, uses MariaDB 5.5-compatible scoped Candidate privilege cleanup with grant conservation, persists terminal upgrade states atomically, and keeps production evidence credential-safe.

Release-gate applicability is now classified from current Scan/report/repository identity at runtime. For an explicitly reviewed `legacy_migrated + LEGACY_STATIC + LEGACY_REPORT_COMPATIBLE` release with incomplete historical LCOV/repository identity, Path Mapping is recorded as `DEFERRED_LEGACY_IDENTITY_GAP` and the VNext report gate as `DEFERRED_LEGACY_REPORT_MODE`; neither deferment is represented as `PASSED`. Native/VNext identity gaps and reviewed-vs-runtime classification drift remain fail-closed.

The targeted R8 repair validation includes release-gate regression cases, production evidence evaluator tests, Candidate lifecycle/cleanup tests, Python 3.6 grammar checks, old-Git compatibility tests, and MariaDB compatibility tests. Production deployment remains a separate gated operation and is not implied by this source state.
