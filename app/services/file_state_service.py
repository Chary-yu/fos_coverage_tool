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

    def validate_rebuilt(self, connection, project_id, scan_id, data_version,
                         affected_file_ids=None, before_aggregate=None,
                         before_affected_aggregate=None):
        state = self.states.get(connection, int(project_id)) or {}
        expected_version = int(data_version)
        if int(state.get("data_version") or 0) != expected_version:
            return {
                "status": "FAILED", "reason": "DATA_VERSION_CHANGED",
                "project_id": int(project_id), "scan_id": int(scan_id),
                "data_version": expected_version,
            }
        selected_file_ids = self.file_states._normalized_file_ids(
            affected_file_ids
        )
        incremental = (
            selected_file_ids is not None and
            before_aggregate is not None and
            before_affected_aggregate is not None
        )
        completeness = self.file_states.file_state_completeness(
            connection, int(scan_id), expected_version,
            affected_file_ids=selected_file_ids if incremental else None,
        )
        conservation = self.file_states.pending_conservation(
            connection, int(scan_id),
            file_ids=selected_file_ids if incremental else None,
        )
        if incremental:
            affected_derived = self.file_states.file_state_aggregate(
                connection, int(scan_id), selected_file_ids
            ) or {}
            derived = {}
            for field in SUMMARY_FIELDS:
                derived[field] = (
                    int(before_aggregate.get(field) or 0) -
                    int(before_affected_aggregate.get(field) or 0) +
                    int(affected_derived.get(field) or 0)
                )
            authoritative = self.file_states.scan_summary_from_facts(
                connection, int(scan_id), file_ids=selected_file_ids
            ) or {}
            reconciled_derived = affected_derived
            reconciliation_scope = "AFFECTED_FILES"
        else:
            derived = self.file_states.scan_aggregate(
                connection, int(scan_id)
            ) or {}
            authoritative = self.file_states.scan_summary_from_facts(
                connection, int(scan_id)
            ) or {}
            reconciled_derived = derived
            reconciliation_scope = "FULL_SCAN"
        mismatches = {}
        compare_fields = SUMMARY_FIELDS
        for field in compare_fields:
            derived_value = int(reconciled_derived.get(field) or 0)
            authoritative_value = int(authoritative.get(field) or 0)
            if derived_value != authoritative_value:
                mismatches[field] = {
                    "derived": derived_value,
                    "authoritative": authoritative_value,
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
                "scope": reconciliation_scope,
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
            affected_file_ids=None, previous_ready=None):
        """Run the complete gate inside a caller-owned transaction."""
        project_id = int(project_id)
        scan_id = int(scan_id)
        data_version = int(data_version)
        selected_file_ids = self.file_states._normalized_file_ids(
            affected_file_ids
        )
        incremental = selected_file_ids is not None
        if incremental:
            previous_completeness = self.file_states.file_state_completeness(
                connection, scan_id, max(0, data_version - 1)
            )
            if previous_ready is None:
                previous_ready = (
                    data_version > 0 and
                    previous_completeness.get("status") == "PASSED"
                )
            else:
                previous_ready = bool(previous_ready) and (
                    previous_completeness.get("status") == "PASSED"
                )
        if incremental and not previous_ready:
            # A previous failed or partial projection cannot be used as the
            # unchanged side of a delta. Fall back to the authoritative full
            # rebuild and full reconciliation; never weaken the Ready gate.
            incremental = False
        before_aggregate = None
        before_affected_aggregate = None
        if incremental:
            before_aggregate = self.file_states.scan_aggregate(
                connection, scan_id
            ) or {}
            before_affected_aggregate = self.file_states.file_state_aggregate(
                connection, scan_id, selected_file_ids
            ) or {}
        # Invalidate before touching the projection.  If a caller catches the
        # gate error and commits its surrounding transaction, the state still
        # advertises that the projection is stale.
        self.states.invalidate(connection, project_id)
        if not incremental:
            self.file_states.rebuild_scan(
                connection, scan_id, data_version, None
            )
        else:
            # The mutation boundary supplies the exact files whose facts
            # changed. Unchanged rows retain their last verified projection
            # version; the delta validation below proves the affected facts
            # and the project aggregate without a full-Scan UPDATE.
            self.file_states.rebuild_scan(
                connection, scan_id, data_version, None,
                file_ids=selected_file_ids,
            )
        gate = self.validate_rebuilt(
            connection, project_id, scan_id, data_version,
            affected_file_ids=selected_file_ids if incremental else None,
            before_aggregate=before_aggregate,
            before_affected_aggregate=before_affected_aggregate,
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
