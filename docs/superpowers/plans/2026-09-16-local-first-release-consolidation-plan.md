# FOS Local-First Release Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate FOS release trust and non-production validation into deterministic offline repository tooling while preserving fail-closed production boundaries.

**Architecture:** A canonical release-trust policy permits only `offline_operator` for current production operations and delegates historical `protected_builder` verification to legacy-only readers. A signed-by-hash release-input manifest supplies all source/tooling/baseline identity to a generic standalone operator; a local release gate emits a complete, offline-verifiable evidence bundle and a Docker-based compatibility gate refuses to pass when its runtime is unavailable.

**Tech Stack:** Python 3.6-compatible standard library for release/runtime code, `unittest`, Git bundles, Bash standalone operators, Docker digest-pinned Python/MariaDB images, JSON/SHA256 evidence.

**Spec:** `docs/superpowers/specs/2026-09-16-local-first-release-consolidation-design.md`

## Global Constraints

- `PRODUCTION_RELEASE_AUTHORITY=OFFLINE_OPERATOR_ONLY`.
- `PROTECTED_BUILDER` remains legacy verification-only; new current publication is denied.
- No GitHub API, Actions, Runner, Environment, Secret, Artifact, Attestation, or workflow status is required for release readiness.
- No production host, production database, `CURRENT`, Nginx, systemd, service, permission, or production Operator/Cutover may be accessed or modified by Codex.
- Do not run full test suites; use only the release files and scripts listed by the user plus the smallest directly related regressions.
- Python 3.6-compatible release code must use only supported standard-library APIs and fixed Docker image digests.
- Preserve unrelated pre-existing untracked files; do not add them to commits.

### Task 1: Canonical production trust policy

**Files:**
- Create: `app/release_trust_policy.py`
- Modify: `app/candidate_artifact.py`
- Modify: `app/release_publication.py`
- Modify: `scripts/release/build_production_candidate_artifact.py`
- Modify: `scripts/release/build_flat_production_candidate_artifact.py`
- Test: `tests/release/test_offline_operator_trust.py`
- Test: `tests/release/test_immutable_release_publication.py`
- Test: `tests/release/test_production_candidate_build.py`

**Interfaces:**
- Produces `CURRENT_PRODUCTION_RELEASE_TRUST_MODE`, `CURRENT_PRODUCTION_RELEASE_TRUST_MODES`, `PRODUCTION_RELEASE_AUTHORITY`, `assert_current_production_trust_mode()`, and `verify_current_production_candidate_trust()`.
- Keeps `RELEASE_TRUST_MODE_PROTECTED_BUILDER`, `verify_trusted_build_policy()`, and receipt verification available for legacy evidence.

- [ ] Write failing tests proving the default current mode is offline, a protected Candidate cannot enter `ImmutableReleasePublisher.prepare()`, and an offline Candidate still verifies.
- [ ] Run only the three named release test files and confirm the new assertions fail for the current default/protected path.
- [ ] Implement the policy module and route Candidate build/publication trust decisions through it; keep legacy verifier functions callable but never current-authoritative.
- [ ] Run those three test files and confirm pass, including existing path/symlink/tamper cases.
- [ ] Commit with `refactor(release): make offline operator the sole production trust authority`.

### Task 2: Offline-only production build and publication inputs

**Files:**
- Modify: `scripts/release/build_production_candidate_artifact.py`
- Modify: `scripts/release/build_flat_production_candidate_artifact.py`
- Modify: `scripts/release/publish_release.py`
- Modify: `scripts/upgrade/run_upgrade.py`
- Test: `tests/release/test_production_candidate_build.py`
- Test: `tests/release/test_immutable_release_publication.py`
- Test: `tests/release/test_upgrade_manifest.py`

**Interfaces:**
- Current production build/publication defaults to offline trust and rejects explicit protected mode with the stable machine-readable denial.
- Legacy protected evidence can still be parsed/verified through the existing verification helpers.

- [ ] Add failing tests for protected build/publication rejection and offline CLI/default acceptance.
- [ ] Run only the affected test methods.
- [ ] Remove current-path requirements for workflow identity/SHA, receipt, attestation, and GitHub metadata; require offline evidence and exact source/baseline/session inputs instead.
- [ ] Run the affected test files and verify fail-closed identity/fixture cases remain green.
- [ ] Commit with `refactor(release): remove github trust from production build and publication`.

