"""Canonical CoverageLine/line-index persistence."""

from typing import Any, Dict, Iterable

from app.db.repositories.base import (
    adapt_sql, bind_chunk_size, execute, fetchall, fetchone, insert_id,
)


LINE_FIELDS = (
    "line_number", "line_text", "coverage_state", "block_start_line", "block_end_line",
    "block_type", "function_name", "function_hash", "code_line_hash", "code_occurrence",
    "suggested_reviewer",
)
MAX_LOOKUP_VALUES = 500
MAX_INSERT_VALUES = 1000


def _chunks(values, size):
    values = list(values or [])
    for start in range(0, len(values), size):
        yield values[start:start + size]


class LineIndexRepository(object):
    @staticmethod
    def _assert_file_scan_building(connection, file_id: int):
        row = fetchone(connection, """
            SELECT s.status FROM coverage_files f
            JOIN coverage_scans s ON s.id = f.scan_id
            WHERE f.id = ?
        """, (int(file_id),))
        if not row:
            raise KeyError("file not found: {}".format(file_id))
        if str(row.get("status") or "").lower() not in {
                "building", "importing", "constructing"}:
            raise ValueError("scan facts are sealed and immutable")
    def get_line(self, connection, file_id: int, line_number: int):
        return fetchone(connection, """
            SELECT * FROM coverage_lines WHERE file_id = ? AND line_number = ?
        """, (file_id, line_number))

    def get_by_ids(self, connection, line_ids):
        line_ids = sorted({int(line_id) for line_id in (line_ids or [])})
        if not line_ids:
            return []
        rows = []
        chunk_size = bind_chunk_size(connection, parameter_width=1,
                                     maximum=MAX_LOOKUP_VALUES)
        for id_chunk in _chunks(line_ids, chunk_size):
            placeholders = ", ".join("?" for _ in id_chunk)
            rows.extend(fetchall(connection, """
                SELECT l.*, f.scan_id, f.repository_name, f.file_path, f.file_path_hash
                FROM coverage_lines l JOIN coverage_files f ON f.id = l.file_id
                WHERE l.id IN ({})
            """.format(placeholders), id_chunk))
        return rows

    def get_by_file_numbers(self, connection, file_numbers):
        """Resolve many (file_id, line_number) pairs with one SELECT."""
        pairs = sorted({(int(file_id), int(line_number))
                        for file_id, line_number in (file_numbers or [])})
        if not pairs:
            return []
        rows = []
        chunk_size = bind_chunk_size(connection, parameter_width=2,
                                     reserved=0, maximum=MAX_LOOKUP_VALUES)
        for pair_chunk in _chunks(pairs, chunk_size):
            clauses = []
            params = []
            for file_id, line_number in pair_chunk:
                clauses.append("(l.file_id = ? AND l.line_number = ?)")
                params.extend((file_id, line_number))
            rows.extend(fetchall(connection, """
                SELECT l.*, f.scan_id, f.repository_name, f.file_path, f.file_path_hash
                FROM coverage_lines l JOIN coverage_files f ON f.id = l.file_id
                WHERE {}
            """.format(" OR ".join(clauses)), params))
        return rows

    def upsert_line(self, connection, file_id: int, record: Dict[str, Any]):
        self._assert_file_scan_building(connection, file_id)
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
            current = tuple(existing.get(field) for field in LINE_FIELDS)
            if current != tuple(values):
                raise ValueError("physical line fact is immutable")
            return existing
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

    def upsert_lines(self, connection, file_id: int, records: Iterable[Dict[str, Any]],
                     return_rows=False):
        self._assert_file_scan_building(connection, file_id)
        normalized = []
        for record in records or []:
            line_number = int(record.get("line_number") or 0)
            if line_number < 1:
                raise ValueError("line_number must be positive")
            defaults = {
                "line_text": "", "coverage_state": "unknown",
                "block_start_line": line_number, "block_end_line": line_number,
                "block_type": "single", "function_name": "", "function_hash": "",
                "code_line_hash": "", "code_occurrence": 1, "suggested_reviewer": "",
            }
            values = [line_number]
            for field in LINE_FIELDS[1:]:
                value = record.get(field)
                values.append(defaults.get(field) if value is None else value)
            normalized.append(tuple(values))
        if not normalized:
            return [] if return_rows else 0
        line_numbers = [item[0] for item in normalized]
        if len(set(line_numbers)) != len(line_numbers):
            raise ValueError("duplicate physical line identity in batch")
        existing = []
        lookup_size = bind_chunk_size(connection, parameter_width=1, reserved=1,
                                      maximum=MAX_LOOKUP_VALUES)
        for number_chunk in _chunks(line_numbers, lookup_size):
            existing.extend(fetchall(connection, """
                SELECT * FROM coverage_lines WHERE file_id = ?
                  AND line_number IN ({})
            """.format(", ".join("?" for _ in number_chunk)),
                (int(file_id),) + tuple(number_chunk)))
        by_line = {int(row["line_number"]): row for row in existing}
        for values in normalized:
            row = by_line.get(values[0])
            if row and tuple(row.get(field) for field in LINE_FIELDS) != values:
                raise ValueError("physical line fact is immutable")
        missing = [values for values in normalized if values[0] not in by_line]
        if not missing:
            return ([by_line[values[0]] for values in normalized]
                    if return_rows else len(normalized))
        for missing_chunk in _chunks(missing, MAX_INSERT_VALUES):
            cursor = connection.cursor()
            try:
                cursor.executemany(adapt_sql(connection, """
                    INSERT INTO coverage_lines(
                        file_id, line_number, line_text, coverage_state, block_start_line,
                        block_end_line, block_type, function_name, function_hash,
                        code_line_hash, code_occurrence, suggested_reviewer
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """), [(int(file_id),) + values for values in missing_chunk])
            finally:
                cursor.close()
        if return_rows:
            return [self.get_line(connection, file_id, values[0])
                    for values in normalized]
        return len(normalized)

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
