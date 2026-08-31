"""Disposable VNext HTTP fixture used by the real canonical-browser test."""

from __future__ import print_function

import json
import os
import sqlite3
import sys
import tempfile
import threading
from urllib.parse import urlsplit

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.api.handler import VNextHTTPRequestHandler
from app.bootstrap import VNextRuntime, create_vnext_server
from app.code_detail.code_region import FunctionRange
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import SourceContext, SourceLineDTO, calc_sidecar_file_key
from scripts.upgrade.migration_runner import create_sqlite_schema
from tests.vnext.release_fixture import prepare_release_root


DEFAULT_FIXTURE_LINES = 120


def _fixture_line_count():
    raw = os.environ.get("COVERAGE_HTTP_FIXTURE_LINES", "")
    if not raw:
        return DEFAULT_FIXTURE_LINES
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("COVERAGE_HTTP_FIXTURE_LINES must be an integer")
    if value < 1 or value > 1000000:
        raise ValueError("COVERAGE_HTTP_FIXTURE_LINES must be between 1 and 1000000")
    return value


def _connection_factory(db_path):
    def factory():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection
    return factory


def _fixture_lines(line_count=None):
    line_count = int(line_count or _fixture_line_count())
    return [
        {
            "line_number": line_number,
            "line_text": "fixture_line_{}();".format(line_number),
            "coverage_state": "uncovered",
            "block_start_line": line_number,
            "block_end_line": line_number,
            "block_type": "single",
            "function_name": "",
            "function_hash": "",
            "code_line_hash": "line-{}".format(line_number),
            "code_occurrence": 1,
            "suggested_reviewer": "git-alice" if line_number == 1 else "",
        }
        for line_number in range(1, line_count + 1)
    ]


def _source_context(line_count=None):
    line_count = int(line_count or _fixture_line_count())
    with_panels = line_count <= DEFAULT_FIXTURE_LINES
    lines = []
    for line_number in range(1, line_count + 1):
        lines.append(SourceLineDTO(
            line_number,
            source="fixture_line_{}();".format(line_number),
            coverage_state="uncovered",
            analysis_state="未确认",
            is_pending_analysis=True,
            block_start_line=line_number,
            block_end_line=line_number,
            block_type="single",
            suggested_reviewer="git-alice" if line_number == 1 else "",
            is_block_entry=with_panels or line_number == 1,
        ))
    return SourceContext(
        "HttpFixture", "src/http_fixture.c", lines,
        function_ranges=[FunctionRange(1, line_count, "fixture")],
        report_id="report_http_fixture",
    )


