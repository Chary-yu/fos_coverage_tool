import copy
import os
import tempfile
import unittest

from scripts.release.vfoswind_attempt_config import (
    normalize_vfoswind_attempt_config,
)


class VfoswindAttemptConfigRegressionTest(unittest.TestCase):
    def _legacy_config(self):
        return {
            "auth": {"mode": "disabled"},
            "upgrade": {
                "lifecycle_adapter": "vfoswind",
                "candidate_browser_url": "http://10.190.162.33:19529",
                "production_integration": {
                    "adapter": "vfoswind",
                    "candidate_gateway": {
                        "static_location": "/coverage/",
                        "browser_url": "http://10.190.162.33:19529",
                        "auth_bridge": {"user_header": "X-Remote-User"},
                    },
                },
            },
        }

    def test_legacy_vfoswind_config_materializes_r8_contract(self):
        source = self._legacy_config()
        before = copy.deepcopy(source)
        with tempfile.TemporaryDirectory(prefix="r8-attempt-config-") as root:
            candidate_app = os.path.join(root, "production-candidate", "app")
            result = normalize_vfoswind_attempt_config(source, candidate_app)

        self.assertEqual(source, before)
        self.assertEqual(result["auth"]["mode"], "reverse_proxy")
        self.assertEqual(result["auth"]["user_header"], "X-Remote-User")
        self.assertEqual(
            result["auth"]["trusted_proxy_addresses"], ["127.0.0.1", "::1"]
        )
        self.assertEqual(result["auth"]["allowed_origins"], [])

        upgrade = result["upgrade"]
        self.assertEqual(upgrade["serving_session_id"], "current-serving")
        self.assertEqual(
            upgrade["serving_session_manifest"],
            "/home/zcyu/coverage_candidate/serving-session.json",
        )
        self.assertEqual(
            upgrade["serving_teardown_evidence_path"],
            "/home/zcyu/coverage_candidate/serving-teardown.json",
        )
        self.assertEqual(
            upgrade["current_serving_state_path"],
            "/home/zcyu/coverage_candidate/current-serving.json",
        )
        self.assertEqual(
            upgrade["candidate_browser_url"],
            "http://10.190.162.33:19529/coverage/coverage_progress.html",
        )

        integration = upgrade["production_integration"]
        self.assertEqual(
            integration["runtime_environment_file"],
            "/etc/onesensor/coverage-runtime.env",
        )
        self.assertEqual(
            integration["validation_systemd_unit"],
            "onesensor-coverage-validation.service",
        )
        self.assertEqual(
            integration["validation_systemd_unit_file"],
            "/etc/systemd/system/onesensor-coverage-validation.service",
        )
        self.assertEqual(
            integration["validation_runtime_environment_file"],
            "/etc/onesensor/coverage-validation.env",
        )
        self.assertEqual(
            integration["validation_config_path"],
            "/etc/onesensor/coverage-validation.json",
        )
        self.assertTrue(
            integration["validation_application_root"].endswith(
                "/production-candidate/app"
            )
        )
        self.assertEqual(
            integration["candidate_gateway"]["browser_url"],
            upgrade["candidate_browser_url"],
        )

    def test_explicit_valid_contract_is_preserved(self):
        source = self._legacy_config()
        source["auth"] = {
            "mode": "reverse_proxy",
            "user_header": "X-Remote-User",
            "trusted_proxy_addresses": ["127.0.0.1"],
            "allowed_origins": ["https://example.invalid"],
        }
        upgrade = source["upgrade"]
        upgrade.update({
            "candidate_browser_url": (
                "http://10.190.162.33:19529/coverage/custom.html"
            ),
            "serving_session_id": "stable-owner",
            "serving_session_manifest": "/custom/serving.json",
            "serving_teardown_evidence_path": "/custom/teardown.json",
            "current_serving_state_path": "/custom/current.json",
        })
        integration = upgrade["production_integration"]
        integration["runtime_environment_file"] = "/custom/runtime.env"
        integration["candidate_gateway"]["browser_url"] = upgrade[
            "candidate_browser_url"
        ]

        result = normalize_vfoswind_attempt_config(source, "")
        self.assertEqual(result["auth"], source["auth"])
        self.assertEqual(result["upgrade"]["serving_session_id"], "stable-owner")
        self.assertEqual(
            result["upgrade"]["production_integration"][
                "runtime_environment_file"
            ],
            "/custom/runtime.env",
        )
        self.assertEqual(
            result["upgrade"]["candidate_browser_url"],
            "http://10.190.162.33:19529/coverage/custom.html",
        )

    def test_non_vfoswind_config_is_unchanged(self):
        source = {
            "auth": {"mode": "disabled"},
            "upgrade": {"lifecycle_adapter": "local"},
        }
        self.assertEqual(normalize_vfoswind_attempt_config(source), source)

    def test_unknown_auth_mode_fails_closed(self):
        source = self._legacy_config()
        source["auth"] = {"mode": "basic"}
        with self.assertRaisesRegex(RuntimeError, "reverse_proxy-compatible"):
            normalize_vfoswind_attempt_config(source)


if __name__ == "__main__":
    unittest.main()
