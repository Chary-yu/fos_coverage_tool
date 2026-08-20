"""Canonical progress service facade.

Consumers use this facade so readiness/fallback semantics do not get
reimplemented in HTTP handlers or background workers.
"""

from app.progress.file_state_service import (
    get_project_aggregate_readiness,
    query_project_progress_aggregated,
)


class ProgressService:
    def __init__(self, connection):
        self.connection = connection

    def project_summary(self, project_name):
        return query_project_progress_aggregated(self.connection, project_name, fallback_authoritative=True)

    def readiness(self, project_name):
        return get_project_aggregate_readiness(self.connection, project_name)
