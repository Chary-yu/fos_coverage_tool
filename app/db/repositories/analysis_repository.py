"""Canonical analysis facts repository."""

from typing import Any, Dict, Iterable

from app.db.repositories.base import execute, fetchall, fetchone


ANALYSIS_FIELDS = (
    "status", "is_draft", "reviewer", "coverage_method", "uncovered_reason", "comment"
)


class AnalysisRepository(object):
    def get_by_line(self, connection, line_id: int):
        return fetchone(connection, "SELECT * FROM coverage_analyses WHERE line_id = ?", (line_id,))

    def get_by_file(self, connection, file_id: int):
        return fetchall(connection, """
            SELECT a.*, l.line_number FROM coverage_analyses a
            JOIN coverage_lines l ON l.id = a.line_id
            WHERE l.file_id = ? ORDER BY l.line_number
        """, (file_id,))

    def get_by_scan(self, connection, scan_id: int):
        return fetchall(connection, """
            SELECT a.*, l.line_number, f.repository_name, f.file_path_hash, f.file_path
            FROM coverage_analyses a
            JOIN coverage_lines l ON l.id = a.line_id
            JOIN coverage_files f ON f.id = l.file_id
            WHERE f.scan_id = ?
            ORDER BY f.repository_name, f.file_path, l.line_number
        """, (scan_id,))

    def upsert(self, connection, line_id: int, values: Dict[str, Any]):
        normalized = {
            "status": values.get("status") or "",
            "is_draft": int(bool(values.get("is_draft", values.get("draft", False)))),
            "reviewer": values.get("reviewer") or "",
            "coverage_method": values.get("coverage_method", values.get("method", "")) or "",
            "uncovered_reason": values.get("uncovered_reason", values.get("reason", "")) or "",
            "comment": values.get("comment", "") or "",
        }
        existing = self.get_by_line(connection, line_id)
        if existing:
            cursor = execute(connection, """
                UPDATE coverage_analyses SET status = ?, is_draft = ?, reviewer = ?,
                    coverage_method = ?, uncovered_reason = ?, comment = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, tuple(normalized[field] for field in ANALYSIS_FIELDS) + (existing["id"],))
            cursor.close()
            return self.get_by_line(connection, line_id)
        cursor = execute(connection, """
            INSERT INTO coverage_analyses(
                line_id, status, is_draft, reviewer, coverage_method, uncovered_reason,
                comment, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (line_id,) + tuple(normalized[field] for field in ANALYSIS_FIELDS))
        cursor.close()
        return self.get_by_line(connection, line_id)

    def upsert_many(self, connection, records: Iterable[Dict[str, Any]]):
        return [self.upsert(connection, int(record["line_id"]), record) for record in records]
