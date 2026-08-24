import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from app.api.handler import VNextHTTPRequestHandler
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import SourceContext, SourceLineDTO
from app.db.repositories.base import fetchall
from app.inject.service import ScanImportService
from app.inheritance.git_snapshot import GitSnapshotProvider
from app.observability.performance import (
    PerformanceEvidenceCollector, bind_collector, instrument_connection,
)


class PerformanceObservabilityTest(unittest.TestCase):
    def test_http_handler_records_request_bytes_response_bytes_and_time(self):
        collector = PerformanceEvidenceCollector(
            {"commit_sha": "a" * 40, "build_id": "build"},
            workload_id="fixed-http-fixture",
        )

        class Runtime(object):
            performance = collector

        class Application(object):
            runtime = Runtime()

            def dispatch(self, method, path, query, body, headers, remote):
                return 200, {"ok": True}

        handler = object.__new__(VNextHTTPRequestHandler)
        handler.application = Application()
        handler.path = "/api/coverage/health"
        handler.headers = {"Content-Length": "3"}
        handler.client_address = ("127.0.0.1", 1)
        handler.rfile = io.BytesIO(b"{}")
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler._dispatch("GET")
        snapshot = collector.snapshot("fixed-http-fixture")
        self.assertEqual(snapshot["counters"]["request_count"], 1)
        self.assertEqual(snapshot["counters"]["request_bytes"], 3)
        self.assertGreater(snapshot["counters"]["response_bytes"], 0)
        self.assertGreater(snapshot["durations"]["http_request"]["count"], 0)

    def test_database_and_git_layers_record_real_operations(self):
        collector = PerformanceEvidenceCollector(
            {"commit_sha": "b" * 40, "build_id": "build"},
            workload_id="fixed-cross-layer-fixture",
        )
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE fixture (value INTEGER)")
        connection.execute("INSERT INTO fixture VALUES (1)")
        connection.commit()
        with bind_collector(collector):
            rows = fetchall(connection, "SELECT * FROM fixture")
        self.assertEqual(len(rows), 1)

        provider = GitSnapshotProvider("/repo", performance=collector)
        with mock.patch(
                "app.inheritance.git_snapshot.subprocess.check_output",
                return_value="fixture-output\n"):
            self.assertEqual(provider._run(["show", "HEAD:file.c"]), "fixture-output")
        snapshot = collector.snapshot("fixed-cross-layer-fixture")
        self.assertGreater(snapshot["counters"]["db_query_count"], 0)
        self.assertEqual(snapshot["counters"]["db_rows"], 1)
        self.assertEqual(snapshot["counters"]["db_rows_read"], 1)
        self.assertEqual(snapshot["counters"]["db_rows_affected"], 0)
        self.assertGreater(snapshot["counters"]["db_time_ms"], 0)
        self.assertGreater(snapshot["counters"]["git_subprocess_count"], 0)
        self.assertGreater(snapshot["counters"]["git_bytes_read"], 0)

    def test_scan_import_phase_is_bound_to_the_collector(self):
        class ProjectService(object):
            def create_scan_and_ingest(self, connection, project_name, files, **kwargs):
                for item in files:
                    list(item["lines"])
                return {"id": 1}

        collector = PerformanceEvidenceCollector(
            {"commit_sha": "c" * 40, "build_id": "build"},
            workload_id="fixed-scan-fixture",
        )
        service = ScanImportService(ProjectService(), performance=collector)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".info", delete=False) as stream:
            stream.write("TN:\nSF:src/a.c\nDA:1,0\nend_of_record\n")
            info_path = stream.name
        try:
            with bind_collector(collector):
                result = service.import_info(None, "scan", info_path)
        finally:
            os.remove(info_path)
        evidence = collector.snapshot("fixed-scan-fixture")
        phases = evidence["scan_phases"]
        counters = evidence["counters"]
        self.assertEqual(result["files"], 1)
        self.assertEqual(
            [item["identity"]["scan_id"]
             for item in evidence["completed_scan_evidence"]],
            [1],
        )
        self.assertGreater(phases["scan_import.import_info"]["count"], 0)
        self.assertGreater(phases["scan_import.import_info"]["total_ms"], 0)
        self.assertGreater(counters["bytes_read"], 0)

    def test_sidecar_cache_events_are_bound_to_the_collector(self):
        collector = PerformanceEvidenceCollector(
            {"commit_sha": "d" * 40, "build_id": "build"},
            workload_id="fixed-sidecar-fixture",
        )
        with tempfile.TemporaryDirectory(prefix="performance-sidecar-") as root:
            store = SidecarStore(
                [root], chunk_size=2, performance=collector,
            )
            context = SourceContext(
                "sidecar", "src/a.c", [
                    SourceLineDTO(1, "return 0;", coverage_state="uncovered"),
                    SourceLineDTO(2, "return 1;", coverage_state="covered"),
                ], report_id="report-sidecar",
            )
            key = "a" * 32
            store.save_chunked_sidecar(root, "report-sidecar", key, context)
            self.assertIsNotNone(store.load_metadata("report-sidecar", key))
            self.assertIsNotNone(store.load_metadata("report-sidecar", key))
            self.assertEqual(
                len(store.load_lines_ranges("report-sidecar", key, [(1, 2)])[0]),
                2,
            )
            self.assertEqual(
                len(store.load_lines_ranges("report-sidecar", key, [(1, 2)])[0]),
                2,
            )
        counters = collector.snapshot("fixed-sidecar-fixture")["counters"]
        self.assertGreater(counters["cache_hits"], 0)
        self.assertGreater(counters["cache_misses"], 0)
        self.assertGreater(counters["sidecar_decode_count"], 0)

    def test_child_scan_collectors_preserve_identity_and_merge_to_runtime(self):
        collector = PerformanceEvidenceCollector(
            {"commit_sha": "e" * 40, "build_id": "build"},
            workload_id="runtime",
        )
        with collector.child_context(7, 101, workload_id="scan-101") as first:
            collector.record_db_query(1.0)
            first.increment("db_query_count", 1)
        with collector.child_context(8, 202, workload_id="scan-202") as second:
            collector.record_db_query(1.0)
            second.increment("db_query_count", 2)
        evidence = collector.snapshot("runtime")
        self.assertEqual(evidence["counters"]["db_query_count"], 5)
        identities = {
            (item["identity"]["project_id"], item["identity"]["scan_id"])
            for item in evidence["completed_scan_evidence"]
        }
        self.assertEqual(identities, {(7, 101), (8, 202)})

    def test_failed_scan_evidence_is_not_marked_completed(self):
        collector = PerformanceEvidenceCollector(
            {"commit_sha": "g" * 40, "build_id": "build"},
            workload_id="runtime",
        )
        with self.assertRaisesRegex(ValueError, "fixture failure"):
            with collector.child_context(9, 303, workload_id="scan-303"):
                collector.increment("db_query_count")
                raise ValueError("fixture failure")

        evidence = collector.snapshot("runtime")
        self.assertEqual(len(evidence["completed_scan_evidence"]), 0)
        self.assertEqual(len(evidence["scan_evidence"]), 1)
        failed = evidence["scan_evidence"][0]
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error_class"], "builtins.ValueError")
        self.assertEqual(failed["identity"]["scan_id"], 303)
        self.assertIsNotNone(failed["finished_at"])

    def test_instrumented_connection_captures_direct_cursor_writes(self):
        collector = PerformanceEvidenceCollector(
            {"commit_sha": "f" * 40, "build_id": "build"},
            workload_id="cursor-fixture",
        )
        raw = sqlite3.connect(":memory:")
        self.addCleanup(raw.close)
        connection = instrument_connection(raw, collector)
        with bind_collector(collector):
            cursor = connection.cursor()
            cursor.execute("CREATE TABLE fixture (value INTEGER)")
            cursor.executemany("INSERT INTO fixture VALUES (?)", [(1,), (2,)])
            cursor.execute("SELECT * FROM fixture")
            self.assertEqual(len(cursor.fetchall()), 2)
            cursor.close()
        counters = collector.snapshot("cursor-fixture")["counters"]
        self.assertGreaterEqual(counters["db_query_count"], 3)
        self.assertEqual(counters["db_rows_read"], 2)
        self.assertEqual(counters["db_rows_affected"], 2)
        self.assertEqual(counters["db_rows"], 4)

    def test_buffered_select_rowcount_is_not_counted_as_affected_rows(self):
        class BufferedCursor(object):
            def __init__(self):
                self.description = None
                self.rowcount = -1

            def execute(self, sql):
                self.description = (("value",),)
                self.rowcount = 2
                return self

            def fetchall(self):
                return [(1,), (2,)]

            def close(self):
                return None

        class BufferedConnection(object):
            def __init__(self):
                self._cursor = BufferedCursor()

            def cursor(self):
                return self._cursor

        collector = PerformanceEvidenceCollector(
            {"commit_sha": "h" * 40, "build_id": "build"},
            workload_id="buffered-select-fixture",
        )
        connection = instrument_connection(BufferedConnection(), collector)
        with bind_collector(collector):
            cursor = connection.cursor()
            cursor.execute("SELECT value FROM fixture")
            self.assertEqual(cursor.fetchall(), [(1,), (2,)])
        counters = collector.snapshot("buffered-select-fixture")["counters"]
        self.assertEqual(counters["db_rows_read"], 2)
        self.assertEqual(counters["db_rows_affected"], 0)
        self.assertEqual(counters["db_rows"], 2)


if __name__ == "__main__":
    unittest.main()
