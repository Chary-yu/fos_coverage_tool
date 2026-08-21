"""Repository Master lifecycle rules."""

from __future__ import absolute_import

from app.db.repositories.repository_repository import RepositoryRepository
from app.db.transaction import transaction


class RepositoryService(object):
    def __init__(self, repository=None):
        self.repositories = repository or RepositoryRepository()

    def ensure(self, connection, project_id, repository_name, **kwargs):
        with transaction(connection) as conn:
            return self.repositories.ensure(conn, project_id, repository_name, **kwargs)

    def rename(self, connection, project_id, repository_id, new_name):
        with transaction(connection) as conn:
            return self.repositories.rename(conn, project_id, repository_id, new_name)

    def retire(self, connection, repository_id):
        with transaction(connection) as conn:
            return self.repositories.retire(conn, repository_id)

    def resolve_resource(self, connection, path):
        resolved = self.repositories.resolve_git_resource(path)
        with transaction(connection) as conn:
            return self.repositories.ensure_resource(
                conn, resolved["common_dir"], resolved["worktree_root"], resolved["stat"]
            )
