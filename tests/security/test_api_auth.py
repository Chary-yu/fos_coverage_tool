import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import enhance_coverage


class TestApiAuthBoundary(unittest.TestCase):
    def _handler(self, peer, headers):
        handler = object.__new__(enhance_coverage.CoverageHTTPRequestHandler)
        handler.client_address = (peer, 1)
        handler.headers = headers
        handler.send_error_response = lambda status, message: setattr(handler, "error", (status, message))
        return handler

    def test_untrusted_proxy_cannot_supply_reviewer(self):
        handler = self._handler("10.0.0.9", {"X-Remote-User": "spoofed"})
        self.assertIsNone(handler._authorize_mutation())
        self.assertEqual(handler.error[0], 401)

    def test_trusted_proxy_identity_wins_over_client_reviewer(self):
        handler = self._handler("127.0.0.1", {"X-Remote-User": "alice"})
        self.assertEqual(handler._authorize_mutation(), "alice")

    def test_disallowed_origin_is_rejected(self):
        handler = self._handler("127.0.0.1", {"X-Remote-User": "alice", "Origin": "https://evil.invalid"})
        self.assertIsNone(handler._authorize_mutation())
        self.assertEqual(handler.error[0], 403)

    def test_freeze_marker_blocks_mutation_before_authentication(self):
        with tempfile.TemporaryDirectory(prefix="coverage-freeze-test-") as state_root:
            os.makedirs(state_root, exist_ok=True)
            marker = os.path.join(state_root, "upgrade-writes-frozen.json")
            with open(marker, "w", encoding="utf-8") as stream:
                stream.write("{}")
            handler = self._handler("127.0.0.1", {"X-Remote-User": "alice"})
            config = {
                "runtime_state": {"root": state_root},
                "auth": {
                    "mode": "reverse_proxy",
                    "trusted_proxy_addresses": ["127.0.0.1"],
                    "user_header": "X-Remote-User",
                },
            }
            with mock.patch.object(enhance_coverage, "load_config", return_value=config):
                self.assertIsNone(handler._authorize_mutation())
            self.assertEqual(handler.error[0], 503)


if __name__ == "__main__":
    unittest.main()
