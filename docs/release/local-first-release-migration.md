# FOS Local-First Release Contract

This document records the migration of the FOS production release path from
hosted release infrastructure to the local repository and an offline
operator. GitHub remains the source repository; it is not a release trust,
readiness, artifact, or evidence authority.

## Current contract

```text
PRODUCTION_RELEASE_AUTHORITY=OFFLINE_OPERATOR_ONLY
GITHUB_SOURCE_REPOSITORY_ONLY=YES
PROTECTED_BUILDER_LEGACY_VERIFY_ONLY=YES
PROTECTED_BUILDER_NEW_PUBLICATION_ALLOWED=NO
DUAL_PRODUCTION_AUTHORITY=NO
```

The canonical policy is in `app/release_trust_policy.py`. Current Candidate
build, preflight, publication, and upgrade paths use the policy entry point.
`protected_builder` parsing and verification remain available only for
historical evidence and rollback/audit inspection; a new protected-builder
Candidate cannot enter Current Production Publication.

## Dependency migration

| Former dependency | Local replacement | Current status |
| --- | --- | --- |
| GitHub Actions / hosted runner | `scripts/release/local_release_gate.py` and repository-local `unittest` targets | Implemented |
| Self-hosted production builder | `generate_flat_production_candidate_operator.py` plus the standalone offline runtime | Implemented; preparation-only |
| Workflow identity, run ID, runner identity, environment, and release secret | Immutable `release_input_manifest.json`, exact source/tooling Git bundles, and SHA256 binding | Implemented |
| GitHub artifact and external attestation | Local evidence files, `evidence_manifest.json`, `SHA256SUMS`, and Candidate integrity metadata | Implemented; external GitHub attestation is not required |
| Workflow status / GitHub API readiness gate | `scripts/diagnostics/production_release_authority.py` and local evidence validation | Implemented; no API call |
| CI browser fixture consumed by production | Transferred local-release evidence as a non-production input; real Candidate browser validation remains manual on VFOS | Implemented; local evidence never masquerades as a production gate |
| Python 3.6 and MariaDB 5.5 CI lane | `scripts/release/compatibility_surface.py` plus `scripts/release/local_compatibility_gate.py` with digest-pinned Docker images | Implemented; reports `PASS`, `NOT_APPLICABLE`, `DEFERRED_TO_TARGET_RUNTIME`, or `FAIL` |
| R8 exact-revision performance lane | Existing exact-revision producer/join tools run from a local clean checkout | Local replacement retained; GitHub workflow retired |
| Independent evidence platform | Offline evidence directory and detached hashes | Implemented |

The local gate does not contact GitHub, download Internet resources, access
VFOS, or mutate production. It removes hosted credentials from child
processes and records `PRODUCTION_MUTATION=NONE` in every produced evidence
record.

## Local release procedure

Run from a clean detached checkout at the exact source commit. The command
creates the source/tooling bundles, canonical release-input manifest,
standalone operator, targeted release test record, authority diagnostic,
security result, compatibility result, and detached evidence manifest:

```bash
SOURCE_SHA=$(git rev-parse HEAD)
python3 scripts/release/local_release_gate.py \
  --source-sha "$SOURCE_SHA" \
  --output ".artifacts/release/$SOURCE_SHA/SESSION_ID"
```

The gate output contains at least:

```text
source_identity.json
environment_identity.json
targeted_tests.json
compatibility_tests.json
release_regressions.json
operator_self_test.json
security_scan.json
release_input_manifest.json
release_input_manifest.sha256
evidence_manifest.json
SHA256SUMS
```

`release_input_manifest.json` is canonicalized and its SHA256 is embedded in
the new `offline_operator.txt`. The old R8-specific TXT operator is not a
valid operator for a new source revision.

## Compatibility behavior

`local_compatibility_gate.py` uses these fixed image references when the
corresponding surface is changed:

```text
python@sha256:f8652afaf88c25f0d22354d547d892591067aa4026a7fa9a6819df9f300af6fc
mariadb@sha256:8665c074af5a5fb7e04b9570fcf8551e9d82955182be50375d5013838d4f9137
```

The compatibility surface classifier decides whether each lane applies. A
MariaDB lane is `NOT_APPLICABLE` when no DB-sensitive production surface was
changed. A changed production-executed Python surface is checked with Python
3.6; if neither Docker nor a local Python 3.6 interpreter is available, it is
reported as `DEFERRED_TO_TARGET_RUNTIME`. A changed DB-sensitive surface is
similarly deferred when no disposable MariaDB 5.5 runtime is available. The
four-state result is explicit:

```text
PASS
NOT_APPLICABLE
DEFERRED_TO_TARGET_RUNTIME
FAIL
```

`DEFERRED_TO_TARGET_RUNTIME` permits isolated Candidate staging only. It sets
`READY_FOR_MANUAL_VFOS_COMPATIBILITY_PREFLIGHT=YES`, but publication and
cutover remain blocked until the target runtime preflight passes. A runtime
`FAIL` exits closed and permits no Candidate publication or production
mutation. Local release evidence never turns a deferred check into a pass.

## Production boundary

The generated operator is standalone, offline, self-verifying, and
preparation-only. It may reconstruct exact source/tooling checkouts, verify a
baseline and path/symlink safety, and prepare an isolated Candidate. It must
not publish `CURRENT`, change Nginx or systemd, access or mutate a database,
stop a service, or contact GitHub.

After local readiness, a human carries the operator and manifest into VFOS.
Only the human may perform the production Candidate build, exact validation
session, server gate, real browser gate, rollback rehearsal, `PRE_CUTOVER_READY`
decision, and final cutover. Local evidence is never a substitute for those
production facts.

## Workflow retirement and stale runs

The five former `.github/workflows/` files have been retired after the local
replacement contract and targeted regressions were added. The directory is
not a current release input. If a workflow file is reintroduced, the static
authority diagnostic reports `GITHUB_WORKFLOW_RESIDUE=YES` and strict
`LOCAL_FIRST_AUTHORITY_READY` becomes `NO`.

The stale workflow run `34938889492` is legacy infrastructure residue. It is
not queried, cancelled, or used as a release prerequisite. Even if it starts,
the current policy gives it no authority to create a Current Production
Candidate.
