"""Persistence for the canonical AnalysisRecord/Block/LineLink domain."""

from __future__ import absolute_import

import hashlib
import json

from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone, insert_id, is_sqlite
from app.time_utils import utc_sql


MANUAL_DRAFT = "MANUAL_DRAFT"
MANUAL_CONFIRMED = "MANUAL_CONFIRMED"
INHERITED_PENDING = "INHERITED_PENDING"
CARRIED_COVERED = "CARRIED_COVERED"
CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")


def _chunks(values, size=500):
    values = list(values or [])
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _bulk_insert_ids(connection, statement_prefix, rows, chunk_size=500):
    """Insert rows as bounded multi-value statements and return their IDs.

    The VNext schema uses auto-increment integer identities.  MariaDB returns
    the first generated ID for a multi-row INSERT while SQLite returns the
    last one; both engines allocate the rows contiguously for one statement.
    Keeping this detail here lets domain services do one DB round trip per
    bounded batch while preserving the existing ordered readback contract.
    """
    rows = [tuple(row) for row in (rows or [])]
    if not rows:
        return []
    width = len(rows[0])
    if width < 1 or any(len(row) != width for row in rows):
        raise ValueError("bulk insert rows have inconsistent widths")

    inserted_ids = []
    for batch in _chunks(rows, size=chunk_size):
        placeholders = "(" + ", ".join("?" for _ in range(width)) + ")"
        sql = "{} VALUES {}".format(
            statement_prefix, ", ".join(placeholders for _ in batch)
        )
        params = tuple(value for row in batch for value in row)
        cursor = connection.cursor()
        try:
            cursor.execute(adapt_sql(connection, sql), params)
            last_id = insert_id(cursor)
        finally:
            cursor.close()
        if not last_id:
            raise RuntimeError("bulk insert did not return an auto-increment identity")
        first_id = last_id - len(batch) + 1 if is_sqlite(connection) else last_id
        if first_id < 1:
            raise RuntimeError("bulk insert returned an invalid identity range")
        inserted_ids.extend(range(first_id, first_id + len(batch)))
    return inserted_ids


