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

    def create_block(self, connection, scan_id, file_id, start_line, end_line,
                     record_id=None, created_by="", repository_id=None,
                     verified=True, content_hash_value=None):
        start_line, end_line = int(start_line), int(end_line)
        if start_line < 1 or end_line < start_line:
            raise ValueError("invalid analysis block range")
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

    def create_link(self, connection, scan_id, line_id, record_id, block_id=None,
                    review_state=MANUAL_DRAFT, relation_origin="MANUAL",
                    reviewed_by="", reviewed_at=None, source_scan_id=None,
                    source_line_id=None, source_relation_id=None,
                    inheritance_group_id=None):
        existing = self.get_link(connection, scan_id, line_id)
        stamp = utc_sql()
        if existing:
            revision = int(existing.get("relation_revision") or 0) + 1
            cursor = execute(connection, """
                UPDATE coverage_analysis_line_links
                SET analysis_record_id=?, analysis_block_id=?, review_state=?,
                    relation_origin=?, inheritance_group_id=?, is_active=1,
                    reviewed_by=?, reviewed_at=?, source_scan_id=?, source_line_id=?,
                    source_relation_id=?, relation_revision=?, updated_at=?
                WHERE scan_id=? AND line_id=?
            """, (int(record_id), block_id, review_state, relation_origin,
                  inheritance_group_id, reviewed_by or "", reviewed_at,
                  source_scan_id, source_line_id, source_relation_id, revision,
                  stamp, int(scan_id), int(line_id)))
            cursor.close()
            return self.get_link(connection, scan_id, line_id)
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
        created = 0
        for row in rows:
            if self.get_link(connection, row["scan_id"], row["physical_line_id"]):
                continue
            record = self.create_record(connection, {
                "status": row.get("status"), "coverage_method": row.get("coverage_method"),
                "uncovered_reason": row.get("uncovered_reason"), "comment": row.get("comment"),
                "legacy_source_analysis_id": row.get("id"),
                "legacy_source_created_at": row.get("created_at"),
                "legacy_source_updated_at": row.get("updated_at"),
                "legacy_raw_status": row.get("status"),
                "legacy_raw_is_draft": row.get("is_draft"),
            }, origin="LEGACY_MIGRATED")
            state = MANUAL_DRAFT if int(row.get("is_draft") or 0) else (
                MANUAL_CONFIRMED if row.get("status") in CONFIRMED_STATUSES else MANUAL_DRAFT
            )
            self.create_link(connection, row["scan_id"], row["physical_line_id"],
                             record["id"], review_state=state,
                             relation_origin="LEGACY_MIGRATED",
                             reviewed_by=row.get("reviewer") or "",
                             reviewed_at=row.get("updated_at"))
            created += 1
        return {"created": created, "scanned": len(rows)}
