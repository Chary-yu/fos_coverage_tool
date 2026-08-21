# Legacy → VNext Migration Matrix

The authoritative mapping is [`migration_matrix.json`](migration_matrix.json). The source database is never treated as a disposable intermediate: legacy values, timestamps, job anomalies, path identity and unresolved identity facts are preserved as target provenance.

Migration runs only from a verified Legacy Source into an empty, separately identified VNext Target. Source/target database identity equality is a hard stop. The rehearsal must include semantic conservation, a second idempotent run, Analysis Domain backfill/orphan checks, and MariaDB 5.5 compatibility. Synthetic SQLite fixtures are useful for deterministic regression tests but cannot certify production migration.
