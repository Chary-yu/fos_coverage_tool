"""Recoverable background job service built on the bounded executor."""

import time
from typing import Any, Callable, Dict, Optional

from app.jobs.bounded_executor import BoundedJobExecutor, STATUS_INTERRUPTED


class BackgroundJobService:
    def __init__(self, executor: Optional[BoundedJobExecutor] = None, store=None, heartbeat_timeout: float = 300.0):
        self.executor = executor or BoundedJobExecutor()
        self.store = store
        self.heartbeat_timeout = heartbeat_timeout

    def submit(self, kind: str, project: str, version: int, fn: Callable, args=(), kwargs=None):
        return self.executor.submit_job(
            job_type="{}:{}".format(kind, version), project_name=project,
            fn=fn, args=args, kwargs=kwargs or {}, reuse_existing=True,
        )

    def recover(self):
        """Mark stale persisted jobs interrupted before they can be retried."""
        if not self.store:
            return 0
        mark = getattr(self.store, "mark_stale_running_jobs_interrupted", None)
        if mark:
            return mark(self.heartbeat_timeout)
        return 0

    def shutdown(self):
        self.executor.shutdown(wait=True)
