import hashlib
import importlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from app.candidate_artifact import PRODUCTION_PROJECT_NAME
from app.release_identity import (
    DEFAULT_RELEASE_ASSET_RELATIVE_PATHS, generate_release_identity,
    save_release_manifest,
)
from app.release_publication import (
    ImmutableReleasePublisher, current_served_root_binding,
    validate_production_candidate_content,
)
bootstrap_module = importlib.import_module(
    "scripts.release.bootstrap_previous_release"
)
from scripts.release.bootstrap_previous_release import bootstrap
from scripts.release.build_production_candidate_artifact import (
    build_production_candidate,
)
from scripts.release.prepare_legacy_flat_adoption import (
    LEGACY_STATIC, prepare_legacy_flat_adoption,
)


adoption_module = importlib.import_module(
    "scripts.release.prepare_legacy_flat_adoption"
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

    def _legacy_identity(self, root, flat_root=None):
        identity_source = os.path.join(root, "identity-source")
        asset_files = []
        for relative in LEGACY_ASSET_RELATIVE_PATHS:
            path = os.path.join(identity_source, *relative.split("/"))
            if flat_root:
                source = os.path.join(flat_root, os.path.basename(relative))
                parent = os.path.dirname(path)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent)
                shutil.copyfile(source, path)
            else:
                self._write_bytes(
                    path, ("identity-asset:" + relative).encode("utf-8")
                )
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
            "coverage_progress.html", "index.html",
            "incremental_coverage.html", "incremental_developer_tasks.html",
            "coverage_summary.html", "coverage_details.html",
            "coverage_history.html",
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
        nested_html_contents = {
            "dhc/index.html": (
                "<!doctype html>\r\n<html><head>\r\n"
                "<title>dhc index</title>\r\n</head><body>\r\n"
                "<a href=\"source-file.html\">source</a>\r\n"
                "<script src=\"../coverage_progress.js\"></script>\r\n"
                "</body></html>\r\n"
            ).encode("utf-8"),
            "dhc/source-file.html": (
                "<!doctype html>\r\n<html><head><title>dhc source</title>"
                "</head><body><pre>legacy source</pre></body></html>\r\n"
            ).encode("utf-8"),
            "inc/index.html": (
                "<!doctype html>\r\n<html><head><title>inc index</title>"
                "</head><body><a href=\"../dhc/source-file.html\">dhc</a>"
                "</body></html>\r\n"
            ).encode("utf-8"),
            "inc/xxx.html": (
                "<!doctype html>\r\n<html><head><title>inc detail</title>"
                "</head><body><code>legacy detail</code></body></html>\r\n"
            ).encode("utf-8"),
            "mpls/index.html": (
                "<!doctype html>\r\n<html><head><title>mpls index</title>"
                "</head><body><a href=\"../inc/xxx.html\">inc</a>"
                "</body></html>\r\n"
            ).encode("utf-8"),
        }
        asset_contents = {
            name: ("legacy-asset:" + name).encode("utf-8")
            for name in (
                "coverage_enhance.js", "coverage_enhance.css",
                "coverage_progress.js", "incremental_coverage.js",
                "incremental_developer_tasks.js",
            )
        }
        asset_contents.update({
            "incremental_coverage.xlsx": b"legacy-xlsx\x00\x01\x02",
            "emerald.png": b"\x89PNG\r\nlegacy-image",
            "legacy-index.dat": b"legacy-index-data",
            "dhc/source.c": b"int dhc_source(void) { return 1; }\n",
            "dhc/coverage-data.txt": b"dhc coverage data\n",
            "inc/xxx.c": b"int inc_source(void) { return 2; }\n",
            "mpls/routes.txt": b"mpls route data\n",
        })
        all_contents = dict(html_contents)
        all_contents.update(nested_html_contents)
        all_contents.update(asset_contents)
        for relative, contents in all_contents.items():
            self._write_bytes(os.path.join(root, *relative.split("/")), contents)
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
            identity_path, identity = self._legacy_identity(root, flat_root)
            adopted_root = os.path.join(root, "adopted")

            adoption = prepare_legacy_flat_adoption(
                flat_root, adopted_root, identity_path, LEGACY_COMMIT_SHA
            )
            self.assertEqual(adoption["status"], "PASSED")
            self.assertEqual(adoption["commit_sha"], LEGACY_COMMIT_SHA)
            self.assertEqual(
                adoption["report_count"],
                len([name for name in original if name.endswith((".html", ".htm"))]),
            )
            self.assertEqual(
                adoption["asset_count"],
                len([name for name in original if not name.endswith((".html", ".htm"))]),
            )
            self.assertEqual(adoption["registry_count"], 7)
            self.assertEqual(identity["asset_count"], 12)
            self.assertTrue(os.path.isfile(adoption["release_identity"]))
            self.assertTrue(os.path.isfile(adoption["adoption_manifest"]))
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

            with open(adoption["adoption_manifest"], "r", encoding="utf-8") as stream:
                adoption_manifest = json.load(stream)
            expected_source_files = [
                {
                    "path": name,
                    "size": len(source_bytes),
                    "sha256": hashlib.sha256(source_bytes).hexdigest(),
                }
                for name, source_bytes in sorted(original.items())
            ]
            self.assertEqual(adoption_manifest["source_files"], expected_source_files)
            self.assertEqual(
                adoption_manifest["source_file_count"], len(expected_source_files)
            )
            self.assertEqual(
                adoption_manifest["source_total_size"],
                sum(item["size"] for item in expected_source_files),
            )
            self.assertEqual(adoption_manifest["source_scan"]["stable"], True)
            self.assertEqual(
                adoption_manifest["source_scan"]["before"],
                adoption_manifest["source_scan"]["after"],
            )
            self.assertEqual(
                adoption_manifest["release_identity_sha256"],
                hashlib.sha256(json.dumps(
                    identity, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                len(adoption_manifest["release_asset_bindings"]),
                identity["asset_count"],
            )

            report_by_source = {
                item["legacy_source_path"]: item
                for item in adoption["reports"]
            }
            source_to_report = {
                item["source_path"]: item
                for item in adoption_manifest["source_to_reports"]
            }
            self.assertEqual(set(source_to_report), set(original))
            for name, source_bytes in original.items():
                self.assertEqual(
                    source_to_report[name]["reports_path"], "reports/" + name
                )
                if name.endswith((".html", ".htm")):
                    entry = report_by_source[name]
                    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
                    self.assertEqual(entry["legacy_source_sha256"], source_sha256)
                    report_path = os.path.join(
                        adopted_root, "reports", name
                    )
                    with open(report_path, "rb") as stream:
                        adopted_html = stream.read()
                    if "/" in name:
                        marker = (
                            '\n<meta name="coverage-project" content="{}">\n'
                            '<meta name="coverage-report-mode" content="{}">'
                        ).format(PRODUCTION_PROJECT_NAME, LEGACY_STATIC).encode("ascii")
                        self.assertEqual(entry["report_id"], "")
                        self.assertEqual(entry["report_scope"], "nested")
                        self.assertFalse(os.path.exists(os.path.join(
                            adopted_root, "registry", "{}.json".format(entry["report_id"])
                        )))
                    else:
                        marker = (
                            '\n<meta name="coverage-project" content="{}">\n'
                            '<meta name="coverage-report-mode" content="{}">\n'
                            '<meta name="coverage-report-id" content="{}">'
                        ).format(
                            PRODUCTION_PROJECT_NAME, LEGACY_STATIC, entry["report_id"]
                        ).encode("ascii")
                        self.assertTrue(entry["report_id"])
                        self.assertEqual(entry["report_scope"], "root")
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
                    modified = {
                        item["source_path"]: item
                        for item in adoption_manifest["modified_html"]
                    }[name]
                    self.assertEqual(modified["before_sha256"], source_sha256)
                    self.assertEqual(
                        modified["after_sha256"], hashlib.sha256(adopted_html).hexdigest()
                    )
                    if "/" not in name:
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
                                os.path.join(adopted_root, directory, *name.split("/")),
                                "rb") as stream:
                            self.assertEqual(stream.read(), source_bytes)
                    self.assertEqual(
                        source_to_report[name]["assets_path"], "assets/" + name
                    )

            second_adopted_root = os.path.join(root, "adopted-again")
            second_adoption = prepare_legacy_flat_adoption(
                flat_root, second_adopted_root, identity_path, LEGACY_COMMIT_SHA
            )
            self.assertEqual(adoption["reports"], second_adoption["reports"])
            with open(adoption["adoption_manifest"], "rb") as stream:
                first_manifest_bytes = stream.read()
            with open(second_adoption["adoption_manifest"], "rb") as stream:
                self.assertEqual(stream.read(), first_manifest_bytes)

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
                            os.path.join(current_root, "reports", *name.split("/")),
                            "rb") as stream:
                        self.assertEqual(stream.read(), source_bytes)
            with open(
                    os.path.join(current_root, "candidate_artifact_manifest.json"),
                    "r", encoding="utf-8") as stream:
                bootstrap_manifest = json.load(stream)
            bootstrap_provenance = bootstrap_manifest["source_provenance"]
            self.assertEqual(
                bootstrap_provenance["legacy_source_tree_sha256"],
                adoption["source_tree_sha256"],
            )
            self.assertEqual(
                bootstrap_provenance["legacy_source_file_count"],
                adoption["source_file_count"],
            )

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
                    self.assertEqual(
                        os.path.exists(os.path.join(
                            candidate_root, "registry", "{}.json".format(entry["report_id"])
                        )), "/" not in name
                    )
                    if "/" not in name:
                        registry = self._registry_for(candidate_root, entry["report_id"])
                        self.assertEqual(registry["project_name"], PRODUCTION_PROJECT_NAME)
                        self.assertEqual(registry["report_mode"], LEGACY_STATIC)
                        for forbidden in (
                                "scan_id", "repository_name", "file_path",
                                "sidecar_schema", "asset_identity"):
                            self.assertNotIn(forbidden, registry)
                    else:
                        self.assertNotIn(b"coverage-report-id", candidate_html)
            with open(
                    os.path.join(candidate_root, "candidate_artifact_manifest.json"),
                    "r", encoding="utf-8") as stream:
                candidate_manifest = json.load(stream)
            self.assertEqual(
                candidate_manifest["source_provenance"]["legacy_source_tree_sha256"],
                adoption["source_tree_sha256"],
            )
            for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
                self.assertTrue(os.path.isfile(os.path.join(
                    candidate_root, *relative.split("/")
                )))

    def test_flat_adoption_allows_nested_reports_but_rejects_unsafe_input(self):
        with tempfile.TemporaryDirectory(prefix="legacy-flat-reject-") as root:
            flat_root = os.path.join(root, "flat")
            os.makedirs(flat_root)
            self._flat_root(flat_root)
            identity_path, _ = self._legacy_identity(root, flat_root)
            allowed = prepare_legacy_flat_adoption(
                flat_root, os.path.join(root, "allowed"), identity_path,
                LEGACY_COMMIT_SHA,
            )
            self.assertEqual(allowed["status"], "PASSED")
            self.assertTrue(os.path.isfile(os.path.join(
                allowed["output_root"], "reports", "dhc", "source-file.html"
            )))

            annotated = os.path.join(root, "annotated")
            os.makedirs(annotated)
            self._flat_root(annotated)
            with open(os.path.join(annotated, "dhc", "index.html"), "ab") as stream:
                stream.write(
                    b'<meta name="coverage-project" content="FOS_V6R2">'
                )
            with self.assertRaisesRegex(ValueError, "identity metadata"):
                prepare_legacy_flat_adoption(
                    annotated, os.path.join(root, "out-annotated"), identity_path,
                    LEGACY_COMMIT_SHA,
                )

            symlinked = os.path.join(root, "symlinked")
            os.makedirs(symlinked)
            self._flat_root(symlinked)
            os.symlink(
                os.path.join(symlinked, "coverage_progress.js"),
                os.path.join(symlinked, "dhc", "linked.js"),
            )
            with self.assertRaisesRegex(ValueError, "symlinks"):
                prepare_legacy_flat_adoption(
                    symlinked, os.path.join(root, "out-symlink"), identity_path,
                    LEGACY_COMMIT_SHA,
                )

            if hasattr(os, "mkfifo"):
                special = os.path.join(root, "special")
                os.makedirs(special)
                self._flat_root(special)
                fifo = os.path.join(special, "dhc", "special.pipe")
                os.mkfifo(fifo)
                with self.assertRaisesRegex(ValueError, "regular files or directories"):
                    prepare_legacy_flat_adoption(
                        special, os.path.join(root, "out-special"), identity_path,
                        LEGACY_COMMIT_SHA,
                    )

            mismatched = os.path.join(root, "mismatched")
            os.makedirs(mismatched)
            self._flat_root(mismatched)
            with open(os.path.join(mismatched, "coverage_progress.js"), "ab") as stream:
                stream.write(b"changed")
            with self.assertRaisesRegex(ValueError, "does not match identity"):
                prepare_legacy_flat_adoption(
                    mismatched, os.path.join(root, "out-mismatched"), identity_path,
                    LEGACY_COMMIT_SHA,
                )

            changed = os.path.join(root, "changed")
            os.makedirs(changed)
            self._flat_root(changed)
            calls = []
            real_scan = adoption_module._scan_source_tree

            def scan_with_change(path):
                calls.append(path)
                if len(calls) == 2:
                    with open(os.path.join(path, "legacy-index.dat"), "ab") as stream:
                        stream.write(b"changed during copy")
                return real_scan(path)

            changed_identity_path, _ = self._legacy_identity(root, changed)
            changed_output = os.path.join(root, "out-changed")
            with mock.patch.object(
                    adoption_module, "_scan_source_tree",
                    side_effect=scan_with_change):
                with self.assertRaisesRegex(ValueError, "changed during adoption"):
                    prepare_legacy_flat_adoption(
                        changed, changed_output, changed_identity_path,
                        LEGACY_COMMIT_SHA,
                    )
            self.assertFalse(os.path.exists(changed_output))

    def test_bootstrap_rebinds_adoption_staging_before_creating_current(self):
        tamper_cases = (
            "nested_html",
            "missing_registry",
            "extra_report",
            "modified_asset",
            "modified_source_root",
        )
        for case in tamper_cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(
                        prefix="legacy-flat-bootstrap-rebind-") as root:
                    flat_root = os.path.join(root, "flat")
                    os.makedirs(flat_root)
                    self._flat_root(flat_root)
                    identity_path, _ = self._legacy_identity(root, flat_root)
                    adopted_root = os.path.join(root, "adopted")
                    adoption = prepare_legacy_flat_adoption(
                        flat_root, adopted_root, identity_path,
                        LEGACY_COMMIT_SHA,
                    )

                    if case == "nested_html":
                        with open(os.path.join(
                                adopted_root, "reports", "dhc", "source-file.html"),
                                "ab") as stream:
                            stream.write(b"TAMPERED")
                    elif case == "missing_registry":
                        root_report = next(
                            item for item in adoption["reports"]
                            if item["report_scope"] == "root"
                        )
                        os.remove(os.path.join(
                            adopted_root, "registry",
                            root_report["report_id"] + ".json",
                        ))
                    elif case == "extra_report":
                        self._write_bytes(
                            os.path.join(adopted_root, "reports", "extra.html"),
                            b"<html><head></head><body>extra</body></html>",
                        )
                    elif case == "modified_asset":
                        with open(os.path.join(
                                adopted_root, "assets", "dhc", "source.c"),
                                "ab") as stream:
                            stream.write(b"TAMPERED")
                    elif case == "modified_source_root":
                        with open(os.path.join(
                                flat_root, "legacy-index.dat"), "ab") as stream:
                            stream.write(b"TAMPERED")

                    publish_root = os.path.join(root, "publish")
                    with self.assertRaises(ValueError):
                        bootstrap(
                            adopted_root, publish_root, identity_path,
                            "previous-e9fcc837",
                            served_identity_path=os.path.join(
                                adopted_root, "release_identity.json"
                            ),
                            switch=True,
                        )
                    self.assertFalse(os.path.lexists(
                        os.path.join(publish_root, "CURRENT")
                    ))

    def test_bootstrap_rejects_copy_race_after_staging_validation(self):
        with tempfile.TemporaryDirectory(
                prefix="legacy-flat-bootstrap-copy-race-") as root:
            flat_root = os.path.join(root, "flat")
            os.makedirs(flat_root)
            self._flat_root(flat_root)
            identity_path, _ = self._legacy_identity(root, flat_root)
            adopted_root = os.path.join(root, "adopted")
            prepare_legacy_flat_adoption(
                flat_root, adopted_root, identity_path, LEGACY_COMMIT_SHA
            )

            real_copy = bootstrap_module._copy_source_without_following_links

            def copy_after_staging_tamper(source_root, target_root):
                with open(os.path.join(
                        source_root, "reports", "dhc", "source-file.html"),
                        "ab") as stream:
                    stream.write(b"TAMPERED BETWEEN VALIDATION AND COPY")
                real_copy(source_root, target_root)

            publish_root = os.path.join(root, "publish")
            with mock.patch.object(
                    bootstrap_module,
                    "_copy_source_without_following_links",
                    side_effect=copy_after_staging_tamper):
                with self.assertRaisesRegex(ValueError, "copy does not match"):
                    bootstrap(
                        adopted_root, publish_root, identity_path,
                        "previous-e9fcc837",
                        served_identity_path=os.path.join(
                            adopted_root, "release_identity.json"
                        ),
                        switch=True,
                    )
            self.assertFalse(os.path.lexists(
                os.path.join(publish_root, "CURRENT")
            ))


if __name__ == "__main__":
    unittest.main()
