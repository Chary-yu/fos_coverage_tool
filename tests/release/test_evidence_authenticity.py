import os
import sys
import tempfile
import unittest
import json
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import (
    ASSET_MANIFEST_VERSION,
    DEFAULT_RELEASE_ASSET_RELATIVE_PATHS,
    build_asset_manifest, compute_asset_hash, get_current_release_identity,
    generate_release_identity, save_release_manifest,
)
from scripts.release.build_release import main as build_release_main
from scripts.upgrade.evidence_manifest import EvidenceManifestV2, ProductionEvidenceManifest


class TestEvidenceAuthenticity(unittest.TestCase):
    def _prepare_release_tree(self, root, initialize_git=False):
        for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
            path = os.path.join(root, *relative.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("asset:" + relative)
        backend = os.path.join(root, "app", "services", "backend.py")
        os.makedirs(os.path.dirname(backend), exist_ok=True)
        with open(backend, "w", encoding="utf-8") as stream:
            stream.write("def backend_value():\n    return 1\n")
        if not initialize_git:
            return ""
        subprocess.check_call(["git", "init", "-q", root])
        subprocess.check_call(["git", "add", "."], cwd=root)
        subprocess.check_call([
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "initial",
        ], cwd=root)
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root
        ).decode("utf-8").strip()

    def test_asset_manifest_binds_path_size_and_missing_assets_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            first = os.path.join(root, "static", "first.js")
            second = os.path.join(root, "static", "second.js")
            os.makedirs(os.path.dirname(first), exist_ok=True)
            for path in (first, second):
                with open(path, "wb") as stream:
                    stream.write(b"same-bytes")

            manifest = build_asset_manifest([second, first], repo_root=root)
            self.assertEqual(
                [item["path"] for item in manifest],
                ["static/first.js", "static/second.js"],
            )
            self.assertEqual(manifest[0]["size"], len(b"same-bytes"))
            self.assertEqual(len(manifest[0]["sha256"]), 64)
            self.assertEqual(
                compute_asset_hash([first, second], repo_root=root),
                compute_asset_hash([second, first], repo_root=root),
            )
            os.remove(second)
            with self.assertRaisesRegex(RuntimeError, "required release asset missing"):
                build_asset_manifest([first, second], repo_root=root)

    def test_asset_manifest_rejects_paths_outside_repository_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            asset = os.path.join(outside, "not-in-repo.js")
            with open(asset, "w", encoding="utf-8") as stream:
                stream.write("outside")
            with self.assertRaisesRegex(RuntimeError, "outside repository root"):
                build_asset_manifest([asset], repo_root=root)

    def test_default_release_assets_include_progress_template_pair(self):
        required = {
            "coverage_progress.html",
            "web/templates/coverage_progress.html",
        }
        self.assertTrue(required.issubset(set(DEFAULT_RELEASE_ASSET_RELATIVE_PATHS)))

        with tempfile.TemporaryDirectory() as root:
            for relative, contents in (
                ("coverage_progress.html", "compatibility-v1"),
                ("web/templates/coverage_progress.html", "canonical-v1"),
            ):
                path = os.path.join(root, *relative.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(contents)

            asset_files = [
                os.path.join(root, *relative.split("/"))
                for relative in (
                    "coverage_progress.html",
                    "web/templates/coverage_progress.html",
                )
            ]
            original = generate_release_identity(
                root, commit_sha="a" * 40, asset_files=asset_files
            )
            with open(
                os.path.join(root, "web", "templates", "coverage_progress.html"),
                "a",
                encoding="utf-8",
            ) as stream:
                stream.write("-tampered")
            changed = generate_release_identity(
                root, commit_sha="a" * 40, asset_files=asset_files
            )
            self.assertNotEqual(original["asset_hash"], changed["asset_hash"])
            self.assertEqual(original["asset_manifest_version"], ASSET_MANIFEST_VERSION)
            self.assertEqual(original["asset_count"], 2)
            self.assertEqual(original["asset_manifest_hash"], original["asset_hash"])

    def test_missing_runtime_release_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError):
                get_current_release_identity(root)

    def test_no_git_release_artifact_uses_manifest_sha_and_verifies_assets(self):
        with tempfile.TemporaryDirectory() as root:
            asset = os.path.join(root, "web", "assets", "js", "coverage_progress.js")
            os.makedirs(os.path.dirname(asset))
            with open(asset, "w", encoding="utf-8") as stream:
                stream.write("asset-v1")
            identity = generate_release_identity(
                root, commit_sha="a" * 40,
                asset_files=[asset], build_provenance="release-build"
            )
            save_release_manifest(os.path.join(root, "release_manifest.json"), identity)
            self.assertEqual(
                get_current_release_identity(root)["commit_sha"], "a" * 40
            )
            with open(asset, "w", encoding="utf-8") as stream:
                stream.write("asset-tampered")
            with self.assertRaisesRegex(RuntimeError, "asset_hash"):
                get_current_release_identity(root)

    def test_no_git_release_artifact_rejects_non_exact_manifest_sha(self):
        with tempfile.TemporaryDirectory() as root:
            identity = generate_release_identity(
                root, commit_sha="a" * 40, asset_files=[],
                build_provenance="release-build",
            )
            identity["commit_sha"] = "not-a-sha"
            save_release_manifest(os.path.join(root, "release_manifest.json"), identity)
            with self.assertRaisesRegex(RuntimeError, "exact commit SHA"):
                get_current_release_identity(root)

    def test_no_git_release_artifact_rejects_all_zero_manifest_sha(self):
        with tempfile.TemporaryDirectory() as root:
            identity = generate_release_identity(
                root, commit_sha="a" * 40, asset_files=[],
                build_provenance="release-build",
            )
            identity["commit_sha"] = "0" * 40
            save_release_manifest(os.path.join(root, "release_manifest.json"), identity)
            with self.assertRaisesRegex(RuntimeError, "concrete commit SHA"):
                get_current_release_identity(root)

    def test_generate_release_identity_rejects_an_explicit_invalid_sha(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "concrete commit SHA"):
                generate_release_identity(
                    root, commit_sha="not-a-sha", asset_files=[]
                )

    def test_build_release_requires_concrete_sha_without_git_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "release_manifest.json")
            with self.assertRaises(SystemExit) as missing:
                build_release_main(["--repo-root", root, "--output", output])
            self.assertEqual(missing.exception.code, 2)
            with self.assertRaises(SystemExit) as zero:
                build_release_main([
                    "--repo-root", root, "--output", output,
                    "--commit-sha", "0" * 40,
                ])
            self.assertEqual(zero.exception.code, 2)

    def test_build_release_uses_git_head_and_rejects_mismatched_sha(self):
        with tempfile.TemporaryDirectory() as root:
            head = self._prepare_release_tree(root, initialize_git=True)
            output = os.path.join(root, "release_manifest.json")

            build_release_main([
                "--repo-root", root, "--output", output,
                "--commit-sha", head,
            ])
            with open(output, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            self.assertEqual(manifest["commit_sha"], head)

            other = "b" * 40 if head.lower() != "b" * 40 else "c" * 40
            with self.assertRaises(SystemExit) as mismatch:
                build_release_main([
                    "--repo-root", root, "--output", output,
                    "--commit-sha", other,
                ])
            self.assertEqual(mismatch.exception.code, 2)

    def test_build_release_rejects_tracked_source_changes_in_git_tree(self):
        with tempfile.TemporaryDirectory() as root:
            head = self._prepare_release_tree(root, initialize_git=True)
            backend = os.path.join(root, "app", "services", "backend.py")
            with open(backend, "a", encoding="utf-8") as stream:
                stream.write("\n# uncommitted source change\n")
            with self.assertRaises(SystemExit) as dirty:
                build_release_main([
                    "--repo-root", root,
                    "--output", os.path.join(root, "release_manifest.json"),
                    "--commit-sha", head,
                ])
            self.assertEqual(dirty.exception.code, 2)

    def test_build_release_rejects_untracked_files_in_git_tree(self):
        with tempfile.TemporaryDirectory() as root:
            head = self._prepare_release_tree(root, initialize_git=True)
            untracked = os.path.join(root, "app", "services", "untracked.py")
            with open(untracked, "w", encoding="utf-8") as stream:
                stream.write("UNTRACKED = True\n")
            with self.assertRaises(SystemExit) as dirty:
                build_release_main([
                    "--repo-root", root,
                    "--output", os.path.join(root, "release_manifest.json"),
                    "--commit-sha", head,
                ])
            self.assertEqual(dirty.exception.code, 2)

    def test_build_release_accepts_external_sha_without_git_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            self._prepare_release_tree(root)
            output = os.path.join(root, "release_manifest.json")
            external_sha = "a" * 40
            build_release_main([
                "--repo-root", root, "--output", output,
                "--commit-sha", external_sha,
            ])
            with open(output, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            self.assertEqual(manifest["commit_sha"], external_sha)

    def test_mock_backup_and_browser_cannot_pass_final_gate(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = ProductionEvidenceManifest(root)
            manifest.record("release_identity", {
                "version": "v", "commit_sha": "fake", "build_id": "b"
            })
            manifest.record("targeted_tests", {"unit": {"status": "PASSED", "revision": "fake"}})
            manifest.record("browser_smoke_suite", {
                "status": "PASSED", "evidence_class": "mock_dom", "revision": "fake"
            })
            manifest.record("data_hash_verification", {
                "verified": True, "evidence_class": "production_database", "revision": "fake"
            })
            manifest.record("schema_migration", {"preflight_safe": True, "status": "PASSED", "revision": "fake"})
            manifest.record("sidecar_audit", {"is_safe": True, "status": "PASSED", "revision": "fake"})
            manifest.record("security_audit", {"is_safe": True, "status": "PASSED", "revision": "fake", "auth_mode": "reverse_proxy"})
            manifest.record("performance_benchmark", {"Tier_A_1k": {"status": "PASSED", "revision": "fake"}})
            manifest.record("backup_evidence", {
                "status": "BACKUP_VERIFIED", "evidence_class": "mock", "revision": "fake",
                "full_sql_gz_sha256": "placeholder"
            })
            passed, unmet = manifest.validate_final_gate()
            self.assertFalse(passed)
            self.assertTrue(any("Browser" in item or "Backup" in item for item in unmet))

    def test_evidence_manifest_v2_binds_record_to_revision_and_artifact_sha(self):
        with tempfile.TemporaryDirectory() as root:
            artifact = os.path.join(root, "evidence.json")
            with open(artifact, "w") as stream:
                stream.write("{}")
            manifest = EvidenceManifestV2(
                root, "gate-a", candidate_revision="abc",
                release_identity={"commit_sha": "abc"},
            )
            record = manifest.record(
                "schema", "db-integration", "PASSED", "unit-test", 0,
                artifact_path=artifact,
                database_runtime_identity={"engine": "sqlite", "database": "test"},
            )
            self.assertEqual(record["candidate_revision"], "abc")
            self.assertTrue(record["artifact_sha256"])
            self.assertEqual(manifest.validate(), (True, []))
            self.assertTrue(manifest.data["manifest_sha256"])

    def test_evidence_manifest_v2_rejects_changed_artifact_and_synthetic_pass(self):
        with tempfile.TemporaryDirectory() as root:
            artifact = os.path.join(root, "evidence.json")
            with open(artifact, "w") as stream:
                stream.write("original")
            manifest = EvidenceManifestV2(root, "gate-a", candidate_revision="abc")
            manifest.record(
                "artifact", "repository_audit", "PASSED", "unit-test", 0,
                artifact_path=artifact,
            )
            with open(artifact, "w") as stream:
                stream.write("changed")
            self.assertFalse(manifest.validate()[0])
            manifest.record(
                "synthetic", "microbenchmark", "PASSED", "unit-test", 0,
                synthetic=True,
            )
            valid, errors = manifest.validate()
            self.assertFalse(valid)
            self.assertTrue(any("synthetic" in item for item in errors))

    def test_evidence_manifest_v2_schema_is_versioned(self):
        path = os.path.join(ROOT, "docs", "contracts", "evidence_manifest_v2.schema.json")
        with open(path, "r", encoding="utf-8") as stream:
            schema = json.load(stream)
        self.assertEqual(schema["properties"]["evidence_schema_version"]["const"], 2)


if __name__ == "__main__":
    unittest.main()
