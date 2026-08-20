"""Canonical CoverageLine/line-index persistence."""

from typing import Any, Dict, Iterable

from app.db.repositories.base import execute, fetchall, fetchone, insert_id


LINE_FIELDS = (
    "line_number", "line_text", "coverage_state", "block_start_line", "block_end_line",
    "block_type", "function_name", "function_hash", "code_line_hash", "code_occurrence",
    "suggested_reviewer",
)


class LineIndexRepository(object):
    def get_line(self, connection, file_id: int, line_number: int):
        return fetchone(connection, """
            SELECT * FROM coverage_lines WHERE file_id = ? AND line_number = ?
        """, (file_id, line_number))

    def upsert_line(self, connection, file_id: int, record: Dict[str, Any]):
        line_number = int(record.get("line_number") or 0)
        if line_number < 1:
            raise ValueError("line_number must be positive")
        defaults = {
            "line_text": "", "coverage_state": "unknown", "block_start_line": line_number,
            "block_end_line": line_number, "block_type": "single", "function_name": "",
            "function_hash": "", "code_line_hash": "", "code_occurrence": 1,
            "suggested_reviewer": "",
        }
        values = []
        for field in LINE_FIELDS:
            value = record.get(field)
            values.append(defaults.get(field) if value is None else value)
        existing = self.get_line(connection, file_id, line_number)
        if existing:
            cursor = execute(connection, """
                UPDATE coverage_lines SET line_text = ?, coverage_state = ?, block_start_line = ?,
                    block_end_line = ?, block_type = ?, function_name = ?, function_hash = ?,
                    code_line_hash = ?, code_occurrence = ?, suggested_reviewer = ?
                WHERE id = ?
            """, tuple(values[1:]) + (existing["id"],))
            cursor.close()
            return fetchone(connection, "SELECT * FROM coverage_lines WHERE id = ?", (existing["id"],))
        cursor = execute(connection, """
            INSERT INTO coverage_lines(
                file_id, line_number, line_text, coverage_state, block_start_line, block_end_line,
                block_type, function_name, function_hash, code_line_hash, code_occurrence,
                suggested_reviewer
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (file_id,) + tuple(values))
        line_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, "SELECT * FROM coverage_lines WHERE id = ?", (line_id,))

    def upsert_lines(self, connection, file_id: int, records: Iterable[Dict[str, Any]]):
        return [self.upsert_line(connection, file_id, record) for record in records]

    def list_lines(self, connection, file_id: int):
        return fetchall(connection, """
            SELECT * FROM coverage_lines WHERE file_id = ? ORDER BY line_number
        """, (file_id,))

    def list_scan_lines(self, connection, scan_id: int):
        return fetchall(connection, """
            SELECT l.*, f.scan_id, f.repository_name, f.file_path_hash, f.file_path,
                   f.source_file_name
            FROM coverage_lines l JOIN coverage_files f ON f.id = l.file_id
            WHERE f.scan_id = ?
            ORDER BY f.repository_name, f.file_path, l.line_number
        """, (scan_id,))

    def line_count(self, connection, scan_id: int):
        row = fetchone(connection, """
            SELECT COUNT(*) AS total FROM coverage_lines l
            JOIN coverage_files f ON f.id = l.file_id WHERE f.scan_id = ?
        """, (scan_id,))
        return int((row or {}).get("total", 0))
