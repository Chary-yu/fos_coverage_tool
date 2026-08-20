"""Persistent background job identity/state repository."""

from datetime import datetime, timedelta

from app.db.repositories.base import execute, fetchall, fetchone


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

    def upsert(self, connection, job: dict):
        existing = self.get(connection, job["job_id"])
        fields = (
            job.get("project_id"), job.get("scan_id"), job.get("kind") or "",
            job.get("state") or "queued", float(job.get("progress") or 0),
            job.get("input_payload") or "{}", job.get("result_path") or "",
            job.get("error_message") or "", job.get("data_version"),
            job.get("heartbeat_at"), job.get("created_at") or _now(),
            job.get("started_at"), job.get("finished_at"), _now(),
        )
        if existing:
            cursor = execute(connection, """
                UPDATE coverage_background_jobs SET project_id = ?, scan_id = ?, kind = ?,
                    state = ?, progress = ?, input_payload = ?, result_path = ?,
                    error_message = ?, data_version = ?, heartbeat_at = ?, created_at = ?,
                    started_at = ?, finished_at = ?, updated_at = ?
                WHERE job_id = ?
            """, fields + (job["job_id"],))
            cursor.close()
        else:
            cursor = execute(connection, """
                INSERT INTO coverage_background_jobs(
                    job_id, project_id, scan_id, kind, state, progress, input_payload,
                    result_path, error_message, data_version, heartbeat_at, created_at,
                    started_at, finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (job["job_id"],) + fields)
            cursor.close()
        return self.get(connection, job["job_id"])

    def mark_stale(self, connection, timeout_seconds: float, now=None):
        now_value = now or datetime.utcnow()
        cutoff = now_value - timedelta(seconds=float(timeout_seconds))
        cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        cursor = execute(connection, """
            UPDATE coverage_background_jobs
            SET state = 'interrupted', error_message = 'heartbeat timeout',
                updated_at = CURRENT_TIMESTAMP, finished_at = CURRENT_TIMESTAMP
            WHERE state IN ('queued', 'running')
              AND heartbeat_at IS NOT NULL AND heartbeat_at < ?
        """, (cutoff_text,))
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        return count


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