def build_fixture():
    line_count = _fixture_line_count()
    temporary = tempfile.TemporaryDirectory(prefix="vnext-http-browser-")
    root = temporary.name
    prepare_release_root(root)
    db_path = os.path.join(root, "fixture.db")
    state_root = os.path.join(root, "state")
    report_root = os.path.join(root, "report")
    os.makedirs(report_root)

    connection = sqlite3.connect(db_path)
    create_sqlite_schema(connection)
    connection.commit()
    connection.close()

    config = {
        "project_name": "HttpFixture",
        "auth": {
            "mode": "reverse_proxy",
            "trusted_proxy_addresses": ["127.0.0.1"],
            "user_header": "X-Remote-User",
            "role_header": "X-Remote-Role",
            "roles": {"browser-reviewer": "reviewer"},
        },
        "runtime_state": {"root": state_root},
        "report_roots": [report_root],
        "input_roots": [root],
        "jobs": {"max_workers": 1, "max_queue_size": 4},
    }
    factory = _connection_factory(db_path)
    seed_runtime = VNextRuntime(config, root, connection_factory=factory)
    try:
        context = _source_context(line_count)
        file_path = "src/http_fixture.c"
        file_key = calc_sidecar_file_key(file_path, "repo-a")
        SidecarStore([report_root], chunk_size=2000).save_chunked_sidecar(
            report_root, "report_http_fixture", file_key, context
        )
        progress_files = [{
            "repository_name": "repo-a",
            "file_path": file_path,
            "file_path_hash": "",
            "source_file_name": "http_fixture.c",
            "lines": _fixture_lines(line_count),
        }]
        # Keep the browser fixture large enough to exercise the canonical
        # Progress cursor window without making the Code Detail page itself
        # larger than the workload under test.
        for index in range(1, 121):
            progress_files.append({
                "repository_name": "repo-a",
                "file_path": "src/progress_{:03d}.c".format(index),
                "file_path_hash": "",
                "source_file_name": "progress_{:03d}.c".format(index),
                "lines": [{
                    "line_number": 1,
                    "line_text": "progress_{}();".format(index),
                    "coverage_state": "uncovered",
                    "block_start_line": 1,
                    "block_end_line": 1,
                    "code_line_hash": "progress-{}".format(index),
                }],
            })
        connection = factory()
        try:
            scan = seed_runtime.project_service.create_scan_and_ingest(
                connection,
                "HttpFixture",
                progress_files,
                info_file_name="http_fixture.info",
                info_sha256="a" * 64,
                repositories=[{
                    "repository_name": "repo-a",
                    "repository_path": "/fixture/repo-a",
                    "branch_name": "main",
                    "verified": True,
                }],
                report={
                    "report_id": "report_http_fixture",
                    "report_root": report_root,
                    "sidecar_schema": 2,
                    "asset_identity": "http-fixture-v1",
                },
            )
            seed_runtime.report_registry.register(
                "report_http_fixture", [report_root], sidecar_required=True,
                report_root=report_root, scan_id=scan["id"],
            )
        finally:
            connection.close()
    finally:
        seed_runtime.close()

    # Bind the production VNext server factory to the same temporary database.
    # The custom handler only adds static page assets; all /api routes are
    # delegated to the production VNext transport unchanged.
    server = create_vnext_server(
        ("127.0.0.1", 0), config, repo_root=root, connection_factory=factory
    )
    runtime = server.vnext_runtime

    class FixtureHandler(VNextHTTPRequestHandler):
        application = runtime.application()

        def _inject_fixture_identity(self):
            # The disposable HTTP fixture represents a trusted reverse proxy.
            # This keeps the browser lane on the same authenticated-reviewer
            # contract as production without requiring a real proxy process.
            self.headers["X-Remote-User"] = "browser-reviewer"
            self.headers["X-Remote-Role"] = "reviewer"

        @staticmethod
        def _asset(path):
            with open(os.path.join(ROOT, path), "rb") as stream:
                return stream.read()

        def _send_static(self, data, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._inject_fixture_identity()
            path = urlsplit(self.path).path
            if path == "/coverage_enhance.js":
                return self._send_static(
                    self._asset("web/assets/js/coverage_enhance.js"),
                    "text/javascript; charset=utf-8",
                )
            if path == "/coverage_progress.js":
                return self._send_static(
                    self._asset("web/assets/js/coverage_progress.js"),
                    "text/javascript; charset=utf-8",
                )
            if path == "/coverage_enhance.css":
                return self._send_static(
                    self._asset("web/assets/css/coverage_enhance.css"),
                    "text/css; charset=utf-8",
                )
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/" or path.endswith(".gcov.html"):
                html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="coverage-project" content="HttpFixture">
<meta name="coverage-report-mode" content="VNEXT_ARTIFACT_READY">
<meta name="coverage-report-id" content="report_http_fixture">
<meta name="coverage-scan-id" content="{scan_id}">
<meta name="coverage-repository-name" content="repo-a">
<meta name="coverage-file-path" content="src/http_fixture.c">
<meta name="coverage-render-mode" content="lazy_collapse">
<meta name="coverage-review-scope" content="full">
<link rel="stylesheet" href="/coverage_enhance.css">
</head><body><pre class="source"></pre>
<script src="/coverage_enhance.js"></script></body></html>""".format(
                    scan_id=scan["id"]
                ).encode("utf-8")
                return self._send_static(html, "text/html; charset=utf-8")
            if path == "/coverage_progress.html":
                return self._send_static(
                    self._asset("web/templates/coverage_progress.html"),
                    "text/html; charset=utf-8",
                )
            return super(FixtureHandler, self).do_GET()

        def do_POST(self):
            self._inject_fixture_identity()
            return super(FixtureHandler, self).do_POST()

    server.RequestHandlerClass = FixtureHandler
    original_server_close = server.server_close

    def close_server():
        try:
            original_server_close()
        finally:
            temporary.cleanup()

    server.server_close = close_server
    return server, scan["id"]


def main():
    server, scan_id = build_fixture()
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    print(json.dumps({
        "base_url": "http://127.0.0.1:{}".format(server.server_address[1]),
        "scan_id": scan_id,
        "report_id": "report_http_fixture",
    }), flush=True)

    # The Node test closes stdin after the browser assertions.  Keeping the
    # shutdown path in this process makes the fixture cleanly release worker
    # threads, DB connections and the temporary report directory.
    try:
        sys.stdin.read()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
