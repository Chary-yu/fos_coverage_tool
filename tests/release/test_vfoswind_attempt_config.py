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
        self.assertEqual(upgrade["publish_root"], "/home/zcyu/coverage_published")
        self.assertEqual(
            upgrade["served_root_path"],
            "/home/zcyu/coverage_published/CURRENT/reports",
        )
        self.assertEqual(
            upgrade["flat_served_root"],
            "/home/zcyu/coverage/export0810/onesensor",
        )
        self.assertEqual(
            upgrade["flat_release_identity_path"],
            "/home/zcyu/coverage/onesensor_code-coverage_tool/release_manifest.json",
        )
        self.assertEqual(
            upgrade["health_endpoint"],
            "http://127.0.0.1:9528/api/coverage/health",
        )
        self.assertEqual(
            upgrade["release_endpoint"],
            "http://127.0.0.1:9528/api/coverage/release",
        )
        self.assertEqual(
            upgrade["previous_release_endpoint"],
            "http://127.0.0.1:9528/api/coverage/release",
        )
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
        self.assertEqual(integration["systemd_unit"], "onesensor-api.service")
        self.assertEqual(
            integration["systemd_unit_file"],
            "/etc/systemd/system/onesensor-api.service",
        )
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
        self.assertEqual(
            integration["legacy_application_root"],
            "/home/zcyu/coverage/onesensor_code-coverage_tool",
        )
        self.assertEqual(
            integration["legacy_served_root"],
            "/home/zcyu/coverage/export0810/onesensor",
        )
        self.assertEqual(
            integration["nginx_config_path"],
            "/etc/nginx/conf.d/coverage.conf",
        )
        self.assertEqual(
            integration["nginx_proxy_pass"], "http://127.0.0.1:9528"
        )
        self.assertEqual(integration["api_location"], "/api/coverage")
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
            "publish_root": "/custom/published",
            "served_root_path": "/custom/published/CURRENT/reports",
            "flat_served_root": "/custom/flat",
            "flat_release_identity_path": "/custom/flat/release_identity.json",
            "health_endpoint": "http://127.0.0.1:19000/custom-health",
            "release_endpoint": "http://127.0.0.1:19000/custom-release",
            "previous_release_endpoint": "http://127.0.0.1:19000/previous-release",
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
        integration["legacy_application_root"] = "/custom/app"
        integration["legacy_served_root"] = "/custom/served"
        integration["nginx_config_path"] = "/custom/nginx.conf"
        integration["candidate_gateway"]["browser_url"] = upgrade[
            "candidate_browser_url"
        ]

        result = normalize_vfoswind_attempt_config(source, "")
        self.assertEqual(result["auth"], source["auth"])
        self.assertEqual(result["upgrade"]["publish_root"], "/custom/published")
        self.assertEqual(
            result["upgrade"]["served_root_path"],
            "/custom/published/CURRENT/reports",
        )
        self.assertEqual(result["upgrade"]["flat_served_root"], "/custom/flat")
        self.assertEqual(
            result["upgrade"]["flat_release_identity_path"],
            "/custom/flat/release_identity.json",
        )
        self.assertEqual(
            result["upgrade"]["health_endpoint"],
            "http://127.0.0.1:19000/custom-health",
        )
        self.assertEqual(
            result["upgrade"]["release_endpoint"],
            "http://127.0.0.1:19000/custom-release",
        )
        self.assertEqual(result["upgrade"]["serving_session_id"], "stable-owner")
        self.assertEqual(
            result["upgrade"]["production_integration"][
                "runtime_environment_file"
            ],
            "/custom/runtime.env",
        )
        self.assertEqual(
            result["upgrade"]["production_integration"]["legacy_application_root"],
            "/custom/app",
        )
        self.assertEqual(
            result["upgrade"]["production_integration"]["legacy_served_root"],
            "/custom/served",
        )
        self.assertEqual(
            result["upgrade"]["production_integration"]["nginx_config_path"],
            "/custom/nginx.conf",
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
