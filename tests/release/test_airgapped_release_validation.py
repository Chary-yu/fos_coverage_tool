import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from app.api import airgapped_release_validation as validation


class _Runtime(object):
    def __init__(self, repo_root):
        self.repo_root = repo_root


class _Application(object):
    def __init__(self, config=None, repo_root="/tmp/release/app"):
        self.config = config or {}
        self.runtime = _Runtime(repo_root)


class AirGappedReleaseValidationTest(unittest.TestCase):
    def test_endpoint_is_candidate_reverse_proxy_only(self):
        self.assertTrue(validation.enabled(_Application({
            "environment": "candidate",
            "auth": {"mode": "reverse_proxy"},
        })))
        self.assertFalse(validation.enabled(_Application({
            "environment": "production",
            "auth": {"mode": "reverse_proxy"},
        })))
        self.assertFalse(validation.enabled(_Application({
            "environment": "candidate",
            "auth": {"mode": "disabled"},
        })))

    def test_report_discovery_rejects_escape_and_prefers_code_detail(self):
        with tempfile.TemporaryDirectory() as root:
            reports = os.path.join(root, "reports")
            os.makedirs(reports)
            first = os.path.join(reports, "summary.html")
            detail = os.path.join(reports, "nested", "file.gcov.html")
            os.makedirs(os.path.dirname(detail))
            with open(first, "wb") as stream:
                stream.write(b"summary")
            with open(detail, "wb") as stream:
                stream.write(b"detail-report")
            values = validation._validated_report_entries(root, [
                {
                    "path": "reports/summary.html",
                    "sha256": hashlib.sha256(b"summary").hexdigest(),
                    "size": 7,
                    "file_path": "",
                },
                {
                    "path": "reports/nested/file.gcov.html",
                    "sha256": hashlib.sha256(b"detail-report").hexdigest(),
                    "size": 13,
                    "file_path": "src/file.c",
                },
            ])
            self.assertEqual("reports/nested/file.gcov.html", values[0]["path"])
            with self.assertRaises(RuntimeError):
                validation._validated_report_entries(root, [{
                    "path": "reports/../outside.html",
                    "sha256": "a" * 64,
                    "size": 1,
                }])

    def test_private_evidence_directory_rejects_symlink_parent(self):
        with tempfile.TemporaryDirectory() as root:
            outside = tempfile.mkdtemp()
            try:
                os.symlink(outside, os.path.join(root, "validation_evidence"))
                target = os.path.join(
                    root, "validation_evidence", "session", "evidence.json"
                )
                with self.assertRaises(RuntimeError):
                    validation._atomic_json(
                        target, {"status": "PASSED"}, trusted_root=root
                    )
            finally:
                os.rmdir(outside)

    def test_negative_control_is_loopback_bound_and_disables_proxy(self):
        application = _Application({"server": {"port": 19528}})
        observed = {}

        class _Response(object):
            status = 403
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        class _Opener(object):
            def open(self, request, timeout=0):
                observed["url"] = request.full_url
                observed["timeout"] = timeout
                return _Response()

        with mock.patch.object(
                validation.urllib.request, "build_opener", return_value=_Opener()
        ) as build_opener:
            status = validation._negative_mutation_probe(application)
        self.assertEqual(403, status)
        self.assertEqual(
            "http://127.0.0.1:19528/api/coverage/auth/mutation-probe",
            observed["url"],
        )
        self.assertEqual(10, observed["timeout"])
        proxy_handler = build_opener.call_args[0][0]
        self.assertIsInstance(proxy_handler, validation.urllib.request.ProxyHandler)

    def test_submission_contract_rejects_browser_claim_drift(self):
        context = {
            "nonce": "n",
            "candidate_revision": "1" * 40,
            "release_validation_session_id": "session-1",
            "candidate_artifact_sha256": "2" * 64,
            "served_root_sha256": "3" * 64,
            "candidate_url": "http://10.0.0.1:19529/coverage/report.html",
            "report_path": "reports/report.html",
            "report_sha256": "4" * 64,
        }
        body = {
            "nonce": "n",
            "candidate_revision": "1" * 40,
            "release_validation_session_id": "session-1",
            "candidate_artifact_sha256": "2" * 64,
            "served_root_sha256": "3" * 64,
            "candidate_url": "http://10.0.0.1:19529/coverage/report.html",
            "report_path": "reports/report.html",
            "report_sha256": "4" * 64,
            "browser_functional": {
                "status": "PASSED", "report_loaded": True,
                "release_identity_verified": True,
            },
            "operator_browser_workload": {
                "status": "PASSED",
                "workload_id": "airgapped-real-candidate-report-v1",
                "visible_content_units": 1,
                "js_errors": [],
                "environment_identity": {"browser_name": "chromium"},
            },
            "authenticated_probe": {
                "status_code": 200,
                "payload": {
                    "mutation_probe": True,
                    "probe_path": "/api/coverage/auth/mutation-probe",
                    "database_mutation": False,
                    "authenticated_user": "operator",
                },
            },
        }
        self.assertEqual([], validation._validate_submission(context, body, "operator"))
        body["report_sha256"] = "5" * 64
        self.assertIn(
            "report_sha256 does not match immutable Candidate context",
            validation._validate_submission(context, body, "operator"),
        )

    def test_operator_observation_never_claims_performance_gate(self):
        with tempfile.TemporaryDirectory() as publish_root:
            attempt = os.path.join(publish_root, "validation_evidence", "s1")
            paths = {
                "publish_root": publish_root,
                "attempt_root": attempt,
                "state": os.path.join(attempt, validation.STATE_NAME),
                "operator": os.path.join(attempt, validation.OPERATOR_EVIDENCE_NAME),
                "workload": os.path.join(attempt, validation.OPERATOR_WORKLOAD_NAME),
                "auth": os.path.join(attempt, validation.AUTH_EVIDENCE_NAME),
            }
            context = {
                "_paths": paths,
                "candidate_revision": "1" * 40,
                "release_validation_session_id": "s1",
                "candidate_artifact_sha256": "2" * 64,
                "served_root_sha256": "3" * 64,
                "candidate_url": "http://10.0.0.1:19529/coverage/report.html",
                "report_path": "reports/report.html",
                "report_sha256": "4" * 64,
                "release_identity": {"commit_sha": "1" * 40},
                "publication": {"release_validation_session_id": "s1"},
                "auth_mode": "reverse_proxy",
                "user_header": "X-Remote-User",
                "gateway_config_sha256": "5" * 64,
            }
            body = {
                "browser_functional": {"status": "PASSED"},
                "operator_browser_workload": {
                    "status": "PASSED",
                    "workload_id": "airgapped-real-candidate-report-v1",
                    "environment_identity": {"browser_name": "chromium"},
                },
                "authenticated_probe": {
                    "status_code": 200,
                    "payload": {
                        "mutation_probe": True,
                        "authenticated_user": "operator",
                        "database_mutation": False,
                    },
                },
            }
            observation, auth = validation._write_evidence(
                context, body, "operator", 401
            )
            self.assertFalse(observation["release_eligible"])
            self.assertTrue(observation["requires_performance_join"])
            self.assertEqual(
                "airgapped_real_candidate_browser_observation",
                observation["evidence_class"],
            )
            self.assertTrue(auth["release_eligible"])
            self.assertEqual(401, auth["mutation_probe"]["unauthenticated_status_code"])
            with open(paths["operator"], "r", encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertFalse(persisted["release_eligible"])

    def test_public_context_does_not_expose_filesystem_paths(self):
        context = {"candidate_revision": "a" * 40, "_paths": {"secret": "/tmp/x"}}
        payload = validation._public_payload(context, "operator")
        self.assertNotIn("_paths", payload)
        self.assertEqual("operator", payload["operator_identity"])


if __name__ == "__main__":
    unittest.main()
