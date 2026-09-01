import json
import os
import tempfile
import unittest

from app.candidate_artifact import CandidateArtifactManifest
from app.release_publication import (
    ImmutableReleasePublisher, validate_release_manifest,
)


class ImmutableReleasePublicationTest(unittest.TestCase):
    def _source(self, root, mode="VNEXT_ARTIFACT_READY", include_mode=True):
        for directory in ("reports", "assets", "registry"):
            os.makedirs(os.path.join(root, directory))
        report_id = "report_vnext"
        html = os.path.join(root, "reports", "index.html")
        report_mode_meta = (
            '<meta name="coverage-report-mode" content="{}">\n'.format(mode)
            if include_mode else ""
        )
        with open(html, "w", encoding="utf-8") as stream:
            stream.write(
                "<html><head>\n" + report_mode_meta +
                '<meta name="coverage-report-id" content="{}">\n'
                '<meta name="coverage-scan-id" content="7">\n'
                '<meta name="coverage-repository-name" content="repo-a">\n'
                '<meta name="coverage-file-path" content="src/a.c">\n'
                '<meta name="coverage-asset-identity" content="asset-a">\n'
                '<meta name="coverage-sidecar-schema" content="1">\n'
                "</head><body></body></html>\n".format(report_id)
            )
        os.makedirs(os.path.join(root, "reports", ".source_cache", report_id))
        with open(
                os.path.join(root, "reports", ".source_cache", report_id, "meta.json"),
                "w", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "report_id": report_id}, stream)
        with open(os.path.join(root, "assets", "coverage.js"), "w") as stream:
            stream.write("asset")
        with open(os.path.join(root, "registry", report_id + ".json"), "w") as stream:
            json.dump({
                "report_id": report_id,
                "report_mode": mode,
                "scan_id": 7,
                "report_root": "reports",
                "sidecar_schema": 1,
                "asset_identity": "asset-a",
            }, stream)

    def _prepare(self, publisher, source, identity, session_id, **kwargs):
        CandidateArtifactManifest.build(source, identity)
        return publisher.prepare(source, identity, session_id, **kwargs)

    def test_prepare_switch_validate_and_rollback_are_atomic(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source(source)
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            identity = {"commit_sha": "a" * 40, "build_id": "candidate-a"}
            manifest = self._prepare(
                publisher, source, identity, "candidate-session",
                api_contract_version="vnext-api-test"
            )
            self.assertEqual(manifest["report_modes"], ["VNEXT_ARTIFACT_READY"])
            self.assertEqual(publisher.validate_current()["status"], "FAILED")
            switched = publisher.switch_current("candidate-session")
            self.assertEqual(switched["status"], "PASSED")
            self.assertEqual(
                publisher.validate_current()["release_validation_session_id"],
                "candidate-session",
            )
            self.assertEqual(
                validate_release_manifest(
                    publisher.release_path("candidate-session"), manifest
                )["status"],
                "PASSED",
            )

    def test_vnext_report_missing_sidecar_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-invalid-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source(source)
            import shutil
            shutil.rmtree(os.path.join(source, "reports", ".source_cache"))
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "Sidecar"):
                self._prepare(
                    publisher, source,
                    {"commit_sha": "b" * 40, "build_id": "candidate-b"},
                    "invalid-session"
                )

    def test_legacy_report_without_mode_is_explicitly_annotated_in_candidate(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-legacy-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source(source, mode="LEGACY_STATIC", include_mode=False)
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            manifest = self._prepare(
                publisher, source,
                {"commit_sha": "c" * 40, "build_id": "candidate-c"},
                "legacy-session"
            )
            report_path = os.path.join(
                publisher.release_path("legacy-session"), "reports", "index.html"
            )
            with open(report_path, encoding="utf-8") as stream:
                html = stream.read()
            self.assertIn(
                'name="coverage-report-mode" content="LEGACY_STATIC"', html
            )
            self.assertEqual(manifest["report_modes"], ["LEGACY_STATIC"])

    def test_vnext_registry_without_html_mode_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-vnext-mode-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source(source, include_mode=False)
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "explicit report mode"):
                self._prepare(
                    publisher, source,
                    {"commit_sha": "d" * 40, "build_id": "candidate-d"},
                    "missing-mode-session"
                )

    def test_nested_source_symlink_fails_closed_before_copy(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-symlink-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            identity = {"commit_sha": "e" * 40, "build_id": "candidate-e"}
            CandidateArtifactManifest.build(source, identity)
            nested = os.path.join(source, "assets", "nested-link")
            outside = os.path.join(root, "outside")
            os.makedirs(outside)
            with open(os.path.join(outside, "secret.js"), "w") as stream:
                stream.write("must not be copied")
            try:
                os.symlink(outside, nested)
            except OSError:
                self.skipTest("symbolic links are unavailable in this environment")
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "symlink"):
                publisher.prepare(source, identity, "nested-symlink-session")

    def test_nested_published_symlink_fails_current_validation(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-published-link-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            self._prepare(
                publisher, source,
                {"commit_sha": "f" * 40, "build_id": "candidate-f"},
                "published-link-session"
            )
            self.assertEqual(
                publisher.switch_current("published-link-session")["status"],
                "PASSED",
            )
            nested = os.path.join(
                publisher.release_path("published-link-session"),
                "assets", "nested-link",
            )
            outside = os.path.join(root, "outside")
            os.makedirs(outside)
            try:
                os.symlink(outside, nested)
            except OSError:
                self.skipTest("symbolic links are unavailable in this environment")
            result = publisher.validate_current()
            self.assertEqual(result["status"], "FAILED")
            self.assertTrue(any("symlink" in item for item in result["violations"]))

    def test_candidate_artifact_tamper_after_manifest_fails_before_prepare(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-tamper-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            identity = {"commit_sha": "1" * 40, "build_id": "candidate-1"}
            CandidateArtifactManifest.build(source, identity)
            with open(os.path.join(source, "assets", "coverage.js"), "a") as stream:
                stream.write("\ntampered\n")
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "candidate_root"):
                publisher.prepare(source, identity, "tampered-session")


if __name__ == "__main__":
    unittest.main()