### Task 3: Immutable release-input manifest and generic operator

**Files:**
- Create: `scripts/release/release_input_manifest.py`
- Create: `docs/contracts/release_input_manifest_v1.schema.json`
- Create: `scripts/release/generate_flat_production_candidate_operator.py`
- Modify: `scripts/release/generate_r8_flat_production_candidate_operator.py`
- Modify: `scripts/release/flat_candidate_operator_runtime.py`
- Modify: `scripts/release/build_flat_production_candidate_artifact.py`
- Modify: `scripts/release/build_production_candidate_artifact.py`
- Test: `tests/release/test_flat_production_candidate_operator.py`
- Test: `tests/release/test_offline_operator_trust.py`

**Interfaces:**
- `load_release_input_manifest()`, `validate_release_input_manifest()`, `release_input_manifest_sha256()`, and `write_release_input_manifest()` provide canonical manifest handling.
- The generator accepts `--release-input-manifest`, `--source-bundle`, `--tooling-bundle`, and `--output`; the runtime receives only embedded manifest/bundle digests and derives all release-specific values from the manifest.

- [ ] Write failing tests for manifest canonical hashing, all identity tampering cases, generic generator output, Bash syntax, and operator self-test without network variables.
- [ ] Run only the new/affected test methods and confirm they fail before implementation.
- [ ] Implement validation, bundle pinning, manifest embedding, generic flat/current runtime dispatch, evidence emission, and a thin R8 wrapper.
- [ ] Run the operator-focused files and the generated operator self-test; confirm no production mutation marker is emitted.
- [ ] Commit with `refactor(release): generalize offline production operator inputs`.

### Task 4: Deterministic local release gate

**Files:**
- Create: `scripts/release/local_release_gate.py`
- Create: `scripts/diagnostics/production_release_authority.py`
- Modify: `scripts/diagnostics/release_readiness.py`
- Modify: `scripts/diagnostics/production_ready_evidence_join.py`
- Modify: `scripts/release/fos_r8_conductor.py`
- Modify: `scripts/release/fos_r8_conductor_base.py`
- Test: `tests/release/test_release_readiness.py`
- Test: `tests/release/test_production_ready_evidence_join.py`
- Test: `tests/release/test_fos_r8_orchestration.py`

**Interfaces:**
- `local_release_gate.py --source-sha SHA --output DIR` emits the named evidence files, `evidence_manifest.json`, and `SHA256SUMS`, and exits nonzero if any required local check is unverified.
- The authority diagnostic emits the exact `PRODUCTION_*`/`GITHUB_*` fields from the user contract without network access.

- [ ] Write failing tests for GitHub-unavailable/no-runner local gate behavior, authority output, evidence tamper rejection, and readiness not depending on workflow/API status.
- [ ] Run only those new/affected tests and verify red status.
- [ ] Implement source/environment identity, targeted test runner, orchestration checks, operator self-test, authority check, release evidence manifest, and fail-closed readiness integration.
- [ ] Run the targeted local gate tests and verify evidence hashes detect JSON tampering.
- [ ] Commit with `feat(release): add deterministic local release gate`.

### Task 5: Local Python 3.6/MariaDB 5.5 compatibility gate

**Files:**
- Create: `scripts/release/local_compatibility_gate.py`
- Modify: `scripts/diagnostics/mariadb55_candidate_grant_cleanup.py`
- Test: `tests/release/test_airgapped_release_validation.py`
- Test: `tests/release/test_airgapped_upgrade_conductor.py`
- Test: `tests/database/test_mariadb_compatibility_contract.py`

**Interfaces:**
- `local_compatibility_gate.py` uses digest-pinned images and emits `compatibility_tests.json`; missing Docker yields `LOCAL_COMPATIBILITY_GATE=NOT_VERIFIED` and nonzero status.
- Evidence contains explicit `PYTHON36_COMPAT` and `MARIADB55_COMPAT` values and never fabricates PASS.

