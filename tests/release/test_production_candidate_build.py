import json
import os
import shutil
import subprocess
import tempfile
import unittest

from app.candidate_artifact import (
    CandidateArtifactManifest, PRODUCTION_PROJECT_NAME,
    PRODUCTION_RELEASE_ARTIFACT_ROLE,
)
from app.release_identity import DEFAULT_RELEASE_ASSET_RELATIVE_PATHS
from app.release_publication import build_release_manifest, validate_release_manifest
from scripts.release.build_production_candidate_artifact import (
    build_production_candidate,
)


class ProductionCandidateBuildTest(unittest.TestCase):
    def _source_repo(self, root):
        for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
            source = os.path.join(os.getcwd(), *relative.split("/"))
            target = os.path.join(root, *relative.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
        subprocess.check_call(["git", "init", "-q"], cwd=root)
        subprocess.check_call(
            ["git", "config", "user.email", "test@example.invalid"], cwd=root
        )
        subprocess.check_call(
            ["git", "config", "user.name", "Production Candidate Test"], cwd=root
        )
        subprocess.check_call(["git", "add", "."], cwd=root)
        subprocess.check_call(["git", "commit", "-q", "-m", "source"], cwd=root)

    def _served_root(self, root, project=PRODUCTION_PROJECT_NAME):
        for directory in ("reports", "assets", "registry"):
            os.makedirs(os.path.join(root, directory))
        report_id = "fos_report"
        with open(os.path.join(root, "reports", "index.html"), "w", encoding="utf-8") as stream:
            stream.write(
                '<html><head><meta name="coverage-project" content="{}">'
                '<meta name="coverage-report-mode" content="VNEXT_ARTIFACT_READY">'
                '<meta name="coverage-report-id" content="{}">'
                '<meta name="coverage-scan-id" content="7">'
                '<meta name="coverage-repository-name" content="fos-repo">'
                '<meta name="coverage-file-path" content="src/a.c">'
                '<meta name="coverage-asset-identity" content="asset-a">'
                '<meta name="coverage-sidecar-schema" content="1">'
                '</head><body>FOS report</body></html>'.format(project, report_id)
            )
        cache = os.path.join(root, "reports", ".source_cache", report_id)
        os.makedirs(cache)
        with open(os.path.join(cache, "meta.json"), "w", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "report_id": report_id}, stream)
        with open(os.path.join(root, "assets", "coverage_enhance.js"), "w") as stream:
            stream.write("old-js")
        with open(os.path.join(root, "assets", "coverage_enhance.css"), "w") as stream:
            stream.write("old-css")
        with open(os.path.join(root, "registry", report_id + ".json"), "w", encoding="utf-8") as stream:
            json.dump({
                "report_id": report_id,
                "report_mode": "VNEXT_ARTIFACT_READY",
                "scan_id": 7,
                "report_root": "reports",
                "sidecar_schema": 1,
                "asset_identity": "asset-a",
                "repository_name": "fos-repo",
                "project_name": project,
            }, stream)

    def _provenance_args(self):
        return (
            "github-actions/trusted-production-builder", "123", "1", "f" * 40
        )

    def test_builder_creates_a_separate_production_role_from_real_served_root(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-") as root:
            source = os.path.join(root, "source")
            served = os.path.join(root, "served")
            candidate = os.path.join(root, "production-candidate")
            os.makedirs(source)
            os.makedirs(served)
            self._source_repo(source)
            self._served_root(served)
            identity_output = os.path.join(root, "release_identity.json")
            result = build_production_candidate(
                served, source, candidate, identity_output, *self._provenance_args()
            )
            self.assertEqual(result["artifact_role"], PRODUCTION_RELEASE_ARTIFACT_ROLE)
            self.assertTrue(result["production_publishable"])
            self.assertEqual(result["project_name"], PRODUCTION_PROJECT_NAME)
            with open(result["candidate_artifact_manifest"], encoding="utf-8") as stream:
                manifest = json.load(stream)
            self.assertEqual(manifest["artifact_role"], PRODUCTION_RELEASE_ARTIFACT_ROLE)
            self.assertTrue(manifest["production_publishable"])
            verified = CandidateArtifactManifest.verify(
                candidate, manifest,
                expected_artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
                expected_project_name=PRODUCTION_PROJECT_NAME,
                require_production_publishable=True,
                require_trusted_provenance=True,
            )
            self.assertEqual(verified["artifact_sha256"], result["artifact_sha256"])
            with open(os.path.join(candidate, "assets", "coverage_enhance.js"), "rb") as stream:
                candidate_js = stream.read()
            with open(os.path.join(source, "web", "assets", "js", "coverage_enhance.js"), "rb") as stream:
                source_js = stream.read()
            self.assertEqual(candidate_js, source_js)
            release_manifest = build_release_manifest(
                candidate, manifest, "production-candidate-test",
                candidate_sha=manifest["commit_sha"],
            )
            self.assertEqual(
                validate_release_manifest(
                    candidate, release_manifest, "production-candidate-test"
                )["status"],
                "PASSED",
            )

    def test_builder_rejects_non_production_project_and_validation_fixture_content(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-reject-") as root:
            source = os.path.join(root, "source")
            served = os.path.join(root, "served")
            os.makedirs(source)
            os.makedirs(served)
            self._source_repo(source)
            self._served_root(served, project="Coverage Candidate")
            with self.assertRaisesRegex(ValueError, "production report project"):
                build_production_candidate(
                    served, source, os.path.join(root, "candidate"),
                    os.path.join(root, "identity.json"), *self._provenance_args()
                )


if __name__ == "__main__":
    unittest.main()
