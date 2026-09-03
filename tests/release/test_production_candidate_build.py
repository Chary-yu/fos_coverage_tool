import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from app.candidate_artifact import (
    CandidateArtifactManifest, PRODUCTION_PROJECT_NAME,
    PRODUCTION_RELEASE_ARTIFACT_ROLE, RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
)
from app.release_identity import DEFAULT_RELEASE_ASSET_RELATIVE_PATHS
from app.release_publication import (
    build_release_manifest, current_served_root_binding,
    validate_production_application_bundle, validate_release_manifest,
)
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
        shutil.copyfile(
            os.path.join(os.getcwd(), "enhance_coverage.py"),
            os.path.join(root, "enhance_coverage.py"),
        )
        # DEFAULT_RELEASE_ASSET_RELATIVE_PATHS already creates the web/assets
        # tree in this temporary checkout.  The builder only needs that
        # merged tree; copying web again would fail on Python 3.6/3.14 where
        # shutil.copytree has no dirs_exist_ok argument.
        for directory in ("app", "contracts"):
            shutil.copytree(
                os.path.join(os.getcwd(), directory),
                os.path.join(root, directory),
            )
        shim = os.path.join(root, "scripts", "compat", "git")
        os.makedirs(os.path.dirname(shim))
        shutil.copy2(os.path.join(os.getcwd(), "scripts", "compat", "git"), shim)
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

    def _current_served_root(self, root, project=PRODUCTION_PROJECT_NAME,
                             extra_files=None):
        publish_root = os.path.join(root, "publish")
        release_root = os.path.join(publish_root, "releases", "baseline")
        os.makedirs(release_root)
        self._served_root(release_root, project)
        for relative, contents in extra_files or ():
            path = os.path.join(release_root, *relative.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(contents)
        previous_sha = "1" * 40
        release_manifest = build_release_manifest(
            release_root,
            {"commit_sha": previous_sha, "build_id": "baseline"},
            "baseline",
            candidate_sha=previous_sha,
        )
        with open(os.path.join(release_root, "release_manifest.json"), "w",
                  encoding="utf-8") as stream:
            json.dump(release_manifest, stream, sort_keys=True)
        current = os.path.join(publish_root, "CURRENT")
        os.symlink(os.path.join("releases", "baseline"), current)
        return current, previous_sha

    def _provenance_args(self):
        return (
            "github-actions/trusted-production-builder", "123", "1", "f" * 40
        )

    @staticmethod
    def _expected_binding_kwargs(current):
        binding = current_served_root_binding(os.path.dirname(current))
        return {
            "expected_previous_release_sha": binding[
                "previous_release_commit_sha"
            ],
            "expected_served_root_tree_sha256": binding[
                "served_root_tree_sha256"
            ],
            "expected_current_identity_sha256": binding[
                "served_root_identity_sha256"
            ],
        }

    def test_builder_creates_a_separate_production_role_from_real_served_root(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-") as root:
            source = os.path.join(root, "source")
            candidate = os.path.join(root, "production-candidate")
            os.makedirs(source)
            self._source_repo(source)
            served, previous_sha = self._current_served_root(root)
            identity_output = os.path.join(root, "release_identity.json")
            result = build_production_candidate(
                served, source, candidate, identity_output, *self._provenance_args(),
                **self._expected_binding_kwargs(served)
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
            application = validate_production_application_bundle(candidate)
            self.assertEqual(application["status"], "PASSED")
            self.assertTrue(os.path.isfile(os.path.join(
                candidate, "app", "enhance_coverage.py"
            )))
            with open(os.path.join(candidate, "assets", "coverage_enhance.js"), "rb") as stream:
                candidate_js = stream.read()
            with open(os.path.join(source, "web", "assets", "js", "coverage_enhance.js"), "rb") as stream:
                source_js = stream.read()
            self.assertEqual(candidate_js, source_js)
            with open(identity_output, encoding="utf-8") as stream:
                identity = json.load(stream)
            self.assertEqual(
                result["served_root_binding"]["previous_release_sha"], previous_sha
            )
            provenance = manifest["source_provenance"]
            self.assertEqual(
                provenance["served_root_path"], os.path.abspath(served)
            )
            self.assertRegex(provenance["served_root_tree_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(provenance["served_root_identity_sha256"], r"^[0-9a-f]{64}$")
            actual_assets = []
            for item in identity["asset_manifest"]:
                path = os.path.join(candidate, *item["path"].split("/"))
                with open(path, "rb") as stream:
                    actual_assets.append({
                        "path": item["path"],
                        "size": os.path.getsize(path),
                        "sha256": hashlib.sha256(stream.read()).hexdigest(),
                    })
            self.assertEqual(actual_assets, identity["asset_manifest"])
            for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
                self.assertTrue(os.path.isfile(os.path.join(
                    candidate, *relative.split("/")
                )))
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

    def test_offline_builder_emits_operator_evidence_without_ci_fields(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-offline-") as root:
            source = os.path.join(root, "source")
            candidate = os.path.join(root, "production-candidate")
            os.makedirs(source)
            self._source_repo(source)
            served, previous_sha = self._current_served_root(root)
            bundle = os.path.join(root, "source.bundle")
            with open(bundle, "wb") as stream:
                stream.write(b"offline source bundle")
            evidence_path = os.path.join(root, "operator-evidence.json")
            result = build_production_candidate(
                served, source, candidate, os.path.join(root, "identity.json"),
                "", "", "", "",
                **self._expected_binding_kwargs(served),
                release_trust_mode=RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
                offline_operator_evidence_output=evidence_path,
                offline_operator_source_bundle=bundle,
                offline_operator_repository="Chary-yu/fos_coverage_tool",
                production_host="coverage-prod-01",
                production_baseline_sha=previous_sha,
                validation_session_id="offline-build-session",
            )
            self.assertEqual(result["release_trust_mode"], "offline_operator")
            self.assertFalse(result["receipt_required"])
            with open(evidence_path, encoding="utf-8") as stream:
                evidence = json.load(stream)
            self.assertEqual(evidence["protected_builder"], "SKIPPED_BY_OPERATOR")
            self.assertEqual(evidence["trust_class"], "OFFLINE_OPERATOR")
            with open(result["candidate_artifact_manifest"], encoding="utf-8") as stream:
                manifest = json.load(stream)
            self.assertEqual(
                manifest["source_provenance"]["provenance_class"],
                "offline-operator",
            )
            self.assertEqual(manifest["source_provenance"]["build_workflow_run_id"], "")

    def test_builder_rejects_non_production_project_and_validation_fixture_content(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-reject-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source_repo(source)
            served, _ = self._current_served_root(root, project="Coverage Candidate")
            with self.assertRaisesRegex(ValueError, "production report project"):
                build_production_candidate(
                    served, source, os.path.join(root, "candidate"),
                    os.path.join(root, "identity.json"), *self._provenance_args(),
                    **self._expected_binding_kwargs(served)
                )

    def test_builder_rejects_a_manually_selected_served_directory(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-path-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source_repo(source)
            current, _ = self._current_served_root(root)
            with self.assertRaisesRegex(ValueError, "CURRENT"):
                build_production_candidate(
                    os.path.realpath(current), source,
                    os.path.join(root, "candidate"),
                    os.path.join(root, "identity.json"), *self._provenance_args()
                )

    def test_builder_rejects_conflicting_served_asset_copies(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-conflict-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source_repo(source)
            current, _ = self._current_served_root(
                root,
                extra_files=(
                    ("assets/coverage_progress.js", "old-a"),
                    ("reports/coverage_progress.js", "old-b"),
                ),
            )
            with self.assertRaisesRegex(ValueError, "conflicting Served asset copies"):
                build_production_candidate(
                    current, source, os.path.join(root, "candidate"),
                    os.path.join(root, "identity.json"), *self._provenance_args(),
                    **self._expected_binding_kwargs(current)
                )

    def test_builder_requires_complete_current_release_manifest(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-manifest-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source_repo(source)
            current, _ = self._current_served_root(root)
            os.remove(os.path.join(os.path.realpath(current), "release_manifest.json"))
            with self.assertRaisesRegex(ValueError, "release_manifest.json"):
                build_production_candidate(
                    current, source, os.path.join(root, "candidate"),
                    os.path.join(root, "identity.json"), *self._provenance_args(),
                    **self._expected_binding_kwargs(current)
                )

    def test_builder_requires_external_current_binding(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-binding-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source_repo(source)
            current, _ = self._current_served_root(root)
            with self.assertRaisesRegex(ValueError, "expected Served Root binding"):
                build_production_candidate(
                    current, source, os.path.join(root, "candidate"),
                    os.path.join(root, "identity.json"), *self._provenance_args()
                )

    def test_builder_can_resolve_authoritative_publish_root_and_expected_binding(self):
        with tempfile.TemporaryDirectory(prefix="production-candidate-publish-root-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source_repo(source)
            current, _ = self._current_served_root(root)
            publish_root = os.path.dirname(current)
            binding = current_served_root_binding(publish_root)
            result = build_production_candidate(
                "", source, os.path.join(root, "candidate"),
                os.path.join(root, "identity.json"), *self._provenance_args(),
                expected_previous_release_sha=binding["previous_release_commit_sha"],
                expected_served_root_tree_sha256=binding["served_root_tree_sha256"],
                expected_current_identity_sha256=binding["served_root_identity_sha256"],
                publish_root=publish_root,
            )
            self.assertEqual(
                result["served_root_binding"]["realpath"],
                binding["realpath"],
            )


if __name__ == "__main__":
    unittest.main()