def content_hash(values):
    payload = {
        "conclusion_status": values.get("conclusion_status", values.get("status", "")) or "",
        "coverage_method": values.get("coverage_method", "") or "",
        "uncovered_reason": values.get("uncovered_reason", "") or "",
        "comment": values.get("comment", "") or "",
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


class AnalysisDomainRepository(object):
    def get_record(self, connection, record_id):
        return fetchone(connection, "SELECT * FROM coverage_analysis_records WHERE id=?",
                        (int(record_id),))

    def get_link(self, connection, scan_id, line_id):
        return fetchone(connection, """
            SELECT * FROM coverage_analysis_line_links
            WHERE scan_id=? AND line_id=?
        """, (int(scan_id), int(line_id)))

    def get_active_for_line(self, connection, line_id, scan_id=None):
        if scan_id is None:
            return fetchone(connection, """
                SELECT * FROM coverage_analysis_line_links
                WHERE line_id=? AND is_active=1 ORDER BY id DESC LIMIT 1
            """, (int(line_id),))
        return fetchone(connection, """
            SELECT * FROM coverage_analysis_line_links
            WHERE scan_id=? AND line_id=? AND is_active=1
        """, (int(scan_id), int(line_id)))

    def read_line(self, connection, scan_id, line_id):
        return fetchone(connection, """
            SELECT l.*, r.conclusion_status, r.coverage_method,
                   r.uncovered_reason, r.comment, r.content_revision,
                   r.content_hash, q.review_state, q.relation_origin,
                   q.reviewed_by, q.reviewed_at, q.relation_revision,
                   q.source_scan_id, q.source_line_id, q.source_relation_id,
                   q.is_active AS relation_is_active
            FROM coverage_analysis_line_links q
            JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
            JOIN coverage_lines l ON l.id=q.line_id
            WHERE q.scan_id=? AND q.line_id=? AND q.is_active=1
        """, (int(scan_id), int(line_id)))

    def read_scan(self, connection, scan_id):
        return fetchall(connection, """
            SELECT q.*, l.line_number, l.file_id, f.file_path, f.repository_name,
                   r.conclusion_status, r.coverage_method, r.uncovered_reason,
                   r.comment, r.content_revision, r.content_hash
            FROM coverage_analysis_line_links q
            JOIN coverage_lines l ON l.id=q.line_id
            JOIN coverage_files f ON f.id=l.file_id
            JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
            WHERE q.scan_id=? AND q.is_active=1
            ORDER BY f.repository_name, f.file_path, l.line_number
        """, (int(scan_id),))

    def read_file(self, connection, scan_id, file_id, ranges=None):
        clauses = ["l.file_id=?"]
        params = [int(file_id)]
        for start_line, end_line in (ranges or []):
            clauses.append("(l.line_number>=? AND l.line_number<=?)")
            params.extend((int(start_line), int(end_line)))
        if ranges:
            range_items = clauses[1:]
            clauses = clauses[:1] + ["({})".format(" OR ".join(range_items))]
        return fetchall(connection, """
            SELECT q.*, l.line_number, l.file_id,
                   q.is_active AS relation_is_active,
                   r.conclusion_status, r.coverage_method,
                   r.uncovered_reason, r.comment, r.content_revision,
                   r.content_hash,
                   x.id AS rejection_id, x.rejection_revision,
                   x.rejected_relation_revision, x.rejected_relation_id,
                   x.rejected_source_scan_id, x.rejected_source_line_id,
                   x.rejected_source_relation_id
            FROM coverage_lines l
            LEFT JOIN coverage_analysis_line_links q
              ON q.scan_id=? AND q.line_id=l.id
            LEFT JOIN coverage_analysis_records r
              ON r.id=q.analysis_record_id
            LEFT JOIN coverage_inheritance_rejections x
              ON x.scan_id=? AND x.line_id=l.id AND x.is_active=1
            WHERE {where}
              AND (q.is_active=1 OR x.id IS NOT NULL)
            ORDER BY l.line_number
        """.format(where=" AND ".join(clauses)),
                    (int(scan_id), int(scan_id)) + tuple(params))

    def create_record(self, connection, values, origin="MANUAL", now=None):
        now = now or utc_sql()
        normalized = {
            "conclusion_status": values.get("conclusion_status", values.get("status", "")) or "",
            "coverage_method": values.get("coverage_method", values.get("method", "")) or "",
            "uncovered_reason": values.get("uncovered_reason", values.get("reason", "")) or "",
            "comment": values.get("comment", "") or "",
        }
        digest = content_hash(normalized)
        cursor = execute(connection, """
            INSERT INTO coverage_analysis_records(
                conclusion_status, coverage_method, uncovered_reason, comment,
                content_revision, content_hash, content_origin,
                legacy_source_analysis_id, legacy_source_created_at,
                legacy_source_updated_at, legacy_raw_status, legacy_raw_is_draft,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (normalized["conclusion_status"], normalized["coverage_method"],
              normalized["uncovered_reason"], normalized["comment"], digest,
              origin, values.get("legacy_source_analysis_id"),
              values.get("legacy_source_created_at"), values.get("legacy_source_updated_at"),
              values.get("legacy_raw_status"), values.get("legacy_raw_is_draft"), now, now))
        record_id = insert_id(cursor)
        cursor.close()
        return self.get_record(connection, record_id)

    def create_records_many(self, connection, values, origin="MANUAL", now=None):
        """Create content records with bounded executemany/readback batches."""
        items = [dict(item or {}) for item in (values or [])]
        if not items:
            return []
        stamp = now or utc_sql()
        normalized = []
        source_ids = []
        for item in items:
            source_id = item.get("legacy_source_analysis_id")
            if source_id is None:
                raise ValueError("bulk analysis record requires a legacy source id")
            source_id = int(source_id)
            content = {
                "conclusion_status": item.get(
                    "conclusion_status", item.get("status", "")
                ) or "",
                "coverage_method": item.get(
                    "coverage_method", item.get("method", "")
                ) or "",
                "uncovered_reason": item.get(
                    "uncovered_reason", item.get("reason", "")
                ) or "",
                "comment": item.get("comment", "") or "",
            }
            normalized.append((
                content, source_id,
                item.get("legacy_source_created_at"),
                item.get("legacy_source_updated_at"),
                item.get("legacy_raw_status", item.get("status")),
                item.get("legacy_raw_is_draft", item.get("is_draft")),
            ))
            source_ids.append(source_id)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("duplicate legacy analysis source id in bulk insert")

        def chunks(sequence, size=500):
            for start in range(0, len(sequence), size):
                yield sequence[start:start + size]

        params = []
        for content, source_id, created_at, updated_at, raw_status, raw_draft in normalized:
            params.append((
                content["conclusion_status"], content["coverage_method"],
                content["uncovered_reason"], content["comment"],
                content_hash(content), origin, source_id, created_at, updated_at,
                raw_status, raw_draft, stamp, stamp,
            ))
        statement = """
            INSERT INTO coverage_analysis_records(
                conclusion_status, coverage_method, uncovered_reason, comment,
                content_revision, content_hash, content_origin,
                legacy_source_analysis_id, legacy_source_created_at,
                legacy_source_updated_at, legacy_raw_status, legacy_raw_is_draft,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for batch in chunks(params):
            cursor = connection.cursor()
            try:
                cursor.executemany(adapt_sql(connection, statement), batch)
            finally:
                cursor.close()
        rows = []
        for batch in chunks(source_ids):
            placeholders = ", ".join("?" for _ in batch)
            rows.extend(fetchall(connection, """
                SELECT * FROM coverage_analysis_records
                WHERE legacy_source_analysis_id IN ({})
            """.format(placeholders), batch))
        by_source = {int(row["legacy_source_analysis_id"]): row for row in rows}
        if len(by_source) != len(source_ids):
            raise RuntimeError("bulk analysis record readback is incomplete")
        return [by_source[source_id] for source_id in source_ids]

    def create_manual_records_many(self, connection, values, origin="MANUAL", now=None):
        """Create manual content records with bounded multi-row INSERTs.

        Manual records do not have a legacy source identity that can be used
        for readback.  The repository therefore uses the auto-increment range
        returned by each bounded INSERT and then reads the rows by those
        identities.  The returned order is exactly the input order so the
        service can attach each record to its operation without another
        per-record lookup.
        """
        items = [dict(item or {}) for item in (values or [])]
        if not items:
            return []
        stamp = now or utc_sql()
        params = []
        for item in items:
            normalized = {
                "conclusion_status": item.get(
                    "conclusion_status", item.get("status", "")
                ) or "",
                "coverage_method": item.get(
                    "coverage_method", item.get("method", "")
                ) or "",
                "uncovered_reason": item.get(
                    "uncovered_reason", item.get("reason", "")
                ) or "",
                "comment": item.get("comment", "") or "",
            }
            params.append((
                normalized["conclusion_status"],
                normalized["coverage_method"],
                normalized["uncovered_reason"],
                normalized["comment"],
                content_hash(normalized), origin,
                item.get("legacy_source_analysis_id"),
                item.get("legacy_source_created_at"),
                item.get("legacy_source_updated_at"),
                item.get("legacy_raw_status"),
                item.get("legacy_raw_is_draft"), stamp, stamp,
            ))
        ids = _bulk_insert_ids(connection, """
            INSERT INTO coverage_analysis_records(
                conclusion_status, coverage_method, uncovered_reason, comment,
                content_hash, content_origin,
                legacy_source_analysis_id, legacy_source_created_at,
                legacy_source_updated_at, legacy_raw_status, legacy_raw_is_draft,
                created_at, updated_at
            )""", params)
        rows = []
        for batch in _chunks(ids):
            placeholders = ", ".join("?" for _ in batch)
            rows.extend(fetchall(connection, """
                SELECT * FROM coverage_analysis_records
                WHERE id IN ({})
            """.format(placeholders), batch))
        by_id = {int(row["id"]): row for row in rows}
        if len(by_id) != len(ids):
            raise RuntimeError("bulk manual record readback is incomplete")
        return [by_id[int(record_id)] for record_id in ids]

    def update_records_many(self, connection, values, now=None):
        """CAS-update existing content records with one bounded batch."""
        items = [dict(item or {}) for item in (values or [])]
        if not items:
            return []
        record_ids = [int(item.get("record_id") or 0) for item in items]
        if any(record_id <= 0 for record_id in record_ids):
            raise ValueError("analysis record id is required")
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("duplicate analysis record in update batch")
        rows = []
        for batch in _chunks(sorted(record_ids)):
            placeholders = ", ".join("?" for _ in batch)
            rows.extend(fetchall(connection, """
                SELECT * FROM coverage_analysis_records
                WHERE id IN ({})
            """.format(placeholders), batch))
        current_by_id = {int(row["id"]): row for row in rows}
        if len(current_by_id) != len(record_ids):
            raise KeyError("analysis record not found")

        stamp = now or utc_sql()
        params = []
        for item in items:
            record_id = int(item["record_id"])
            current = current_by_id[record_id]
            expected = item.get("expected_record_revision")
            if expected is None:
                raise ValueError("EXPECTED_RECORD_REVISION_REQUIRED")
            revision = int(current.get("content_revision") or 0)
            if revision != int(expected):
                raise ValueError("STALE_CONTENT_REVISION")
            normalized = {
                "conclusion_status": item.get(
                    "conclusion_status", item.get("status", "")
                ) or "",
                "coverage_method": item.get(
                    "coverage_method", item.get("method", "")
                ) or "",
                "uncovered_reason": item.get(
                    "uncovered_reason", item.get("reason", "")
                ) or "",
                "comment": item.get("comment", "") or "",
            }
            params.append((
                normalized["conclusion_status"],
                normalized["coverage_method"],
                normalized["uncovered_reason"],
                normalized["comment"], revision + 1,
                content_hash(normalized), stamp, record_id, revision,
            ))

        cursor = connection.cursor()
        try:
            cursor.executemany(adapt_sql(connection, """
                UPDATE coverage_analysis_records
                SET conclusion_status=?, coverage_method=?, uncovered_reason=?, comment=?,
                    content_revision=?, content_hash=?, updated_at=?
                WHERE id=? AND content_revision=?
            """), params)
            if int(getattr(cursor, "rowcount", 0) or 0) != len(params):
                raise ValueError("STALE_CONTENT_REVISION")
        finally:
            cursor.close()

        result = []
        for batch in _chunks(record_ids):
            placeholders = ", ".join("?" for _ in batch)
            result.extend(fetchall(connection, """
                SELECT * FROM coverage_analysis_records
                WHERE id IN ({})
            """.format(placeholders), batch))
        updated_by_id = {int(row["id"]): row for row in result}
        if len(updated_by_id) != len(record_ids):
            raise RuntimeError("bulk analysis record readback is incomplete")
        return [updated_by_id[record_id] for record_id in record_ids]

    def update_record(self, connection, record_id, values, expected_revision=None):
        current = self.get_record(connection, record_id)
        if not current:
            raise KeyError("analysis record not found")
        normalized = {
            "conclusion_status": values.get("conclusion_status", values.get("status", "")) or "",
            "coverage_method": values.get("coverage_method", values.get("method", "")) or "",
            "uncovered_reason": values.get("uncovered_reason", values.get("reason", "")) or "",
            "comment": values.get("comment", "") or "",
        }
        digest = content_hash(normalized)
        revision = int(current.get("content_revision") or 0)
        if expected_revision is not None and revision != int(expected_revision):
            raise ValueError("STALE_CONTENT_REVISION")
        cursor = execute(connection, """
            UPDATE coverage_analysis_records
            SET conclusion_status=?, coverage_method=?, uncovered_reason=?, comment=?,
                content_revision=?, content_hash=?, updated_at=?
            WHERE id=? AND content_revision=?
        """, (normalized["conclusion_status"], normalized["coverage_method"],
              normalized["uncovered_reason"], normalized["comment"], revision + 1,
              digest, utc_sql(), int(record_id), revision))
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        if count != 1:
            raise ValueError("STALE_CONTENT_REVISION")
        return self.get_record(connection, record_id)

    def close_active_rejection(self, connection, scan_id, line_id,
                               terminal_reason="MANUAL_REANALYSIS"):
        """Terminate a rejection when a new manual relation is written.

        The rejection row remains immutable evidence of the old inherited
        relation; only its active/undo eligibility changes.  This update is
        deliberately idempotent so a retried save cannot create a second
        lineage transition.
        """
        cursor = execute(connection, """
            UPDATE coverage_inheritance_rejections
            SET is_active=0, terminal_reason=?, resolved_at=?,
                rejection_revision=rejection_revision+1
            WHERE scan_id=? AND line_id=? AND is_active=1
        """, (str(terminal_reason), utc_sql(), int(scan_id), int(line_id)))
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        return count

    def close_active_rejections_many(self, connection, scan_id, line_ids,
                                     terminal_reason="MANUAL_REANALYSIS"):
        """Close rejection lineage for a batch of physical lines."""
        line_ids = sorted({int(line_id) for line_id in (line_ids or [])})
        if not line_ids:
            return 0
        resolved = 0
        stamp = utc_sql()
        for batch in _chunks(line_ids):
            placeholders = ", ".join("?" for _ in batch)
            cursor = execute(connection, """
                UPDATE coverage_inheritance_rejections
                SET is_active=0, terminal_reason=?, resolved_at=?,
                    rejection_revision=rejection_revision+1
                WHERE scan_id=? AND line_id IN ({}) AND is_active=1
            """.format(placeholders),
                (str(terminal_reason), stamp, int(scan_id)) + tuple(batch))
            resolved += int(getattr(cursor, "rowcount", 0) or 0)
            cursor.close()
        return resolved

    def create_block(self, connection, scan_id, file_id, start_line, end_line,
                     record_id=None, created_by="", repository_id=None,
                     verified=True, content_hash_value=None):
        start_line, end_line = int(start_line), int(end_line)
        if start_line < 1 or end_line < start_line:
            raise ValueError("invalid analysis block range")
        file_row = fetchone(connection, """
            SELECT scan_id FROM coverage_files WHERE id=?
        """, (int(file_id),))
        if not file_row or int(file_row.get("scan_id") or 0) != int(scan_id):
            raise ValueError("ANALYSIS_BLOCK_FILE_SCAN_IDENTITY_MISMATCH")
        if repository_id is not None:
            repository_row = fetchone(connection, """
                SELECT repository_id FROM coverage_scan_repositories
                WHERE scan_id=? AND repository_id=?
            """, (int(scan_id), int(repository_id)))
            if not repository_row:
                raise ValueError("ANALYSIS_BLOCK_REPOSITORY_IDENTITY_MISMATCH")
        cursor = execute(connection, """
            INSERT INTO coverage_analysis_blocks(
                scan_id, repository_id, file_id, start_line, end_line, origin,
                block_identity_verified, originating_record_id, initial_content_hash,
                created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, 'MANUAL', ?, ?, ?, ?, ?)
        """, (int(scan_id), repository_id, int(file_id), start_line, end_line,
              int(bool(verified)), record_id, content_hash_value, created_by or "",
              utc_sql()))
        block_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, "SELECT * FROM coverage_analysis_blocks WHERE id=?",
                        (block_id,))

    def create_blocks_many(self, connection, blocks, origin="MANUAL", now=None):
        """Validate and create analysis blocks in bounded multi-row batches."""
        items = [dict(item or {}) for item in (blocks or [])]
        if not items:
            return []
        scan_ids = {int(item.get("scan_id") or 0) for item in items}
        if len(scan_ids) != 1 or 0 in scan_ids:
            raise ValueError("ANALYSIS_BLOCK_BATCH_SCAN_IDENTITY_MISMATCH")
        scan_id = next(iter(scan_ids))
        file_ids = sorted({int(item.get("file_id") or 0) for item in items})
        if any(file_id <= 0 for file_id in file_ids):
            raise ValueError("ANALYSIS_BLOCK_FILE_REQUIRED")
        placeholders = ", ".join("?" for _ in file_ids)
        file_rows = fetchall(connection, """
            SELECT id, scan_id FROM coverage_files
            WHERE id IN ({})
        """.format(placeholders), file_ids)
        files_by_id = {int(row["id"]): row for row in file_rows}
        if len(files_by_id) != len(file_ids) or any(
                int(files_by_id[file_id].get("scan_id") or 0) != scan_id
                for file_id in file_ids):
            raise ValueError("ANALYSIS_BLOCK_FILE_SCAN_IDENTITY_MISMATCH")

        repository_ids = sorted({int(item["repository_id"])
                                 for item in items
                                 if item.get("repository_id") is not None})
        if repository_ids:
            placeholders = ", ".join("?" for _ in repository_ids)
            repository_rows = fetchall(connection, """
                SELECT repository_id FROM coverage_scan_repositories
                WHERE scan_id=? AND repository_id IN ({})
            """.format(placeholders), [scan_id] + repository_ids)
            observed = {int(row["repository_id"]) for row in repository_rows}
            if observed != set(repository_ids):
                raise ValueError("ANALYSIS_BLOCK_REPOSITORY_IDENTITY_MISMATCH")

        record_ids = sorted({int(item.get("record_id") or 0) for item in items})
        if any(record_id <= 0 for record_id in record_ids):
            raise ValueError("ANALYSIS_BLOCK_RECORD_REQUIRED")
        placeholders = ", ".join("?" for _ in record_ids)
        record_rows = fetchall(connection, """
            SELECT id FROM coverage_analysis_records
            WHERE id IN ({})
        """.format(placeholders), record_ids)
        if {int(row["id"]) for row in record_rows} != set(record_ids):
            raise ValueError("ANALYSIS_RECORD_NOT_FOUND")

        stamp = now or utc_sql()
        params = []
        for item in items:
            start_line = int(item.get("start_line") or 0)
            end_line = int(item.get("end_line") or 0)
            if start_line < 1 or end_line < start_line:
                raise ValueError("invalid analysis block range")
            params.append((
                scan_id,
                item.get("repository_id"),
                int(item["file_id"]), start_line, end_line,
                int(bool(item.get("verified", True))),
                int(item["record_id"]), item.get("content_hash"),
                item.get("created_by") or "", stamp,
            ))
        ids = _bulk_insert_ids(connection, """
            INSERT INTO coverage_analysis_blocks(
                scan_id, repository_id, file_id, start_line, end_line, origin,
                block_identity_verified, originating_record_id, initial_content_hash,
                created_by, created_at
            )""", [
                (row[0], row[1], row[2], row[3], row[4], origin,
                 row[5], row[6], row[7], row[8], row[9])
                for row in params
            ])
        rows = []
        for batch in _chunks(ids):
            placeholders = ", ".join("?" for _ in batch)
            rows.extend(fetchall(connection, """
                SELECT * FROM coverage_analysis_blocks
                WHERE id IN ({})
            """.format(placeholders), batch))
        by_id = {int(row["id"]): row for row in rows}
        if len(by_id) != len(ids):
            raise RuntimeError("bulk analysis block readback is incomplete")
        return [by_id[int(block_id)] for block_id in ids]

    def create_link(self, connection, scan_id, line_id, record_id, block_id=None,
                    review_state=MANUAL_DRAFT, relation_origin="MANUAL",
                    reviewed_by="", reviewed_at=None, source_scan_id=None,
                    source_line_id=None, source_relation_id=None,
                    inheritance_group_id=None, expected_relation_revision=None):
        line = fetchone(connection, """
            SELECT l.id, f.scan_id, l.file_id
            FROM coverage_lines l JOIN coverage_files f ON f.id=l.file_id
            WHERE l.id=?
        """, (int(line_id),))
        if not line or int(line.get("scan_id") or 0) != int(scan_id):
            raise ValueError("LINE_SCAN_IDENTITY_MISMATCH")
        if not self.get_record(connection, record_id):
            raise ValueError("ANALYSIS_RECORD_NOT_FOUND")
        if block_id is not None:
            block = fetchone(connection, """
                SELECT scan_id, file_id FROM coverage_analysis_blocks WHERE id=?
            """, (int(block_id),))
            if (not block or int(block.get("scan_id") or 0) != int(scan_id) or
                    int(block.get("file_id") or 0) != int(line.get("file_id") or 0)):
                raise ValueError("ANALYSIS_BLOCK_IDENTITY_MISMATCH")
        if inheritance_group_id is not None:
            group = fetchone(connection, """
                SELECT candidate_scan_id, candidate_file_id
                FROM coverage_inheritance_groups WHERE id=?
            """, (int(inheritance_group_id),))
            if (not group or int(group.get("candidate_scan_id") or 0) != int(scan_id) or
                    int(group.get("candidate_file_id") or 0) != int(line.get("file_id") or 0)):
                raise ValueError("INHERITANCE_GROUP_IDENTITY_MISMATCH")
        source_values = (source_scan_id, source_line_id, source_relation_id)
        if any(value is not None for value in source_values):
            if not all(value is not None for value in source_values):
                raise ValueError("SOURCE_RELATION_IDENTITY_MISMATCH")
            source = fetchone(connection, """
                SELECT scan_id, line_id FROM coverage_analysis_line_links WHERE id=?
            """, (int(source_relation_id),))
            if (not source or int(source.get("scan_id") or 0) != int(source_scan_id) or
                    int(source.get("line_id") or 0) != int(source_line_id)):
                raise ValueError("SOURCE_RELATION_IDENTITY_MISMATCH")
        existing = self.get_link(connection, scan_id, line_id)
        stamp = utc_sql()
        if existing:
            current_revision = int(existing.get("relation_revision") or 0)
            if (expected_relation_revision is not None and
                    current_revision != int(expected_relation_revision)):
                raise ValueError("STALE_RELATION_REVISION")
            revision = current_revision + 1
            where = "WHERE scan_id=? AND line_id=?"
            params = (int(record_id), block_id, review_state, relation_origin,
                      inheritance_group_id, reviewed_by or "", reviewed_at,
                      source_scan_id, source_line_id, source_relation_id, revision,
                      stamp, int(scan_id), int(line_id))
            if expected_relation_revision is not None:
                where += " AND relation_revision=?"
                params += (int(expected_relation_revision),)
            cursor = execute(connection, """
                UPDATE coverage_analysis_line_links
                SET analysis_record_id=?, analysis_block_id=?, review_state=?,
                    relation_origin=?, inheritance_group_id=?, is_active=1,
                    reviewed_by=?, reviewed_at=?, source_scan_id=?, source_line_id=?,
                    source_relation_id=?, relation_revision=?, updated_at=?
                {where}
            """.format(where=where), params)
            if (expected_relation_revision is not None and
                    int(getattr(cursor, "rowcount", 0) or 0) != 1):
                cursor.close()
                raise ValueError("STALE_RELATION_REVISION")
            cursor.close()
            return self.get_link(connection, scan_id, line_id)
        if (expected_relation_revision is not None and
                int(expected_relation_revision) != 0):
            raise ValueError("STALE_RELATION_REVISION")
        cursor = execute(connection, """
            INSERT INTO coverage_analysis_line_links(
                scan_id, line_id, analysis_record_id, analysis_block_id,
                review_state, relation_origin, inheritance_group_id, is_active,
                reviewed_by, reviewed_at, source_scan_id, source_line_id,
                source_relation_id, relation_revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 1, ?, ?)
        """, (int(scan_id), int(line_id), int(record_id), block_id, review_state,
              relation_origin, inheritance_group_id, reviewed_by or "", reviewed_at,
              source_scan_id, source_line_id, source_relation_id, stamp, stamp))
        link_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, "SELECT * FROM coverage_analysis_line_links WHERE id=?",
                        (link_id,))

    def create_links_many(self, connection, links):
        """Validate and persist a batch of current line relations.

        Analysis content and blocks are still created by the service because
        they carry different human-operation identities.  The physical line
        relation is the hot path, however: validating every line through
        ``create_link`` caused a query fan-out for large saves.  This method
        resolves all referenced identities once, performs one bulk insert and
        one bulk CAS update, then reads the resulting rows in bounded chunks.
        """
        items = [dict(item or {}) for item in (links or [])]
        if not items:
            return []
        scan_ids = {int(item.get("scan_id") or 0) for item in items}
        if len(scan_ids) != 1 or 0 in scan_ids:
            raise ValueError("LINE_LINK_BATCH_SCAN_IDENTITY_MISMATCH")
        scan_id = next(iter(scan_ids))

        line_ids = [int(item.get("line_id") or 0) for item in items]
        if any(line_id <= 0 for line_id in line_ids):
            raise ValueError("LINE_ID_REQUIRED")
        if len(set(line_ids)) != len(line_ids):
            raise ValueError("DUPLICATE_LINE_IN_BATCH")

        def chunks(values, size=500):
            values = list(values)
            for start in range(0, len(values), size):
                yield values[start:start + size]

        def fetch_in(sql_template, values):
            rows = []
            for group in chunks(sorted(set(values))):
                placeholders = ", ".join("?" for _ in group)
                rows.extend(fetchall(
                    connection, sql_template.format(placeholders=placeholders), group
                ))
            return rows

        line_rows = fetch_in("""
            SELECT l.id, l.file_id, f.scan_id
            FROM coverage_lines l
            JOIN coverage_files f ON f.id=l.file_id
            WHERE l.id IN ({placeholders})
        """, line_ids)
        lines_by_id = {int(row["id"]): row for row in line_rows}
        if len(lines_by_id) != len(set(line_ids)) or any(
                int(lines_by_id[line_id].get("scan_id") or 0) != scan_id
                for line_id in line_ids):
            raise ValueError("LINE_SCAN_IDENTITY_MISMATCH")

        record_ids = [int(item.get("record_id") or 0) for item in items]
        if any(record_id <= 0 for record_id in record_ids):
            raise ValueError("ANALYSIS_RECORD_REQUIRED")
        record_rows = fetch_in("""
            SELECT id FROM coverage_analysis_records
            WHERE id IN ({placeholders})
        """, record_ids)
        records_by_id = {int(row["id"]): row for row in record_rows}
        if len(records_by_id) != len(set(record_ids)):
            raise ValueError("ANALYSIS_RECORD_NOT_FOUND")

        block_ids = [int(item["block_id"]) for item in items
                     if item.get("block_id") is not None]
        blocks_by_id = {}
        if block_ids:
            block_rows = fetch_in("""
                SELECT id, scan_id, file_id
                FROM coverage_analysis_blocks
                WHERE id IN ({placeholders})
            """, block_ids)
            blocks_by_id = {int(row["id"]): row for row in block_rows}
            if len(blocks_by_id) != len(set(block_ids)):
                raise ValueError("ANALYSIS_BLOCK_NOT_FOUND")

        group_ids = [int(item["inheritance_group_id"]) for item in items
                     if item.get("inheritance_group_id") is not None]
        groups_by_id = {}
        if group_ids:
            group_rows = fetch_in("""
                SELECT id, candidate_scan_id, candidate_file_id
                FROM coverage_inheritance_groups
                WHERE id IN ({placeholders})
            """, group_ids)
            groups_by_id = {int(row["id"]): row for row in group_rows}
            if len(groups_by_id) != len(set(group_ids)):
                raise ValueError("INHERITANCE_GROUP_NOT_FOUND")

        source_ids = [int(item["source_relation_id"]) for item in items
                      if item.get("source_relation_id") is not None]
        sources_by_id = {}
        if source_ids:
            source_rows = fetch_in("""
                SELECT id, scan_id, line_id
                FROM coverage_analysis_line_links
                WHERE id IN ({placeholders})
            """, source_ids)
            sources_by_id = {int(row["id"]): row for row in source_rows}
            if len(sources_by_id) != len(set(source_ids)):
                raise ValueError("SOURCE_RELATION_NOT_FOUND")

        # The scan id is a fixed predicate, so fetch it explicitly for every
        # bounded line-id chunk rather than passing it through the generic IN
        # helper.
        existing_rows = []
        for group in chunks(sorted(set(line_ids))):
            placeholders = ", ".join("?" for _ in group)
            existing_rows.extend(fetchall(connection, """
                SELECT * FROM coverage_analysis_line_links
                WHERE scan_id=? AND line_id IN ({})
            """.format(placeholders), [scan_id] + group))
        existing_by_line = {int(row["line_id"]): row for row in existing_rows}

        now = utc_sql()
        inserts = []
        updates = []
        update_count = 0
        for item in items:
            line_id = int(item["line_id"])
            line = lines_by_id[line_id]
            record_id = int(item["record_id"])
            block_id = int(item["block_id"]) if item.get("block_id") is not None else None
            if block_id is not None:
                block = blocks_by_id[block_id]
                if (int(block.get("scan_id") or 0) != scan_id or
                        int(block.get("file_id") or 0) != int(line["file_id"])):
                    raise ValueError("ANALYSIS_BLOCK_IDENTITY_MISMATCH")
            group_id = (int(item["inheritance_group_id"])
                        if item.get("inheritance_group_id") is not None else None)
            if group_id is not None:
                group = groups_by_id[group_id]
                if (int(group.get("candidate_scan_id") or 0) != scan_id or
                        int(group.get("candidate_file_id") or 0) != int(line["file_id"])):
                    raise ValueError("INHERITANCE_GROUP_IDENTITY_MISMATCH")

            source_values = (
                item.get("source_scan_id"), item.get("source_line_id"),
                item.get("source_relation_id"),
            )
            if any(value is not None for value in source_values):
                if not all(value is not None for value in source_values):
                    raise ValueError("SOURCE_RELATION_IDENTITY_MISMATCH")
                source = sources_by_id[int(item["source_relation_id"])]
                if (int(source.get("scan_id") or 0) != int(item["source_scan_id"]) or
                        int(source.get("line_id") or 0) != int(item["source_line_id"])):
                    raise ValueError("SOURCE_RELATION_IDENTITY_MISMATCH")

            existing = existing_by_line.get(line_id)
            expected = item.get("expected_relation_revision")
            if existing:
                current_revision = int(existing.get("relation_revision") or 0)
                if expected is not None and current_revision != int(expected):
                    raise ValueError("STALE_RELATION_REVISION")
                # Even callers that do not send an explicit revision get a
                # compare-and-swap against the revision observed above. This
                # prevents a concurrent write from being silently overwritten.
                updates.append((
                    int(record_id), block_id, item.get("review_state") or MANUAL_DRAFT,
                    item.get("relation_origin") or "MANUAL", group_id, int(bool(
                        item.get("is_active", True))), item.get("reviewed_by") or "",
                    item.get("reviewed_at"), item.get("source_scan_id"),
                    item.get("source_line_id"), item.get("source_relation_id"),
                    current_revision + 1, now, scan_id, line_id, current_revision,
                ))
                update_count += 1
            else:
                if expected is not None and int(expected) != 0:
                    raise ValueError("STALE_RELATION_REVISION")
                inserts.append((
                    scan_id, line_id, record_id, block_id,
                    item.get("review_state") or MANUAL_DRAFT,
                    item.get("relation_origin") or "MANUAL", group_id,
                    int(bool(item.get("is_active", True))), item.get("reviewed_by") or "",
                    item.get("reviewed_at"), item.get("source_scan_id"),
                    item.get("source_line_id"), item.get("source_relation_id"),
                    1, now, now,
                ))

        cursor = connection.cursor()
        try:
            if inserts:
                cursor.executemany(adapt_sql(connection, """
                    INSERT INTO coverage_analysis_line_links(
                        scan_id, line_id, analysis_record_id, analysis_block_id,
                        review_state, relation_origin, inheritance_group_id, is_active,
                        reviewed_by, reviewed_at, source_scan_id, source_line_id,
                        source_relation_id, relation_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """), inserts)
            if updates:
                cursor.executemany(adapt_sql(connection, """
                    UPDATE coverage_analysis_line_links
                    SET analysis_record_id=?, analysis_block_id=?, review_state=?,
                        relation_origin=?, inheritance_group_id=?, is_active=?,
                        reviewed_by=?, reviewed_at=?, source_scan_id=?, source_line_id=?,
                        source_relation_id=?, relation_revision=?, updated_at=?
                    WHERE scan_id=? AND line_id=? AND relation_revision=?
                """), updates)
                if int(getattr(cursor, "rowcount", 0) or 0) != update_count:
                    raise ValueError("STALE_RELATION_REVISION")
        finally:
            cursor.close()

        result = []
        for group in chunks(line_ids):
            placeholders = ", ".join("?" for _ in group)
            result.extend(fetchall(connection, """
                SELECT * FROM coverage_analysis_line_links
                WHERE scan_id=? AND line_id IN ({})
            """.format(placeholders), [scan_id] + group))
        by_line = {int(row["line_id"]): row for row in result}
        return [by_line[line_id] for line_id in line_ids if line_id in by_line]

    def backfill_legacy(self, connection, scan_id=None):
        """Idempotently project old analysis facts into the canonical domain."""
        clauses = []
        params = []
        if scan_id is not None:
            clauses.append("f.scan_id=?")
            params.append(int(scan_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = fetchall(connection, """
            SELECT a.*, l.id AS physical_line_id, l.file_id, f.scan_id
            FROM coverage_analyses a
            JOIN coverage_lines l ON l.id=a.line_id
            JOIN coverage_files f ON f.id=l.file_id
            {where}
            ORDER BY a.id
        """.format(where=where), params)
        if not rows:
            return {"created": 0, "scanned": 0}

        def chunks(sequence, size=500):
            for start in range(0, len(sequence), size):
                yield sequence[start:start + size]

        line_ids = [int(row["physical_line_id"]) for row in rows]
        existing_links = []
        for batch in chunks(sorted(set(line_ids))):
            placeholders = ", ".join("?" for _ in batch)
            params = list(batch)
            sql = """
                SELECT scan_id, line_id, analysis_record_id
                FROM coverage_analysis_line_links
                WHERE line_id IN ({placeholders})
            """.format(placeholders=placeholders)
            if scan_id is not None:
                sql += " AND scan_id=?"
                params.append(int(scan_id))
            existing_links.extend(fetchall(connection, sql, params))
        existing_by_line = {
            (int(row["scan_id"]), int(row["line_id"])): row
            for row in existing_links
        }
        pending = [
            row for row in rows
            if (int(row["scan_id"]), int(row["physical_line_id"]))
            not in existing_by_line
        ]
        if not pending:
            return {"created": 0, "scanned": len(rows)}

        source_ids = [int(row["id"]) for row in pending]
        existing_records = []
        for batch in chunks(sorted(set(source_ids))):
            placeholders = ", ".join("?" for _ in batch)
            existing_records.extend(fetchall(connection, """
                SELECT * FROM coverage_analysis_records
                WHERE legacy_source_analysis_id IN ({})
            """.format(placeholders), batch))
        records_by_source = {
            int(row["legacy_source_analysis_id"]): row
            for row in existing_records
        }
        new_record_values = []
        for row in pending:
            if int(row["id"]) in records_by_source:
                continue
            new_record_values.append({
                "status": row.get("status"),
                "coverage_method": row.get("coverage_method"),
                "uncovered_reason": row.get("uncovered_reason"),
                "comment": row.get("comment"),
                "legacy_source_analysis_id": row.get("id"),
                "legacy_source_created_at": row.get("created_at"),
                "legacy_source_updated_at": row.get("updated_at"),
                "legacy_raw_status": row.get("status"),
                "legacy_raw_is_draft": row.get("is_draft"),
            })
        if new_record_values:
            records_by_source.update({
                int(row["legacy_source_analysis_id"]): row
                for row in self.create_records_many(
                    connection, new_record_values, origin="LEGACY_MIGRATED"
                )
            })

        links = []
        for row in pending:
            record = records_by_source.get(int(row["id"]))
            if not record:
                raise RuntimeError("legacy analysis record readback is incomplete")
            state = MANUAL_DRAFT if int(row.get("is_draft") or 0) else (
                MANUAL_CONFIRMED if row.get("status") in CONFIRMED_STATUSES
                else MANUAL_DRAFT
            )
            links.append({
                "scan_id": int(row["scan_id"]),
                "line_id": int(row["physical_line_id"]),
                "record_id": int(record["id"]),
                "review_state": state,
                "relation_origin": "LEGACY_MIGRATED",
                "reviewed_by": row.get("reviewer") or "",
                "reviewed_at": row.get("updated_at"),
            })
        # A link batch is intentionally scoped to one scan: the repository
        # validates scan/file/line identity before doing its bulk write.  The
        # legacy table can contain several scans, so partition here rather
        # than weakening that fail-closed boundary.
        links_by_scan = {}
        for link in links:
            links_by_scan.setdefault(int(link["scan_id"]), []).append(link)
        created = 0
        for scan_links in links_by_scan.values():
            for batch in chunks(scan_links):
                self.create_links_many(connection, batch)
                created += len(batch)
        return {"created": created, "scanned": len(rows),
                "record_batch": len(new_record_values), "link_batch": created,
                "scan_batches": len(links_by_scan)}
