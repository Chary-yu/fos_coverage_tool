import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import app.release_publication as release_publication
import app.candidate_build_receipt as candidate_build_receipt

from app.candidate_artifact import (
    CandidateArtifactManifest, build_git_source_provenance,
    VALIDATION_FIXTURE_ARTIFACT_ROLE,
    identity_manifest_sha256,
)
from app.candidate_build_receipt import (
    create_candidate_build_receipt, verify_github_artifact_attestation,
)
from app.release_publication import (
    ImmutableReleasePublisher, current_publication_identity,
    normalize_candidate_artifact,
    validate_release_manifest,
)
from scripts.release.publish_release import main as publish_release_main


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
                '<meta name="coverage-project" content="FOS_V6R2">\n'
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
                "project_name": "FOS_V6R2",
            }, stream)

    def _prepare(self, publisher, source, identity, session_id, **kwargs):
        normalize_candidate_artifact(source)
        CandidateArtifactManifest.build(
            source, identity,
            source_provenance=self._bootstrap_fixture_provenance(identity),
        )
        return publisher.prepare_bootstrap(source, identity, session_id, **kwargs)

    @staticmethod
    def _trusted_fixture_provenance(identity):
        return {
            "provenance_class": "trusted-ci-build",
            "provenance_schema_version": 1,
            "source_commit_sha": identity["commit_sha"],
            "source_tree_sha": "d" * 40,
            "worktree_clean": True,
            "build_workflow_identity": "tests.release.test_immutable_release_publication",
            "build_workflow_run_id": "123",
            "build_workflow_run_attempt": "1",
            "build_workflow_sha": "f" * 40,
            "source_manifest_sha256": "1" * 64,
            "build_input_manifest_sha256": "2" * 64,
        }

    @staticmethod
    def _bootstrap_fixture_provenance(identity):
        provenance = ImmutableReleasePublicationTest._trusted_fixture_provenance(
            identity
        )
        provenance["provenance_class"] = "served-root-bootstrap"
        return provenance

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
            publication = current_publication_identity(os.path.join(root, "publish"))
            self.assertEqual(publication["release_validation_session_id"], "candidate-session")
            self.assertEqual(
                publication["candidate_artifact_sha256"],
                manifest["candidate_artifact_manifest"]["artifact_sha256"],
            )
            self.assertEqual(
                publication["served_root_sha256"], manifest["served_root"]["sha256"]
            )
            self.assertEqual(publication["commit_sha"], identity["commit_sha"])

    def test_release_endpoint_reads_validated_identity_without_rescanning_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-cache-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source(source)
            publish_root = os.path.join(root, "publish")
            publisher = ImmutableReleasePublisher(publish_root)
            self._prepare(
                publisher, source,
                {"commit_sha": "7" * 40, "build_id": "candidate-7"},
                "cached-session",
            )
            publisher.switch_current("cached-session")
            self.assertTrue(os.path.isfile(os.path.join(
                publisher.release_path("cached-session"),
                release_publication.VALIDATED_PUBLICATION_IDENTITY_NAME,
            )))
            release_publication._PUBLICATION_IDENTITY_CACHE.clear()
            with mock.patch.object(
                    release_publication, "validate_release_manifest",
                    side_effect=AssertionError("unexpected full release scan")):
                identity = current_publication_identity(publish_root)
            self.assertEqual(identity["commit_sha"], "7" * 40)

    def test_runtime_payload_drift_invalidates_publication_identity_cache(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-drift-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source(source)
            publish_root = os.path.join(root, "publish")
            publisher = ImmutableReleasePublisher(publish_root)
            self._prepare(
                publisher, source,
                {"commit_sha": "0a" * 20, "build_id": "candidate-drift"},
                "drift-session",
            )
            publisher.switch_current("drift-session")
            self.assertTrue(current_publication_identity(publish_root))
            with open(os.path.join(
                    publisher.release_path("drift-session"),
                    "reports", "index.html"), "a", encoding="utf-8") as stream:
                stream.write("<!-- drift -->\n")
            self.assertEqual(current_publication_identity(publish_root), {})

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
            with open(os.path.join(source, "reports", "index.html"), encoding="utf-8") as stream:
                source_html = stream.read()
            self.assertEqual(html, source_html)
            self.assertEqual(manifest["report_modes"], ["LEGACY_STATIC"])

    def test_publisher_does_not_normalize_after_candidate_manifest(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-no-mutation-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source(source, mode="LEGACY_STATIC", include_mode=False)
            identity = {"commit_sha": "9" * 40, "build_id": "candidate-9"}
            CandidateArtifactManifest.build(
                source, identity,
                source_provenance=self._bootstrap_fixture_provenance(identity),
            )
            source_html_path = os.path.join(source, "reports", "index.html")
            with open(source_html_path, "rb") as stream:
                before = stream.read()
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "explicit report mode"):
                publisher.prepare_bootstrap(source, identity, "no-normalize-session")
            with open(source_html_path, "rb") as stream:
                self.assertEqual(stream.read(), before)

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
            CandidateArtifactManifest.build(
                source, identity,
                source_provenance=self._bootstrap_fixture_provenance(identity),
            )
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
                publisher.prepare_bootstrap(source, identity, "nested-symlink-session")

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
            CandidateArtifactManifest.build(
                source, identity,
                source_provenance=self._bootstrap_fixture_provenance(identity),
            )
            with open(os.path.join(source, "assets", "coverage.js"), "a") as stream:
                stream.write("\ntampered\n")
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "candidate_root"):
                publisher.prepare_bootstrap(source, identity, "tampered-session")

    def test_candidate_manifest_requires_source_provenance(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-provenance-") as root:
            source = os.path.join(root, "source")
            os.makedirs(source)
            self._source(source)
            with self.assertRaisesRegex(ValueError, "source provenance"):
                CandidateArtifactManifest.build(
                    source,
                    {"commit_sha": "2" * 40, "build_id": "candidate-2"},
                )

    def test_publisher_rejects_test_fixture_provenance(self):
        with tempfile.TemporaryDirectory(prefix="release-publication-untrusted-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            identity = {"commit_sha": "3" * 40, "build_id": "candidate-3"}
            provenance = self._trusted_fixture_provenance(identity)
            provenance["provenance_class"] = "test-fixture"
            CandidateArtifactManifest.build(
                source, identity, source_provenance=provenance
            )
            with self.assertRaisesRegex(ValueError, "trusted provenance class"):
                ImmutableReleasePublisher(os.path.join(root, "publish")).prepare(
                    source, identity, "untrusted-session"
                )

    def test_validation_fixture_can_never_enter_immutable_publication(self):
        with tempfile.TemporaryDirectory(prefix="release-validation-fixture-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            identity = {"commit_sha": "4" * 40, "build_id": "validation-fixture"}
            CandidateArtifactManifest.build(
                source, identity,
                source_provenance=self._bootstrap_fixture_provenance(identity),
                artifact_role=VALIDATION_FIXTURE_ARTIFACT_ROLE,
                production_publishable=False,
                project_name="Coverage Candidate",
            )
            with self.assertRaisesRegex(ValueError, "publication role"):
                ImmutableReleasePublisher(os.path.join(root, "publish")).prepare_bootstrap(
                    source, identity, "validation-fixture-session"
                )

    def test_trusted_ci_publish_requires_source_checkout(self):
        with tempfile.TemporaryDirectory(prefix="release-missing-source-root-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            identity = {"commit_sha": "5" * 40, "build_id": "candidate-5"}
            CandidateArtifactManifest.build(
                source, identity,
                source_provenance=self._trusted_fixture_provenance(identity),
            )
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "source_repo_root is required"):
                publisher.prepare(source, identity, "missing-source-root-session")

    def test_bootstrap_provenance_cannot_use_generic_publisher(self):
        with tempfile.TemporaryDirectory(prefix="release-bootstrap-boundary-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            identity = {"commit_sha": "6" * 40, "build_id": "candidate-6"}
            CandidateArtifactManifest.build(
                source, identity,
                source_provenance=self._bootstrap_fixture_provenance(identity),
            )
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "trusted-ci-build"):
                publisher.prepare(
                    source, identity, "bootstrap-generic-session",
                    source_repo_root=root,
                )

    def test_bootstrap_provenance_cannot_use_normal_publish_cli(self):
        with tempfile.TemporaryDirectory(prefix="release-bootstrap-cli-boundary-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            identity = {"commit_sha": "8" * 40, "build_id": "candidate-8"}
            CandidateArtifactManifest.build(
                source, identity,
                source_provenance=self._bootstrap_fixture_provenance(identity),
            )
            identity_path = os.path.join(root, "identity.json")
            with open(identity_path, "w", encoding="utf-8") as stream:
                json.dump(identity, stream)
            with self.assertRaisesRegex(ValueError, "trusted-ci-build"):
                publish_release_main([
                    "--publish-root", os.path.join(root, "publish"),
                    "--source-root", source,
                    "--release-identity", identity_path,
                    "--session-id", "bootstrap-cli-session",
                    "--source-repo-root", root,
                    "--trusted-build-workflow-identity", "trusted-ci",
                    "--trusted-build-workflow-sha", "f" * 40,
                    "--candidate-build-receipt", os.path.join(
                        source, "candidate_build_receipt.json"
                    ),
                    "--candidate-build-attestation-bundle", os.path.join(
                        root, "candidate-build-attestation.bundle.json"
                    ),
                    "--candidate-build-attestation-repository",
                    "Chary-yu/fos_coverage_tool",
                    "--candidate-build-attestation-workflow",
                    "Chary-yu/fos_coverage_tool/.github/workflows/ci.yml",
                ])

    def test_git_provenance_uses_source_tree_manifest_and_attestation(self):
        with tempfile.TemporaryDirectory(prefix="release-source-attestation-") as root:
            source_repo = os.path.join(root, "source-repo")
            candidate = os.path.join(root, "candidate")
            os.makedirs(source_repo)
            os.makedirs(candidate)
            subprocess.check_call(["git", "init", "-q"], cwd=source_repo)
            subprocess.check_call(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=source_repo,
            )
            subprocess.check_call(
                ["git", "config", "user.name", "Release Test"],
                cwd=source_repo,
            )
            with open(os.path.join(source_repo, "source.txt"), "w", encoding="utf-8") as stream:
                stream.write("source\n")
            subprocess.check_call(["git", "add", "source.txt"], cwd=source_repo)
            subprocess.check_call(["git", "commit", "-q", "-m", "source"], cwd=source_repo)
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source_repo
            ).decode("ascii").strip()
            identity = {"commit_sha": commit, "build_id": "candidate-attested"}
            self._source(candidate)
            provenance = build_git_source_provenance(
                source_repo, identity, "trusted-ci",
                build_workflow_run_id="123",
                build_workflow_run_attempt="1",
                build_workflow_sha="f" * 40,
            )
            self.assertEqual(provenance["provenance_class"], "trusted-ci-build")
            self.assertNotEqual(
                provenance["source_manifest_sha256"],
                # This was the old, incorrect identity-only value.
                identity_manifest_sha256(identity),
            )
            manifest = CandidateArtifactManifest.build(
                candidate, identity, source_provenance=provenance
            )
            self.assertEqual(manifest["build_workflow_run_id"], "123")
            self.assertEqual(manifest["build_workflow_run_attempt"], "1")
            self.assertEqual(manifest["build_workflow_sha"], "f" * 40)
            self.assertTrue(os.path.isfile(os.path.join(
                candidate, "candidate_build_attestation.json"
            )))
            bundle = os.path.join(root, "candidate-build-attestation.bundle.json")
            with open(bundle, "w", encoding="utf-8") as stream:
                stream.write("external GitHub attestation bundle\n")
            receipt_path = os.path.join(candidate, "candidate_build_receipt.json")
            receipt = create_candidate_build_receipt(
                candidate, identity,
                output_path=receipt_path,
                attestation_bundle_path=bundle,
                signing_key="test-protected-build-key-1234567890",
            )
            self.assertEqual(
                receipt["payload"]["candidate_artifact_sha256"],
                manifest["artifact_sha256"],
            )
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "workflow SHA"):
                publisher.prepare(
                    candidate, identity, "workflow-mismatch-session",
                    source_repo_root=source_repo,
                    trusted_build_workflow_identity="trusted-ci",
                    trusted_build_workflow_sha="e" * 40,
                )
            previous_key = os.environ.get("COVERAGE_BUILD_PROVENANCE_HMAC_KEY")
            os.environ["COVERAGE_BUILD_PROVENANCE_HMAC_KEY"] = \
                "test-protected-build-key-1234567890"
            try:
                with mock.patch(
                        "app.candidate_build_receipt.verify_github_artifact_attestation"):
                    prepared = publisher.prepare(
                        candidate, identity, "attested-session", source_repo_root=source_repo,
                        trusted_build_workflow_identity="trusted-ci",
                        trusted_build_workflow_sha="f" * 40,
                        candidate_build_receipt=receipt_path,
                        candidate_build_attestation_bundle=bundle,
                        candidate_build_attestation_repository="Chary-yu/fos_coverage_tool",
                        candidate_build_attestation_workflow=(
                            "Chary-yu/fos_coverage_tool/.github/workflows/ci.yml"
                        ),
                    )
            finally:
                if previous_key is None:
                    os.environ.pop("COVERAGE_BUILD_PROVENANCE_HMAC_KEY", None)
                else:
                    os.environ["COVERAGE_BUILD_PROVENANCE_HMAC_KEY"] = previous_key
            self.assertEqual(
                prepared["release_validation_session_id"], "attested-session"
            )

    def test_trusted_ci_publish_requires_protected_receipt_and_bundle(self):
        with tempfile.TemporaryDirectory(prefix="release-receipt-required-") as root:
            source_repo = os.path.join(root, "source-repo")
            candidate = os.path.join(root, "candidate")
            os.makedirs(source_repo)
            os.makedirs(candidate)
            subprocess.check_call(["git", "init", "-q"], cwd=source_repo)
            subprocess.check_call(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=source_repo,
            )
            subprocess.check_call(
                ["git", "config", "user.name", "Release Test"],
                cwd=source_repo,
            )
            with open(os.path.join(source_repo, "source.txt"), "w") as stream:
                stream.write("source\n")
            subprocess.check_call(["git", "add", "source.txt"], cwd=source_repo)
            subprocess.check_call(["git", "commit", "-q", "-m", "source"], cwd=source_repo)
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source_repo
            ).decode("ascii").strip()
            identity = {"commit_sha": commit, "build_id": "candidate-receipt"}
            self._source(candidate)
            provenance = build_git_source_provenance(
                source_repo, identity, "trusted-ci",
                build_workflow_run_id="456",
                build_workflow_run_attempt="1",
                build_workflow_sha="f" * 40,
            )
            CandidateArtifactManifest.build(
                candidate, identity, source_provenance=provenance
            )
            publisher = ImmutableReleasePublisher(os.path.join(root, "publish"))
            with self.assertRaisesRegex(ValueError, "attestation_bundle"):
                publisher.prepare(
                    candidate, identity, "receipt-missing",
                    source_repo_root=source_repo,
                    trusted_build_workflow_identity="trusted-ci",
                    trusted_build_workflow_sha="f" * 40,
                )

    def test_github_attestation_wrapper_enforces_subject_digest_and_policy(self):
        with tempfile.TemporaryDirectory(prefix="release-github-attestation-") as root:
            subject = os.path.join(root, "candidate_artifact_manifest.json")
            bundle = os.path.join(root, "bundle.json")
            with open(subject, "w", encoding="utf-8") as stream:
                stream.write("manifest bytes\n")
            with open(bundle, "w", encoding="utf-8") as stream:
                stream.write("bundle bytes\n")
            subject_sha = candidate_build_receipt._sha256(subject)
            output = json.dumps([{
                "verificationResult": {
                    "statement": {
                        "subject": [{"name": "manifest", "digest": {
                            "sha256": subject_sha,
                        }}],
                        "predicate": {
                            "runDetails": {
                                "metadata": {
                                    "invocationId": (
                                        "https://github.com/Chary-yu/fos_coverage_tool/"
                                        "actions/runs/123/attempts/1"
                                    ),
                            },
                        },
                    },
                },
                },
            }]).encode("utf-8")
            with mock.patch.object(
                    candidate_build_receipt.subprocess, "check_output",
                    return_value=output) as check:
                result = verify_github_artifact_attestation(
                    subject, bundle, "Chary-yu/fos_coverage_tool",
                    "Chary-yu/fos_coverage_tool/.github/workflows/ci.yml",
                    "a" * 40, "b" * 40, "123", "1",
                )
            self.assertEqual(result["status"], "PASSED")
            command = check.call_args[0][0]
            self.assertIn("--bundle", command)
            self.assertIn("--signer-workflow", command)
            self.assertIn("--source-digest", command)
            self.assertIn("--signer-digest", command)

            bad_output = json.dumps([{
                "verificationResult": {
                    "statement": {
                        "subject": [{"digest": {"sha256": "0" * 64}}],
                        "predicate": {
                            "runDetails": {
                                "metadata": {
                                    "invocationId": (
                                        "https://github.com/Chary-yu/fos_coverage_tool/"
                                        "actions/runs/123/attempts/1"
                                    ),
                            },
                        },
                    },
                },
                },
            }]).encode("utf-8")
            with mock.patch.object(
                    candidate_build_receipt.subprocess, "check_output",
                    return_value=bad_output):
                with self.assertRaisesRegex(ValueError, "does not contain"):
                    verify_github_artifact_attestation(
                        subject, bundle, "Chary-yu/fos_coverage_tool",
                        "Chary-yu/fos_coverage_tool/.github/workflows/ci.yml",
                        "a" * 40, "b" * 40, "123", "1",
                    )
            wrong_run_output = json.dumps([{
                "verificationResult": {
                    "statement": {
                        "subject": [{"digest": {"sha256": subject_sha}}],
                        "predicate": {
                            "runDetails": {
                                "metadata": {
                                    "invocationId": (
                                        "https://github.com/Chary-yu/fos_coverage_tool/"
                                        "actions/runs/91234/attempts/1"
                                    ),
                                },
                            },
                        },
                    },
                },
            }]).encode("utf-8")
            with mock.patch.object(
                    candidate_build_receipt.subprocess, "check_output",
                    return_value=wrong_run_output):
                with self.assertRaisesRegex(ValueError, "exact Candidate build run ID"):
                    verify_github_artifact_attestation(
                        subject, bundle, "Chary-yu/fos_coverage_tool",
                        "Chary-yu/fos_coverage_tool/.github/workflows/ci.yml",
                        "a" * 40, "b" * 40, "123", "1",
                    )
            wrong_attempt_output = json.dumps([{
                "verificationResult": {
                    "statement": {
                        "subject": [{"digest": {"sha256": subject_sha}}],
                        "predicate": {
                            "runDetails": {
                                "metadata": {
                                    "invocationId": (
                                        "https://github.com/Chary-yu/fos_coverage_tool/"
                                        "actions/runs/123/attempts/2"
                                    ),
                                },
                            },
                        },
                    },
                },
            }]).encode("utf-8")
            with mock.patch.object(
                    candidate_build_receipt.subprocess, "check_output",
                    return_value=wrong_attempt_output):
                with self.assertRaisesRegex(ValueError, "exact Candidate build run ID"):
                    verify_github_artifact_attestation(
                        subject, bundle, "Chary-yu/fos_coverage_tool",
                        "Chary-yu/fos_coverage_tool/.github/workflows/ci.yml",
                        "a" * 40, "b" * 40, "123", "1",
                    )
            try:
                os.symlink(subject, os.path.join(root, "subject-link.json"))
            except OSError:
                self.skipTest("symbolic links are unavailable in this environment")
            with self.assertRaisesRegex(ValueError, "symlink"):
                verify_github_artifact_attestation(
                    os.path.join(root, "subject-link.json"), bundle,
                    "Chary-yu/fos_coverage_tool",
                    "Chary-yu/fos_coverage_tool/.github/workflows/ci.yml",
                    "a" * 40, "b" * 40, "123", "1",
                )

    def test_candidate_build_attestation_tamper_fails_before_publish(self):
        with tempfile.TemporaryDirectory(prefix="release-attestation-tamper-") as root:
            source = os.path.join(root, "source")
            self._source(source)
            identity = {"commit_sha": "4" * 40, "build_id": "candidate-4"}
            CandidateArtifactManifest.build(
                source, identity,
                source_provenance=self._bootstrap_fixture_provenance(identity),
            )
            attestation_path = os.path.join(source, "candidate_build_attestation.json")
            with open(attestation_path, "r", encoding="utf-8") as stream:
                attestation = json.load(stream)
            attestation["build_workflow_run_id"] = "different-run"
            with open(attestation_path, "w", encoding="utf-8") as stream:
                json.dump(attestation, stream, sort_keys=True)
            with self.assertRaisesRegex(ValueError, "attestation"):
                ImmutableReleasePublisher(os.path.join(root, "publish")).prepare_bootstrap(
                    source, identity, "attestation-tamper-session"
                )

    def test_git_source_provenance_binds_head_tree_and_clean_worktree(self):
        with tempfile.TemporaryDirectory(prefix="release-source-provenance-") as root:
            subprocess.check_call(["git", "init", "-q"], cwd=root)
            subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=root)
            subprocess.check_call(["git", "config", "user.name", "Release Test"], cwd=root)
            with open(os.path.join(root, "source.txt"), "w", encoding="utf-8") as stream:
                stream.write("source\n")
            subprocess.check_call(["git", "add", "source.txt"], cwd=root)
            subprocess.check_call(["git", "commit", "-q", "-m", "source"], cwd=root)
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root
            ).decode("ascii").strip()
            provenance = build_git_source_provenance(
                root,
                {"commit_sha": commit, "build_id": "candidate-source"},
                "tests.release.test_immutable_release_publication",
            )
            self.assertEqual(provenance["source_commit_sha"], commit)
            self.assertRegex(provenance["source_tree_sha"], r"^[0-9a-f]{40}$")
            self.assertTrue(provenance["worktree_clean"])


if __name__ == "__main__":
    unittest.main()
