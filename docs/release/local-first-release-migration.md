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
| Python 3.6 and MariaDB 5.5 CI lane | `scripts/release/local_compatibility_gate.py` with digest-pinned Docker images | Implemented; must be verified before release readiness |
| R8 exact-revision performance lane | Existing exact-revision producer/join tools run from a local clean checkout | Local replacement retained; workflow retirement waits for verification |
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

`local_compatibility_gate.py` uses these fixed image references:

```text
python@sha256:f8652afaf88c25f0d22354d547d892591067aa4026a7fa9a6819df9f300af6fc
mariadb@sha256:8665c074af5a5fb7e04b9570fcf8551e9d82955182be50375d5013838d4f9137
```

The Python lane compiles/imports the release tooling under Python 3.6. The
MariaDB lane executes disposable grant/revoke/cleanup checks only. If Docker
or its daemon is unavailable, the result is explicitly:

```text
LOCAL_COMPATIBILITY_GATE=NOT_VERIFIED
PYTHON36_COMPAT=NOT_VERIFIED
MARIADB55_COMPAT=NOT_VERIFIED
```

That result is fail-closed and cannot produce release readiness.

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

The five existing `.github/workflows/` files are retained until the local
compatibility and other replacement checks are actually verified PASS. Their
presence is reported as legacy workflow residue and prevents strict
`LOCAL_FIRST_AUTHORITY_READY` until the retirement commit is safe. They are
not consulted for current trust or readiness.

The stale workflow run `34938889492` is legacy infrastructure residue. It is
not queried, cancelled, or used as a release prerequisite. Even if it starts,
the current policy gives it no authority to create a Current Production
Candidate.

