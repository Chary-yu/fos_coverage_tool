# Deferred Compatibility Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify release compatibility surfaces truthfully, permit isolated Candidate staging when Python 3.6 verification is deferred, and remove the retired GitHub Workflow infrastructure without permitting publication or cutover.

**Architecture:** `scripts/release/compatibility_surface.py` compares an exact source commit with the declared baseline and classifies production-executed Python and DB-sensitive changes. `local_compatibility_gate.py` consumes that classification and returns only `PASS`, `NOT_APPLICABLE`, `DEFERRED_TO_TARGET_RUNTIME`, or `FAIL`; the local release gate treats deferred compatibility as staging-only. The standalone Operator performs a real Python 3.6 preflight before any Candidate preparation, while readiness and publication retain independent fail-closed boundaries.

**Tech Stack:** Python 3.6-compatible standard library, Git exact-SHA inspection, Bash standalone Operator, JSON evidence manifests, `unittest` targeted regressions.

**Spec:** User-requested FOS local-first compatibility refinement in the current conversation, building on `docs/superpowers/specs/2026-09-16-local-first-release-consolidation-design.md`.

## Global Constraints

- Production is never accessed or modified: `PRODUCTION_ACCESSED=NO`, `PRODUCTION_MODIFIED=NO`.
- No full test suite, CI matrix, GitHub API, GitHub token, workflow status, runner, artifact, or attestation is used.
- Exact source commit/tree, manifest, bundle, baseline, host, and session identities remain fail-closed.
- `DEFERRED_TO_TARGET_RUNTIME` may enter isolated Candidate staging only; it may not authorize publication or cutover.
- MariaDB 5.5 compatibility is `NOT_APPLICABLE` when no DB-sensitive surface changed.
- Missing Python 3.6 is `DEFERRED_TO_TARGET_RUNTIME` only when production-executed Python changed.
- Any real compatibility failure exits immediately and emits no publication or production mutation.
- Existing user untracked files are preserved and never staged implicitly.

---

### Task 1: Add compatibility surface classifier

**Files:**
- Create: `scripts/release/compatibility_surface.py`
- Test: `tests/release/test_compatibility_surface.py`

**Interfaces:**
- Produces `classify_compatibility_surface(repo_root, source_sha, baseline_sha)` returning a JSON-safe dictionary with exact changed paths, production Python paths, DB-sensitive paths, and per-surface `applicable`/`reason` values.
- Consumes Git only through exact `diff --name-status`/`diff` commands in the local repository; it never contacts a network or production.

- [ ] **Step 1: Write failing classifier tests**

  Cover a changed `app/release_trust_policy.py` as production Python, a release-only documentation change as no Python surface, a non-DB release-tooling change as no MariaDB surface, and an added SQL/`app/db/` change as DB-sensitive.

- [ ] **Step 2: Run only the classifier tests and verify the expected RED failures**

  Run `python3 -m unittest tests.release.test_compatibility_surface`; expect import or missing-function failures before implementation.

- [ ] **Step 3: Implement the exact-SHA classifier**

  Use deterministic sorted Git paths, explicit production Python path rules, and DB-sensitive path/content rules. Reject malformed or missing exact SHAs and Git failures instead of guessing.

- [ ] **Step 4: Run the classifier tests and verify GREEN**

  Run `python3 -m unittest tests.release.test_compatibility_surface`; all classifier cases must pass.

- [ ] **Step 5: Commit**

  ```bash
  git add scripts/release/compatibility_surface.py tests/release/test_compatibility_surface.py
  git commit -m "feat(release): classify compatibility surfaces"
  ```

### Task 2: Implement four-state local compatibility gate

**Files:**
- Modify: `scripts/release/local_compatibility_gate.py`
- Modify: `scripts/release/local_release_gate.py`
- Test: `tests/release/test_local_compatibility_gate.py`
- Test: `tests/release/test_local_first_release.py`

**Interfaces:**
- `run_gate(repo_root, output_path, source_sha=None, baseline_sha=None)` returns per-surface statuses from the four-value set.
- `compatibility_tests.json` includes `compatibility_surface`, `PYTHON36_COMPAT`, `MARIADB55_COMPAT`, and an overall status that is `PASS` when every applicable check is PASS and every non-applicable/deferred check is explicitly allowed for staging.
- `local_release_gate.py` records `compatibility_scope=isolated_candidate_staging` and `publication_allowed=false`/`cutover_allowed=false` whenever any surface is deferred.

