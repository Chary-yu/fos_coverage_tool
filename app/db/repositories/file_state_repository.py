"""Rebuildable progress state derived from CoverageLine and Analysis facts."""

from typing import Any, Dict, Iterable, List

from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone, is_sqlite


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")


class FileStateRepository(object):
    _UNCOVERED_SQL = "LOWER(COALESCE(l.coverage_state, '')) IN ('uncovered', 'uncovered_line', 'uncovered-code', '0', '未覆盖')"
    # The old coverage_analyses table remains a compatibility projection.  A
    # current line link is authoritative whenever it exists, including when
    # its content is intentionally empty or still pending inheritance.
    _STATUS_SQL = "CASE WHEN q.id IS NOT NULL THEN COALESCE(r.conclusion_status, '') ELSE COALESCE(a.status, '') END"
    _DRAFT_SQL = "CASE WHEN q.id IS NOT NULL THEN CASE WHEN q.review_state IN ('MANUAL_DRAFT', 'INHERITED_PENDING') THEN 1 ELSE 0 END ELSE COALESCE(a.is_draft, 0) END"
    _CONFIRMED_SQL = "({status}) IN ('可覆盖', '无法覆盖', '冗余代码') AND ({draft}) = 0".format(
        status=_STATUS_SQL, draft=_DRAFT_SQL
    )
    _ORDINARY_PENDING_SQL = "({uncovered} AND q.id IS NULL AND NOT ({confirmed}))".format(
        uncovered=_UNCOVERED_SQL, confirmed=_CONFIRMED_SQL
    )
    _INHERITED_PENDING_SQL = "({uncovered} AND q.id IS NOT NULL AND q.review_state = 'INHERITED_PENDING')".format(
        uncovered=_UNCOVERED_SQL
    )
    _MANUAL_DRAFT_PENDING_SQL = "({uncovered} AND q.id IS NOT NULL AND q.review_state <> 'INHERITED_PENDING' AND NOT ({confirmed}))".format(
        uncovered=_UNCOVERED_SQL, confirmed=_CONFIRMED_SQL
    )

    def get(self, connection, scan_id: int, file_id: int):
        return fetchone(connection, """
            SELECT * FROM coverage_file_state WHERE scan_id = ? AND file_id = ?
        """, (scan_id, file_id))

    def authoritative_file_summary(self, connection, scan_id: int, file_id: int):
        rows = fetchall(connection, """
            SELECT l.id, l.line_number, l.coverage_state,
                   {status} AS status, {draft} AS is_draft,
                   q.review_state
            FROM coverage_lines l
            JOIN coverage_files f ON f.id = l.file_id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            LEFT JOIN coverage_analysis_line_links q
              ON q.scan_id = f.scan_id AND q.line_id = l.id AND q.is_active = 1
            LEFT JOIN coverage_analysis_records r ON r.id = q.analysis_record_id
            WHERE l.file_id = ? AND f.scan_id = ? ORDER BY l.line_number
        """.format(status=self._STATUS_SQL, draft=self._DRAFT_SQL), (file_id, scan_id))
        total = len(rows)
        uncovered = [row for row in rows if str(row.get("coverage_state") or "").lower() in
                     ("uncovered", "uncovered_line", "uncovered-code", "0", "未覆盖")]
        confirmed = [
            row for row in uncovered
            if not int(row.get("is_draft") or 0) and row.get("status") in CONFIRMED_STATUSES
        ]
        pending = [row for row in uncovered if row not in confirmed]
        ordinary_pending = [row for row in pending if not row.get("review_state")]
        inherited_pending = [
            row for row in pending if row.get("review_state") == "INHERITED_PENDING"
        ]
        manual_draft_pending = [
            row for row in pending
            if row.get("review_state") and row.get("review_state") != "INHERITED_PENDING"
        ]
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
            "ordinary_pending_total": len(ordinary_pending),
            "inherited_pending_total": len(inherited_pending),
            "manual_draft_pending_total": len(manual_draft_pending),
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
            summary["ordinary_pending_total"], summary["inherited_pending_total"],
            summary["manual_draft_pending_total"],
            int(data_version),
        )
        if existing:
            cursor = execute(connection, """
                UPDATE coverage_file_state SET total_lines = ?, total_uncovered = ?,
                    filled_total = ?, draft_total = ?, confirmed_total = ?, pending_total = ?,
                    ordinary_pending_total = ?, inherited_pending_total = ?,
                    manual_draft_pending_total = ?, data_version = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE scan_id = ? AND file_id = ?
            """, fields + (scan_id, file_id))
            cursor.close()
        else:
            cursor = execute(connection, """
                INSERT INTO coverage_file_state(
                    scan_id, file_id, total_lines, total_uncovered, filled_total, draft_total,
                    confirmed_total, pending_total, ordinary_pending_total,
                    inherited_pending_total, manual_draft_pending_total,
                    data_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
        ordinary = self._ORDINARY_PENDING_SQL
        inherited = self._INHERITED_PENDING_SQL
        manual_draft = self._MANUAL_DRAFT_PENDING_SQL
        grouped_select = """
            SELECT f.scan_id, f.id,
                   COUNT(l.id),
                   COALESCE(SUM(CASE WHEN {uncovered} THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN ({status}) <> '' THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN ({draft}) = 1 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN {uncovered} AND {confirmed} THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN {uncovered} AND NOT ({confirmed}) THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN {ordinary} THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN {inherited} THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN {manual_draft} THEN 1 ELSE 0 END), 0),
                   ?, CURRENT_TIMESTAMP
            FROM coverage_files f
            LEFT JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            LEFT JOIN coverage_analysis_line_links q
              ON q.scan_id = f.scan_id AND q.line_id = l.id AND q.is_active = 1
            LEFT JOIN coverage_analysis_records r ON r.id = q.analysis_record_id
            WHERE f.scan_id = ?
            GROUP BY f.scan_id, f.id
        """.format(uncovered=uncovered, status=self._STATUS_SQL,
                   draft=self._DRAFT_SQL, confirmed=confirmed, ordinary=ordinary,
                   inherited=inherited, manual_draft=manual_draft)
        if is_sqlite(connection):
            upsert_suffix = """
                ON CONFLICT(scan_id, file_id) DO UPDATE SET
                    total_lines = excluded.total_lines,
                    total_uncovered = excluded.total_uncovered,
                    filled_total = excluded.filled_total,
                    draft_total = excluded.draft_total,
                    confirmed_total = excluded.confirmed_total,
                    pending_total = excluded.pending_total,
                    ordinary_pending_total = excluded.ordinary_pending_total,
                    inherited_pending_total = excluded.inherited_pending_total,
                    manual_draft_pending_total = excluded.manual_draft_pending_total,
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
                    ordinary_pending_total = VALUES(ordinary_pending_total),
                    inherited_pending_total = VALUES(inherited_pending_total),
                    manual_draft_pending_total = VALUES(manual_draft_pending_total),
                    data_version = VALUES(data_version),
                    updated_at = VALUES(updated_at)
            """
        insert_sql = """
            INSERT INTO coverage_file_state(
                scan_id, file_id, total_lines, total_uncovered, filled_total,
                draft_total, confirmed_total, pending_total, ordinary_pending_total,
                inherited_pending_total, manual_draft_pending_total,
                data_version, updated_at
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
                   COALESCE(SUM(pending_total), 0) AS pending_total,
                   COALESCE(SUM(ordinary_pending_total), 0) AS ordinary_pending_total,
                   COALESCE(SUM(inherited_pending_total), 0) AS inherited_pending_total,
                   COALESCE(SUM(manual_draft_pending_total), 0) AS manual_draft_pending_total
            FROM coverage_file_state WHERE scan_id = ?
        """, (scan_id,))

    def file_state_completeness(self, connection, scan_id: int,
                                data_version=None):
        """Return the file-state coverage/version gate for one immutable Scan.

        ``coverage_file_state`` is a rebuildable projection.  A row count that
        merely happens to be non-zero is not enough: every physical file must
        have exactly one state row, and every row must carry the version being
        published.  Keep this check in SQL so the online read path never has
        to materialize a large project.
        """
        if data_version is not None:
            row = fetchone(connection, """
                SELECT COUNT(DISTINCT f.id) AS expected_file_count,
                       COUNT(DISTINCT fs.file_id) AS state_file_count,
                       COALESCE(SUM(CASE WHEN fs.file_id IS NULL THEN 1 ELSE 0 END), 0)
                           AS missing_file_count,
                       COALESCE(SUM(CASE WHEN fs.file_id IS NOT NULL
                                              AND fs.data_version = ?
                                         THEN 0 ELSE 1 END), 0) AS stale_file_count
                FROM coverage_files f
                LEFT JOIN coverage_file_state fs
                  ON fs.scan_id = f.scan_id AND fs.file_id = f.id
                WHERE f.scan_id = ?
                """, (int(data_version), int(scan_id)))
        else:
            row = fetchone(connection, """
                SELECT COUNT(DISTINCT f.id) AS expected_file_count,
                       COUNT(DISTINCT fs.file_id) AS state_file_count,
                       COALESCE(SUM(CASE WHEN fs.file_id IS NULL THEN 1 ELSE 0 END), 0)
                           AS missing_file_count,
                       0 AS stale_file_count
                FROM coverage_files f
                LEFT JOIN coverage_file_state fs
                  ON fs.scan_id = f.scan_id AND fs.file_id = f.id
                WHERE f.scan_id = ?
            """, (int(scan_id),))
        orphan_row = fetchone(connection, """
            SELECT COUNT(*) AS orphan_state_count
            FROM coverage_file_state fs
            LEFT JOIN coverage_files f
              ON f.id = fs.file_id AND f.scan_id = fs.scan_id
            WHERE fs.scan_id = ? AND f.id IS NULL
        """, (int(scan_id),)) or {}
        row = row or {}
        expected = int(row.get("expected_file_count") or 0)
        states = int(row.get("state_file_count") or 0)
        missing = int(row.get("missing_file_count") or 0)
        stale = int(row.get("stale_file_count") or 0)
        orphaned = int(orphan_row.get("orphan_state_count") or 0)
        return {
            "scan_id": int(scan_id),
            "data_version": None if data_version is None else int(data_version),
            "expected_file_count": expected,
            "state_file_count": states,
            "missing_file_count": missing,
            "stale_file_count": stale,
            "orphan_state_count": orphaned,
            "status": "PASSED" if expected == states and not missing and
                       not stale and not orphaned
                       else "FAILED",
        }

    def pending_conservation(self, connection, scan_id: int):
        """Verify pending partition and file-row completeness in one query."""
        row = fetchone(connection, """
                SELECT COUNT(DISTINCT f.id) AS expected_file_count,
                       COUNT(DISTINCT fs.file_id) AS state_file_count,
                       COALESCE(SUM(CASE WHEN fs.file_id IS NULL THEN 1 ELSE 0 END), 0)
                           AS missing_file_count,
                       COALESCE(SUM(CASE WHEN fs.file_id IS NOT NULL AND
                       fs.pending_total = fs.ordinary_pending_total +
                       fs.inherited_pending_total + fs.manual_draft_pending_total
                       THEN 0 WHEN fs.file_id IS NULL THEN 0 ELSE 1 END), 0)
                       AS mismatched_files,
                   COALESCE(SUM(CASE WHEN fs.file_id IS NOT NULL THEN fs.pending_total ELSE 0 END), 0)
                       AS pending_total,
                   COALESCE(SUM(CASE WHEN fs.file_id IS NOT NULL THEN fs.ordinary_pending_total ELSE 0 END), 0)
                       AS ordinary_pending_total,
                   COALESCE(SUM(CASE WHEN fs.file_id IS NOT NULL THEN fs.inherited_pending_total ELSE 0 END), 0)
                       AS inherited_pending_total,
                   COALESCE(SUM(CASE WHEN fs.file_id IS NOT NULL THEN fs.manual_draft_pending_total ELSE 0 END), 0)
                       AS manual_draft_pending_total
            FROM coverage_files f
            LEFT JOIN coverage_file_state fs
              ON fs.scan_id = f.scan_id AND fs.file_id = f.id
            WHERE f.scan_id=?
        """, (int(scan_id),)) or {}
        expected = int(row.get("expected_file_count") or 0)
        states = int(row.get("state_file_count") or 0)
        missing = int(row.get("missing_file_count") or 0)
        mismatched = int(row.get("mismatched_files") or 0)
        orphan_row = fetchone(connection, """
            SELECT COUNT(*) AS orphan_state_count
            FROM coverage_file_state fs
            LEFT JOIN coverage_files f
              ON f.id = fs.file_id AND f.scan_id = fs.scan_id
            WHERE fs.scan_id = ? AND f.id IS NULL
        """, (int(scan_id),)) or {}
        orphaned = int(orphan_row.get("orphan_state_count") or 0)
        # A missing row is a mismatch for the gate even though its totals are
        # represented as zero by the LEFT JOIN.
        mismatched += missing
        mismatched += orphaned
        return {
            "scan_id": int(scan_id),
            "file_count": states,
            "expected_file_count": expected,
            "state_file_count": states,
            "missing_file_count": missing,
            "orphan_state_count": orphaned,
            "mismatched_files": mismatched,
            "pending_total": int(row.get("pending_total") or 0),
            "ordinary_pending_total": int(row.get("ordinary_pending_total") or 0),
            "inherited_pending_total": int(row.get("inherited_pending_total") or 0),
            "manual_draft_pending_total": int(row.get("manual_draft_pending_total") or 0),
            "status": "PASSED" if expected == states and not mismatched else "FAILED",
        }

    def scan_summary_from_facts(self, connection, scan_id: int):
        """Aggregate authoritative facts in SQL for stale/empty derived state."""
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        ordinary = self._ORDINARY_PENDING_SQL
        inherited = self._INHERITED_PENDING_SQL
        manual_draft = self._MANUAL_DRAFT_PENDING_SQL
        row = fetchone(connection, """
            SELECT COUNT(DISTINCT f.id) AS file_count,
                   COUNT(l.id) AS total_lines,
                   COALESCE(SUM(CASE WHEN {uncovered} THEN 1 ELSE 0 END), 0) AS total_uncovered,
                   COALESCE(SUM(CASE WHEN ({status}) <> '' THEN 1 ELSE 0 END), 0) AS filled_total,
                   COALESCE(SUM(CASE WHEN ({draft}) = 1 THEN 1 ELSE 0 END), 0) AS draft_total,
                   COALESCE(SUM(CASE WHEN {uncovered} AND {confirmed} THEN 1 ELSE 0 END), 0) AS confirmed_total,
                   COALESCE(SUM(CASE WHEN {uncovered} AND NOT ({confirmed}) THEN 1 ELSE 0 END), 0) AS pending_total,
                   COALESCE(SUM(CASE WHEN {ordinary} THEN 1 ELSE 0 END), 0) AS ordinary_pending_total,
                   COALESCE(SUM(CASE WHEN {inherited} THEN 1 ELSE 0 END), 0) AS inherited_pending_total,
                   COALESCE(SUM(CASE WHEN {manual_draft} THEN 1 ELSE 0 END), 0) AS manual_draft_pending_total
            FROM coverage_files f
            LEFT JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            LEFT JOIN coverage_analysis_line_links q
              ON q.scan_id = f.scan_id AND q.line_id = l.id AND q.is_active = 1
            LEFT JOIN coverage_analysis_records r ON r.id = q.analysis_record_id
            WHERE f.scan_id = ?
        """.format(uncovered=uncovered, status=self._STATUS_SQL,
                   draft=self._DRAFT_SQL, confirmed=confirmed, ordinary=ordinary,
                   inherited=inherited, manual_draft=manual_draft), (scan_id,))
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
            "ordinary_pending_total": int(row.get("ordinary_pending_total") or 0),
            "inherited_pending_total": int(row.get("inherited_pending_total") or 0),
            "manual_draft_pending_total": int(row.get("manual_draft_pending_total") or 0),
            "pending_line_references": [],
        }

    def file_page(self, connection, scan_id: int, limit=200, cursor=None,
                  pending_only=False, repository_name=None,
                  data_version=None, derived_ready=None):
        """Return a bounded file window without materializing physical lines.

        ``pending_only`` keeps the historical developer-task query narrow;
        progress pages can request all file aggregates through the same
        keyset/capability-aware path.
        """
        page_limit = min(500, max(1, int(limit or 200)))
        after_id = int((cursor or {}).get("file_id") or 0)
        repository_clause = ""
        repository_params = []
        if repository_name is not None:
            repository_clause = " AND f.repository_name = ?"
            repository_params.append(str(repository_name or ""))
        if data_version is not None:
            coverage_sql = """
                SELECT COUNT(DISTINCT f.id) AS files,
                       COUNT(DISTINCT fs.file_id) AS states,
                       COUNT(DISTINCT CASE WHEN fs.data_version = ?
                                           THEN fs.file_id END) AS matching_states
                FROM coverage_files f
                LEFT JOIN coverage_file_state fs
                  ON fs.scan_id=f.scan_id AND fs.file_id=f.id
                WHERE f.scan_id=? {repository_clause}
            """
            coverage_params = (int(data_version), int(scan_id)) + tuple(repository_params)
        else:
            coverage_sql = """
                SELECT COUNT(DISTINCT f.id) AS files,
                       COUNT(DISTINCT fs.file_id) AS states,
                       COUNT(DISTINCT fs.file_id) AS matching_states
                FROM coverage_files f
                LEFT JOIN coverage_file_state fs
                  ON fs.scan_id=f.scan_id AND fs.file_id=f.id
                WHERE f.scan_id=? {repository_clause}
            """
            coverage_params = (int(scan_id),) + tuple(repository_params)
        coverage = fetchone(connection, coverage_sql.format(
            repository_clause=repository_clause
        ), coverage_params) or {}
        use_derived = (
            derived_ready is not False and
            int(coverage.get("files") or 0) > 0 and
            int(coverage.get("files") or 0) ==
            int(coverage.get("matching_states") or 0)
        )
        if use_derived:
            derived_filter = "f.scan_id=? AND f.id>?{}".format(repository_clause)
            if pending_only:
                derived_filter += " AND fs.pending_total > 0"
            rows = fetchall(connection, """
                SELECT f.id AS file_id, f.repository_name, f.file_path,
                       fs.total_lines, fs.total_uncovered, fs.filled_total,
                       fs.draft_total, fs.confirmed_total, fs.pending_total,
                       fs.ordinary_pending_total, fs.inherited_pending_total,
                       fs.manual_draft_pending_total
                FROM coverage_files f
                JOIN coverage_file_state fs
                  ON fs.scan_id=f.scan_id AND fs.file_id=f.id
                WHERE {where}
                ORDER BY f.id
                LIMIT ?
            """.format(where=derived_filter),
                (int(scan_id), after_id) + tuple(repository_params) +
                (page_limit + 1,))
        else:
            uncovered = self._UNCOVERED_SQL
            confirmed = self._CONFIRMED_SQL
            grouped_filter = "grouped.file_id > ?"
            if repository_name is not None:
                grouped_filter += " AND grouped.repository_name = ?"
            if pending_only:
                grouped_filter = "grouped.pending_total > 0 AND " + grouped_filter
            rows = fetchall(connection, """
                SELECT * FROM (
                    SELECT f.id AS file_id, f.repository_name, f.file_path,
                           COUNT(l.id) AS total_lines,
                           COALESCE(SUM(CASE WHEN {uncovered} THEN 1 ELSE 0 END), 0)
                               AS total_uncovered,
                           COALESCE(SUM(CASE WHEN ({status}) <> '' THEN 1 ELSE 0 END), 0)
                               AS filled_total,
                           COALESCE(SUM(CASE WHEN ({draft}) = 1 THEN 1 ELSE 0 END), 0)
                               AS draft_total,
                           COALESCE(SUM(CASE WHEN {uncovered} AND {confirmed}
                                             THEN 1 ELSE 0 END), 0)
                               AS confirmed_total,
                           COALESCE(SUM(CASE WHEN {uncovered} AND NOT ({confirmed})
                                             THEN 1 ELSE 0 END), 0)
                               AS pending_total,
                           COALESCE(SUM(CASE WHEN {ordinary} THEN 1 ELSE 0 END), 0)
                               AS ordinary_pending_total,
                           COALESCE(SUM(CASE WHEN {inherited} THEN 1 ELSE 0 END), 0)
                               AS inherited_pending_total,
                           COALESCE(SUM(CASE WHEN {manual} THEN 1 ELSE 0 END), 0)
                               AS manual_draft_pending_total
                    FROM coverage_files f
                    LEFT JOIN coverage_lines l ON l.file_id=f.id
                    LEFT JOIN coverage_analyses a ON a.line_id=l.id
                    LEFT JOIN coverage_analysis_line_links q
                      ON q.scan_id=f.scan_id AND q.line_id=l.id AND q.is_active=1
                    LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
                    WHERE f.scan_id=? {repository_clause}
                    GROUP BY f.id, f.repository_name, f.file_path
                ) grouped
                WHERE {where}
                ORDER BY grouped.file_id
                LIMIT ?
            """.format(
                uncovered=uncovered, status=self._STATUS_SQL, draft=self._DRAFT_SQL,
                confirmed=confirmed, ordinary=self._ORDINARY_PENDING_SQL,
                inherited=self._INHERITED_PENDING_SQL,
                manual=self._MANUAL_DRAFT_PENDING_SQL,
                repository_clause=repository_clause,
                where=grouped_filter,
            ),
                (int(scan_id),) + tuple(repository_params) +
                (after_id,) + ((str(repository_name or ""),)
                               if repository_name is not None else ()) +
                (page_limit + 1,))
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        result = []
        for row in rows:
            item = dict(row)
            item["scan_id"] = int(scan_id)
            item["file_id"] = int(item.get("file_id") or 0)
            item["repository_name"] = item.get("repository_name") or ""
            item["file_path"] = item.get("file_path") or ""
            item["unanalyzed"] = int(item.get("pending_total") or 0)
            # The legacy field remains present but deliberately empty: the
            # homepage must never materialize a large physical pending list.
            item["pending_line_numbers"] = (
                self.pending_line_numbers_for_file(
                    connection, int(scan_id), item["file_id"], limit=200
                ) if pending_only and item["unanalyzed"] <= 200 else []
            )
            result.append(item)
        next_cursor = (
            {"file_id": result[-1]["file_id"]} if has_more and result else None
        )
        return {"rows": result, "has_more": has_more, "next_cursor": next_cursor}

    def pending_file_page(self, connection, scan_id: int, limit=200, cursor=None,
                          repository_name=None, data_version=None,
                          derived_ready=None):
        """Compatibility wrapper for the bounded pending-file API."""
        return self.file_page(
            connection, scan_id, limit=limit, cursor=cursor, pending_only=True,
            repository_name=repository_name, data_version=data_version,
            derived_ready=derived_ready,
        )

    def pending_line_numbers_for_file(self, connection, scan_id, file_id, limit=200):
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        rows = fetchall(connection, """
            SELECT l.line_number
            FROM coverage_lines l
            LEFT JOIN coverage_analyses a ON a.line_id=l.id
            LEFT JOIN coverage_analysis_line_links q
              ON q.scan_id=? AND q.line_id=l.id AND q.is_active=1
            LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
            WHERE l.file_id=? AND {uncovered} AND NOT ({confirmed})
            ORDER BY l.line_number
            LIMIT ?
        """.format(uncovered=uncovered, confirmed=confirmed),
            (int(scan_id), int(file_id), min(200, max(1, int(limit or 200)))))
        return [int(row["line_number"]) for row in rows]

    def pending_line_references(self, connection, scan_id: int, limit=None,
                                cursor=None):
        """Return a keyset window of pending physical lines for a scan."""
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        sql = """
            SELECT f.id AS file_id, l.id AS line_id,
                   f.repository_name, f.file_path, l.line_number
            FROM coverage_files f
            JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            LEFT JOIN coverage_analysis_line_links q
              ON q.scan_id = f.scan_id AND q.line_id = l.id AND q.is_active = 1
            LEFT JOIN coverage_analysis_records r ON r.id = q.analysis_record_id
            WHERE f.scan_id = ? AND {uncovered} AND NOT ({confirmed})
        """.format(uncovered=uncovered, confirmed=confirmed)
        params = [int(scan_id)]
        if cursor:
            try:
                file_id = int(cursor["file_id"])
                line_number = int(cursor["line_number"])
                line_id = int(cursor["line_id"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("PAGINATION_CURSOR_STALE")
            sql += """
              AND (
                    f.id > ?
                    OR (f.id = ? AND l.line_number > ?)
                    OR (f.id = ? AND l.line_number = ? AND l.id > ?)
              )
            """
            params.extend((file_id, file_id, line_number,
                           file_id, line_number, line_id))
        sql += """
            ORDER BY f.id, l.line_number, l.id
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = fetchall(connection, sql, params)
        return [{
            "file_id": int(row["file_id"]),
            "line_id": int(row["line_id"]),
            "repository_name": row.get("repository_name") or "",
            "file_path": row.get("file_path") or "",
            "line_number": int(row["line_number"]),
        } for row in rows]

    def line_detail_page(self, connection, scan_id: int, file_id: int,
                         limit=200, cursor=None):
        """Return one bounded file-detail window using a line keyset."""
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        sql = """
            SELECT l.id AS line_id, l.line_number, l.line_text, l.coverage_state,
                   l.suggested_reviewer,
                   CASE WHEN x.id IS NOT NULL THEN ''
                        WHEN q.id IS NOT NULL THEN r.conclusion_status
                        ELSE a.status END AS status,
                   CASE WHEN x.id IS NOT NULL THEN 1
                        WHEN q.id IS NOT NULL THEN
                            CASE WHEN q.review_state IN ('MANUAL_DRAFT', 'INHERITED_PENDING')
                                 THEN 1 ELSE 0 END
                        ELSE a.is_draft END AS is_draft,
                   CASE WHEN x.id IS NOT NULL THEN ''
                        WHEN q.id IS NOT NULL THEN q.reviewed_by
                        ELSE a.reviewer END AS reviewer,
                   CASE WHEN x.id IS NOT NULL THEN ''
                        WHEN q.id IS NOT NULL THEN r.coverage_method
                        ELSE a.coverage_method END AS coverage_method,
                   CASE WHEN x.id IS NOT NULL THEN ''
                        WHEN q.id IS NOT NULL THEN r.uncovered_reason
                        ELSE a.uncovered_reason END AS uncovered_reason,
                   CASE WHEN x.id IS NOT NULL THEN x.rejected_at
                        WHEN q.id IS NOT NULL THEN r.updated_at
                        ELSE a.updated_at END AS updated_at,
                   CASE WHEN x.id IS NOT NULL THEN 'INHERITANCE_REJECTED'
                        ELSE q.review_state END AS review_state,
                   q.relation_origin, q.analysis_record_id, q.relation_revision,
                   q.is_active AS relation_is_active,
                   x.id AS rejection_id, x.rejection_revision
            FROM coverage_lines l
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            LEFT JOIN coverage_analysis_line_links q
              ON q.scan_id=? AND q.line_id=l.id AND q.is_active=1
            LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
            LEFT JOIN coverage_inheritance_rejections x
              ON x.scan_id=? AND x.line_id=l.id AND x.is_active=1
            WHERE l.file_id = ?
        """
        params = [int(scan_id), int(scan_id), int(file_id)]
        if cursor:
            try:
                line_number = int(cursor["line_number"])
                line_id = int(cursor["line_id"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("PAGINATION_CURSOR_STALE")
            sql += " AND (l.line_number > ? OR (l.line_number = ? AND l.id > ?))"
            params.extend((line_number, line_number, line_id))
        page_limit = min(500, max(1, int(limit or 200)))
        sql += " ORDER BY l.line_number, l.id LIMIT ?"
        params.append(page_limit + 1)
        rows = fetchall(connection, sql, params)
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = {
                "line_number": int(last["line_number"]),
                "line_id": int(last["line_id"]),
            }
        return {"rows": rows, "has_more": has_more,
                "next_cursor": next_cursor}

    def pending_line_count(self, connection, scan_id: int):
        uncovered = self._UNCOVERED_SQL
        confirmed = self._CONFIRMED_SQL
        row = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_files f
            JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            LEFT JOIN coverage_analysis_line_links q
              ON q.scan_id = f.scan_id AND q.line_id = l.id AND q.is_active = 1
            LEFT JOIN coverage_analysis_records r ON r.id = q.analysis_record_id
            WHERE f.scan_id = ? AND {uncovered} AND NOT ({confirmed})
        """.format(uncovered=uncovered, confirmed=confirmed), (int(scan_id),))
        return int((row or {}).get("total") or 0)
