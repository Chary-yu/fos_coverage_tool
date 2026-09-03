import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from app.upgrade.lifecycle import UpgradeLifecycle


class UpgradeLifecycleServingTest(unittest.TestCase):
    def _config(self, root):
        state_path = os.path.join(root, "current-serving.json")
        return {
            "runtime_state": {"root": root},
            "upgrade": {
                "current_serving_state_path": state_path,
                "commands": {
                    "stop_current_api": ["stop-current"],
                    "stop_validation_api": ["stop-validation"],
                    "start_validation_api": ["start-validation"],
                    "start_serving_api": ["start-serving"],
                    "stop_serving_api": ["stop-serving"],
                    "start_previous_api": ["start-previous"],
                },
            },
        }, state_path

    @staticmethod
    def _result(name):
        return {"name": name, "status": "PASSED", "exit_code": 0}

    def test_two_consecutive_transitions_restore_managed_previous_serving(self):
        with tempfile.TemporaryDirectory(prefix="upgrade-serving-lifecycle-") as root:
            config, state_path = self._config(root)
            previous = {"commit_sha": "b" * 40, "_published_session_id": "release-b"}

            requested_paths = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    requested_paths.append(self.path)
                    if self.path == "/serving":
                        payload = {"release": {"commit_sha": previous["commit_sha"]}}
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps(payload).encode("utf-8"))
                    else:
                        self.send_response(404)
                        self.end_headers()

                def log_message(self, *_args):
                    pass

            try:
                server = HTTPServer(("127.0.0.1", 0), Handler)
            except PermissionError:
                self.skipTest("the test environment does not permit local TCP sockets")
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            config["upgrade"]["release_endpoint"] = (
                "http://127.0.0.1:{}/serving".format(server.server_port)
            )
            config["upgrade"]["previous_release_endpoint"] = (
                "http://127.0.0.1:{}/previous".format(server.server_port)
            )

            try:
                # First transition A -> B starts from the explicit legacy fallback.
                first = UpgradeLifecycle(root, config, "staging", previous)
                first_results = []
                with mock.patch.object(
                        first, "_run_command",
                        side_effect=lambda name, **kwargs: (
                            first_results.append(name) or self._result(name))):
                    self.assertEqual(first.stop_current_api()["managed_serving_before_stop"], False)
                    first.active = True
                    first.start_validation_api()
                    first.stop_validation_api()
                    first.start_serving_api()
                self.assertEqual(
                    first_results,
                    ["stop_current_api", "start_validation_api", "stop_validation_api",
                     "start_serving_api"],
                )

                # B is now the stable CURRENT serving owner.  A C attempt must not
                # fall back to the old 9528/start_previous_api lifecycle.
                previous["_previous_runtime_mysql"] = {
                    "host": "old-db",
                    "database": "coverage_vnext_e9fcc837",
                    "user": "coverage",
                }
                with open(state_path, "w", encoding="utf-8") as stream:
                    json.dump({
                        "schema_version": 1,
                        "role": "production_serving",
                        "status": "ACTIVE",
                        "session_id": "current-serving",
                        "pid_file": os.path.join(root, "serving-api.pid"),
                    }, stream)
                second = UpgradeLifecycle(root, config, "staging", previous)
                second_results = []
                with mock.patch.object(
                        second, "_run_command",
                        side_effect=lambda name, **kwargs: (
                            second_results.append((name, kwargs)) or self._result(name))):
                    self.assertEqual(second.stop_current_api()["managed_serving_before_stop"], True)
                    second.active = True
                    second.api_started = True
                    rollback = second.abort()

                self.assertEqual(
                    [item[0] for item in second_results],
                    ["stop_current_api", "stop_validation_api", "start_serving_api"],
                )
                restore_kwargs = second_results[-1][1]
                self.assertFalse(restore_kwargs["use_candidate_runtime"])
                restored_mysql = json.loads(
                    restore_kwargs["extra_env"]["COVERAGE_CANDIDATE_MYSQL_JSON"]
                )
                self.assertEqual(
                    restored_mysql["database"], "coverage_vnext_e9fcc837"
                )
                self.assertEqual(rollback["status"], "PASSED")
                self.assertEqual(rollback["restore_endpoint_key"], "release_endpoint")
                self.assertEqual(rollback["restore_endpoint"], config["upgrade"]["release_endpoint"])
                self.assertEqual(requested_paths, ["/serving"])
                self.assertNotIn("start_previous_api", second_results)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_abort_before_current_stop_preserves_existing_process(self):
        with tempfile.TemporaryDirectory(prefix="upgrade-preserve-current-") as root:
            config, _state_path = self._config(root)
            config["upgrade"]["commands"]["open_traffic"] = ["open-traffic"]
            lifecycle = UpgradeLifecycle(
                root, config, "staging",
                {"commit_sha": "b" * 40, "_published_session_id": "release-b"},
            )
            os.makedirs(os.path.dirname(lifecycle.marker), exist_ok=True)
            with open(lifecycle.marker, "w") as stream:
                stream.write("frozen")
            lifecycle.active = True
            requested = []
            with mock.patch.object(
                    lifecycle, "_run_command",
                    side_effect=lambda name, **kwargs: (
                        requested.append((name, kwargs)) or self._result(name))):
                result = lifecycle.abort()
            self.assertEqual(result["status"], "PASSED")
            self.assertTrue(result["current_process_preserved"])
            self.assertEqual([item[0] for item in requested], ["open_traffic"])
            self.assertFalse(os.path.exists(lifecycle.marker))


if __name__ == "__main__":
    unittest.main()
