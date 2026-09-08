# R8-SRC-023 Closure

- Severity: P1
- Root owner: Release Governance / source release build
- Symptom: Flat-to-immutable bootstrap copied the previous release `app/` bundle into scratch `CURRENT`; production Candidate construction then copied that previous `app/` into the Candidate before attempting to copy the pinned target application bundle, causing `production release already contains an application bundle`.
- Fix: `build_production_candidate_artifact.py::_copy_served_root()` excludes the previous release application bundle from the copied Served Root. The Candidate application is populated only from the pinned target source checkout via `copy_production_application_bundle()`.
- Regression: added a production-candidate test with an existing previous-release `app/` containing a stale file; the resulting Candidate must contain the target application entrypoint and must not preserve the stale previous-release application file.
- Targeted validation before commit: release-gate regressions 8/8 PASS; 29 targeted release tests PASS; Python 3.6 grammar PASS; `git diff --check` PASS.
