"""Rebuildable progress state derived from CoverageLine and Analysis facts."""

from typing import Any, Dict, Iterable, List

from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone, is_sqlite


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")


class FileStateRepository(object):
    _UNCOVERED_SQL = "LOWER(COALESCE(l.coverage_state, '')) IN ('uncovered', 'uncovered_line', 'uncovered-code', '0', '未覆盖')"
    _CONFIRMED_SQL = "COALESCE(a.is_draft, 0) = 0 AND COALESCE(a.status, '') IN ('可覆盖', '无法覆盖', '冗余代码')"

    def get(self, connection, scan_id: int, file_id: int):
        return fetchone(connection, """
            SELECT * FROM coverage_file_state WHERE scan_id = ? AND file_id = ?
        """, (scan_id, file_id))

    def authoritative_file_summary(self, connection, scan_id: int, file_id: int):
        rows = fetchall(connection, """
            SELECT l.id, l.line_number, l.coverage_state,
                   a.status, COALESCE(a.is_draft, 0) AS is_draft
            FROM coverage_lines l
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            WHERE l.file_id = ? ORDER BY l.line_number
        """, (file_id,))
        total = len(rows)
        uncovered = [row for row in rows if str(row.get("coverage_state") or "").lower() in
                     ("uncovered", "uncovered_line", "uncovered-code", "0", "未覆盖")]
        confirmed = [
            row for row in uncovered
            if not int(row.get("is_draft") or 0) and row.get("status") in CONFIRMED_STATUSES
        ]
        pending = [row for row in uncovered if row not in confirmed]
        filled = [row for row in rows if row.get("status") not in (None, "")]
        draft = [row for row in rows if int(row.get("is_draft") or 0)]
        return {
            "scan_id": scan_id,
            "file_id": file_id,
            "total_lines": total,
            "total_uncovered": len(uncovered),
            "filled_total": len(filled),
            "draft_total": len(draft),
            "confirmed_total": len(confirmed),
            "pending_total": len(pending),
            "pending_line_numbers": [int(row["line_number"]) for row in pending],
            "uncovered_line_numbers": [int(row["line_number"]) for row in uncovered],
            "file_state_version": None,
        }

    def rebuild_file(self, connection, scan_id: int, file_id: int, data_version: int):
        summary = self.authoritative_file_summary(connection, scan_id, file_id)
        existing = self.get(connection, scan_id, file_id)
        fields = (
            summary["total_lines"], summary["total_uncovered"], summary["filled_total"],
            summary["draft_total"], summary["confirmed_total"], summary["pending_total"],
            int(data_version),
        )
        if existing:
            cursor = execute(connection, """
                UPDATE coverage_file_state SET total_lines = ?, total_uncovered = ?,
                    filled_total = ?, draft_total = ?, confirmed_total = ?, pending_total = ?,
                    data_version = ?, updated_at = CURRENT_TIMESTAMP
                WHERE scan_id = ? AND file_id = ?
            """, fields + (scan_id, file_id))
            cursor.close()
        else:
            cursor = execute(connection, """
                INSERT INTO coverage_file_state(
                    scan_id, file_id, total_lines, total_uncovered, filled_total, draft_total,
                    confirmed_total, pending_total, data_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (scan_id, file_id) + fields)
            cursor.close()
        return self.get(connection, scan_id, file_id)

    def rebuild_scan(self, connection, scan_id: int, data_version: int, file_rows):
        """Rebuild all file states with one grouped INSERT/UPDATE statement."""
        # ``file_rows`` was part of the original per-file implementation. Keep
        # the argument for callers that still pass it, but derive the complete
        # file set in SQL so the caller does not need to materialize or loop it.
        del file_rows
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        grouped_select = """
            SELECT f.scan_id, f.id,
                   COUNT(l.id),
                   COALESCE(SUM(CASE WHEN {uncovered} THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN a.id IS NOT NULL AND COALESCE(a.status, '') <> '' THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN COALESCE(a.is_draft, 0) = 1 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN {uncovered} AND {confirmed} THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN {uncovered} AND NOT ({confirmed}) THEN 1 ELSE 0 END), 0),
                   ?, CURRENT_TIMESTAMP
            FROM coverage_files f
            LEFT JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            WHERE f.scan_id = ?
            GROUP BY f.scan_id, f.id
        """.format(uncovered=uncovered, confirmed=confirmed)
        if is_sqlite(connection):
            upsert_suffix = """
                ON CONFLICT(scan_id, file_id) DO UPDATE SET
                    total_lines = excluded.total_lines,
                    total_uncovered = excluded.total_uncovered,
                    filled_total = excluded.filled_total,
                    draft_total = excluded.draft_total,
                    confirmed_total = excluded.confirmed_total,
                    pending_total = excluded.pending_total,
                    data_version = excluded.data_version,
                    updated_at = excluded.updated_at
            """
        else:
            upsert_suffix = """
                ON DUPLICATE KEY UPDATE
                    total_lines = VALUES(total_lines),
                    total_uncovered = VALUES(total_uncovered),
                    filled_total = VALUES(filled_total),
                    draft_total = VALUES(draft_total),
                    confirmed_total = VALUES(confirmed_total),
                    pending_total = VALUES(pending_total),
                    data_version = VALUES(data_version),
                    updated_at = VALUES(updated_at)
            """
        insert_sql = """
            INSERT INTO coverage_file_state(
                scan_id, file_id, total_lines, total_uncovered, filled_total,
                draft_total, confirmed_total, pending_total, data_version, updated_at
            )
        """ + grouped_select + upsert_suffix
        cursor = connection.cursor()
        try:
            cursor.execute(adapt_sql(connection, insert_sql), (int(data_version), int(scan_id)))
            cursor.execute(adapt_sql(connection, """
                DELETE FROM coverage_file_state
                WHERE scan_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM coverage_files f
                      WHERE f.id = coverage_file_state.file_id AND f.scan_id = ?
                  )
            """), (int(scan_id), int(scan_id)))
        finally:
            cursor.close()
        return {
            "scan_id": int(scan_id),
            "data_version": int(data_version),
            "status": "REBUILT",
        }

    def scan_aggregate(self, connection, scan_id: int):
        """Aggregate derived file state in SQL without loading every file row."""
        return fetchone(connection, """
            SELECT COUNT(*) AS file_count,
                   COALESCE(SUM(total_lines), 0) AS total_lines,
                   COALESCE(SUM(total_uncovered), 0) AS total_uncovered,
                   COALESCE(SUM(filled_total), 0) AS filled_total,
                   COALESCE(SUM(draft_total), 0) AS draft_total,
                   COALESCE(SUM(confirmed_total), 0) AS confirmed_total,
                   COALESCE(SUM(pending_total), 0) AS pending_total
            FROM coverage_file_state WHERE scan_id = ?
        """, (scan_id,))

    def scan_summary_from_facts(self, connection, scan_id: int):
        """Aggregate authoritative facts in SQL for stale/empty derived state."""
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        row = fetchone(connection, """
            SELECT COUNT(DISTINCT f.id) AS file_count,
                   COUNT(l.id) AS total_lines,
                   COALESCE(SUM(CASE WHEN {uncovered} THEN 1 ELSE 0 END), 0) AS total_uncovered,
                   COALESCE(SUM(CASE WHEN a.id IS NOT NULL AND COALESCE(a.status, '') <> '' THEN 1 ELSE 0 END), 0) AS filled_total,
                   COALESCE(SUM(CASE WHEN COALESCE(a.is_draft, 0) = 1 THEN 1 ELSE 0 END), 0) AS draft_total,
                   COALESCE(SUM(CASE WHEN {uncovered} AND {confirmed} THEN 1 ELSE 0 END), 0) AS confirmed_total,
                   COALESCE(SUM(CASE WHEN {uncovered} AND NOT ({confirmed}) THEN 1 ELSE 0 END), 0) AS pending_total
            FROM coverage_files f
            LEFT JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            WHERE f.scan_id = ?
        """.format(uncovered=uncovered, confirmed=confirmed), (scan_id,))
        row = row or {}
        return {
            "scan_id": scan_id,
            "file_count": int(row.get("file_count") or 0),
            "total_lines": int(row.get("total_lines") or 0),
            "total_uncovered": int(row.get("total_uncovered") or 0),
            "filled_total": int(row.get("filled_total") or 0),
            "draft_total": int(row.get("draft_total") or 0),
            "confirmed_total": int(row.get("confirmed_total") or 0),
            "pending_total": int(row.get("pending_total") or 0),
            "pending_line_references": [],
        }

    def pending_line_references(self, connection, scan_id: int, limit=None, offset=0):
        """Return file-qualified pending physical lines for a scan.

        The optional window is used by the pending-detail page. Summary and
        developer-task callers can omit it for their existing grouped view.
        """
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        sql = """
            SELECT f.repository_name, f.file_path, l.line_number
            FROM coverage_files f
            JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            WHERE f.scan_id = ? AND {uncovered} AND NOT ({confirmed})
            ORDER BY f.repository_name, f.file_path, l.line_number
        """.format(uncovered=uncovered, confirmed=confirmed)
        params = [int(scan_id)]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((max(1, int(limit)), max(0, int(offset))))
        rows = fetchall(connection, sql, params)
        return [{
            "repository_name": row.get("repository_name") or "",
            "file_path": row.get("file_path") or "",
            "line_number": int(row["line_number"]),
        } for row in rows]

    def pending_line_count(self, connection, scan_id: int):
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        row = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_files f
            JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            WHERE f.scan_id = ? AND {uncovered} AND NOT ({confirmed})
        """.format(uncovered=uncovered, confirmed=confirmed), (int(scan_id),))
        return int((row or {}).get("total") or 0)
