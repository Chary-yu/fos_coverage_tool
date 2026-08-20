"""Project, immutable Scan, repository snapshot, report and file identity."""

from datetime import datetime
from typing import Any, Optional

from app.db.repositories.base import execute, fetchall, fetchone, insert_id


def _now(value=None):
    return value or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


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

    def list_scans(self, connection, project_id: int):
        return fetchall(connection, """
            SELECT * FROM coverage_scans WHERE project_id = ? ORDER BY imported_at, id
        """, (project_id,))

    def create_scan(self, connection, project_id: int, scan_key: str, scan_type: str,
                    review_scope: str, info_file_name: str = "", info_sha256: str = "",
                    status: str = "ready", legacy_migrated: int = 0,
                    metadata_version: int = 1, imported_at=None):
        existing = self.get_scan_by_key(connection, scan_key)
        if existing:
            return existing
        cursor = execute(connection, """
            INSERT INTO coverage_scans(
                project_id, scan_key, scan_type, review_scope, info_file_name,
                info_sha256, imported_at, status, legacy_migrated, metadata_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (project_id, scan_key, scan_type, review_scope, info_file_name or "",
              info_sha256 or "", _now(imported_at), status, int(bool(legacy_migrated)),
              int(metadata_version)))
        scan_id = insert_id(cursor)
        cursor.close()
        row = self.get_scan(connection, scan_id) if scan_id else None
        if not row:
            row = self.get_scan_by_key(connection, scan_key)
        if not row:
            raise RuntimeError("scan insert did not return an identity")
        return row

    def upsert_repository_snapshot(self, connection, scan_id: int, repository_name: str,
                                   repository_path: str = "", branch_name: str = "",
                                   old_commit_sha: Optional[str] = None,
                                   new_commit_sha: Optional[str] = None,
                                   verified: int = 0, captured_at=None,
                                   provenance: str = ""):
        existing = fetchone(connection, """
            SELECT * FROM coverage_scan_repositories
            WHERE scan_id = ? AND repository_name = ?
        """, (scan_id, repository_name))
        values = (repository_path or "", branch_name or "", old_commit_sha,
                  new_commit_sha, int(bool(verified)), _now(captured_at), provenance or "")
        if existing:
            cursor = execute(connection, """
                UPDATE coverage_scan_repositories SET repository_path = ?, branch_name = ?,
                    old_commit_sha = ?, new_commit_sha = ?, verified = ?, captured_at = ?, provenance = ?
                WHERE id = ?
            """, values + (existing["id"],))
            cursor.close()
            return fetchone(connection, "SELECT * FROM coverage_scan_repositories WHERE id = ?",
                            (existing["id"],))
        cursor = execute(connection, """
            INSERT INTO coverage_scan_repositories(
                scan_id, repository_name, repository_path, branch_name,
                old_commit_sha, new_commit_sha, verified, captured_at, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (scan_id, repository_name) + values)
        row_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, "SELECT * FROM coverage_scan_repositories WHERE id = ?", (row_id,))

    def get_report(self, connection, report_id: str):
        return fetchone(connection, "SELECT * FROM coverage_reports WHERE report_id = ?", (report_id,))

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
            cursor = execute(connection, """
                UPDATE coverage_reports SET report_root = ?, source_signature = ?,
                    sidecar_schema = ?, asset_identity = ?, generated_at = ? WHERE id = ?
            """, values[1:] + (existing["id"],))
            cursor.close()
            return self.get_report(connection, report_id)
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

    def ensure_file(self, connection, scan_id: int, repository_name: str, file_path_hash: str,
                    file_path: str, source_file_name: str = ""):
        existing = self.get_file(connection, scan_id, repository_name, file_path_hash)
        if existing:
            if (existing.get("file_path") != file_path or
                    existing.get("source_file_name") != (source_file_name or "")):
                cursor = execute(connection, """
                    UPDATE coverage_files SET file_path = ?, source_file_name = ? WHERE id = ?
                """, (file_path, source_file_name or "", existing["id"]))
                cursor.close()
                return fetchone(connection, "SELECT * FROM coverage_files WHERE id = ?", (existing["id"],))
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
