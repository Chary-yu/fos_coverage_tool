"""Recoverable background job services built on the bounded executor."""

import json
import threading
import time
import uuid
from typing import Callable, Optional

from app.db.repositories.job_repository import JobRepository
from app.db.transaction import transaction
from app.jobs.bounded_executor import BoundedJobExecutor


class BackgroundJobService:
    def __init__(self, executor: Optional[BoundedJobExecutor] = None, store=None,
                 heartbeat_timeout: float = 300.0):
        self.executor = executor or BoundedJobExecutor()
        self.store = store
        self.heartbeat_timeout = heartbeat_timeout

    def submit(self, kind: str, project: str, version: int, fn: Callable,
               args=(), kwargs=None, resource_class: str = "default"):
        return self.executor.submit_job(
            job_type="{}:{}".format(kind, version), project_name=project,
            fn=fn, args=args, kwargs=kwargs or {}, reuse_existing=True,
            resource_class=resource_class,
        )

    def recover(self):
        if not self.store:
            return 0
        mark = getattr(self.store, "mark_stale_running_jobs_interrupted", None)
        return mark(self.heartbeat_timeout) if mark else 0

    def shutdown(self):
        self.executor.shutdown(wait=True)


class VNextBackgroundJobService(object):
    """Durable job owner with identity-aware dedupe and lease heartbeats."""

    def __init__(self, connection_factory, repository=None, executor=None,
                 heartbeat_timeout=300.0, heartbeat_interval=15.0,
                 lease_owner=None, recovery_handlers=None):
        self.connection_factory = connection_factory
        self.repository = repository or JobRepository()
        self.executor = executor or BoundedJobExecutor()
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        # A configured name is only a worker-group label.  Every process gets
        # a fresh lease identity so a restart with the same configuration can
        # reclaim queued/running rows left by the previous process.
        lease_prefix = str(lease_owner or "worker")
        self.lease_owner = "{}_{}".format(lease_prefix, uuid.uuid4().hex)
        self.recovery_handlers = dict(recovery_handlers or {})
        self._submit_lock = threading.Lock()

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
               input_payload=None, job_id=None, resource_class=None):
        with self._submit_lock:
            connection = self.connection_factory()
            try:
                active = self.repository.find_active(
                    connection, project_id, scan_id, kind, data_version
                )
                if active and not job_id:
                    return active
            finally:
                close = getattr(connection, "close", None)
                if close:
                    close()

            job_id = job_id or "job_{}".format(uuid.uuid4().hex[:16])
            job = {
                "job_id": job_id, "project_id": project_id, "scan_id": scan_id,
                "kind": kind, "state": "queued", "progress": 0,
                "input_payload": json.dumps(input_payload or {}, sort_keys=True),
                "data_version": data_version, "lease_owner": self.lease_owner,
            }
            persisted = self._save(job)
            try:
                self._enqueue(job, callback, resource_class=resource_class)
            except Exception as error:
                # Persisting before enqueue gives restart recovery a durable
                # identity, but a full/closed executor must not leave a
                # forever-queued job behind in the same process.
                failed = dict(job)
                failed.update({
                    "state": "failed", "error_message": str(error),
                    "finished_at": _now(), "heartbeat_at": _now(),
                })
                self._save(failed)
                raise
        return persisted

    def register_recovery_handler(self, kind, factory):
        if not str(kind or "").strip() or not callable(factory):
            raise ValueError("recovery handler requires a kind and callable factory")
        self.recovery_handlers[str(kind)] = factory

    def _enqueue(self, job, callback, resource_class=None):
        job_id = str(job["job_id"])
        self.executor.submit_job(
            job_type="{}:{}:{}".format(
                job.get("kind") or "", job.get("data_version") or 0,
                job.get("scan_id") or "",
            ),
            project_name=str(job.get("project_id") or ""),
            fn=self._runner(job, callback), job_id=job_id,
            # Durable repository dedupe already includes scan_id. Executor
            # reuse without it can orphan another scan's callback.
            reuse_existing=False,
            metadata={"project_id": job.get("project_id"),
                      "scan_id": job.get("scan_id"),
                      "kind": job.get("kind"),
                      "data_version": job.get("data_version")},
            resource_class=resource_class or self.resource_for_kind(job.get("kind")),
        )

    def _runner(self, job, callback):
        def run():
            started = dict(job)
            started.update({"state": "running", "heartbeat_at": _now(),
                            "lease_owner": self.lease_owner,
                            "started_at": _now()})
            self._save(started)
            stop_heartbeat = threading.Event()

            def heartbeat():
                while not stop_heartbeat.wait(self.heartbeat_interval):
                    heartbeat_job = dict(started)
                    heartbeat_job.update({"state": "running", "heartbeat_at": _now()})
                    try:
                        self._save(heartbeat_job)
                    except Exception:
                        # The callback still owns the terminal state.
                        pass

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                name="CoverageJobHeartbeat-{}".format(job["job_id"]), daemon=True,
            )
            heartbeat_thread.start()
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
            finally:
                stop_heartbeat.set()
                heartbeat_thread.join(timeout=min(self.heartbeat_interval, 1.0))
        return run

    @staticmethod
    def resource_for_kind(kind):
        return {
            "rebuild_progress": "database",
            "export": "disk",
            "incremental": "cpu",
        }.get(str(kind or ""), "default")

    def metrics(self):
        result = self.executor.metrics()
        result["lease_owner"] = self.lease_owner
        return result

    def recover(self, heartbeat_timeout=None, exclude_kinds=None):
        connection = self.connection_factory()
        try:
            with transaction(connection) as conn:
                timeout = (self.heartbeat_timeout if heartbeat_timeout is None
                           else heartbeat_timeout)
                list_recoverable = getattr(self.repository, "list_recoverable", None)
                claim = getattr(self.repository, "claim_for_recovery", None)
                if not list_recoverable or not claim:
                    return self.repository.mark_stale(
                        conn, timeout, lease_owner=self.lease_owner,
                        exclude_kinds=exclude_kinds,
                    )
                candidates = list_recoverable(
                    conn, timeout, lease_owner=self.lease_owner,
                    exclude_kinds=exclude_kinds,
                )
                claimed = []
                interrupted = 0
                for candidate in candidates:
                    factory = self.recovery_handlers.get(
                        str(candidate.get("kind") or "")
                    )
                    if not factory:
                        mark_interrupted = getattr(self.repository, "mark_interrupted", None)
                        interrupted += (mark_interrupted(conn, candidate["job_id"])
                                        if mark_interrupted else 0)
                        continue
                    recovered = claim(
                        conn, candidate["job_id"], self.lease_owner,
                        expected_state=candidate.get("state"),
                        expected_lease_owner=candidate.get("lease_owner"),
                        expected_heartbeat_at=candidate.get("heartbeat_at"),
                    )
                    if recovered:
                        claimed.append((recovered, factory))
            recovered_count = interrupted
            for job, factory in claimed:
                try:
                    callback = factory(job)
                    self._enqueue(job, callback)
                    recovered_count += 1
                except Exception as error:
                    failure_connection = self.connection_factory()
                    try:
                        with transaction(failure_connection) as conn:
                            failed = dict(job)
                            failed.update({
                                "state": "failed", "error_message": str(error),
                                "finished_at": _now(), "heartbeat_at": _now(),
                            })
                            self.repository.upsert(conn, failed)
                    finally:
                        close = getattr(failure_connection, "close", None)
                        if close:
                            close()
            return recovered_count
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
