"""Single owner for rebuilding and publishing FileState readiness.

``coverage_file_state`` is a derived projection of the canonical line and
analysis domain.  This service is deliberately the only layer that may move
``coverage_project_state.file_state_version`` to a ready version.
"""

from app.db.repositories import FileStateRepository, ProjectStateRepository
from app.db.transaction import transaction


SUMMARY_FIELDS = (
    "file_count", "total_lines", "total_uncovered", "filled_total",
    "draft_total", "confirmed_total", "pending_total",
    "ordinary_pending_total", "inherited_pending_total",
    "manual_draft_pending_total",
)


class FileStateReadyGateError(ValueError):
    """Raised when a rebuilt projection cannot be marked ready."""

    def __init__(self, gate):
        self.gate = gate or {}
        ValueError.__init__(self, "FILE_STATE_READY_GATE_FAILED")


class FileStateService(object):
    """Rebuild, reconcile and publish FileState in one explicit gate."""

    def __init__(self, file_state_repo=None, state_repo=None):
        self.file_states = file_state_repo or FileStateRepository()
        self.states = state_repo or ProjectStateRepository()

    def validate_rebuilt(self, connection, project_id, scan_id, data_version):
        state = self.states.get(connection, int(project_id)) or {}
        expected_version = int(data_version)
        if int(state.get("data_version") or 0) != expected_version:
            return {
                "status": "FAILED", "reason": "DATA_VERSION_CHANGED",
                "project_id": int(project_id), "scan_id": int(scan_id),
                "data_version": expected_version,
            }
        completeness = self.file_states.file_state_completeness(
            connection, int(scan_id), expected_version
        )
        conservation = self.file_states.pending_conservation(
            connection, int(scan_id)
        )
        derived = self.file_states.scan_aggregate(connection, int(scan_id)) or {}
        authoritative = self.file_states.scan_summary_from_facts(
            connection, int(scan_id)
        ) or {}
        mismatches = {}
        for field in SUMMARY_FIELDS:
            if int(derived.get(field) or 0) != int(authoritative.get(field) or 0):
                mismatches[field] = {
                    "derived": int(derived.get(field) or 0),
                    "authoritative": int(authoritative.get(field) or 0),
                }
        gate = {
            "status": "PASSED" if (
                completeness.get("status") == "PASSED" and
                conservation.get("status") == "PASSED" and
                not mismatches
            ) else "FAILED",
            "project_id": int(project_id), "scan_id": int(scan_id),
            "data_version": expected_version,
            "completeness": completeness,
            "pending_conservation": conservation,
            "reconciliation": {
                "status": "PASSED" if not mismatches else "FAILED",
                "mismatches": mismatches,
            },
        }
        if gate["status"] != "PASSED":
            if completeness.get("status") != "PASSED":
                gate["reason"] = "FILE_STATE_INCOMPLETE"
            elif conservation.get("status") != "PASSED":
                gate["reason"] = "PENDING_CONSERVATION_FAILED"
            else:
                gate["reason"] = "FILE_STATE_RECONCILIATION_FAILED"
        else:
            gate["reason"] = "READY"
        return gate

    def rebuild_validate_and_mark_ready_in_transaction(
            self, connection, project_id, scan_id, data_version,
            affected_file_ids=None):
        """Run the complete gate inside a caller-owned transaction."""
        project_id = int(project_id)
        scan_id = int(scan_id)
        data_version = int(data_version)
        # Invalidate before touching the projection.  If a caller catches the
        # gate error and commits its surrounding transaction, the state still
        # advertises that the projection is stale.
        self.states.invalidate(connection, project_id)
        if affected_file_ids is None:
            self.file_states.rebuild_scan(
                connection, scan_id, data_version, None
            )
        else:
            # The mutation boundary supplies the exact files whose facts
            # changed. Rebase unchanged rows to the new version, then
            # recompute only that bounded file set. The complete Ready gate
            # below remains mandatory, so this is a performance optimization
            # rather than a weaker correctness condition.
            self.file_states.rebase_scan_version(
                connection, scan_id, data_version
            )
            self.file_states.rebuild_scan(
                connection, scan_id, data_version, None,
                file_ids=affected_file_ids,
            )
        gate = self.validate_rebuilt(
            connection, project_id, scan_id, data_version
        )
        if gate.get("status") != "PASSED":
            raise FileStateReadyGateError(gate)
        ready = self.states.mark_ready(connection, project_id, data_version)
        # ``mark_ready`` is guarded by the authoritative data_version in SQL,
        # but a concurrent advance can still occur between validation and the
        # update.  Never report a successful publication for that race: the
        # caller must retry against the new version and the wrapper will leave
        # the projection stale.
        if (
                not ready or
                int(ready.get("data_version") or 0) != data_version or
                int(ready.get("file_state_version") or 0) != data_version):
            raise FileStateReadyGateError({
                "status": "FAILED", "reason": "DATA_VERSION_CHANGED",
                "project_id": project_id, "scan_id": scan_id,
                "data_version": data_version,
            })
        return ready

    def rebuild_validate_and_mark_ready(self, connection, project_id, scan_id,
                                        data_version):
        """Standalone transaction wrapper for jobs and upgrade phases."""
        try:
            with transaction(connection) as conn:
                return self.rebuild_validate_and_mark_ready_in_transaction(
                    conn, project_id, scan_id, data_version
                )
        except Exception:
            # The failed rebuild transaction is rolled back.  Persist stale
            # state separately so a pre-existing false-ready marker cannot
            # survive *any* rebuild/validation failure, including a database
            # or repository exception that occurs before a structured gate
            # result can be returned.  Preserve the original exception: a
            # best-effort invalidation must never hide the actual failure.
            try:
                with transaction(connection) as conn:
                    self.states.invalidate(conn, int(project_id))
            except Exception:
                pass
            raise
