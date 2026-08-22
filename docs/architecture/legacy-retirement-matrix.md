# Legacy capability retirement matrix

This matrix is the ownership boundary for `MAINT-001`. `DELEGATE` means the
VNext path uses the canonical owner while the compatibility surface is kept
for a previous release or an explicit old input contract. It does not claim
that production usage is zero. `KEEP` is limited to rollback/previous-release
support. `RETIRE` is safe to remove only after the exact-SHA retirement audit
has accepted usage, contract, and rollback evidence.

| Capability | VNext authoritative owner | Compatibility surface | Current status | Contract evidence | Rollback path |
|---|---|---|---|---|---|
| HTTP server/runtime composition | `app/bootstrap.py`, `app/api/*` | `app/legacy_runtime.py` → `app/compat/legacy_runtime_impl.py` | KEEP (previous release) | runtime participation + HTTP contract | previous release boot |
| Auth and mutation policy | `app/api/auth.py` | legacy request adapter | DELEGATE | API security tests + config preflight | previous release endpoint |
| Jobs and recovery | `app/jobs/service.py`, scan-import recovery | legacy job dispatch | DELEGATE | job lifecycle and recovery audits | previous release job worker |
| Progress aggregation | `app/services/progress_service.py`, file-state repository | legacy progress handlers | DELEGATE | progress/API/browser contract tests | previous release report server |
| Export | `app/services/export_service.py` | legacy export handler | DELEGATE | export security/release identity tests | previous release export |
| Incremental analysis | `app/incremental/*`, `app/services/incremental_service.py` | `app/incremental/legacy.py` → compat implementation | DELEGATE | canonical incremental tests | previous release CLI |
| Inject / report binding | `app/inject/service.py`, `ProjectService` | legacy inject adapter | DELEGATE | report identity and path tests | original report + previous release |
| Static assets | `web/assets/*`, `web/templates/*` | root generated compatibility copies | DELEGATE | canonical ownership/asset parity audit | previous release assets |
| `inherit` CLI mutation | no VNext writer; explicit retirement exit 2 | `enhance_coverage.py inherit` | RETIRE | legacy CLI retirement test | previous release CLI |

Every row has one VNext business owner. Compatibility code may translate
parameters or keep a previous-release implementation, but it must not add a
second CURRENT pointer, analysis writer, job state machine, progress aggregate,
or report identity rule. The row can move from KEEP/DELEGATE to RETIRE only
with fresh usage telemetry, contract evidence, an exact candidate SHA, and a
verified rollback manifest.
