"""Repository Master Identity and physical Git resource persistence."""

from __future__ import absolute_import

import hashlib
import json
import os
import subprocess

from app.db.repositories.base import execute, fetchall, fetchone, insert_id
from app.time_utils import utc_sql


class RepositoryRepository(object):
    def get(self, connection, repository_id):
        return fetchone(connection, "SELECT * FROM coverage_repositories WHERE id=?",
                        (int(repository_id),))

    def get_by_name(self, connection, project_id, repository_name):
        row = fetchone(connection, """
            SELECT * FROM coverage_repositories
            WHERE project_id=? AND repository_name=?
        """, (int(project_id), str(repository_name or "")))
        if row:
            return row
        alias = fetchone(connection, """
            SELECT r.* FROM coverage_repository_aliases a
            JOIN coverage_repositories r ON r.id=a.repository_id
            WHERE a.project_id=? AND a.alias_name=? AND a.retired_at IS NULL
        """, (int(project_id), str(repository_name or "")))
        return alias

    def list_for_project(self, connection, project_id):
        return fetchall(connection, """
            SELECT * FROM coverage_repositories WHERE project_id=? ORDER BY repository_name
        """, (int(project_id),))

    def ensure(self, connection, project_id, repository_name, canonical_remote="",
               physical_resource_id=None, physical_path=""):
        name = str(repository_name or "").strip()
        if not name:
            raise ValueError("repository_name is required")
        existing = self.get_by_name(connection, project_id, name)
        if existing:
            if str(existing.get("lifecycle_state") or "ACTIVE").upper() == "RETIRED":
                raise ValueError("REPOSITORY_RETIRED")
            return existing
        stamp = utc_sql()
        cursor = execute(connection, """
            INSERT INTO coverage_repositories(
                project_id, repository_name, canonical_remote,
                last_observed_physical_path, physical_resource_id,
                lifecycle_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
        """, (int(project_id), name, canonical_remote or "", physical_path or "",
              physical_resource_id, stamp, stamp))
        repository_id = insert_id(cursor)
        cursor.close()
        return self.get(connection, repository_id)

    def add_alias(self, connection, project_id, repository_id, alias_name):
        """Add an explicit compatibility alias without changing identity."""
        alias = str(alias_name or "").strip()
        repository = self.get(connection, repository_id)
        if not alias or not repository or int(repository.get("project_id")) != int(project_id):
            raise ValueError("repository alias identity is invalid")
        canonical = self.get_by_name(connection, project_id, alias)
        if canonical and int(canonical.get("id")) != int(repository_id):
            raise ValueError("repository alias conflicts with another identity")
        existing = fetchone(connection, """
            SELECT * FROM coverage_repository_aliases
            WHERE project_id=? AND alias_name=? AND retired_at IS NULL
        """, (int(project_id), alias))
        if existing:
            if int(existing.get("repository_id")) != int(repository_id):
                raise ValueError("repository alias conflicts with another identity")
            return existing
        cursor = execute(connection, """
            INSERT INTO coverage_repository_aliases(
                project_id, repository_id, alias_name, created_at
            ) VALUES (?, ?, ?, ?)
        """, (int(project_id), int(repository_id), alias, utc_sql()))
        alias_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, "SELECT * FROM coverage_repository_aliases WHERE id=?",
                        (alias_id,))

    def rename(self, connection, project_id, repository_id, new_name):
        new_name = str(new_name or "").strip()
        if not new_name:
            raise ValueError("new repository name is required")
        current = self.get(connection, repository_id)
        if not current or int(current["project_id"]) != int(project_id):
            raise KeyError("repository not found")
        if new_name == str(current.get("repository_name") or ""):
            return current
        if self.get_by_name(connection, project_id, new_name):
            raise ValueError("repository name conflicts with an existing identity")
        stamp = utc_sql()
        self.add_alias(connection, project_id, repository_id, current["repository_name"])
        cursor = execute(connection, """
            UPDATE coverage_repositories
            SET repository_name=?, updated_at=?
            WHERE id=? AND project_id=?
        """, (new_name, stamp, int(repository_id), int(project_id)))
        cursor.close()
        return self.get(connection, repository_id)

    def retire(self, connection, repository_id):
        cursor = execute(connection, """
            UPDATE coverage_repositories SET lifecycle_state='RETIRED', updated_at=?
            WHERE id=?
        """, (utc_sql(), int(repository_id)))
        cursor.close()
        cursor = execute(connection, """
            UPDATE coverage_repository_aliases
            SET retired_at=COALESCE(retired_at, ?)
            WHERE repository_id=? AND retired_at IS NULL
        """, (utc_sql(), int(repository_id)))
        cursor.close()
        return self.get(connection, repository_id)

    def ensure_resource(self, connection, common_dir, worktree_root, fs_stat=None):
        common_dir = os.path.realpath(str(common_dir or ""))
        worktree_root = os.path.realpath(str(worktree_root or ""))
        if not common_dir or not worktree_root:
            raise ValueError("physical Git resource paths are required")
        fs_stat = fs_stat or os.stat(common_dir)
        key_payload = {
            "common_dir": common_dir,
            "device": int(getattr(fs_stat, "st_dev", 0)),
            "inode": int(getattr(fs_stat, "st_ino", 0)),
        }
        resource_key = hashlib.sha256(json.dumps(
            key_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        existing = fetchone(connection, """
            SELECT * FROM coverage_repository_resources WHERE resource_key=?
        """, (resource_key,))
        if existing:
            return existing
        stamp = utc_sql()
        cursor = execute(connection, """
            INSERT INTO coverage_repository_resources(
                resource_key, resolved_git_common_dir, resolved_worktree_root,
                fs_device, fs_inode, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (resource_key, common_dir, worktree_root,
              int(getattr(fs_stat, "st_dev", 0)), int(getattr(fs_stat, "st_ino", 0)), stamp))
        resource_id = insert_id(cursor)
        cursor.close()
        return fetchone(connection, """
            SELECT * FROM coverage_repository_resources WHERE id=?
        """, (resource_id,))

    def observe_physical_path(self, connection, repository_id, path,
                              resource_id=None):
        cursor = execute(connection, """
            UPDATE coverage_repositories
            SET last_observed_physical_path=?, physical_resource_id=?, updated_at=?
            WHERE id=?
        """, (os.path.realpath(str(path or "")), resource_id, utc_sql(),
              int(repository_id)))
        cursor.close()
        return self.get(connection, repository_id)

    @staticmethod
    def resolve_git_resource(path, timeout=10):
        """Resolve trusted Git common-dir/worktree identity without a shell."""
        path = os.path.realpath(str(path or ""))
        if not os.path.isdir(path):
            raise ValueError("repository path is not a directory")
        def run(args):
            return subprocess.check_output(
                args, cwd=path, stderr=subprocess.STDOUT,
                timeout=float(timeout), universal_newlines=True,
            ).strip()
        common = run(["git", "rev-parse", "--git-common-dir"])
        common = os.path.realpath(os.path.join(path, common)) if not os.path.isabs(common) else os.path.realpath(common)
        root = run(["git", "rev-parse", "--show-toplevel"])
        return {
            "common_dir": common,
            "worktree_root": os.path.realpath(root),
            "stat": os.stat(common),
        }
