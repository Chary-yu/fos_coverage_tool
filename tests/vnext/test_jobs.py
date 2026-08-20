import os
import sqlite3
import tempfile
import time
import unittest

from app.jobs.bounded_executor import BoundedJobExecutor
from app.jobs.service import VNextBackgroundJobService
from scripts.upgrade.migration_runner import create_sqlite_schema


class VNextJobsTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
