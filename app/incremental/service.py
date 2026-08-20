"""Canonical incremental path service facade."""

from app.incremental.path_index import LCOVPathLookupIndex


class IncrementalService:
    def __init__(self, repo_target_paths):
        self.path_index = LCOVPathLookupIndex(repo_target_paths)
