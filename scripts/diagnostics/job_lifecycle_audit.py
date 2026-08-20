"""Executable audit for durable VNext job ownership and shutdown semantics."""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract

from app.db.repositories.job_repository import JobRepository
from app.jobs.bounded_executor import BoundedJobExecutor
from app.jobs.service import VNextBackgroundJobService
from scripts.upgrade.migration_runner import create_sqlite_schema


class _RecordingExecutor(object):
    def __init__(self):
        self.calls = []

    def submit_job(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


class _RejectingExecutor(object):
    def submit_job(self, **kwargs):
        raise RuntimeError("queue is full")


def _factory(db_path):
    def create_connection():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection
    return create_connection


def audit():
    failures = []
    checks = {}

    with tempfile.TemporaryDirectory(prefix="vnext-job-audit-") as root:
        db_path = os.path.join(root, "jobs.db")
        connection = sqlite3.connect(db_path)
        create_sqlite_schema(connection)
        connection.commit()
        connection.close()
        factory = _factory(db_path)

        # A fresh heartbeat from a different worker must be fenced on restart;
        # a queued job must also be made visible to recovery.
        connection = factory()
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        connection.execute(
            "INSERT INTO coverage_background_jobs "
            "(job_id, project_id, scan_id, kind, state, progress, input_payload, "
            "data_version, heartbeat_at, lease_owner, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("running-foreign", 1, 11, "export", "running", 0, "{}", 1,
             now, "old-worker", now, now),
        )
        connection.execute(
            "INSERT INTO coverage_background_jobs "
            "(job_id, project_id, scan_id, kind, state, progress, input_payload, "
            "data_version, lease_owner, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("queued-restart", 1, 12, "export", "queued", 0, "{}", 1,
             "old-worker", now, now),
        )
        connection.commit()
        recovered = JobRepository().mark_stale(
            connection, timeout_seconds=3600, lease_owner="new-worker"
        )
        states = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT job_id, state FROM coverage_background_jobs "
                "WHERE job_id IN (?, ?)",
                ("running-foreign", "queued-restart"),
            ).fetchall()
        }
        connection.close()
        checks["fresh_foreign_lease_recovery"] = (
            recovered == 2
            and states == {"running-foreign": "interrupted", "queued-restart": "interrupted"}
        )
        if not checks["fresh_foreign_lease_recovery"]:
            failures.append("fresh foreign running/queued jobs were not fenced on recovery")

        # Durable repository identity owns dedupe. Executor reuse must be off
        # so different scans of one project cannot steal each other's callback.
        recorder = _RecordingExecutor()
        service = VNextBackgroundJobService(
            factory, executor=recorder, lease_owner="audit-worker"
        )
        first = service.submit(7, 101, "export", 3, lambda: "scan-a.zip")
        second = service.submit(7, 202, "export", 3, lambda: "scan-b.zip")
        calls = recorder.calls
        checks["cross_scan_executor_identity"] = (
            len(calls) == 2
            and first["job_id"] != second["job_id"]
            and all(call.get("reuse_existing") is False for call in calls)
            and [call.get("metadata", {}).get("scan_id") for call in calls] == [101, 202]
        )
        if not checks["cross_scan_executor_identity"]:
            failures.append("cross-scan durable jobs were collapsed by executor dedupe")

        # A durable row must not remain queued when enqueue itself fails.
        rejecting = VNextBackgroundJobService(factory, executor=_RejectingExecutor())
        try:
            rejecting.submit(7, 303, "export", 4, lambda: "never.zip")
        except RuntimeError:
            pass
        inspection = factory()
        failed_row = inspection.execute(
            "SELECT state, error_message FROM coverage_background_jobs "
            "WHERE project_id = ? AND scan_id = ?",
            (7, 303),
        ).fetchone()
        inspection.close()
        checks["enqueue_failure_not_orphaned"] = bool(
            failed_row
            and failed_row[0] == "failed"
            and "queue is full" in (failed_row[1] or "")
        )
        if not checks["enqueue_failure_not_orphaned"]:
            failures.append("executor rejection left a durable queued orphan")

        # shutdown(wait=True) must drain an active callback before the worker
        # owner can close its database pool.
        active = threading.Event()
        release = threading.Event()

        def long_callback():
            active.set()
            release.wait(3.0)
            return "drained"

        executor = BoundedJobExecutor(max_workers=1, max_queue_size=2)
        descriptor = executor.submit_job("audit", "audit", long_callback)
        checks["callback_started"] = active.wait(2.0)
        shutdown_done = threading.Event()

        def stop_executor():
            executor.shutdown(wait=True)
            shutdown_done.set()

        stopper = threading.Thread(target=stop_executor)
        stopper.start()
        time.sleep(0.1)
        blocked_while_active = not shutdown_done.is_set()
        release.set()
        stopper.join(3.0)
        checks["graceful_shutdown_drains"] = (
            checks["callback_started"]
            and blocked_while_active
            and shutdown_done.is_set()
            and descriptor.status == "completed"
        )
        if not checks["graceful_shutdown_drains"]:
            failures.append("shutdown returned before active callback was drained")

    return with_contract({
        "status": "PASSED" if not failures else "FAILED",
        "evidence_class": "runtime_job_lifecycle_audit",
        "checks": checks,
        "violations": failures,
    })


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
