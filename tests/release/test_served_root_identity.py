import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest

from scripts.diagnostics.served_root_identity import verify_http_served_root


class _StaticRootHandler(BaseHTTPRequestHandler):
    root = ""

    def do_GET(self):  # noqa: N802 - stdlib handler contract
        relative = urllib.parse.urlsplit(self.path).path
        prefix = "/coverage/"
        if not relative.startswith(prefix):
            self.send_error(404)
            return
        relative = relative[len(prefix):] or "index.html"
        path = os.path.realpath(os.path.join(self.root, *relative.split("/")))
        if not os.path.isfile(path) or not path.startswith(self.root + os.sep):
            self.send_error(404)
            return
        with open(path, "rb") as stream:
            body = stream.read()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class ServedRootIdentityTest(unittest.TestCase):
    def _release_root(self, root, body="release-a"):
        reports = os.path.join(root, "reports")
        os.makedirs(reports)
        with open(os.path.join(reports, "index.html"), "w", encoding="utf-8") as stream:
            stream.write(
                '<html><link rel="stylesheet" href="coverage_enhance.css">'
                '<script src="coverage_enhance.js"></script>{}</html>'.format(body)
            )
        with open(os.path.join(reports, "coverage_enhance.css"), "w") as stream:
            stream.write("css-a")
        with open(os.path.join(reports, "coverage_enhance.js"), "w") as stream:
            stream.write("js-a")
        return reports

    def test_real_http_report_and_assets_match_validated_current_root(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="served-root-http-") as root:
            reports = self._release_root(root)
            _StaticRootHandler.root = reports
            server = HTTPServer(("127.0.0.1", 0), _StaticRootHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                result = verify_http_served_root(
                    "http://127.0.0.1:{}/coverage/".format(server.server_port),
                    root,
                    configured_served_root_path=reports,
                    url_prefix="/coverage/",
                    relative_path="index.html",
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(len(result["referenced_assets"]), 2)

    def test_http_old_root_cannot_pass_against_current_release(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="served-root-http-drift-") as root:
            current = os.path.join(root, "current")
            old = os.path.join(root, "old")
            reports = self._release_root(current, body="release-current")
            old_reports = self._release_root(old, body="release-old")
            _StaticRootHandler.root = old_reports
            server = HTTPServer(("127.0.0.1", 0), _StaticRootHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                with self.assertRaisesRegex(ValueError, "bytes do not match"):
                    verify_http_served_root(
                        "http://127.0.0.1:{}/coverage/".format(server.server_port),
                        current,
                        configured_served_root_path=reports,
                        url_prefix="/coverage/",
                        relative_path="index.html",
                    )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_configured_static_root_must_match_validated_current(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="served-root-contract-") as root:
            reports = self._release_root(root)
            with self.assertRaisesRegex(ValueError, "does not match"):
                verify_http_served_root(
                    "http://127.0.0.1:1/coverage/", root,
                    configured_served_root_path=os.path.join(root, "other"),
                )


if __name__ == "__main__":
    unittest.main()
