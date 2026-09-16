import importlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from app.candidate_artifact import (
    CandidateArtifactManifest, PRODUCTION_PROJECT_NAME,
    PRODUCTION_RELEASE_ARTIFACT_ROLE, RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
    verify_offline_operator_trust,
)
from app.release_identity import (
    DEFAULT_RELEASE_ASSET_RELATIVE_PATHS, generate_release_identity,
    save_release_manifest,
)
from app.release_publication import (
    ImmutableReleasePublisher, validate_release_manifest,
)
from scripts.release.build_flat_production_candidate_artifact import (
    build_flat_production_candidate,
)
from scripts.release.prepare_legacy_flat_adoption import flat_source_binding


flat_builder_module = importlib.import_module(
    "scripts.release.build_flat_production_candidate_artifact"
)


LEGACY_SHA = "e" * 40
REPOSITORY = "Chary-yu/fos_coverage_tool"
HOST = "vfoswind"
LEGACY_ASSETS = tuple(DEFAULT_RELEASE_ASSET_RELATIVE_PATHS[:6])


class FlatProductionCandidateOperatorTest(unittest.TestCase):
    @staticmethod
    def _copy_file(source, target):
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        shutil.copyfile(source, target)

    def _source_repo(self, root):
        for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
            self._copy_file(
                os.path.join(os.getcwd(), *relative.split("/")),
                os.path.join(root, *relative.split("/")),
            )
        self._copy_file(
            os.path.join(os.getcwd(), "enhance_coverage.py"),
            os.path.join(root, "enhance_coverage.py"),
        )
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
            ["git", "config", "user.email", "flat-test@example.invalid"],
            cwd=root,
        )
        subprocess.check_call(
            ["git", "config", "user.name", "Flat Candidate Test"], cwd=root
        )
        subprocess.check_call(["git", "add", "."], cwd=root)
        subprocess.check_call(["git", "commit", "-q", "-m", "target"], cwd=root)
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root
        ).decode("ascii").strip()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=root
        ).decode("ascii").strip()
        return commit, tree

    def _flat_root(self, root, fixture=False):
        os.makedirs(root)
        report_text = (
            "<html><head><title>Legacy</title></head>"
            "<body>{}</body></html>\n"
        ).format("Coverage Candidate" if fixture else "FOS legacy report")
        with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as stream:
            stream.write(report_text)
        for relative in LEGACY_ASSETS:
            with open(os.path.join(root, os.path.basename(relative)), "wb") as stream:
                stream.write(("legacy-asset:" + relative).encode("utf-8"))

    def _legacy_identity(self, root, flat_root):
        identity_source = os.path.join(root, "identity-source")
        asset_files = []
        for relative in LEGACY_ASSETS:
            source = os.path.join(flat_root, os.path.basename(relative))
            target = os.path.join(identity_source, *relative.split("/"))
            self._copy_file(source, target)
            asset_files.append(target)
        identity = generate_release_identity(
            identity_source, commit_sha=LEGACY_SHA,
            asset_files=asset_files, build_provenance="legacy-adoption",
        )
        identity_path = os.path.join(root, "legacy-release-identity.json")
        save_release_manifest(identity_path, identity)
        return identity_path, identity

    def _fixture(self, fixture=False):
        root = tempfile.TemporaryDirectory(prefix="flat-production-candidate-")
        self.addCleanup(root.cleanup)
        flat = os.path.join(root.name, "flat")
        self._flat_root(flat, fixture=fixture)
        identity_path, legacy_identity = self._legacy_identity(root.name, flat)
        binding = flat_source_binding(flat, identity_path, LEGACY_SHA)
        source = os.path.join(root.name, "source")
        os.makedirs(source)
        source_sha, source_tree = self._source_repo(source)
        bundle = os.path.join(root.name, "source.bundle")
        with open(bundle, "wb") as stream:
            stream.write(b"offline source bundle for focused fixture\n")
        candidate = os.path.join(root.name, "candidate")
        identity_output = os.path.join(root.name, "candidate-release-identity.json")
        evidence = os.path.join(root.name, "offline-operator-evidence.json")
        return {
            "root": root,
            "flat": flat,
            "identity_path": identity_path,
            "legacy_identity": legacy_identity,
            "binding": binding,
            "source": source,
            "source_sha": source_sha,
            "source_tree": source_tree,
            "bundle": bundle,
            "candidate": candidate,
            "identity_output": identity_output,
            "evidence": evidence,
            "session": "flat-focused-session-1",
        }

    @staticmethod
    def _build(fixture, expected_binding=None):
        return build_flat_production_candidate(
            fixture["flat"], fixture["identity_path"], fixture["source"],
            fixture["candidate"], fixture["identity_output"],
            expected_previous_release_sha=LEGACY_SHA,
            expected_source_binding=(
                fixture["binding"] if expected_binding is None else expected_binding
            ),
            release_trust_mode=RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
            offline_operator_evidence_output=fixture["evidence"],
            offline_operator_source_bundle=fixture["bundle"],
            offline_operator_repository=REPOSITORY,
            production_host=HOST,
            production_baseline_sha=LEGACY_SHA,
            validation_session_id=fixture["session"],
            flat_baseline_session_id="legacy-flat-baseline-1",
        )

    def test_flat_builder_is_first_migration_path_and_final_publication_accepts_offline_trust(self):
        fixture = self._fixture()
        result = self._build(fixture)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["artifact_role"], PRODUCTION_RELEASE_ARTIFACT_ROLE)
        self.assertTrue(result["production_publishable"])
        self.assertEqual(result["project_name"], PRODUCTION_PROJECT_NAME)
        self.assertEqual(result["release_trust_mode"], "offline_operator")
        self.assertFalse(os.path.exists(os.path.join(fixture["root"].name, "publish", "CURRENT")))

        with open(fixture["identity_output"], encoding="utf-8") as stream:
            identity = json.load(stream)
        with open(result["candidate_artifact_manifest"], encoding="utf-8") as stream:
            manifest = json.load(stream)
        with open(fixture["evidence"], encoding="utf-8") as stream:
            evidence = json.load(stream)
        self.assertEqual(manifest["source_commit_sha"], fixture["source_sha"])
        self.assertEqual(manifest["source_tree_sha"], fixture["source_tree"])
        self.assertEqual(manifest["artifact_role"], "production_candidate")
        self.assertTrue(manifest["production_publishable"])
        self.assertEqual(manifest["project_name"], "FOS_V6R2")
        self.assertEqual(
            manifest["source_provenance"]["flat_source_binding"], fixture["binding"]
        )
        self.assertEqual(evidence["release_trust_mode"], "offline_operator")
        self.assertEqual(evidence["protected_builder"], "SKIPPED_BY_OPERATOR")

        publish_root = os.path.join(fixture["root"].name, "publish")
        publisher = ImmutableReleasePublisher(publish_root)
        prepared = publisher.prepare(
            fixture["candidate"], identity, "offline-publication-session",
            candidate_sha=fixture["source_sha"],
            candidate_artifact_manifest=result["candidate_artifact_manifest"],
            source_repo_root=fixture["source"],
            release_trust_mode=RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
            offline_operator_evidence=fixture["evidence"],
            offline_operator_source_bundle=fixture["bundle"],
            offline_operator_repository=REPOSITORY,
            production_host=HOST,
            production_baseline_sha=LEGACY_SHA,
            validation_session_id=fixture["session"],
        )
        self.assertEqual(
            validate_release_manifest(
                publisher.release_path("offline-publication-session"), prepared,
                "offline-publication-session",
            )["status"],
            "PASSED",
        )
        self.assertEqual(
            publisher.switch_current("offline-publication-session")["status"],
            "PASSED",
        )
        self.assertEqual(publisher.validate_current()["status"], "PASSED")

    def test_wrong_baseline_and_full_source_binding_are_rejected_before_build(self):
        fixture = self._fixture()
        with self.assertRaisesRegex(ValueError, "does not match expected commit SHA"):
            build_flat_production_candidate(
                fixture["flat"], fixture["identity_path"], fixture["source"],
                fixture["candidate"], fixture["identity_output"],
                expected_previous_release_sha="f" * 40,
            )
        wrong = dict(fixture["binding"])
        wrong["source_files"] = list(wrong["source_files"])
        wrong["source_files"][0] = dict(wrong["source_files"][0])
        wrong["source_files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Flat source binding"):
            self._build(fixture, expected_binding=wrong)
        self.assertFalse(os.path.exists(fixture["candidate"]))

    def test_flat_root_mutation_after_adoption_is_caught_by_final_scan(self):
        fixture = self._fixture()
        original = flat_builder_module.verify_flat_source_binding

        def mutate_then_verify(*args, **kwargs):
            with open(os.path.join(fixture["flat"], "index.html"), "ab") as stream:
                stream.write(b"mutation after adoption\n")
            return original(*args, **kwargs)

        with mock.patch.object(
                flat_builder_module, "verify_flat_source_binding",
                side_effect=mutate_then_verify):
            with self.assertRaisesRegex(ValueError, "source binding changed"):
                self._build(fixture)
        if os.path.isdir(fixture["candidate"]):
            shutil.rmtree(fixture["candidate"])

    def test_validation_fixture_and_partial_candidate_are_rejected(self):
        fixture = self._fixture(fixture=True)
        with self.assertRaisesRegex(ValueError, "validation fixture content"):
            self._build(fixture)
        if os.path.isdir(fixture["candidate"]):
            shutil.rmtree(fixture["candidate"])

        partial = os.path.join(fixture["root"].name, "partial")
        for directory in ("reports", "assets", "registry"):
            os.makedirs(os.path.join(partial, directory))
        with self.assertRaisesRegex(ValueError, "manifest is missing"):
            CandidateArtifactManifest.verify(
                partial, {"commit_sha": "a" * 40, "build_id": "partial"},
                expected_artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
                expected_project_name=PRODUCTION_PROJECT_NAME,
                require_production_publishable=True,
            )

    def test_source_commit_tree_and_offline_evidence_mismatches_fail_closed(self):
        fixture = self._fixture()
        result = self._build(fixture)
        with open(fixture["identity_output"], encoding="utf-8") as stream:
            identity = json.load(stream)
        with self.assertRaisesRegex(ValueError, "candidate_sha"):
            CandidateArtifactManifest.verify(
                fixture["candidate"], identity, candidate_sha="a" * 40,
                manifest_path=result["candidate_artifact_manifest"],
                require_trusted_provenance=True,
                expected_artifact_role=PRODUCTION_RELEASE_ARTIFACT_ROLE,
                expected_project_name=PRODUCTION_PROJECT_NAME,
                require_production_publishable=True,
            )
        with open(fixture["evidence"], encoding="utf-8") as stream:
            evidence = json.load(stream)
        with open(result["candidate_artifact_manifest"], encoding="utf-8") as stream:
            manifest = json.load(stream)
        with self.assertRaisesRegex(ValueError, "tree_sha"):
            verify_offline_operator_trust(
                fixture["candidate"], identity, manifest, fixture["source"],
                evidence=dict(evidence, tree_sha="f" * 40),
                source_bundle_path=fixture["bundle"],
            )


if __name__ == "__main__":
    unittest.main()
