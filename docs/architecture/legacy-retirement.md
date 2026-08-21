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

Until those conditions are proven, the audit reports `INCOMPLETE` instead of
calling the large legacy owners retired.