- [ ] **Step 1: Write failing four-state tests**

  Assert MariaDB is `NOT_APPLICABLE` without DB-sensitive changes, Python is `DEFERRED_TO_TARGET_RUNTIME` for changed production Python without Docker/Python 3.6, and a mocked real failure is `FAIL` rather than deferred. Assert deferred compatibility permits staging but not publication/cutover.

- [ ] **Step 2: Run only these tests and verify RED**

  Run `python3 -m unittest tests.release.test_local_compatibility_gate tests.release.test_local_first_release`; confirm the new assertions fail against the old PASS/NOT_VERIFIED-only implementation.

- [ ] **Step 3: Implement status mapping and evidence**

  Preserve digest-pinned Docker checks when applicable, return `NOT_APPLICABLE` without starting a database, return `DEFERRED_TO_TARGET_RUNTIME` only for missing required target runtimes, and keep actual command failures as `FAIL`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

  Re-run the two test files and inspect the machine-readable status fields.

- [ ] **Step 5: Commit**

  ```bash
  git add scripts/release/local_compatibility_gate.py scripts/release/local_release_gate.py tests/release/test_local_compatibility_gate.py tests/release/test_local_first_release.py
  git commit -m "feat(release): defer unavailable target compatibility"
  ```

### Task 3: Add readiness and publication scope boundaries

**Files:**
- Modify: `scripts/diagnostics/release_readiness.py`
- Modify: `scripts/upgrade/run_upgrade.py`
- Modify: `app/release_publication.py`
- Test: `tests/release/test_release_readiness.py`
- Test: `tests/release/test_upgrade_manifest.py`
- Test: `tests/release/test_immutable_release_publication.py`

**Interfaces:**
- Readiness output includes `READY_FOR_MANUAL_VFOS_COMPATIBILITY_PREFLIGHT`.
- Deferred local evidence can make that field `YES` and can support isolated staging, but any publication or cutover preflight with deferred compatibility fails with a stable target-runtime error.
- `ImmutableReleasePublisher` rejects deferred compatibility before creating a current production publication.

- [ ] **Step 1: Write failing readiness/scope tests**

  Assert readiness emits `READY_FOR_MANUAL_VFOS_COMPATIBILITY_PREFLIGHT=YES` for an otherwise valid local gate with deferred Python, and assert publication/cutover rejects the same evidence with `TARGET_RUNTIME_COMPATIBILITY_REQUIRED_BEFORE_PUBLICATION`.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run the three listed test files and confirm the new field and rejection are absent.

- [ ] **Step 3: Implement explicit staging-only semantics**

  Thread local compatibility evidence into readiness, keep production browser/server/DB gates distinct, and enforce the deferred prohibition before any publication mutation.

- [ ] **Step 4: Run the focused tests and verify GREEN**

  Re-run the three test files; no production path is invoked.

- [ ] **Step 5: Commit**

  ```bash
  git add scripts/diagnostics/release_readiness.py scripts/upgrade/run_upgrade.py app/release_publication.py tests/release/test_release_readiness.py tests/release/test_upgrade_manifest.py tests/release/test_immutable_release_publication.py
  git commit -m "feat(release): separate deferred staging from publication"
  ```

### Task 4: Put a real Python 3.6 gate at the Operator entry point

**Files:**
- Modify: `scripts/release/generate_flat_production_candidate_operator.py`
- Modify: `scripts/release/flat_candidate_operator_runtime.py`
- Test: `tests/release/test_flat_production_candidate_operator.py`
- Test: `tests/release/test_release_input_manifest.py`

**Interfaces:**
- Generated Operator build mode runs an embedded shell `python3.6` version check before extracting or constructing Candidate state.
- A failed check exits nonzero, emits `PYTHON36_COMPAT=FAIL`, `PRODUCTION_MUTATION=NONE`, and does not create publication state.
- `--self-test` remains a local structural/self-integrity check and does not masquerade as target-runtime compatibility evidence; target VFOS build mode is the real compatibility gate.

- [ ] **Step 1: Write failing Operator tests**

  Use a controlled `FOS_OPERATOR_PYTHON` executable that reports Python 3.5 or 3.14 and assert build mode exits before Candidate state creation; assert a 3.6-reporting executable reaches the embedded runtime path.

- [ ] **Step 2: Run the focused Operator tests and verify RED**

  Run `python3 -m unittest tests.release.test_flat_production_candidate_operator tests.release.test_release_input_manifest`; confirm the old wrapper accepts non-3.6 Python.

