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