- [ ] Write failing tests for missing Docker and fixed image digest enforcement.
- [ ] Run the compatibility-specific tests and confirm red status.
- [ ] Implement Python syntax/import and disposable MariaDB grant/revoke commands with no production paths or credentials in evidence.
- [ ] Run the compatibility tests; record `NOT_VERIFIED` if Docker is unavailable.
- [ ] Commit with `feat(release): move production compatibility validation local`.

### Task 6: Local-first/no-GitHub regression coverage

**Files:**
- Modify: `tests/release/test_offline_operator_trust.py`
- Modify: `tests/release/test_flat_production_candidate_operator.py`
- Modify: `tests/release/test_production_candidate_build.py`
- Modify: `tests/release/test_immutable_release_publication.py`
- Modify: `tests/release/test_release_readiness.py`
- Modify: `tests/release/test_production_ready_evidence_join.py`
- Modify: `tests/release/test_upgrade_manifest.py`
- Modify: `tests/release/test_fos_r8_orchestration.py`
- Create: `tests/release/test_local_first_release_gate.py`

**Interfaces:**
- Tests cover T1–T10: no GitHub/tokens/runner, protected rejection/legacy verification, all manifest tampering, fixture non-publishability, no production mutation, evidence integrity, and compatibility outcomes.

- [ ] Add one behavior-focused failing test per missing regression, using controlled temporary Git/bundle/evidence fixtures.
- [ ] Run each new test individually to confirm the expected red failure.
- [ ] Implement only the missing behavior; avoid source-text-only assertions except for static policy diagnostics.
- [ ] Run the full directly-related release test collection once and record exact counts.
- [ ] Commit with `test(release): add local-first and no-github regressions`.

### Task 7: Retire workflows after local capability verification

**Files:**
- Delete: `.github/workflows/ci.yml`
- Delete: `.github/workflows/trusted-candidate-builder.yml`
- Delete: `.github/workflows/trusted-production-candidate-builder.yml`
- Delete: `.github/workflows/r8-production-compat-regression.yml`
- Delete: `.github/workflows/r8-release-performance.yml`
- Modify: `scripts/validate_release_orchestration.py`
- Test: `tests/release/test_fos_r8_orchestration.py`

**Interfaces:**
- No workflow file remains; local gate and compatibility gate are the documented replacement for each release-path capability.

- [ ] Run local authority/orchestration tests and the local gate first; do not delete workflows until all repository-local checks that can execute pass.
- [ ] Remove workflow-only runtime assumptions from the static orchestration validator and preserve historical evidence parsing.
- [ ] Run only workflow-retirement tests and source/config checks.
- [ ] Commit with `ci(release): retire github actions workflows`.

### Task 8: Document and final verify

**Files:**
- Create: `docs/release/local-first-release-migration.md`
- Modify: `README.md` only if the current release instructions require correction
- Test: targeted release collection from Task 6

**Interfaces:**
- Migration document maps every retired GitHub dependency to the local replacement, documents compatibility handling and evidence paths, and states the manual VFOS boundary.

- [ ] Write failing documentation/policy diagnostic tests only where a machine-readable contract is missing.
- [ ] Implement the migration record and final operator/gate instructions.
- [ ] Run `git diff --check`, all directly related targeted tests, local authority/gate checks, and generated operator self-test.
- [ ] Record final SHA256/size and the minimum remaining blocker if Docker compatibility is not verifiable.
- [ ] Commit with `docs(release): document local-first production release contract`.

## Final verification commands

Run only after all phase commits:

```bash
python3 -m unittest \
  tests.release.test_offline_operator_trust \
  tests.release.test_flat_production_candidate_operator \
  tests.release.test_production_candidate_build \
  tests.release.test_immutable_release_publication \
  tests.release.test_fos_r8_orchestration \
  tests.release.test_release_readiness \
  tests.release.test_production_ready_evidence_join \
  tests.release.test_upgrade_manifest \
  tests.release.test_local_first_release_gate
python3 scripts/validate_release_orchestration.py --repo-root .
python3 scripts/validate_release_orchestration_regressions.py --repo-root .
git diff --check
```

The final report must set `READY_FOR_MANUAL_VFOS_CANDIDATE_BUILD=NO` whenever Docker cannot verify both compatibility lanes or any other required gate is not PASS. No final report may claim production access or production mutation.
