"""Recoverable background job services built on the bounded executor."""

import json
import time
import uuid
from typing import Any, Callable, Dict, Optional

from app.jobs.bounded_executor import BoundedJobExecutor, STATUS_INTERRUPTED
from app.db.repositories.job_repository import JobRepository
from app.db.transaction import transaction


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


class VNextBackgroundJobService(object):
    """Canonical persisted Job lifecycle owner for VNext runtime.

    The executor owns queue/worker mechanics. This service owns the durable
    identity, state transitions, scan/data-version validity and recovery
    decision. A callback is never serialized; after a process restart stale
    work is marked interrupted until the caller explicitly requeues it.
    """

    def __init__(self, connection_factory, repository=None, executor=None,
                 heartbeat_timeout=300.0):
        self.connection_factory = connection_factory
        self.repository = repository or JobRepository()
        self.executor = executor or BoundedJobExecutor()
        self.heartbeat_timeout = float(heartbeat_timeout)

    def _save(self, job):
        connection = self.connection_factory()
        try:
            with transaction(connection) as conn:
                return self.repository.upsert(conn, job)
        finally:
            close = getattr(connection, "close", None)
            if close:
                close()

    def submit(self, project_id, scan_id, kind, data_version, callback,
               input_payload=None, job_id=None):
        job_id = job_id or "job_{}".format(uuid.uuid4().hex[:16])
        job = {
            "job_id": job_id, "project_id": project_id, "scan_id": scan_id,
            "kind": kind, "state": "queued", "progress": 0,
            "input_payload": json.dumps(input_payload or {}, sort_keys=True),
            "data_version": data_version,
        }
        self._save(job)

        def run():
            started = dict(job)
            started.update({"state": "running", "heartbeat_at": _now()})
            self._save(started)
            try:
                result = callback()
                completed = dict(started)
                completed.update({
                    "state": "completed", "progress": 1,
                    "result_path": result if isinstance(result, str) else "",
                    "finished_at": _now(), "heartbeat_at": _now(),
                })
                self._save(completed)
                return result
            except Exception as exc:
                failed = dict(started)
                failed.update({
                    "state": "failed", "error_message": str(exc),
                    "finished_at": _now(), "heartbeat_at": _now(),
                })
                self._save(failed)
                raise

        self.executor.submit_job(
            job_type="{}:{}".format(kind, data_version),
            project_name=str(project_id),
            fn=run,
            job_id=job_id,
            reuse_existing=True,
        )
        return self._save(job)

    def recover(self):
        connection = self.connection_factory()
        try:
            with transaction(connection) as conn:
                return self.repository.mark_stale(conn, self.heartbeat_timeout)
        finally:
            close = getattr(connection, "close", None)
            if close:
                close()

    def get(self, job_id):
        connection = self.connection_factory()
        try:
            return self.repository.get(connection, job_id)
        finally:
            close = getattr(connection, "close", None)
            if close:
                close()

    def shutdown(self):
        self.executor.shutdown(wait=True)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
