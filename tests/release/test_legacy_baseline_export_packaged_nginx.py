import hashlib
import os
import subprocess
import tempfile
import unittest


from tests.release.test_legacy_baseline_export_sensitive_paths import (
    _load_embedded_helper,
)


class LegacyBaselineExportPackagedNginxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper, cls.helper_temp_root = _load_embedded_helper()

    @classmethod
    def tearDownClass(cls):
        if os.path.isdir(cls.helper_temp_root):
            import shutil
            shutil.rmtree(cls.helper_temp_root)

    @staticmethod
    def _write(path, content):
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "wb") as stream:
            stream.write(content)

    def _packaged_fixture(self, root):
        self._write(os.path.join(root, "app.js"), b"fixture-app\n")
        self._write(
            os.path.join(root, "web", "index.html"),
            b"<html>fixture</html>\n",
        )
        declared = []
        for relative in ("app.js", "web/index.html"):
            path = os.path.join(root, *relative.split("/"))
            with open(path, "rb") as stream:
                content = stream.read()
            declared.append({
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        asset_hash = self.helper.canonical(declared)
        manifest = {
            "version": "fixture-release",
            "commit_sha": "e9fcc837a1ac9847f3966fc8ddb2aed92ca473fc",
            "build_id": "fixture-release-e9fcc837-{}".format(
                asset_hash[:8]
            ),
            "asset_hash": asset_hash,
            "schema_version": 2,
            "asset_manifest_version": 1,
            "asset_count": len(declared),
            "asset_manifest_hash": asset_hash,
            "asset_manifest": declared,
        }
        manifest_path = os.path.join(root, "release_manifest.json")
        self.helper.write_json(manifest_path, manifest)
        return manifest_path, manifest, declared, asset_hash

    def test_packaged_identity_requires_manifest_and_verifies_bytes(self):
        with tempfile.TemporaryDirectory(prefix="r8-packaged-identity-") as root:
            manifest_path, manifest, declared, asset_hash = self._packaged_fixture(root)
            manifest_sha = self.helper.sha256_file(manifest_path)
            result = self.helper.validate_application_identity(
                root, manifest["commit_sha"], manifest_sha, asset_hash
            )
            self.assertEqual(
                result["application_identity_mode"],
                "PACKAGED_RELEASE_MANIFEST",
            )
            self.assertEqual(
                result["application_git_sha"],
                "NOT_AVAILABLE_PACKAGED_DEPLOYMENT",
            )
            self.assertEqual(
                result["application_git_tree_sha"],
                "NOT_AVAILABLE_PACKAGED_DEPLOYMENT",
            )
            self.assertEqual(result["release_manifest_sha256"], manifest_sha)
            self.assertEqual(result["release_manifest_asset_hash"], asset_hash)
            self.assertEqual(result["packaged_release_anchor"]["verified"], True)
            self.assertEqual(result["packaged_asset_bindings"], declared)

    def test_packaged_identity_blocks_commit_hash_bytes_and_missing_variants(self):
        with tempfile.TemporaryDirectory(prefix="r8-packaged-invalid-") as root:
            manifest_path, manifest, _declared, asset_hash = self._packaged_fixture(root)
            original_manifest_sha = self.helper.sha256_file(manifest_path)

            invalid_commit = dict(manifest)
            invalid_commit["commit_sha"] = "f" * 40
            invalid_commit["build_id"] = "fixture-release-ffffffff-{}".format(
                asset_hash[:8]
            )
            self.helper.write_json(manifest_path, invalid_commit)
            with self.assertRaises(ValueError):
                self.helper.validate_application_identity(
                    root, manifest["commit_sha"],
                    self.helper.sha256_file(manifest_path), asset_hash
                )

            self.helper.write_json(manifest_path, manifest)
            with open(os.path.join(root, "app.js"), "ab") as stream:
                stream.write(b"tamper\n")
            with self.assertRaises(ValueError):
                self.helper.validate_application_identity(
                    root, manifest["commit_sha"], original_manifest_sha, asset_hash
                )

            self.helper.write_json(manifest_path, manifest)
            self._write(os.path.join(root, "app.js"), b"fixture-app\n")
            os.unlink(os.path.join(root, "web", "index.html"))
            with self.assertRaises(ValueError):
                self.helper.validate_application_identity(
                    root, manifest["commit_sha"], original_manifest_sha, asset_hash
                )

            self.helper.write_json(manifest_path, manifest)
            self._write(
                os.path.join(root, "web", "index.html"),
                b"<html>fixture</html>\n",
            )
            invalid_hash = dict(manifest)
            invalid_hash["asset_manifest_hash"] = "0" * 64
            self.helper.write_json(manifest_path, invalid_hash)
            with self.assertRaises(ValueError):
                self.helper.validate_application_identity(
                    root, manifest["commit_sha"],
                    self.helper.sha256_file(manifest_path), asset_hash
                )

    def test_git_checkout_identity_remains_strict(self):
        with tempfile.TemporaryDirectory(prefix="r8-git-identity-") as root:
            subprocess.check_call(["git", "init", "-q", root])
            subprocess.check_call(["git", "-C", root, "config", "user.email", "test@example.invalid"])
            subprocess.check_call(["git", "-C", root, "config", "user.name", "test"])
            self._write(os.path.join(root, "app.py"), b"fixture\n")
            subprocess.check_call(["git", "-C", root, "add", "app.py"])
            subprocess.check_call(["git", "-C", root, "commit", "-qm", "fixture"])
            head = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            result = self.helper.validate_application_identity(
                root, head, "unused", "unused"
            )
            self.assertEqual(result["application_identity_mode"], "GIT_CHECKOUT")
            self.assertEqual(result["application_git_sha"], head)
            self.assertRegex(result["application_git_tree_sha"], r"^[0-9a-f]{40}$")
            with self.assertRaises(ValueError):
                self.helper.validate_application_identity(
                    root, "e9fcc837a1ac9847f3966fc8ddb2aed92ca473fc",
                    "unused", "unused"
                )

    @staticmethod
    def _nginx_args(config, output):
        return (
            config,
            "/etc/nginx/conf.d/coverage.conf",
            "80",
            "10.190.162.33",
            "/coverage/",
            "/home/zcyu/coverage/export0810/onesensor/",
            "/api/coverage",
            "http://127.0.0.1:9528/api/coverage",
            output,
        )

    def test_nginx_summary_scopes_production_and_ignores_candidate(self):
        dump = """\
# configuration file /etc/nginx/conf.d/coverage-candidate.conf:
server {
    listen 10.190.162.33:19529;
    server_name 10.190.162.33;
    location /coverage/ {
        alias /home/zcyu/coverage_published/VALIDATION_CURRENT/reports/;
    }
}
# configuration file /etc/nginx/conf.d/coverage.conf:
server {
    listen 80;
    server_name 10.190.162.33;
    location /coverage/ {
        alias /home/zcyu/coverage/export0810/onesensor/;
    }
    location /api/coverage {
        proxy_pass http://127.0.0.1:9528/api/coverage;
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="r8-nginx-topology-") as root:
            config = os.path.join(root, "nginx.dump")
            output = os.path.join(root, "summary.txt")
            with open(config, "w") as stream:
                stream.write(dump)
            result = self.helper.nginx_summary(
                *self._nginx_args(config, output)
            )
            self.assertEqual(result["nginx_production_config_scoped"], "MATCH")
            self.assertEqual(result["nginx_candidate_gateway_ignored"], "MATCH")
            self.assertEqual(result["production_static_alias_match"], "MATCH")
            self.assertEqual(result["production_api_proxy_match"], "MATCH")

    def test_nginx_summary_blocks_route_drift_and_duplicate_production(self):
        base = """\
# configuration file /etc/nginx/conf.d/coverage.conf:
server {
    listen 80;
    server_name 10.190.162.33;
    location /coverage/ {
        alias /home/zcyu/coverage/export0810/onesensor/;
    }
    location /api/coverage {
        proxy_pass http://127.0.0.1:9528/api/coverage;
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="r8-nginx-invalid-") as root:
            variants = {
                "port": base.replace(
                    "127.0.0.1:9528/api/coverage",
                    "127.0.0.1:9530/api/coverage",
                ),
                "path": base.replace(
                    "127.0.0.1:9528/api/coverage",
                    "127.0.0.1:9528/api/wrong",
                ),
                "alias": base.replace(
                    "/home/zcyu/coverage/export0810/onesensor/",
                    "/home/zcyu/coverage/wrong/",
                ),
                "duplicate": base + base.split(
                    "# configuration file /etc/nginx/conf.d/coverage.conf:\n",
                    1,
                )[1],
            }
            for name, dump in variants.items():
                with self.subTest(variant=name):
                    config = os.path.join(root, name + ".dump")
                    output = os.path.join(root, name + ".summary")
                    with open(config, "w") as stream:
                        stream.write(dump)
                    with self.assertRaises(ValueError):
                        self.helper.nginx_summary(
                            *self._nginx_args(config, output)
                        )


if __name__ == "__main__":
    unittest.main()
