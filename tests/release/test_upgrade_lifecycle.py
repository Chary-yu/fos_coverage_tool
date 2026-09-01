import json
import os
import tempfile
import unittest
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
                        second_results.append(name) or self._result(name))):
                self.assertEqual(second.stop_current_api()["managed_serving_before_stop"], True)
                second.active = True
                second.api_started = True
                rollback = second.abort()

            self.assertEqual(
                second_results,
                ["stop_current_api", "stop_validation_api", "start_serving_api"],
            )
            self.assertEqual(rollback["status"], "PASSED")
            self.assertNotIn("start_previous_api", second_results)


if __name__ == "__main__":
    unittest.main()
