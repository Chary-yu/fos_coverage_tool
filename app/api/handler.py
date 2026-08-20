"""stdlib HTTP transport for the canonical VNext application."""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from app.api.serialization import dumps, loads


class VNextHTTPRequestHandler(BaseHTTPRequestHandler):
    application = None

    def _send(self, status, payload):
        data = dumps(payload).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _request(self):
        parsed = urlsplit(self.path)
        query = {key: values[-1] if len(values) == 1 else values
                 for key, values in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = loads(self.rfile.read(length)) if length else {}
        return parsed.path, query, body

    def _dispatch(self, method):
        if not self.application:
            self._send(503, {"error": "runtime_unavailable"})
            return
        try:
            path, query, body = self._request()
            status, payload = self.application.dispatch(
                method, path, query, body, self.headers, self.client_address[0]
            )
        except ValueError as exc:
            status, payload = 400, {"error": "invalid_request", "message": str(exc)}
        except Exception as exc:
            status, payload = 500, {"error": "internal_error", "message": str(exc)}
        self._send(status, payload)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Remote-User")
        self.end_headers()
