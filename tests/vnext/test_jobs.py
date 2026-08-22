import os
import json
import sqlite3
import tempfile
import time
import unittest

from app.bootstrap import VNextRuntime
from app.jobs.bounded_executor import BoundedJobExecutor
from app.jobs.service import VNextBackgroundJobService
from scripts.upgrade.migration_runner import create_sqlite_schema
from tests.vnext.release_fixture import prepare_release_root


class VNextJobsTest(unittest.TestCase):
    def test_executor_rejection_does_not_leave_a_queued_orphan(self):
        class RejectingExecutor(object):
            def submit_job(self, **kwargs):
                raise RuntimeError("queue is full")

        with tempfile.TemporaryDirectory(prefix="vnext-job-reject-") as root:
            db_path = os.path.join(root, "jobs.db")
            connection = sqlite3.connect(db_path)
            create_sqlite_schema(connection)
            connection.close()

            def factory():
                item = sqlite3.connect(db_path)
                item.row_factory = sqlite3.Row
                return item

            service = VNextBackgroundJobService(factory, executor=RejectingExecutor())
            with self.assertRaises(RuntimeError):
                service.submit(1, 2, "export", 3, lambda: "never-runs")
            connection = factory()
            row = connection.execute(
                "SELECT state, error_message FROM coverage_background_jobs"
            ).fetchone()
            connection.close()
            self.assertEqual(row[0], "failed")
            self.assertIn("queue is full", row[1])

    def test_persisted_job_survives_executor_and_recovery_marks_stale(self):
        with tempfile.TemporaryDirectory(prefix="vnext-job-") as root:
            db_path = os.path.join(root, "jobs.db")
            connection = sqlite3.connect(db_path)
            create_sqlite_schema(connection)
            connection.close()

            def factory():
                item = sqlite3.connect(db_path)
                item.row_factory = sqlite3.Row
                return item

            executor = BoundedJobExecutor(max_workers=1, max_queue_size=4)
            service = VNextBackgroundJobService(factory, executor=executor,
                                                heartbeat_timeout=0.01)
            job = service.submit(1, 2, "export", 3, lambda: "candidate/result.zip")
            deadline = time.time() + 3
            while time.time() < deadline:
                current = service.get(job["job_id"])
                if current and current.get("state") == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(service.get(job["job_id"])["state"], "completed")
            stale_connection = factory()
            stale_connection.execute(
                "UPDATE coverage_background_jobs SET state = 'running', "
                "heartbeat_at = '2000-01-01 00:00:00' WHERE job_id = ?",
                (job["job_id"],),
            )
            stale_connection.commit()
            stale_connection.close()
            self.assertEqual(service.recover(), 1)
            self.assertEqual(service.get(job["job_id"])["state"], "interrupted")
            service.shutdown()

    def test_generic_job_recovery_reconstructs_callback(self):
        """A restarted worker must rebuild a callback from durable input."""
        with tempfile.TemporaryDirectory(prefix="vnext-job-recovery-") as root:
            db_path = os.path.join(root, "jobs.db")
            connection = sqlite3.connect(db_path)
            create_sqlite_schema(connection)
            connection.close()

            def factory():
                item = sqlite3.connect(db_path)
                item.row_factory = sqlite3.Row
                return item

            class RecordingExecutor(object):
                def submit_job(self, **kwargs):
                    return kwargs

            original = VNextBackgroundJobService(
                factory, executor=RecordingExecutor(), lease_owner="old-worker"
            )
            job = original.submit(
                1, 2, "export", 3, lambda: "must-not-run",
                input_payload={"report_id": "report-2", "output_path": "result.zip"},
            )
            stale = factory()
            stale.execute(
                "UPDATE coverage_background_jobs SET state='running', "
                "lease_owner='old-worker', heartbeat_at='2000-01-01 00:00:00' "
                "WHERE job_id=?", (job["job_id"],)
            )
            stale.commit()
            stale.close()

            rebuilt = []

            def reconstruct(durable_job):
                payload = json.loads(durable_job["input_payload"])
                rebuilt.append(payload)
                return lambda: payload["output_path"]

            restarted = VNextBackgroundJobService(
                factory,
                executor=BoundedJobExecutor(max_workers=1, max_queue_size=1),
                heartbeat_timeout=0.01,
                lease_owner="new-worker",
                recovery_handlers={"export": reconstruct},
            )
            try:
                self.assertEqual(restarted.recover(), 1)
                deadline = time.time() + 3
                while time.time() < deadline:
                    if restarted.get(job["job_id"])["state"] == "completed":
                        break
                    time.sleep(0.02)
                self.assertEqual(restarted.get(job["job_id"])["state"], "completed")
                self.assertEqual(
                    rebuilt, [{"output_path": "result.zip", "report_id": "report-2"}]
                )
            finally:
                restarted.shutdown()

    def test_vnext_runtime_connection_factory_supports_worker_threads(self):
        with tempfile.TemporaryDirectory(prefix="vnext-runtime-job-") as root:
            db_path = os.path.join(root, "runtime.db")
            prepare_release_root(root)
            initial = sqlite3.connect(db_path)
            initial.row_factory = sqlite3.Row
            create_sqlite_schema(initial)
            initial.close()

            def factory():
                connection = sqlite3.connect(db_path)
                connection.row_factory = sqlite3.Row
                return connection

            runtime = VNextRuntime(
                {
                    "project_name": "fixture",
                    "auth": {"mode": "disabled"},
                    "runtime_state": {"root": os.path.join(root, "state")},
                },
                root, connection_factory=factory,
            )
            try:
                app = runtime.application()
                self.assertEqual(
                    app.dispatch("POST", "/api/coverage/projects",
                                 body={"project_name": "fixture"})[0], 201
                )
                status, payload = app.dispatch(
                    "POST", "/api/coverage/scans",
                    body={"project_name": "fixture", "info_sha256": "a" * 64},
                )
                self.assertEqual(status, 201)
                scan_id = payload["scan"]["id"]
                status, payload = app.dispatch(
                    "POST", "/api/coverage/jobs",
                    body={"kind": "rebuild_progress", "scan_id": scan_id},
                )
                self.assertEqual(status, 202)
                job_id = payload["job"]["job_id"]
                deadline = time.time() + 3
                while time.time() < deadline:
                    current = runtime.job_service.get(job_id)
                    if current and current["state"] in ("completed", "failed"):
                        break
                    time.sleep(0.02)
                self.assertEqual(runtime.job_service.get(job_id)["state"], "completed")
            finally:
                runtime.close()

    def test_resource_classes_keep_cpu_work_from_waiting_on_database_work(self):
        """Disk/DB-bound queues have independent workers and observable limits."""
        import threading

        executor = BoundedJobExecutor(
            max_workers=1,
            max_queue_size=2,
            resource_limits={
                "database": {"max_workers": 1, "max_queue_size": 2},
                "cpu": {"max_workers": 1, "max_queue_size": 2},
            },
        )
        database_started = threading.Event()
        release_database = threading.Event()
        cpu_finished = threading.Event()

        def database_job():
            database_started.set()
            release_database.wait(2)

        def cpu_job():
            cpu_finished.set()

        try:
            database = executor.submit_job(
                "rebuild", "fixture", database_job, resource_class="database"
            )
            self.assertTrue(database_started.wait(1))
            executor.submit_job("incremental", "fixture", cpu_job, resource_class="cpu")
            self.assertTrue(cpu_finished.wait(1), "CPU queue must not wait for DB queue")
            metrics = executor.metrics()
            self.assertEqual(metrics["resources"]["database"]["workers"], 1)
            self.assertEqual(metrics["resources"]["cpu"]["workers"], 1)
            self.assertEqual(database.resource_class, "database")
        finally:
            release_database.set()
            executor.shutdown(wait=True)

        self.assertEqual(VNextBackgroundJobService.resource_for_kind("rebuild_progress"), "database")
        self.assertEqual(VNextBackgroundJobService.resource_for_kind("export"), "disk")
        self.assertEqual(VNextBackgroundJobService.resource_for_kind("incremental"), "cpu")

    def test_resource_workers_share_one_global_execution_budget(self):
        import threading

        executor = BoundedJobExecutor(
            max_workers=2, max_queue_size=8,
            resource_limits={
                "database": {"max_workers": 3, "max_queue_size": 4},
                "cpu": {"max_workers": 3, "max_queue_size": 4},
            },
            global_worker_budget=2,
        )
        active = 0
        maximum = 0
        lock = threading.Lock()
        release = threading.Event()

        def work():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            release.wait(2)
            with lock:
                active -= 1

        try:
            for index in range(4):
                executor.submit_job("db-{}".format(index), "fixture", work,
                                    resource_class="database")
            for index in range(4):
                executor.submit_job("cpu-{}".format(index), "fixture", work,
                                    resource_class="cpu")
            deadline = time.time() + 2
            while time.time() < deadline:
                if executor.metrics()["global_active"] == 2:
                    break
                time.sleep(0.01)
            self.assertLessEqual(maximum, 2)
            self.assertEqual(executor.metrics()["global_worker_limit"], 2)
        finally:
            release.set()
            executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
