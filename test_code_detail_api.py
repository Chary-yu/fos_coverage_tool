"""
Unit tests for Code Detail API routes in CoverageHTTPRequestHandler.
Tests layout API, batch lines API, single lines API, security checks, and input validations.
"""

import io
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import enhance_coverage
from enhance_coverage import CoverageHTTPRequestHandler, get_code_detail_service


class MockRequest:
    """Mock socket request object for BaseHTTPRequestHandler testing."""

    def __init__(self, raw_input=b""):
        self._raw_input = raw_input

    def makefile(self, mode, *args, **kwargs):
        if "b" in mode:
            if "w" in mode:
                return io.BytesIO()
            return io.BytesIO(self._raw_input)
        return io.StringIO()


class TestCodeDetailApi(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_file_path = os.path.join(self.temp_dir, "calc.c.gcov.html")
        self.sample_html = """
        <!DOCTYPE html>
        <html>
        <head><title>LCOV - cov - src/calc.c</title></head>
        <body>
          <pre class="source">
            <span class="lineNum"> 1 </span><span class="lineCov">  int add(int a, int b) {</span>
            <span class="lineNum"> 2 </span><span class="lineCov">      return a + b;</span>
            <span class="lineNum"> 3 </span><span class="lineCov">  }</span>
            <span class="lineNum"> 4 </span><span class="lineCov">  int sub(int a, int b) {</span>
            <span class="lineNum"> 5 </span><span class="lineNoCov">      return a - b;</span>
            <span class="lineNum"> 6 </span><span class="lineCov">  }</span>
          </pre>
        </body>
        </html>
        """
        with open(self.mock_file_path, "w", encoding="utf-8") as f:
            f.write(self.sample_html)

        # Configure service search dirs
        service = get_code_detail_service(search_dirs=[self.temp_dir])
        service.add_search_dir(self.temp_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _execute_handler_get(self, path):
        request_text = f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("utf-8")
        mock_sock = MockRequest(request_text)
        handler = CoverageHTTPRequestHandler(mock_sock, ("127.0.0.1", 9528), None)
        return handler

    def _execute_handler_post(self, path, payload_dict):
        body = json.dumps(payload_dict).encode("utf-8")
        request_text = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("utf-8") + body
        mock_sock = MockRequest(request_text)
        handler = CoverageHTTPRequestHandler(mock_sock, ("127.0.0.1", 9528), None)
        return handler

    @patch.object(CoverageHTTPRequestHandler, "send_response")
    @patch.object(CoverageHTTPRequestHandler, "send_header")
    @patch.object(CoverageHTTPRequestHandler, "end_headers")
    @patch.object(CoverageHTTPRequestHandler, "safe_write")
    def test_get_code_layout_success(self, mock_write, mock_end, mock_header, mock_resp):
        handler = self._execute_handler_get(
            "/api/coverage/code-layout?project=TestProj&file=src/calc.c"
        )
        mock_resp.assert_called_with(200)
        written_data = b"".join(call[0][0] for call in mock_write.call_args_list)
        resp_json = json.loads(written_data.decode("utf-8"))
        self.assertEqual(resp_json["status"], "success")
        data = resp_json["data"]
        self.assertEqual(data["file_path"], "src/calc.c")
        self.assertEqual(data["total_lines"], 6)
        self.assertEqual(data["pending_line_count"], 1)  # line 5
        self.assertTrue(len(data["regions"]) >= 2)

    @patch.object(CoverageHTTPRequestHandler, "send_response")
    @patch.object(CoverageHTTPRequestHandler, "send_header")
    @patch.object(CoverageHTTPRequestHandler, "end_headers")
    @patch.object(CoverageHTTPRequestHandler, "safe_write")
    def test_get_code_layout_missing_params(self, mock_write, mock_end, mock_header, mock_resp):
        handler = self._execute_handler_get("/api/coverage/code-layout?project=TestProj")
        mock_resp.assert_called_with(400)

    @patch.object(CoverageHTTPRequestHandler, "send_response")
    @patch.object(CoverageHTTPRequestHandler, "send_header")
    @patch.object(CoverageHTTPRequestHandler, "end_headers")
    @patch.object(CoverageHTTPRequestHandler, "safe_write")
    def test_get_code_layout_unsafe_path(self, mock_write, mock_end, mock_header, mock_resp):
        handler = self._execute_handler_get(
            "/api/coverage/code-layout?project=TestProj&file=/etc/passwd"
        )
        mock_resp.assert_called_with(400)

    @patch.object(CoverageHTTPRequestHandler, "send_response")
    @patch.object(CoverageHTTPRequestHandler, "send_header")
    @patch.object(CoverageHTTPRequestHandler, "end_headers")
    @patch.object(CoverageHTTPRequestHandler, "safe_write")
    def test_get_code_lines_single_success(self, mock_write, mock_end, mock_header, mock_resp):
        handler = self._execute_handler_get(
            "/api/coverage/code-lines?project=TestProj&file=src/calc.c&start_line=4&end_line=6"
        )
        mock_resp.assert_called_with(200)
        written_data = b"".join(call[0][0] for call in mock_write.call_args_list)
        resp_json = json.loads(written_data.decode("utf-8"))
        self.assertEqual(resp_json["status"], "success")
        data = resp_json["data"]
        self.assertEqual(data["start_line"], 4)
        self.assertEqual(data["end_line"], 6)
        self.assertEqual(len(data["lines"]), 3)
        self.assertEqual(data["lines"][1]["line_no"], 5)
        self.assertEqual(data["lines"][1]["coverage_state"], "uncovered")

    @patch.object(CoverageHTTPRequestHandler, "send_response")
    @patch.object(CoverageHTTPRequestHandler, "send_header")
    @patch.object(CoverageHTTPRequestHandler, "end_headers")
    @patch.object(CoverageHTTPRequestHandler, "safe_write")
    def test_get_code_lines_invalid_range(self, mock_write, mock_end, mock_header, mock_resp):
        handler = self._execute_handler_get(
            "/api/coverage/code-lines?project=TestProj&file=src/calc.c&start_line=6&end_line=2"
        )
        mock_resp.assert_called_with(400)

    @patch.object(CoverageHTTPRequestHandler, "send_response")
    @patch.object(CoverageHTTPRequestHandler, "send_header")
    @patch.object(CoverageHTTPRequestHandler, "end_headers")
    @patch.object(CoverageHTTPRequestHandler, "safe_write")
    def test_post_code_lines_batch_success(self, mock_write, mock_end, mock_header, mock_resp):
        payload = {
            "project_name": "TestProj",
            "file_path": "src/calc.c",
            "ranges": [
                {"start_line": 1, "end_line": 3},
                {"start_line": 4, "end_line": 6},
            ],
        }
        handler = self._execute_handler_post("/api/coverage/code-lines/batch", payload)
        mock_resp.assert_called_with(200)
        written_data = b"".join(call[0][0] for call in mock_write.call_args_list)
        resp_json = json.loads(written_data.decode("utf-8"))
        self.assertEqual(resp_json["status"], "success")
        ranges = resp_json["data"]["ranges"]
        self.assertEqual(len(ranges), 2)
        self.assertEqual(len(ranges[0]["lines"]), 3)
        self.assertEqual(len(ranges[1]["lines"]), 3)

    @patch.object(CoverageHTTPRequestHandler, "send_response")
    @patch.object(CoverageHTTPRequestHandler, "send_header")
    @patch.object(CoverageHTTPRequestHandler, "end_headers")
    @patch.object(CoverageHTTPRequestHandler, "safe_write")
    def test_post_code_lines_batch_excessive_ranges(self, mock_write, mock_end, mock_header, mock_resp):
        payload = {
            "project_name": "TestProj",
            "file_path": "src/calc.c",
            "ranges": [{"start_line": 1, "end_line": 1} for _ in range(101)],
        }
        handler = self._execute_handler_post("/api/coverage/code-lines/batch", payload)
        mock_resp.assert_called_with(400)


if __name__ == "__main__":
    unittest.main()
