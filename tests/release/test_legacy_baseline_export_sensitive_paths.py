import json
import importlib.util
import os
import shutil
import tempfile
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
TOOL_PATH = os.path.join(
    ROOT, "fos_r8_legacy_baseline_export_e9fcc837_20260915.txt"
)


def _load_embedded_helper():
    with open(TOOL_PATH, "r", encoding="utf-8") as stream:
        source = stream.read()
    start_marker = 'cat >"$HELPER" <<\'PY\'\n'
    end_marker = '\nPY\n\n"$PYTHON_BIN" "$HELPER" constants'
    start = source.index(start_marker) + len(start_marker)
    end = source.index(end_marker, start)
    helper_path = os.path.join(
        tempfile.mkdtemp(prefix="r8-sensitive-helper-"), "helper.py"
    )
    with open(helper_path, "w", encoding="utf-8") as stream:
        stream.write(source[start:end])
    spec = importlib.util.spec_from_file_location(
        "r8_embedded_baseline_export_helper", helper_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, os.path.dirname(helper_path)


class LegacyBaselineExportSensitivePathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper, cls.helper_temp_root = _load_embedded_helper()

    @classmethod
    def tearDownClass(cls):
        if os.path.isdir(cls.helper_temp_root):
            shutil.rmtree(cls.helper_temp_root)

    @staticmethod
    def _write(root, relative, content=b"fixture\n"):
        path = os.path.join(root, *relative.split("/"))
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "wb") as stream:
            stream.write(content)

    def test_coverage_source_artifacts_are_not_blocked_by_security_words(self):
        names = (
            "home/git_for_coverage/git_fosip/fosip/fosip/nbm/source/comp/ssh/ssh/openssh/src/auth-passwd.c.func-sort-c.html",
            "password.c.html",
            "ssh/auth.c.html",
            "secret.c.html",
            "private_key_parser.cpp.html",
            "passwd.c.gcov.html",
            "coverage/index.html",
            "report.html",
        )
        with tempfile.TemporaryDirectory(prefix="r8-sensitive-allow-") as root:
            for name in names:
                self._write(root, name)
            observed = self.helper.inventory(root)
            self.assertEqual(observed["file_count"], len(names))
            self.assertEqual(
                sorted(item["path"] for item in observed["files"]),
                sorted(names),
            )

    def test_genuine_sensitive_material_remains_blocked(self):
        names = (
            "etc/passwd",
            "etc/shadow",
            "etc/gshadow",
            "root/.ssh/id_rsa",
            "home/test/.ssh/id_ed25519",
            "server.key",
            "client.pem",
            "credentials",
            "credentials.json",
            "secret.env",
            ".env-secret",
        )
        for index, name in enumerate(names):
            with self.subTest(path=name):
                with tempfile.TemporaryDirectory(
                        prefix="r8-sensitive-block-{}-".format(index)
                ) as root:
                    self._write(root, name, b"fixture-not-a-secret\n")
                    with self.assertRaises(ValueError):
                        self.helper.inventory(root)

    def test_traversal_absolute_escape_and_symlink_escape_fail_closed(self):
        with self.assertRaises(ValueError):
            self.helper.reject_sensitive_path("../escape")
        with self.assertRaises(ValueError):
            self.helper.reject_sensitive_path("/etc/passwd")

        with tempfile.TemporaryDirectory(prefix="r8-sensitive-containment-") as root:
            outside = os.path.join(root, "outside")
            served = os.path.join(root, "served")
            os.mkdir(served)
            os.mkdir(outside)
            with self.assertRaises(ValueError):
                self.helper.reject_sensitive_path(
                    outside, export_root=served
                )
            link = os.path.join(served, "escape")
            try:
                os.symlink(outside, link)
            except OSError as exc:
                self.skipTest("symlink fixture unavailable: {}".format(exc))
            with self.assertRaises(ValueError):
                self.helper.inventory(served)

    def test_identity_and_manifest_call_sites_use_the_same_semantic_policy(self):
        commit = "a" * 40
        artifact = "auth-passwd.c.func-sort-c.html"
        with tempfile.TemporaryDirectory(prefix="r8-sensitive-call-sites-") as root:
            source = os.path.join(root, "source")
            os.mkdir(source)
            self._write(source, artifact, b"coverage-source\n")
            artifact_path = os.path.join(source, artifact)
            declared = [{
                "path": artifact,
                "size": os.path.getsize(artifact_path),
                "sha256": self.helper.sha256_file(artifact_path),
            }]
            identity = {
                "version": "legacy",
                "commit_sha": commit,
                "build_id": "fixture",
                "asset_hash": self.helper.canonical(declared),
                "schema_version": 1,
                "asset_manifest_version": 1,
                "asset_count": 1,
                "asset_manifest_hash": self.helper.canonical(declared),
                "asset_manifest": declared,
            }
            identity_path = os.path.join(source, "release_identity.json")
            self.helper.write_json(identity_path, identity)
            normalized = os.path.join(root, "normalized.json")
            result = self.helper.validate_identity(
                identity_path, source, commit, normalized
            )
            self.assertEqual(result["commit_sha"], commit)

            export = os.path.join(root, "export")
            raw = os.path.join(export, "legacy-flat-root", "ssh")
            evidence = os.path.join(export, "evidence")
            os.makedirs(raw)
            os.makedirs(evidence)
            self._write(
                os.path.join(export, "legacy-flat-root"),
                "ssh/" + artifact,
            )
            snapshot = self.helper.inventory(
                os.path.join(export, "legacy-flat-root")
            )
            for name in (
                    "inventory-source-before.json",
                    "inventory-source-after.json",
                    "inventory-legacy-flat-root.json"):
                self.helper.write_json(os.path.join(evidence, name), snapshot)
            with open(os.path.join(evidence, "sidecar-summary.txt"), "w") as stream:
                stream.write("SIDECAR_LAYOUT=reports/.source_cache/**/meta.json\n")
                stream.write("SIDECAR_STATUS=NOT_APPLICABLE\n")
                stream.write("SIDECAR_INVALID=NONE\n")
                for name in (
                        "SIDECAR_META_JSON_COUNT",
                        "SIDECAR_SCHEMA_V2_COUNT",
                        "SIDECAR_SCHEMA_V1_COUNT",
                        "SIDECAR_V2_TOTAL_LINES_COUNT",
                        "SIDECAR_V2_FUNCTION_RANGES_COUNT"):
                    stream.write("{}=0\n".format(name))
            metadata_path = os.path.join(root, "metadata.json")
            self.helper.write_json(metadata_path, {
                "attempt_id": "sensitive-call-site",
                "tool_filename": "fixture.txt",
                "tool_sha256": "a" * 64,
                "tool_size": 1,
                "hostname": "vfoswind",
                "expected_baseline_sha": commit,
            })
            manifest_path = os.path.join(export, "baseline-export-manifest.json")
            manifest = self.helper.manifest(
                export, manifest_path, metadata_path
            )
            self.assertIn(
                "legacy-flat-root/ssh/" + artifact,
                [item["path"] for item in manifest["files"]],
            )

    def _pre_adoption_fixture(self, root):
        source = os.path.join(root, "legacy")
        adoption = os.path.join(root, "adoption-source")
        os.mkdir(source)
        self._write(source, "auth-passwd.c.func-sort-c.html")
        self._write(source, "ssh/auth.c.html")
        self._write(source, "coverage/report.html")
        before = self.helper.inventory(source)
        before_path = os.path.join(root, "source-before.json")
        adoption_path = os.path.join(root, "adoption.json")
        self.helper.write_json(before_path, before)
        self.helper.copy_files(source, adoption)
        adoption_inventory = self.helper.inventory(adoption)
        self.helper.write_json(adoption_path, adoption_inventory)
        identity_path = os.path.join(root, "staging", "release_identity.json")
        result_path = os.path.join(root, "staging", "identity-result.json")
        normalized_path = os.path.join(root, "normalized.json")
        if not os.path.isdir(os.path.dirname(identity_path)):
            os.makedirs(os.path.dirname(identity_path))
        identity, result = self.helper.build_legacy_adoption_identity(
            adoption_path, adoption, before_path,
            self.helper.CONTRACT["baseline_sha"],
            self.helper.CONTRACT["baseline_sha"],
            "b" * 40, "fixture-pre-adoption", "2026-09-15T00:00:00Z",
            identity_path, result_path, normalized_path,
        )
        self.assertEqual(result["identity_mode"], "LEGACY_PRE_ADOPTION")
        self.assertFalse(os.path.exists(os.path.join(
            source, "release_identity.json"
        )))
        return source, adoption, before_path, identity_path, identity

    def test_legacy_pre_adoption_generates_verified_observed_identity(self):
        with tempfile.TemporaryDirectory(prefix="r8-pre-adoption-valid-") as root:
            source, adoption, before_path, identity_path, identity = \
                self._pre_adoption_fixture(root)
            self.assertEqual(identity["identity_mode"], "LEGACY_PRE_ADOPTION")
            self.assertEqual(identity["identity_origin"], "legacy_adoption")
            self.assertEqual(identity["original_release_identity"], "ABSENT")
            self.assertEqual(
                identity["historical_build_attestation"], "NOT_AVAILABLE"
            )
            self.assertIn("not an original historical build attestation",
                          identity["provenance_claim"])
            self.assertEqual(
                identity["artifact_role"], "validation_input_baseline_only"
            )
            self.assertIs(identity["production_publishable"], False)
            with open(before_path, "r") as stream:
                before = json.load(stream)
            self.assertEqual(before, self.helper.inventory(source))
            self.assertFalse(os.path.exists(os.path.join(
                source, "release_identity.json"
            )))
            self.assertEqual(identity["snapshot_tree_sha256"], before["tree_sha256"])
            self.assertEqual(
                identity["adoption_source_tree_sha256"],
                self.helper.inventory(adoption)["tree_sha256"],
            )

    def test_existing_valid_identity_remains_strict(self):
        with tempfile.TemporaryDirectory(prefix="r8-existing-identity-") as root:
            source, _adoption, _before, identity_path, _identity = \
                self._pre_adoption_fixture(root)
            existing_path = os.path.join(source, "release_identity.json")
            shutil.copyfile(identity_path, existing_path)
            self.assertEqual(
                self.helper.detect_identity_mode(existing_path),
                "EXISTING_VERIFIED_IDENTITY",
            )
            result = self.helper.validate_identity(
                existing_path, source, self.helper.CONTRACT["baseline_sha"],
                os.path.join(root, "existing-normalized.json"),
            )
            self.assertEqual(result["commit_sha"],
                             self.helper.CONTRACT["baseline_sha"])

    def test_malformed_and_mismatched_existing_identity_block(self):
        with tempfile.TemporaryDirectory(prefix="r8-existing-invalid-") as root:
            source, _adoption, _before, identity_path, identity = \
                self._pre_adoption_fixture(root)
            existing_path = os.path.join(source, "release_identity.json")
            with open(existing_path, "w") as stream:
                stream.write("{not-json\n")
            with self.assertRaises(ValueError):
                self.helper.validate_identity(
                    existing_path, source,
                    self.helper.CONTRACT["baseline_sha"],
                    os.path.join(root, "malformed-normalized.json"),
                )
            self.helper.write_json(existing_path, identity)
            invalid = dict(identity)
            invalid["commit_sha"] = "f" * 40
            self.helper.write_json(existing_path, invalid)
            with self.assertRaises(ValueError):
                self.helper.validate_identity(
                    existing_path, source,
                    self.helper.CONTRACT["baseline_sha"],
                    os.path.join(root, "mismatch-normalized.json"),
                )

    def test_adoption_identity_byte_and_git_anchor_mismatches_block(self):
        with tempfile.TemporaryDirectory(prefix="r8-pre-adoption-mismatch-") as root:
            source, adoption, before_path, _identity_path, identity = \
                self._pre_adoption_fixture(root)
            invalid = dict(identity)
            invalid["asset_manifest"] = list(identity["asset_manifest"])
            invalid["asset_manifest"][0] = dict(invalid["asset_manifest"][0])
            invalid["asset_manifest"][0]["size"] += 1
            invalid["asset_hash"] = self.helper.canonical(
                invalid["asset_manifest"]
            )
            invalid["asset_manifest_hash"] = invalid["asset_hash"]
            invalid_path = os.path.join(root, "invalid-identity.json")
            self.helper.write_json(invalid_path, invalid)
            with self.assertRaises(ValueError):
                self.helper.validate_identity(
                    invalid_path, adoption,
                    self.helper.CONTRACT["baseline_sha"],
                    os.path.join(root, "invalid-normalized.json"),
                )
            with self.assertRaises(ValueError):
                self.helper.build_legacy_adoption_identity(
                    os.path.join(root, "adoption.json"), adoption, before_path,
                    self.helper.CONTRACT["baseline_sha"], "f" * 40, "b" * 40,
                    "git-mismatch", "2026-09-15T00:00:00Z",
                    os.path.join(root, "git-mismatch.json"),
                    os.path.join(root, "git-mismatch-result.json"),
                    os.path.join(root, "git-mismatch-normalized.json"),
                )

    def test_source_change_and_sidecar_absence_fail_or_classify_explicitly(self):
        with tempfile.TemporaryDirectory(prefix="r8-pre-adoption-state-") as root:
            source = os.path.join(root, "legacy")
            os.mkdir(source)
            self._write(source, "coverage.c.html", b"AAAA\n")
            before = self.helper.inventory(source)
            before_path = os.path.join(root, "before.json")
            after_path = os.path.join(root, "after.json")
            self.helper.write_json(before_path, before)
            with open(os.path.join(source, "coverage.c.html"), "wb") as stream:
                stream.write(b"BBBB\n")
            self.helper.write_json(after_path, self.helper.inventory(source))
            with self.assertRaises(ValueError):
                self.helper.compare(before_path, after_path)
            sidecar_path = os.path.join(root, "sidecar.txt")
            summary = self.helper.sidecar_summary(source, sidecar_path)
            self.assertEqual(summary["SIDECAR_STATUS"], "NOT_APPLICABLE")


if __name__ == "__main__":
    unittest.main()
