"""Project, immutable Scan, repository snapshot, report and file identity."""

from typing import Any, Optional

from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone, insert_id, row_to_dict
from app.time_utils import utc_sql


MAX_IDENTITY_LOOKUP = 500


def _chunks(values, size):
    values = list(values or [])
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _now(value=None):
    return value or utc_sql()


class ProjectRepository(object):
    """Owns only project/scan/file identity SQL; no business transactions."""

    def get_project(self, connection, project_id: int):
        return fetchone(connection, "SELECT * FROM coverage_projects WHERE id = ?", (project_id,))

    def get_project_by_name(self, connection, project_name: str):
        return fetchone(connection, "SELECT * FROM coverage_projects WHERE project_name = ?", (project_name,))

    def list_projects(self, connection):
        return fetchall(connection, "SELECT * FROM coverage_projects ORDER BY project_name")

    def ensure_project(self, connection, project_name: str, now=None):
        if not str(project_name or "").strip():
            raise ValueError("project_name is required")
        existing = self.get_project_by_name(connection, project_name)
        if existing:
            return existing
        stamp = _now(now)
        cursor = execute(connection, """
            INSERT INTO coverage_projects(project_name, created_at, updated_at)
            VALUES (?, ?, ?)
        """, (project_name, stamp, stamp))
        project_id = insert_id(cursor)
        cursor.close()
        row = self.get_project(connection, project_id) if project_id else None
        if not row:
            row = self.get_project_by_name(connection, project_name)
        if not row:
            raise RuntimeError("project insert did not return an identity")
        return row

    def update_project_timestamp(self, connection, project_id: int, now=None):
        cursor = execute(connection, "UPDATE coverage_projects SET updated_at = ? WHERE id = ?",
                         (_now(now), project_id))
        cursor.close()

    def get_scan(self, connection, scan_id: int):
        return fetchone(connection, "SELECT * FROM coverage_scans WHERE id = ?", (scan_id,))

    def get_scan_by_key(self, connection, scan_key: str):
        return fetchone(connection, "SELECT * FROM coverage_scans WHERE scan_key = ?", (scan_key,))

    @staticmethod
    def _assert_scan_building(connection, scan_id: int):
        scan = fetchone(connection, "SELECT status FROM coverage_scans WHERE id = ?", (int(scan_id),))
        if not scan:
            raise KeyError("scan not found: {}".format(scan_id))
        if str(scan.get("status") or "").lower() not in {
                "building", "importing", "constructing"}:
            raise ValueError("scan {} is sealed and immutable".format(scan_id))
        return scan

    def list_scans(self, connection, project_id: int):
        return fetchall(connection, """
            SELECT * FROM coverage_scans WHERE project_id = ? ORDER BY imported_at, id
        """, (project_id,))

    def create_scan(self, connection, project_id: int, scan_key: str, scan_type: str,
                    review_scope: str, info_file_name: str = "", info_sha256: str = "",
                    status: str = "building", legacy_migrated: int = 0,
                    metadata_version: int = 1, imported_at=None,
                    predecessor_scan_id=None, algorithm_version=""):
        if str(status or "").lower() not in {"building", "importing", "constructing"}:
            raise ValueError("new scans must be created in a construction state")
        existing = self.get_scan_by_key(connection, scan_key)
        if existing:
            return existing
        cursor = execute(connection, """
            INSERT INTO coverage_scans(
                project_id, scan_key, scan_type, review_scope, info_file_name,
                info_sha256, imported_at, status, legacy_migrated, metadata_version,
                predecessor_scan_id, algorithm_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (project_id, scan_key, scan_type, review_scope, info_file_name or "",
              info_sha256 or "", _now(imported_at), status, int(bool(legacy_migrated)),
              int(metadata_version), predecessor_scan_id, algorithm_version or ""))
        scan_id = insert_id(cursor)
        cursor.close()
        row = self.get_scan(connection, scan_id) if scan_id else None
        if not row:
            row = self.get_scan_by_key(connection, scan_key)
        if not row:
            raise RuntimeError("scan insert did not return an identity")
        return row

    def seal_scan(self, connection, scan_id: int, status: str = "ready"):
        """Publish a constructed scan and close its immutable fact boundary."""
        current = self._assert_scan_building(connection, scan_id)
        if str(status or "").lower() not in {"ready", "sealed"}:
            raise ValueError("sealed scan status must be ready or sealed")
        cursor = execute(connection, """
            UPDATE coverage_scans SET status = ? WHERE id = ?
        """, (status, int(scan_id)))
        cursor.close()
        return self.get_scan(connection, scan_id)

    def upsert_repository_snapshot(self, connection, scan_id: int, repository_name: str,
                                   repository_path: str = "", branch_name: str = "",
                                   old_commit_sha: Optional[str] = None,
                                   new_commit_sha: Optional[str] = None,
                                   verified: int = 0, captured_at=None,
                                   provenance: str = "", repository_id=None,
                                   commit_sha: Optional[str] = None,
                                   identity_verified: int = 0,
                                   identity_provenance: str = ""):
        self._assert_scan_building(connection, scan_id)
        existing = fetchone(connection, """
            SELECT * FROM coverage_scan_repositories
            WHERE scan_id = ? AND repository_name = ?
        """, (scan_id, repository_name))
        values = (repository_path or "", branch_name or "", old_commit_sha,
                  new_commit_sha, int(bool(verified)), _now(captured_at), provenance or "")
        if existing:
            current = (
                existing.get("repository_path") or "", existing.get("branch_name") or "",
                existing.get("old_commit_sha"), existing.get("new_commit_sha"),
                int(existing.get("verified") or 0), existing.get("provenance") or "",
                existing.get("repository_id"), existing.get("commit_sha"),
                int(existing.get("identity_verified") or 0),
                existing.get("identity_provenance") or "",
            )
            requested = (values[0], values[1], values[2], values[3], values[4], values[6],
                         repository_id, commit_sha, int(bool(identity_verified)),
                         identity_provenance or "")
            if current != requested:
                raise ValueError("repository snapshot is immutable")
            return existing
        cursor = execute(connection, """
            INSERT INTO coverage_scan_repositories(
                scan_id, repository_name, repository_path, branch_name,
                old_commit_sha, new_commit_sha, verified, captured_at, provenance,
                repository_id, commit_sha, identity_verified, identity_provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (scan_id, repository_name) + values + (
            repository_id, commit_sha, int(bool(identity_verified)), identity_provenance or ""
        ))
        row_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, "SELECT * FROM coverage_scan_repositories WHERE id = ?", (row_id,))

    def get_report(self, connection, report_id: str):
        return fetchone(connection, "SELECT * FROM coverage_reports WHERE report_id = ?", (report_id,))

    def get_report_for_scan(self, connection, scan_id: int):
        return fetchone(connection, """
            SELECT * FROM coverage_reports WHERE scan_id = ? ORDER BY id LIMIT 1
        """, (scan_id,))

    def list_repository_snapshots(self, connection, scan_id: int):
        return fetchall(connection, """
            SELECT * FROM coverage_scan_repositories
            WHERE scan_id = ? ORDER BY repository_name
        """, (scan_id,))

    def bind_report(self, connection, scan_id: int, report_id: str, report_root: str = "",
                    source_signature: str = "", sidecar_schema: int = 0,
                    asset_identity: str = "", generated_at=None):
        if not report_id:
            raise ValueError("report_id is required")
        existing = self.get_report(connection, report_id)
        values = (scan_id, report_root or "", source_signature or "", int(sidecar_schema or 0),
                  asset_identity or "", _now(generated_at))
        if existing:
            if int(existing["scan_id"]) != int(scan_id):
                raise ValueError("report_id is already bound to another scan")
            current = (
                existing.get("report_root") or "", existing.get("source_signature") or "",
                int(existing.get("sidecar_schema") or 0), existing.get("asset_identity") or "",
            )
            requested = (values[1], values[2], values[3], values[4])
            if current != requested:
                raise ValueError("report identity is immutable")
            return existing
        self._assert_scan_building(connection, scan_id)
        cursor = execute(connection, """
            INSERT INTO coverage_reports(
                scan_id, report_id, report_root, source_signature, sidecar_schema,
                asset_identity, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (scan_id, report_id) + values[1:])
        report_db_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, "SELECT * FROM coverage_reports WHERE id = ?", (report_db_id,))

    def get_file(self, connection, scan_id: int, repository_name: str, file_path_hash: str):
        return fetchone(connection, """
            SELECT * FROM coverage_files
            WHERE scan_id = ? AND repository_name = ? AND file_path_hash = ?
        """, (scan_id, repository_name or "", file_path_hash))

    def get_files_by_identities(self, connection, scan_id: int, identities):
        """Resolve many immutable file identities with one SELECT."""
        identities = list({
            (str(repository_name or ""), str(file_path_hash or ""))
            for repository_name, file_path_hash in (identities or [])
        })
        if not identities:
            return {}
        rows = []
        for identity_chunk in _chunks(identities, MAX_IDENTITY_LOOKUP):
            clauses = []
            params = [int(scan_id)]
            for repository_name, file_path_hash in identity_chunk:
                clauses.append("(repository_name = ? AND file_path_hash = ?)")
                params.extend((repository_name, file_path_hash))
            rows.extend(fetchall(connection, """
                SELECT * FROM coverage_files
                WHERE scan_id = ? AND ({})
            """.format(" OR ".join(clauses)), params))
        return {
            (str(row.get("repository_name") or ""), str(row.get("file_path_hash") or "")): row
            for row in rows
        }

    def ensure_files(self, connection, scan_id: int, records):
        """Resolve and insert a whole scan file set with bounded SQL."""
        self._assert_scan_building(connection, scan_id)
        normalized = {}
        for item in records or []:
            repository_name = str(item.get("repository_name") or "")
            file_path_hash = str(item.get("file_path_hash") or "")
            file_path = str(item.get("file_path") or "")
            if not file_path_hash or not file_path:
                raise ValueError("file_path and file_path_hash are required")
            key = (repository_name, file_path_hash)
            source_file_name = str(item.get("source_file_name") or "")
            candidate = (file_path, source_file_name)
            if key in normalized and normalized[key] != candidate:
                raise ValueError("duplicate file identity has conflicting paths")
            normalized[key] = candidate
        if not normalized:
            return {}
        existing = self.get_files_by_identities(connection, scan_id, normalized.keys())
        missing = []
        for key, (file_path, source_file_name) in normalized.items():
            row = existing.get(key)
            if row:
                if (row.get("file_path") != file_path or
                        row.get("source_file_name") != source_file_name):
                    raise ValueError("file identity is immutable")
            else:
                missing.append((key[0], key[1], file_path, source_file_name))
        if missing:
            cursor = connection.cursor()
            try:
                cursor.executemany(adapt_sql(connection, """
                    INSERT INTO coverage_files(
                        scan_id, repository_name, file_path_hash, file_path, source_file_name
                    ) VALUES (?, ?, ?, ?, ?)
                """), [(int(scan_id),) + item for item in missing])
            finally:
                cursor.close()
            existing.update(self.get_files_by_identities(
                connection, scan_id, [item[:2] for item in missing]
            ))
        return existing

    def ensure_file(self, connection, scan_id: int, repository_name: str, file_path_hash: str,
                    file_path: str, source_file_name: str = ""):
        self._assert_scan_building(connection, scan_id)
        existing = self.get_file(connection, scan_id, repository_name, file_path_hash)
        if existing:
            if (existing.get("file_path") != file_path or
                    existing.get("source_file_name") != (source_file_name or "")):
                raise ValueError("file identity is immutable")
            return existing
        cursor = execute(connection, """
            INSERT INTO coverage_files(scan_id, repository_name, file_path_hash, file_path, source_file_name)
            VALUES (?, ?, ?, ?, ?)
        """, (scan_id, repository_name or "", file_path_hash, file_path, source_file_name or ""))
        file_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, "SELECT * FROM coverage_files WHERE id = ?", (file_id,))

    def iter_files(self, connection, scan_id: int):
        return fetchall(connection, """
            SELECT * FROM coverage_files WHERE scan_id = ? ORDER BY repository_name, file_path
        """, (scan_id,))

    def iter_scan_export_rows(self, connection, scan_id: int):
        """Yield joined facts one row at a time for bounded exports."""
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            SELECT f.repository_name, f.file_path_hash, f.file_path,
                   f.source_file_name, l.line_number, l.line_text,
                   l.coverage_state, l.block_start_line, l.block_end_line,
                   l.block_type, l.function_name, l.function_hash,
                   l.code_line_hash, l.code_occurrence, l.suggested_reviewer,
                   a.status, a.is_draft, a.reviewer, a.coverage_method,
                   a.uncovered_reason, a.comment
            FROM coverage_files f
            JOIN coverage_lines l ON l.file_id = f.id
            LEFT JOIN coverage_analyses a ON a.line_id = l.id
            WHERE f.scan_id = ?
            ORDER BY f.repository_name, f.file_path, l.line_number
        """), (scan_id,))
        try:
            for row in cursor:
                yield row_to_dict(cursor, row)
        finally:
            cursor.close()
