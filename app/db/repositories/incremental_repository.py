"""Persistence for immutable incremental report payloads bound to a Scan."""

import json

from app.db.repositories.base import execute, fetchone
from app.time_utils import utc_sql


class IncrementalRepository(object):
    def get(self, connection, scan_id, report_id, repository_name):
        return fetchone(connection, """
            SELECT * FROM coverage_incremental_results
            WHERE scan_id = ? AND report_id = ? AND repository_name = ?
        """, (int(scan_id), report_id or "", repository_name or ""))

    def upsert(self, connection, scan_id, report_id, repository_name, report):
        existing = self.get(connection, scan_id, report_id, repository_name)
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)
        repositories = report.get("repositories") or []
        item = repositories[0] if repositories else {}
        values = (
            report.get("oldgit") or item.get("old_commit_sha") or "",
            report.get("newgit") or item.get("new_commit_sha") or "",
            payload,
            utc_sql(),
        )
        if existing:
            cursor = execute(connection, """
                UPDATE coverage_incremental_results SET old_commit_sha = ?,
                    new_commit_sha = ?, payload = ?, generated_at = ?
                WHERE id = ?
            """, values + (existing["id"],))
            cursor.close()
            return self.get(connection, scan_id, report_id, repository_name)
        cursor = execute(connection, """
            INSERT INTO coverage_incremental_results(
                scan_id, report_id, repository_name, old_commit_sha,
                new_commit_sha, payload, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (int(scan_id), report_id or "", repository_name or "") + values)
        cursor.close()
        return self.get(connection, scan_id, report_id, repository_name)

    def load_payload(self, connection, scan_id, report_id, repository_name):
        row = self.get(connection, scan_id, report_id, repository_name)
        return json.loads(row["payload"]) if row else None
