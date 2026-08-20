"""stdlib HTTP transport for the canonical VNext application."""

import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from app.api.serialization import dumps, loads


logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 8 * 1024 * 1024


class RequestTooLarge(ValueError):
    pass


class VNextHTTPRequestHandler(BaseHTTPRequestHandler):
    application = None

    def _send(self, status, payload):
        if isinstance(payload, dict) and payload.get("__download__"):
            return self._send_download(status, payload)
        data = dumps(payload).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_download(self, status, payload):
        path = os.path.realpath(str(payload.get("__download__")))
        if not os.path.isfile(path):
            self._send(404, {"error": "not_found", "message": "download is unavailable"})
            return
        filename = os.path.basename(str(payload.get("filename") or path))
        filename = "".join(
            char if ord(char) >= 32 and char not in '\\"\r\n' else "_"
            for char in filename
        ) or "export.zip"
        size = os.path.getsize(path)
        self.send_response(int(status))
        self.send_header("Content-Type", str(payload.get("content_type") or "application/zip"))
        self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(filename))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        # Stream completed exports so a large ZIP does not create a second
        # full-size in-memory copy in the HTTP worker.
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _request(self):
        parsed = urlsplit(self.path)
        query = {key: values[-1] if len(values) == 1 else values
                 for key, values in parse_qs(parsed.query).items()}
        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ValueError("invalid Content-Length")
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise RequestTooLarge("request body exceeds {} bytes".format(MAX_BODY_BYTES))
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("chunked request bodies are not supported")
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
        except RequestTooLarge:
            status, payload = 413, {
                "error": "request_too_large",
                "message": "request body exceeds the configured limit",
            }
        except ValueError:
            status, payload = 400, {
                "error": "invalid_request",
                "message": "invalid request",
            }
        except Exception as exc:
            logger.exception("VNext HTTP request failed")
            status, payload = 500, {"error": "internal_error", "message": "internal server error"}
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
