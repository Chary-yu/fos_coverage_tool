"""Canonical analysis facts repository."""

from typing import Any, Dict, Iterable

from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone, is_sqlite


ANALYSIS_FIELDS = (
    "status", "is_draft", "reviewer", "coverage_method", "uncovered_reason", "comment"
)
MAX_ANALYSIS_LOOKUP = 500


def _chunks(values, size):
    values = list(values or [])
    for start in range(0, len(values), size):
        yield values[start:start + size]


class AnalysisRepository(object):
    def get_by_line(self, connection, line_id: int):
        return fetchone(connection, "SELECT * FROM coverage_analyses WHERE line_id = ?", (line_id,))

    def get_by_file(self, connection, file_id: int):
        return fetchall(connection, """
            SELECT a.*, l.line_number FROM coverage_analyses a
            JOIN coverage_lines l ON l.id = a.line_id
            WHERE l.file_id = ? ORDER BY l.line_number
        """, (file_id,))

    def get_by_file_ranges(self, connection, file_id: int, ranges):
        """Read only analysis rows intersecting requested line ranges."""
        normalized = []
        for start_line, end_line in ranges or []:
            start_line, end_line = int(start_line), int(end_line)
            if end_line >= start_line:
                normalized.append((start_line, end_line))
        if not normalized:
            return []
        clauses = []
        params = [int(file_id)]
        for start_line, end_line in normalized:
            clauses.append("(l.line_number >= ? AND l.line_number <= ?)")
            params.extend((start_line, end_line))
        return fetchall(connection, """
            SELECT a.*, l.line_number FROM coverage_analyses a
            JOIN coverage_lines l ON l.id = a.line_id
            WHERE l.file_id = ? AND ({})
            ORDER BY l.line_number
        """.format(" OR ".join(clauses)), params)

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
        """Bulk upsert analysis facts with one executemany and one readback."""
        normalized_records = []
        for record in records or []:
            values = {
                "status": record.get("status") or "",
                "is_draft": int(bool(record.get("is_draft", record.get("draft", False)))),
                "reviewer": record.get("reviewer") or "",
                "coverage_method": record.get("coverage_method", record.get("method", "")) or "",
                "uncovered_reason": record.get("uncovered_reason", record.get("reason", "")) or "",
                "comment": record.get("comment", "") or "",
            }
            normalized_records.append((int(record["line_id"]), values))
        if not normalized_records:
            return []

        if is_sqlite(connection):
            conflict_sql = """
                ON CONFLICT(line_id) DO UPDATE SET
                    status = excluded.status,
                    is_draft = excluded.is_draft,
                    reviewer = excluded.reviewer,
                    coverage_method = excluded.coverage_method,
                    uncovered_reason = excluded.uncovered_reason,
                    comment = excluded.comment,
                    updated_at = excluded.updated_at
            """
        else:
            conflict_sql = """
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status),
                    is_draft = VALUES(is_draft),
                    reviewer = VALUES(reviewer),
                    coverage_method = VALUES(coverage_method),
                    uncovered_reason = VALUES(uncovered_reason),
                    comment = VALUES(comment),
                    updated_at = VALUES(updated_at)
            """
        sql = """
            INSERT INTO coverage_analyses(
                line_id, status, is_draft, reviewer, coverage_method,
                uncovered_reason, comment, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """ + conflict_sql
        params = [
            (line_id,) + tuple(values[field] for field in ANALYSIS_FIELDS)
            for line_id, values in normalized_records
        ]
        cursor = connection.cursor()
        try:
            cursor.executemany(adapt_sql(connection, sql), params)
        finally:
            cursor.close()

        line_ids = [line_id for line_id, _ in normalized_records]
        rows = []
        for id_chunk in _chunks(sorted(set(line_ids)), MAX_ANALYSIS_LOOKUP):
            placeholders = ", ".join("?" for _ in id_chunk)
            rows.extend(fetchall(connection, """
                SELECT a.*, l.line_number
                FROM coverage_analyses a
                JOIN coverage_lines l ON l.id = a.line_id
                WHERE a.line_id IN ({})
            """.format(placeholders), id_chunk))
        by_line = {int(row["line_id"]): row for row in rows}
        return [by_line[int(line_id)] for line_id in line_ids if int(line_id) in by_line]
