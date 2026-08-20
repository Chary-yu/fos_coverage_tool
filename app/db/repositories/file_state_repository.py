"""Rebuildable progress state derived from CoverageLine and Analysis facts."""

from typing import Any, Dict, Iterable, List

from app.db.repositories.base import execute, fetchall, fetchone


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")


class FileStateRepository(object):
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
        return [self.rebuild_file(connection, scan_id, int(row["id"]), data_version)
                for row in file_rows]

    def scan_summary_from_facts(self, connection, scan_id: int):
        rows = fetchall(connection, """
            SELECT l.line_number, l.coverage_state, a.status,
                   COALESCE(a.is_draft, 0) AS is_draft,
                   f.id AS file_id, f.repository_name, f.file_path
            FROM coverage_lines l
            JOIN coverage_files f ON f.id = l.file_id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            WHERE f.scan_id = ?
            ORDER BY f.repository_name, f.file_path, l.line_number
        """, (scan_id,))
        total = len(rows)
        uncovered = [row for row in rows if str(row.get("coverage_state") or "").lower() in
                     ("uncovered", "uncovered_line", "uncovered-code", "0", "未覆盖")]
        confirmed = [
            row for row in uncovered
            if not int(row.get("is_draft") or 0) and row.get("status") in CONFIRMED_STATUSES
        ]
        pending = [row for row in uncovered if row not in confirmed]
        return {
            "scan_id": scan_id,
            "total_lines": total,
            "total_uncovered": len(uncovered),
            "filled_total": sum(1 for row in rows if row.get("status") not in (None, "")),
            "draft_total": sum(1 for row in rows if int(row.get("is_draft") or 0)),
            "confirmed_total": len(confirmed),
            "pending_total": len(pending),
            "pending_line_references": [
                "{}:{}:{}".format(row.get("repository_name") or "", row.get("file_path") or "", row["line_number"])
                for row in pending
            ],
        }
