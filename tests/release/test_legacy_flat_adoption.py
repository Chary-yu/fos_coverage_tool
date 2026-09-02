import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from app.candidate_artifact import PRODUCTION_PROJECT_NAME
from app.release_identity import (
    DEFAULT_RELEASE_ASSET_RELATIVE_PATHS, generate_release_identity,
    save_release_manifest,
)
from app.release_publication import (
    ImmutableReleasePublisher, current_served_root_binding,
    validate_production_candidate_content,
)
from scripts.release.bootstrap_previous_release import bootstrap
from scripts.release.build_production_candidate_artifact import (
    build_production_candidate,
)
from scripts.release.prepare_legacy_flat_adoption import (
    LEGACY_STATIC, prepare_legacy_flat_adoption,
)


LEGACY_COMMIT_SHA = "e9fcc837a1ac9847f3966fc8ddb2aed92ca473fc"
LEGACY_ASSET_RELATIVE_PATHS = tuple(
    relative for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS
    if "pending_snapshot" not in relative
)


class LegacyFlatAdoptionTest(unittest.TestCase):
    @staticmethod
    def _write_bytes(path, value):
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "wb") as stream:
            stream.write(value)

    def _legacy_identity(self, root):
        identity_source = os.path.join(root, "identity-source")
        asset_files = []
        for relative in LEGACY_ASSET_RELATIVE_PATHS:
            path = os.path.join(identity_source, *relative.split("/"))
            self._write_bytes(path, ("identity-asset:" + relative).encode("utf-8"))
            asset_files.append(path)
        identity = generate_release_identity(
            identity_source,
            commit_sha=LEGACY_COMMIT_SHA,
            asset_files=asset_files,
        )
        identity_path = os.path.join(root, "e9fcc837-release-identity.json")
        save_release_manifest(identity_path, identity)
        return identity_path, identity

    def _flat_root(self, root):
        html_names = (
            "coverage_progress.html", "coverage_summary.html",
            "coverage_details.html", "coverage_branch.html",
            "coverage_module.html", "coverage_history.html",
            "coverage_index.html",
        )
        html_contents = {}
        for index, name in enumerate(html_names, 1):
            html_contents[name] = (
                "<!doctype html>\r\n"
                "<html><head>\r\n"
                "<meta charset=\"utf-8\">\r\n"
                "<title>Legacy {} – 旧报告</title>\r\n"
                "</head><body>\r\n"
                "<main data-report=\"{}\">Legacy business DOM</main>\r\n"
                "<script src=\"coverage_progress.js\"></script>\r\n"
                "</body></html>\r\n"
            ).format(name, index).encode("utf-8")
        asset_contents = {
            name: ("legacy-asset:" + name).encode("utf-8")
            for name in (
                "coverage_enhance.js", "coverage_enhance.css",
                "coverage_progress.js", "incremental_coverage.js",
                "incremental_developer_tasks.js",
            )
        }
        all_contents = dict(html_contents)
        all_contents.update(asset_contents)
        for name, contents in all_contents.items():
            self._write_bytes(os.path.join(root, name), contents)
        return all_contents

    @staticmethod
    def _target_source(root):
        for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
            source = os.path.join(os.getcwd(), *relative.split("/"))
            target = os.path.join(root, *relative.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
        subprocess.check_call(["git", "init", "-q"], cwd=root)
        subprocess.check_call(
            ["git", "config", "user.email", "legacy-adoption@example.invalid"],
            cwd=root,
        )
        subprocess.check_call(
            ["git", "config", "user.name", "Legacy Adoption Test"], cwd=root
        )
        subprocess.check_call(["git", "add", "."], cwd=root)
        subprocess.check_call(["git", "commit", "-q", "-m", "target"], cwd=root)

    @staticmethod
    def _provenance_args():
        return (
            "github-actions/trusted-production-builder", "123", "1", "f" * 40
        )

    @staticmethod
    def _expected_binding_kwargs(publish_root):
        binding = current_served_root_binding(publish_root)
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

    @staticmethod
    def _registry_for(adopted_root, report_id):
        with open(
                os.path.join(adopted_root, "registry", report_id + ".json"),
                "r", encoding="utf-8") as stream:
            return json.load(stream)

    def test_real_flat_root_adoption_bootstrap_and_next_candidate(self):
        with tempfile.TemporaryDirectory(prefix="legacy-flat-adoption-") as root:
            flat_root = os.path.join(root, "flat")
            os.makedirs(flat_root)
            original = self._flat_root(flat_root)
            for directory in ("reports", "assets", "registry", ".source_cache"):
                self.assertFalse(os.path.exists(os.path.join(flat_root, directory)))
            self.assertFalse(os.path.exists(os.path.join(flat_root, "pending_snapshot.js")))
            for name, source_bytes in original.items():
                if name.endswith((".html", ".htm")):
                    for identity_name in (
                            b"coverage-project", b"coverage-report-mode",
                            b"coverage-report-id", b"coverage-scan-id",
                            b"coverage-repository-name", b"coverage-file-path",
                            b"coverage-asset-identity", b"coverage-sidecar-schema"):
                        self.assertNotIn(identity_name, source_bytes)
            identity_path, identity = self._legacy_identity(root)
            adopted_root = os.path.join(root, "adopted")

            adoption = prepare_legacy_flat_adoption(
                flat_root, adopted_root, identity_path, LEGACY_COMMIT_SHA
            )
            self.assertEqual(adoption["status"], "PASSED")
            self.assertEqual(adoption["commit_sha"], LEGACY_COMMIT_SHA)
            self.assertEqual(adoption["report_count"], 7)
            self.assertEqual(adoption["asset_count"], 5)
            self.assertEqual(identity["asset_count"], 12)
            self.assertTrue(os.path.isfile(adoption["release_identity"]))
            self.assertFalse(os.path.exists(os.path.join(
                adopted_root, "pending_snapshot.js"
            )))
            with open(identity_path, "rb") as stream:
                expected_identity_bytes = stream.read()
            with open(adoption["release_identity"], "rb") as stream:
                self.assertEqual(stream.read(), expected_identity_bytes)
            self.assertFalse(os.path.exists(os.path.join(
                adopted_root, "reports", ".source_cache"
            )))

            report_by_source = {
                item["legacy_source_path"]: item
                for item in adoption["reports"]
            }
            for name, source_bytes in original.items():
                if name.endswith((".html", ".htm")):
                    entry = report_by_source[name]
                    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
                    self.assertEqual(entry["legacy_source_sha256"], source_sha256)
                    report_path = os.path.join(
                        adopted_root, "reports", name
                    )
                    with open(report_path, "rb") as stream:
                        adopted_html = stream.read()
                    marker = (
                        '\n<meta name="coverage-project" content="{}">\n'
                        '<meta name="coverage-report-mode" content="{}">\n'
                        '<meta name="coverage-report-id" content="{}">'
                    ).format(
                        PRODUCTION_PROJECT_NAME, LEGACY_STATIC, entry["report_id"]
                    ).encode("ascii")
                    head_start = source_bytes.index(b"<head>")
                    head_end = head_start + len(b"<head>")
                    self.assertEqual(
                        adopted_html,
                        source_bytes[:head_end] + marker + source_bytes[head_end:],
                    )
                    self.assertNotIn(b"coverage-scan-id", adopted_html)
                    self.assertNotIn(b"coverage-repository-name", adopted_html)
                    self.assertNotIn(b"coverage-file-path", adopted_html)
                    self.assertNotIn(b"coverage-asset-identity", adopted_html)
                    self.assertNotIn(b"coverage-sidecar-schema", adopted_html)
                    registry = self._registry_for(adopted_root, entry["report_id"])
                    self.assertEqual(
                        set(registry), {
                            "project_name", "report_id", "report_mode",
                            "report_root", "legacy_source_path",
                            "legacy_source_sha256",
                        }
                    )
                    self.assertEqual(registry["project_name"], PRODUCTION_PROJECT_NAME)
                    self.assertEqual(registry["report_mode"], LEGACY_STATIC)
                    self.assertEqual(registry["report_root"], "reports")
                    self.assertEqual(registry["legacy_source_path"], name)
                    self.assertEqual(registry["legacy_source_sha256"], source_sha256)
                else:
                    for directory in ("reports", "assets"):
                        with open(
                                os.path.join(adopted_root, directory, name),
                                "rb") as stream:
                            self.assertEqual(stream.read(), source_bytes)

            second_adopted_root = os.path.join(root, "adopted-again")
            second_adoption = prepare_legacy_flat_adoption(
                flat_root, second_adopted_root, identity_path, LEGACY_COMMIT_SHA
            )
            self.assertEqual(adoption["reports"], second_adoption["reports"])

            publish_root = os.path.join(root, "publish")
            bootstrap(
                adopted_root, publish_root, identity_path,
                "previous-e9fcc837",
                served_identity_path=os.path.join(
                    adopted_root, "release_identity.json"
                ),
                switch=True,
            )
            publisher = ImmutableReleasePublisher(publish_root)
            self.assertEqual(publisher.validate_current()["status"], "PASSED")
            current_root = os.path.realpath(os.path.join(publish_root, "CURRENT"))
            self.assertFalse(os.path.exists(os.path.join(
                current_root, "pending_snapshot.js"
            )))
            for name, source_bytes in original.items():
                if not name.endswith((".html", ".htm")):
                    with open(
                            os.path.join(current_root, "reports", name),
                            "rb") as stream:
                        self.assertEqual(stream.read(), source_bytes)

            target_source = os.path.join(root, "target-source")
            os.makedirs(target_source)
            self._target_source(target_source)
            candidate_root = os.path.join(root, "production-candidate")
            target_identity_path = os.path.join(root, "target-identity.json")
            result = build_production_candidate(
                "", target_source, candidate_root, target_identity_path,
                *self._provenance_args(),
                publish_root=publish_root,
                **self._expected_binding_kwargs(publish_root)
            )
            self.assertEqual(result["status"], "PASSED")
            self.assertEqual(
                validate_production_candidate_content(
                    candidate_root, PRODUCTION_PROJECT_NAME
                )["status"],
                "PASSED",
            )
            self.assertFalse(os.path.exists(os.path.join(
                candidate_root, "reports", ".source_cache"
            )))
            for name, source_bytes in original.items():
                if name.endswith((".html", ".htm")):
                    with open(
                            os.path.join(candidate_root, "reports", name),
                            "rb") as stream:
                        candidate_html = stream.read()
                    with open(
                            os.path.join(adopted_root, "reports", name),
                            "rb") as stream:
                        self.assertEqual(candidate_html, stream.read())
                    entry = report_by_source[name]
                    registry = self._registry_for(candidate_root, entry["report_id"])
                    self.assertEqual(registry["project_name"], PRODUCTION_PROJECT_NAME)
                    self.assertEqual(registry["report_mode"], LEGACY_STATIC)
                    for forbidden in (
                            "scan_id", "repository_name", "file_path",
                            "sidecar_schema", "asset_identity"):
                        self.assertNotIn(forbidden, registry)
            for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
                self.assertTrue(os.path.isfile(os.path.join(
                    candidate_root, *relative.split("/")
                )))

    def test_flat_adoption_rejects_nested_or_preannotated_input(self):
        with tempfile.TemporaryDirectory(prefix="legacy-flat-reject-") as root:
            identity_path, _ = self._legacy_identity(root)
            nested = os.path.join(root, "nested")
            os.makedirs(nested)
            self._flat_root(nested)
            os.makedirs(os.path.join(nested, "unexpected"))
            with self.assertRaisesRegex(ValueError, "only regular root-level files"):
                prepare_legacy_flat_adoption(
                    nested, os.path.join(root, "out"), identity_path,
                    LEGACY_COMMIT_SHA,
                )

            annotated = os.path.join(root, "annotated")
            os.makedirs(annotated)
            self._flat_root(annotated)
            with open(os.path.join(annotated, "coverage_index.html"), "ab") as stream:
                stream.write(
                    b'<meta name="coverage-project" content="FOS_V6R2">'
                )
            with self.assertRaisesRegex(ValueError, "identity metadata"):
                prepare_legacy_flat_adoption(
                    annotated, os.path.join(root, "out-annotated"), identity_path,
                    LEGACY_COMMIT_SHA,
                )


if __name__ == "__main__":
    unittest.main()
