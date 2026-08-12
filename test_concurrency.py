#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Concurrency and thread-safety tests for ThreadingHTTPServer fallback and ThreadLocalDatabaseManagerProxy.
"""

import unittest
import threading
import time
import urllib.request
import urllib.error
import json

# Import target components
import enhance_coverage
from enhance_coverage import ThreadingHTTPServer, CoverageHTTPRequestHandler


class MockDatabaseManager:
    """Mock database manager designed to simulate concurrent DB actions without MySQL dependencies."""
    def __init__(self, config):
        pass

    def export_report(self, report_type, project_name):
        # Print thread name to verify concurrent dispatching
        thread_name = threading.current_thread().name
        print(f"[MockDB] Thread {thread_name} starting sleep...")
        time.sleep(0.1)
        print(f"[MockDB] Thread {thread_name} waking up.")
        if report_type == "full_project_summary":
            return (
                [
                    "project_name", "file_total", "total_uncovered", "filled_total",
                    "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated",
                ],
                [[project_name, 2, 10, 6, 4, 5, 3, 1, 1, 60.0, 50.0, "2026-08-12"]],
            )
        if report_type == "full_dir_summary":
            return (
                [
                    "project_name", "dir_path", "file_total", "total_uncovered", "filled_total",
                    "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated",
                ],
                [[project_name, "src", 2, 10, 6, 4, 5, 3, 1, 1, 60.0, 50.0, "2026-08-12"]],
            )
        if report_type == "full_file_summary":
            return (
                [
                    "project_name", "file_path", "total_uncovered", "filled_total",
                    "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated",
                ],
                [[project_name, "src/main.c", 10, 6, 4, 5, 3, 1, 1, 60.0, 50.0, "2026-08-12"]],
            )
        return ["header1"], [["row1"]]


class TestServerConcurrency(unittest.TestCase):
    def setUp(self):
        # Stash original global db_manager
        self.old_db_manager = getattr(enhance_coverage, "db_manager", None)

        # Setup stashed proxy to MockDatabaseManager
        class MockProxy:
            def __getattr__(self, name):
                mgr = MockDatabaseManager(None)
                return getattr(mgr, name)

        enhance_coverage.db_manager = MockProxy()

    def tearDown(self):
        # Restore stashed global db_manager
        enhance_coverage.db_manager = self.old_db_manager

    def test_threading_http_server_concurrency(self):
        # Start server on a free test port
        port = 19529
        server_address = ("127.0.0.1", port)
        httpd = ThreadingHTTPServer(server_address, CoverageHTTPRequestHandler)

        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()

        # Wait briefly for server startup bind
        time.sleep(0.2)

        # Fire 3 concurrent requests to see if they execute and return in parallel
        urls = [
            f"http://127.0.0.1:{port}/api/coverage/progress?project=test_concurrency_project",
            f"http://127.0.0.1:{port}/api/coverage/progress?project=test_concurrency_project",
            f"http://127.0.0.1:{port}/api/coverage/progress?project=test_concurrency_project"
        ]

        results = []
        threads = []
        start_time = time.time()

        def fetch(url):
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=5) as response:
                    res = response.read().decode('utf-8')
                    results.append(json.loads(res))
            except Exception as e:
                results.append({"status": "error", "message": str(e)})

        for url in urls:
            t = threading.Thread(target=fetch, args=(url,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        end_time = time.time()
        elapsed = end_time - start_time

        # Stop server
        httpd.shutdown()
        httpd.server_close()

        # Validate that all 3 requests succeeded
        self.assertEqual(len(results), 3)
        for res in results:
            self.assertEqual(res.get("status"), "success")

        # Since each mock call sleeps for 0.1s and is executed 3 times per request:
        # - Single-threaded server would take 0.9+ seconds
        # - ThreadingHTTPServer runs in parallel and should take < 0.5 seconds (usually ~0.3s)
        print(f"[Concurrency Verification] Concurrent elapsed: {elapsed:.3f} seconds (Expected: < 0.5s)")
        self.assertLess(elapsed, 0.5, "Server is not handling requests in parallel (blocked synchronously)")

    def test_progress_endpoint_returns_review_counts(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), CoverageHTTPRequestHandler)
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        port = httpd.server_address[1]
        try:
            url = "http://127.0.0.1:{}/api/coverage/progress?project=progress_project".format(port)
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            httpd.server_close()

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["data"]["project"][0]["total_uncovered"], 10)
        self.assertEqual(payload["data"]["project"][0]["filled_total"], 6)
        self.assertEqual(payload["data"]["project"][0]["confirmed_total"], 5)
        self.assertEqual(payload["data"]["dirs"][0]["dir_path"], "src")
        self.assertEqual(payload["data"]["files"][0]["file_path"], "src/main.c")


if __name__ == "__main__":
    unittest.main()