- [ ] **Step 3: Implement the entry-point gate**

  Add a shell-safe version probe before payload extraction/build preparation, require major/minor `3.6`, and preserve the preparation-only contract. Do not add network, publication, CURRENT, Nginx, systemd, or database operations.

- [ ] **Step 4: Run the focused tests and verify GREEN**

  Re-run the two test files and execute `bash -n` on a generated Operator.

- [ ] **Step 5: Commit**

  ```bash
  git add scripts/release/generate_flat_production_candidate_operator.py scripts/release/flat_candidate_operator_runtime.py tests/release/test_flat_production_candidate_operator.py tests/release/test_release_input_manifest.py
  git commit -m "feat(release): gate operator on Python 3.6"
  ```

### Task 5: Retire obsolete GitHub Workflows and update current contracts

**Files:**
- Delete: `.github/workflows/ci.yml`
- Delete: `.github/workflows/trusted-candidate-builder.yml`
- Delete: `.github/workflows/trusted-production-candidate-builder.yml`
- Delete: `.github/workflows/r8-production-compat-regression.yml`
- Delete: `.github/workflows/r8-release-performance.yml`
- Modify: `scripts/diagnostics/production_release_authority.py`
- Modify: `scripts/validate_release_orchestration.py`
- Modify: `tests/release/test_release_governance_tools.py`
- Modify: `tests/release/test_local_first_release.py`
- Modify: `docs/release/local-first-release-migration.md`

**Interfaces:**
- Authority diagnostic reports no workflow residue and `LOCAL_FIRST_AUTHORITY_READY=YES` after deletion.
- Historical GitHub provenance parsers remain available for verification only; no current readiness or publication path requires the deleted files.

- [ ] **Step 1: Write failing retirement tests**

  Assert the five paths are absent, the authority diagnostic reports no residue, and the governance tests inspect local replacement contracts instead of opening deleted files.

- [ ] **Step 2: Run the focused retirement tests and verify RED**

  Run `python3 -m unittest tests.release.test_local_first_release tests.release.test_release_governance_tools`; expect failures while the old files and assertions remain.

- [ ] **Step 3: Delete only the five workflow files and update current tests/docs**

  Remove workflow residue, preserve legacy parser code and historical documentation, and make the validator assert local gates rather than hosted workflow existence.

- [ ] **Step 4: Run the focused retirement tests and verify GREEN**

  Re-run the two test files and `python3 scripts/validate_release_orchestration.py --repo-root "$PWD"`.

- [ ] **Step 5: Commit**

  ```bash
  git add -u .github/workflows scripts/diagnostics/production_release_authority.py scripts/validate_release_orchestration.py tests/release/test_release_governance_tools.py tests/release/test_local_first_release.py docs/release/local-first-release-migration.md
  git commit -m "ci(release): retire github actions workflows"
  ```

### Task 6: Regenerate final local evidence and Operator

**Files:**
- Create outside the repository: `.artifacts/release/<FINAL_SOURCE_SHA>/<SESSION_ID>/` or an equivalent `/tmp` evidence directory.
- Test: all directly related release test modules only.

**Interfaces:**
- Final evidence binds `FINAL_MAIN_SHA`, `FINAL_TREE_SHA`, source/tooling bundles, release input manifest, compatibility surface, four-state compatibility results, Operator self-test, and `SHA256SUMS`.
- Final Operator is standalone, preparation-only, Python 3.6-gated, and hash-identified.

- [ ] **Step 1: Lock the final exact source identity**

  Record `git rev-parse HEAD`, `git rev-parse HEAD^{tree}`, and a clean tracked worktree check without touching existing untracked files.

- [ ] **Step 2: Run the final directly related targeted test set**

  Run only the release modules changed or directly affected by this plan, including classifier, compatibility gate, local-first, Operator, publication, readiness, orchestration, and upgrade manifest tests.

- [ ] **Step 3: Run the final local release gate**

  Supply the exact production baseline SHA when available. Accept `NOT_APPLICABLE`/`DEFERRED_TO_TARGET_RUNTIME` only as staging-scoped outcomes; reject `FAIL`.

- [ ] **Step 4: Independently verify final artifacts**

  Run `bash -n`, `bash <operator> --self-test`, `sha256sum -c SHA256SUMS`, and the repository evidence-manifest verifier. Confirm no production access or mutation.

- [ ] **Step 5: Commit final documentation or metadata only if changed**

  ```bash
  git diff --check
  git status --short
  ```

