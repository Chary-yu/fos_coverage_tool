import json
import os
import tempfile
import unittest

from app.release_publication import ImmutableReleasePublisher
from scripts.release.bootstrap_previous_release import bootstrap, main as bootstrap_main


class BootstrapPreviousReleaseTest(unittest.TestCase):
    def _identity(self):
        return {
            "version": "v-test",
            "commit_sha": "a" * 40,
            "build_id": "build-a",
            "asset_hash": "b" * 64,
            "schema_version": 2,
            "asset_manifest_version": 1,
            "asset_count": 1,
            "asset_manifest_hash": "c" * 64,
            "asset_manifest": [{"path": "coverage.js"}],
        }

    def _served_root(self, root, identity=None):
        for directory in ("reports", "assets", "registry"):
            os.makedirs(os.path.join(root, directory))
        report_id = "report_vnext"
        with open(os.path.join(root, "reports", "index.html"), "w", encoding="utf-8") as stream:
            stream.write(
                '<html><head><meta name="coverage-report-mode" content="VNEXT_ARTIFACT_READY">'
                '<meta name="coverage-report-id" content="{}">'
                '<meta name="coverage-scan-id" content="7">'
                '<meta name="coverage-repository-name" content="repo-a">'
                '<meta name="coverage-file-path" content="src/a.c">'
                '<meta name="coverage-asset-identity" content="asset-a">'
                '<meta name="coverage-sidecar-schema" content="1"></head></html>'.format(report_id)
            )
        os.makedirs(os.path.join(root, "reports", ".source_cache", report_id))
        with open(os.path.join(root, "reports", ".source_cache", report_id, "meta.json"), "w") as stream:
            json.dump({"schema_version": 1, "report_id": report_id}, stream)
        with open(os.path.join(root, "assets", "coverage.js"), "w") as stream:
            stream.write("asset")
        with open(os.path.join(root, "registry", report_id + ".json"), "w") as stream:
            json.dump({
                "report_id": report_id,
                "report_mode": "VNEXT_ARTIFACT_READY",
                "scan_id": 7,
                "report_root": "reports",
                "sidecar_schema": 1,
                "asset_identity": "asset-a",
            }, stream)
        identity_path = os.path.join(root, "release_identity.json")
        with open(identity_path, "w", encoding="utf-8") as stream:
            json.dump(identity or self._identity(), stream)
        return identity_path

    def _write_identity(self, root):
        path = os.path.join(root, "expected-identity.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(self._identity(), stream)
        return path

    def test_bootstrap_hashes_served_root_and_creates_valid_current(self):
        with tempfile.TemporaryDirectory(prefix="bootstrap-previous-") as root:
            served = os.path.join(root, "served")
            publish = os.path.join(root, "published")
            os.makedirs(served)
            identity_path = self._served_root(served)
            expected_path = self._write_identity(root)
            result = bootstrap(
                served, publish, expected_path, "previous-a",
                served_identity_path=identity_path, switch=True,
            )
            self.assertEqual(result["status"], "PASSED")
            self.assertGreater(result["candidate_artifact_manifest"]["file_count"], 0)
            current = ImmutableReleasePublisher(publish).validate_current()
            self.assertEqual(current["status"], "PASSED")
            self.assertEqual(current["commit_sha"], self._identity()["commit_sha"])

    def test_bootstrap_requires_served_identity_and_does_not_auto_create_current(self):
        with tempfile.TemporaryDirectory(prefix="bootstrap-no-identity-") as root:
            served = os.path.join(root, "served")
            publish = os.path.join(root, "published")
            os.makedirs(served)
            expected_path = self._write_identity(root)
            with self.assertRaises(SystemExit) as failure:
                bootstrap_main([
                    "--served-root", served,
                    "--publish-root", publish,
                    "--release-identity", expected_path,
                    "--session-id", "previous-a",
                    "--switch",
                ])
            self.assertEqual(failure.exception.code, 2)
            self.assertFalse(os.path.lexists(os.path.join(publish, "CURRENT")))

    def test_bootstrap_is_not_called_by_normal_upgrade_entrypoint(self):
        with open("scripts/upgrade/run_upgrade.py", encoding="utf-8") as stream:
            source = stream.read()
        self.assertNotIn("bootstrap_previous_release", source)


if __name__ == "__main__":
    unittest.main()

