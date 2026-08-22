"""Persistent background job identity/state repository."""

from datetime import timedelta

from app.db.repositories.base import execute, fetchall, fetchone
from app.time_utils import utc_now_naive, utc_sql


class JobRepository(object):
    def get(self, connection, job_id: str):
        return fetchone(connection, "SELECT * FROM coverage_background_jobs WHERE job_id = ?", (job_id,))

    def list(self, connection, project_id=None, states=None):
        sql = "SELECT * FROM coverage_background_jobs"
        clauses = []
        params = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append("state IN ({})".format(placeholders))
            params.extend(states)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, job_id"
        return fetchall(connection, sql, params)

    def find_active(self, connection, project_id, scan_id, kind, data_version):
        return fetchone(connection, """
            SELECT * FROM coverage_background_jobs
            WHERE project_id = ? AND scan_id = ? AND kind = ? AND data_version = ?
              AND state IN ('queued', 'running')
            ORDER BY created_at, job_id LIMIT 1
        """, (project_id, scan_id, kind, data_version))

    def upsert(self, connection, job: dict):
        existing = self.get(connection, job["job_id"])
        fields = (
            job.get("project_id"), job.get("scan_id"), job.get("kind") or "",
            job.get("state") or "queued", float(job.get("progress") or 0),
            job.get("input_payload") or "{}", job.get("result_path") or "",
            job.get("error_message") or "", job.get("data_version"),
            job.get("heartbeat_at"), job.get("created_at") or _now(),
            job.get("started_at"), job.get("finished_at"), utc_sql(),
            job.get("lease_owner") or "",
        )
        if existing:
            cursor = execute(connection, """
                UPDATE coverage_background_jobs SET project_id = ?, scan_id = ?, kind = ?,
                    state = ?, progress = ?, input_payload = ?, result_path = ?,
                    error_message = ?, data_version = ?, heartbeat_at = ?, created_at = ?,
                    started_at = ?, finished_at = ?, updated_at = ?, lease_owner = ?
                WHERE job_id = ?
            """, fields + (job["job_id"],))
            cursor.close()
        else:
            cursor = execute(connection, """
                INSERT INTO coverage_background_jobs(
                    job_id, project_id, scan_id, kind, state, progress, input_payload,
                    result_path, error_message, data_version, heartbeat_at, created_at,
                    started_at, finished_at, updated_at, lease_owner
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (job["job_id"],) + fields)
            cursor.close()
        return self.get(connection, job["job_id"])

    def mark_stale(self, connection, timeout_seconds: float, now=None, lease_owner=None,
                   exclude_kinds=None):
        now_value = now or utc_now_naive()
        cutoff = now_value - timedelta(seconds=float(timeout_seconds))
        cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        owner_clause = ""
        params = [cutoff_text]
        if lease_owner:
            owner_clause = " OR (state = 'running' AND COALESCE(lease_owner, '') <> ?)"
            params.append(str(lease_owner))
        kind_clause = ""
        if exclude_kinds:
            values = list(exclude_kinds)
            kind_clause = " AND kind NOT IN ({})".format(
                ", ".join("?" for _ in values)
            )
            params.extend(str(value) for value in values)
        state_sql = """(
            state = 'queued'
            OR (state = 'running' AND (heartbeat_at IS NULL
                OR heartbeat_at < ?){})
        ){}""".format(owner_clause, kind_clause)
        cursor = execute(connection, """
            UPDATE coverage_background_jobs
            SET state = 'interrupted', error_message = 'worker lease expired',
                updated_at = CURRENT_TIMESTAMP, finished_at = CURRENT_TIMESTAMP
            WHERE {}
        """.format(state_sql), params)
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        return count

    def list_recoverable(self, connection, timeout_seconds: float, now=None,
                         lease_owner=None, exclude_kinds=None):
        """List queued or fenced/stale jobs before a recovery claim.

        A recovery worker must inspect the durable input before changing state;
        blindly marking every row interrupted loses the only callback identity
        that can safely resume a long task after process shutdown.
        """
        now_value = now or utc_now_naive()
        cutoff_text = (now_value - timedelta(seconds=float(timeout_seconds))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if lease_owner:
            clauses = [
                "((state = 'queued' AND COALESCE(lease_owner, '') <> ?) OR "
                "(state = 'running' AND (heartbeat_at IS NULL OR "
                "heartbeat_at < ? OR COALESCE(lease_owner, '') <> ?)))"
            ]
            params = [str(lease_owner), cutoff_text, str(lease_owner)]
        else:
            clauses = [
                "(state = 'queued' OR (state = 'running' AND "
                "(heartbeat_at IS NULL OR heartbeat_at < ?)))"
            ]
            params = [cutoff_text]
        if exclude_kinds:
            values = list(exclude_kinds)
            clauses.append("kind NOT IN ({})".format(
                ", ".join("?" for _ in values)
            ))
            params.extend(str(value) for value in values)
        return fetchall(connection, """
            SELECT * FROM coverage_background_jobs
            WHERE {}
            ORDER BY created_at, job_id
        """.format(" AND ".join(clauses)), params)

    def claim_for_recovery(self, connection, job_id, lease_owner, now=None,
                           expected_state=None, expected_lease_owner=None,
                           expected_heartbeat_at=None):
        """Atomically fence one observed recoverable job.

        The observed state/owner/heartbeat form a compare-and-set fence. Two
        workers may list the same durable row, but only the worker that still
        sees the exact row it inspected may enqueue the callback.
        """
        stamp = now or utc_sql()
        clauses = ["job_id=?", "state IN ('queued', 'running')"]
        params = [str(job_id)]
        if expected_state is not None:
            clauses.append("state=?")
            params.append(str(expected_state))
        if expected_lease_owner is not None:
            clauses.append("COALESCE(lease_owner, '')=?")
            params.append(str(expected_lease_owner or ""))
        if expected_heartbeat_at is not None:
            clauses.append("COALESCE(heartbeat_at, '')=COALESCE(?, '')")
            params.append(expected_heartbeat_at)
        cursor = execute(connection, """
            UPDATE coverage_background_jobs
            SET state='queued', lease_owner=?, heartbeat_at=?,
                error_message='', finished_at=NULL, updated_at=?
            WHERE {}
        """.format(" AND ".join(clauses)),
                       (str(lease_owner or ""), stamp, stamp) + tuple(params))
        claimed = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        return self.get(connection, job_id) if claimed else None

    def mark_interrupted(self, connection, job_id, message="worker lease expired"):
        cursor = execute(connection, """
            UPDATE coverage_background_jobs
            SET state='interrupted', error_message=?,
                updated_at=CURRENT_TIMESTAMP, finished_at=CURRENT_TIMESTAMP
            WHERE job_id=? AND state IN ('queued', 'running')
        """, (str(message), str(job_id)))
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        return count


def _now():
    return utc_sql()
