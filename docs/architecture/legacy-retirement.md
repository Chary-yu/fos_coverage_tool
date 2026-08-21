# Legacy runtime retirement gate

The VNext runtime is canonical. `app/legacy_runtime.py` and
`app/incremental/legacy.py` are retained only for explicit compatibility
entrypoints and are currently classified as `TRANSITIONAL_LEGACY`.

They are not allowed to be imported by the VNext composition root. The root
CLI/import names remain compatibility shims so existing integrations can be
migrated without an abrupt break.

The final retirement gate is intentionally explicit:

```text
python scripts/diagnostics/legacy_retirement_audit.py --strict
```

Retirement requires all of the following to be evidenced in the same release
window:

1. No supported deployment selects `runtime_mode=legacy`.
2. Compatibility CLI/import tests pass from the shim surface.
3. No VNext module imports either transitional implementation.
4. Legacy usage telemetry is zero for the agreed deprecation window.
5. The release manifest records the removal commit and rollback plan.

The audit consumes optional machine evidence through:

```text
COVERAGE_LEGACY_USAGE_FILE
COVERAGE_LEGACY_COMPAT_TESTS_MANIFEST
COVERAGE_LEGACY_RETIREMENT_MANIFEST
```

Compatibility and retirement manifests must carry the current checkout's exact
`candidate_revision`; a stale artifact is rejected even when its status says
`PASSED`.

CI produces the compatibility-surface portion with:

```text
python scripts/diagnostics/legacy_compatibility_smoke.py \
  --output .artifacts/vnext/legacy-compatibility.json
```

That artifact proves the import surface and runs `--help` through both public
CLI shims (`enhance_coverage.py` and `coverage_check.py`) without starting a
server or opening a database. It does not prove that the large compatibility
implementations are unused or safe to delete.

It reports each condition separately. Missing compatibility/deprecation-window
or release evidence keeps the result `INCOMPLETE`; `--strict` exits non-zero.

The CI job also writes `legacy-retirement.json` by invoking:

```text
python scripts/diagnostics/legacy_retirement_audit.py \
  --output .artifacts/vnext/legacy-retirement.json
```

That artifact is bound to the checkout revision and records the host, command,
timestamps, transitional owners, and each retirement condition. It remains
`INCOMPLETE` until the compatibility manifest, zero-usage deprecation window,
and removal/rollback manifest are supplied for the same release revision.

Until those conditions are proven, the audit reports `INCOMPLETE` instead of
calling the large legacy owners retired.
