"""Authoritative project version and current-scan state."""

from app.db.repositories.base import execute, fetchone, is_sqlite


class ProjectStateRepository(object):
    def get(self, connection, project_id: int):
        return fetchone(connection, """
            SELECT * FROM coverage_project_state WHERE project_id = ?
        """, (project_id,))

    def ensure(self, connection, project_id: int, current_scan_id=None, data_version: int = 0):
        existing = self.get(connection, project_id)
        if existing:
            return existing
        cursor = execute(connection, """
            INSERT INTO coverage_project_state(
                project_id, current_scan_id, data_version, file_state_version, updated_at
            ) VALUES (?, ?, ?, 0, CURRENT_TIMESTAMP)
        """, (project_id, current_scan_id, int(data_version)))
        cursor.close()
        return self.get(connection, project_id)

    def set_current_scan(self, connection, project_id: int, scan_id: int):
        self.lock_for_update(connection, project_id)
        cursor = execute(connection, """
            UPDATE coverage_project_state SET current_scan_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE project_id = ?
        """, (scan_id, project_id))
        cursor.close()
        return self.get(connection, project_id)

    def advance(self, connection, project_id: int):
        self.lock_for_update(connection, project_id)
        cursor = execute(connection, """
            UPDATE coverage_project_state
            SET data_version = data_version + 1, file_state_version = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE project_id = ?
        """, (project_id,))
        cursor.close()
        return self.get(connection, project_id)

    def lock_for_update(self, connection, project_id: int):
        """Return the project state at the shared mutation linearization point.

        MariaDB callers must hold this row lock for the short transaction that
        validates CURRENT/read-set identity and applies the corresponding
        mutation. SQLite has no portable row-level ``FOR UPDATE``; its writer
        transaction still serializes the same operation without emitting
        MySQL-only syntax.
        """
        self.ensure(connection, int(project_id))
        suffix = "" if is_sqlite(connection) else " FOR UPDATE"
        return fetchone(connection, (
            "SELECT * FROM coverage_project_state WHERE project_id = ?" + suffix
        ), (int(project_id),))

    def invalidate(self, connection, project_id: int):
        """Explicitly mark the derived projection stale before a rebuild."""
        self.lock_for_update(connection, int(project_id))
        cursor = execute(connection, """
            UPDATE coverage_project_state
            SET file_state_version = 0, updated_at = CURRENT_TIMESTAMP
            WHERE project_id = ?
        """, (int(project_id),))
        cursor.close()
        return self.get(connection, project_id)

    def mark_ready(self, connection, project_id: int, version: int):
        self.lock_for_update(connection, int(project_id))
        cursor = execute(connection, """
            UPDATE coverage_project_state SET file_state_version = ?, updated_at = CURRENT_TIMESTAMP
            WHERE project_id = ? AND data_version = ?
        """, (int(version), project_id, int(version)))
        cursor.close()
        return self.get(connection, project_id)
