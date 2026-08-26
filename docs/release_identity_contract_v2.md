# Release Identity Contract v2

`app/release_identity.py` is the canonical release identity owner. A release identity contains `version`, exact `commit_sha`, `build_id`, `schema_version`, and a versioned static asset manifest. Each asset record binds its relative path, byte size, and full SHA-256; `asset_count` and `asset_manifest_hash` are derived from canonical JSON. `asset_hash` remains the compatibility alias for `asset_manifest_hash`. Missing mandatory assets fail the release build instead of being silently skipped. The build step writes `release_manifest.json`; runtime verification never rewrites a missing or drifting manifest.

`commit_sha` must equal the Candidate revision recorded by Evidence Manifest v2. Asset hashing covers the canonical `web/assets` sources and any explicitly retained compatibility assets. A mismatch in version, commit, asset hash or schema version is runtime drift and must fail closed.

Rollback evidence must identify the exact pre-cutover release identity and target database identity. A directory that merely looks previous is not an acceptable rollback target. Release performance A/B evidence is separate from same-run synthetic DOM benchmarking and must bind both source artifacts to exact baseline/candidate commits, workload hash and environment identity.
