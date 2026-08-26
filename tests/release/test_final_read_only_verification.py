import argparse
import json
import os
import tempfile
import unittest
from unittest import mock

from scripts.diagnostics import final_read_only_verification as verifier


class FinalReadOnlyVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="final-read-only-")
        self.config_path = os.path.join(self.temp.name, "candidate.json")
        with open(self.config_path, "w", encoding="utf-8") as stream:
            json.dump({"mysql": {"database": "coverage"}}, stream)
        self.release = {
            "commit_sha": "a" * 40,
            "build_id": "build-a",
            "asset_hash": "b" * 64,
            "schema_version": 2,
            "asset_manifest_version": 1,
            "asset_count": 12,
            "asset_manifest_hash": "c" * 64,
            "asset_manifest": [{"path": "coverage_progress.js"}],
        }
        self.release_path = os.path.join(self.temp.name, "release.json")
        with open(self.release_path, "w", encoding="utf-8") as stream:
            json.dump(self.release, stream)

    def tearDown(self):
        self.temp.cleanup()

    def _args(self, with_release=False):
        return argparse.Namespace(
            config=self.config_path,
            endpoint="http://candidate.invalid",
            release_identity=self.release_path if with_release else None,
            project="",
            scan_id="",
            header=[],
            output=None,
        )

    def _http(self, release_payload=None, health_error=None):
        payloads = {
            "/api/coverage/health": {"status": "ok"},
            "/api/coverage/release": release_payload or {"release": self.release},
            "/api/coverage/projects": {"projects": []},
            "/api/coverage/progress": {"scan_id": None},
        }

        def get_json(_endpoint, path, _query, _headers):
            if path == "/api/coverage/health" and health_error:
                raise RuntimeError(health_error)
            return 200, payloads[path]

        return get_json

    def _patched(self, args, get_json):
        return mock.patch.multiple(
            verifier,
            _database_checks=mock.Mock(return_value={"status": "PASSED"}),
            _get_json=mock.Mock(side_effect=get_json),
            generate_release_identity=mock.Mock(return_value=self.release),
        )

    def test_without_release_identity_health_projects_and_progress_are_checked(self):
        with self._patched(self._args(), self._http()):
            result = verifier.verify(self._args())

        self.assertEqual(result["status"], "PASSED")

    def test_correct_release_identity_passes_after_health_request(self):
        args = self._args(with_release=True)
        with self._patched(args, self._http()) as _patches:
            result = verifier.verify(args)

        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["violations"], [])

    def test_asset_manifest_hash_mismatch_fails_release_comparison(self):
        actual = dict(self.release, asset_manifest_hash="d" * 64)
        args = self._args(with_release=True)
        with self._patched(args, self._http({"release": actual})):
            result = verifier.verify(args)

        self.assertNotEqual(result["status"], "PASSED")
        self.assertTrue(any("asset_manifest_hash" in item
                            for item in result["violations"]))

    def test_health_failure_does_not_pollute_release_comparison(self):
        args = self._args(with_release=True)
        with self._patched(
                args, self._http(health_error="health unavailable")):
            result = verifier.verify(args)

        self.assertNotEqual(result["status"], "PASSED")
        self.assertTrue(any("/api/coverage/health" in item
                            for item in result["violations"]))
        self.assertFalse(any("release identity mismatch" in item
                             for item in result["violations"]))

    def test_non_dict_release_payload_is_an_explicit_failure(self):
        args = self._args(with_release=True)
        with self._patched(args, self._http({"release": []})):
            result = verifier.verify(args)

        self.assertNotEqual(result["status"], "PASSED")
        self.assertTrue(any("release identity mismatch" in item
                            for item in result["violations"]))


if __name__ == "__main__":
    unittest.main()
