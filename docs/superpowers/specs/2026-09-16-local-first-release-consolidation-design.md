# FOS Local-First Release Consolidation Design

## Objective

Make the local repository and Codex/local tooling the sole non-production release authority. GitHub remains a source repository only. A production Candidate may be prepared only by an offline operator carrying an immutable release-input manifest; the operator cannot publish `CURRENT`, alter Nginx/systemd, touch a database, or stop a service.

## Trust model

`offline_operator` is the only value in `CURRENT_PRODUCTION_RELEASE_TRUST_MODES`. `protected_builder` remains a parseable historical trust mode and its verifier remains available for old evidence, but current Candidate build, preflight, publication, and cutover reject it with `PROTECTED_BUILDER_CURRENT_PUBLICATION_ALLOWED=NO`. This policy is centralized in `app/release_trust_policy.py`; callers do not select a production authority independently.

## Immutable release input

`scripts/release/release_input_manifest.py` validates and canonicalizes schema version 1. The manifest binds repository/source commit and tree, source bundle, tooling commit/tree/bundle, production host, baseline commit and all baseline evidence digests, project, candidate parent, and trust mode. Its canonical SHA256 is embedded in the generated operator and repeated in candidate/offline evidence. Any mismatch is a fail-closed error.

## Offline operator

`generate_flat_production_candidate_operator.py` validates exact source/tooling Git bundles and embeds the canonical manifest, its digest, and the runtime. `flat_candidate_operator_runtime.py` is generic: it verifies embedded inputs, reconstructs clean detached checkouts, validates the declared baseline, invokes the pinned builder, verifies the resulting Candidate and offline trust evidence, and emits immutable evidence. It obtains all release-specific values from the manifest. The old R8 generator remains only as a compatibility wrapper and no longer owns release constants.

## Local release gate

`scripts/release/local_release_gate.py` creates one offline evidence directory for one exact source SHA. It records source/environment identity, the selected release tests and orchestration regressions, static release-path security findings, authority policy, operator self-test, release-input manifest integrity, and compatibility evidence. It creates a clean exact-commit checkout/bundle from Git when the working directory contains unrelated untracked files, without touching those files. The gate exits non-zero unless every required local check is verified.

`scripts/release/compatibility_surface.py` first classifies whether the
changed source actually touches production-executed Python or DB-sensitive
surfaces. `scripts/release/local_compatibility_gate.py` then runs pinned
Python 3.6 and MariaDB 5.5.64 checks in Docker when those surfaces apply,
including candidate privilege revoke/conservation behavior and release-tooling
syntax/import checks. Docker image references are digest-pinned. Each lane
reports exactly `PASS`, `NOT_APPLICABLE`, `DEFERRED_TO_TARGET_RUNTIME`, or
`FAIL`. Missing local target runtimes produces a truthful deferred result:
isolated Candidate staging may continue, while publication and cutover remain
blocked pending the manual VFOS compatibility preflight.

## Readiness and evidence

`scripts/diagnostics/production_release_authority.py` is a read-only source/config diagnostic. Readiness consumes its result and never queries GitHub, workflow status, runner state, artifacts, attestations, or tokens. Validation fixtures remain explicitly non-publishable and are never accepted as production evidence. Local evidence is not a production gate; VFOS server/browser/rollback evidence remains a human-run prerequisite before cutover.

## Workflow retirement

The former workflow responsibilities are represented by local targeted tests,
local orchestration checks, the compatibility gate, and the immutable local
evidence manifest; the workflow files are retired. Historical parsers and
protected-builder verification code may retain GitHub vocabulary when they
only read old records; current production execution paths may not require it.

## Safety invariants

- No task action connects to or mutates VFOS, production databases, `CURRENT`, Nginx, systemd, or production processes.
- Candidate publication rejects validation fixtures and protected-builder trust.
- Source, tree, bundle, tooling, baseline, host, session, operator, Candidate, and evidence digest tampering fails closed.
- Local evidence records `PRODUCTION_MUTATION=NONE`; only a human may carry the operator to VFOS.
