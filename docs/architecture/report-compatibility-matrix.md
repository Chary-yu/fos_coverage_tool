# Report compatibility matrix

`COMPAT-005` uses an explicit report contract instead of guessing which API a
static HTML file expects. New VNext reports carry:

- `api_contract_version`: `vnext-api-20260822.1`;
- the immutable release fields `version`, exact `commit_sha`, `build_id`,
  `asset_hash`, and `schema_version`;
- `asset_identity`, a compact database/cache token derived from the release
  commit, asset hash, and API contract digest.

The database `coverage_reports.asset_identity` column is retained for old
VNext schema compatibility. The full release/API metadata remains in the
serialized report payload and is never inferred from the current server HEAD.

| Report class | API/static owner | Allowed path | Action | Retirement evidence |
|---|---|---|---|---|
| `CANONICAL_VNEXT` | Current VNext API + `web/assets/*` | Candidate/current VNext | Serve directly | Release identity and asset hash |
| `VERSIONED_NONCURRENT` | Declared previous release | Previous release or an explicitly tested proxy | Preserve original and migrate only with a manifest | Inventory + Chromium sample |
| `UNVERSIONED_HISTORICAL` | Unknown legacy API/assets | Previous server only until classified | Do not silently re-inject or reinterpret | Inventory + owner decision |

Run the read-only inventory against each production/backup report root before
choosing a migration or proxy action:

```text
python scripts/diagnostics/report_compatibility_inventory.py \
  --root /path/to/report-root \
  --output .artifacts/vnext/report-compatibility-inventory.json
```

The repository does not contain the production report directory, so the
external inventory and real Chromium sample remain release-gate evidence, not
an implicit green result. Any re-injection must write a new directory and
retain the original report plus its inventory manifest for rollback.
