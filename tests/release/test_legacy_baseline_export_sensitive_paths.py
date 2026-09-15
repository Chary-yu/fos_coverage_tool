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


if __name__ == "__main__":
    unittest.main()
