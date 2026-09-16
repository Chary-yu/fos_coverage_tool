import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from scripts.release.release_input_manifest import (
    RELEASE_INPUT_MANIFEST_SCHEMA_VERSION,
    build_release_input_manifest,
    load_release_input_manifest,
    release_input_manifest_sha256,
    validate_release_input_manifest,
    write_release_input_manifest,
)


class ReleaseInputManifestTest(unittest.TestCase):
    @staticmethod
    def _manifest():
        return {
            "schema_version": RELEASE_INPUT_MANIFEST_SCHEMA_VERSION,
            "repository": "Chary-yu/fos_coverage_tool",
            "source_commit_sha": "a" * 40,
            "source_tree_sha": "b" * 40,
            "source_bundle_sha256": "c" * 64,
            "tooling_commit_sha": "d" * 40,
            "tooling_tree_sha": "e" * 40,
            "tooling_bundle_sha256": "f" * 64,
            "production_host": "vfoswind",
            "baseline_commit_sha": "1" * 40,
            "baseline_attempt_id": "baseline-attempt-1",
            "baseline_tree_sha256": "2" * 64,
            "baseline_manifest_sha256": "3" * 64,
            "baseline_adoption_identity_sha256": "4" * 64,
            "project_name": "FOS_V6R2",
            "candidate_parent": "/home/zcyu/coverage_candidate",
            "release_trust_mode": "offline_operator",
        }

    def test_manifest_is_canonical_and_round_trips_with_sidecar_digest(self):
        manifest = build_release_input_manifest(self._manifest())
        self.assertEqual(validate_release_input_manifest(manifest), manifest)
        digest = release_input_manifest_sha256(manifest)
        self.assertEqual(len(digest), 64)
        with tempfile.TemporaryDirectory(prefix="release-input-manifest-") as root:
            path = os.path.join(root, "release_input_manifest.json")
            sidecar = os.path.join(root, "release_input_manifest.sha256")
            written = write_release_input_manifest(path, manifest, sidecar)
            self.assertEqual(written, digest)
            self.assertEqual(load_release_input_manifest(path, expected_sha256=digest), manifest)
            with open(sidecar, encoding="utf-8") as stream:
                self.assertEqual(
                    stream.read(), digest + "  release_input_manifest.json\n"
                )

    def test_each_immutable_binding_rejects_tampering(self):
        manifest = self._manifest()
        fields = (
            "source_commit_sha", "source_tree_sha", "source_bundle_sha256",
            "tooling_commit_sha", "tooling_tree_sha", "tooling_bundle_sha256",
            "baseline_commit_sha", "baseline_tree_sha256",
            "baseline_manifest_sha256", "baseline_adoption_identity_sha256",
            "production_host", "baseline_attempt_id", "candidate_parent",
        )
        for field in fields:
            with self.subTest(field=field):
                tampered = dict(manifest)
                if field.endswith("sha") or field.endswith("sha256"):
                    tampered[field] = "0" * len(str(tampered[field]))
                else:
                    tampered[field] = str(tampered[field]) + "-tampered"
                if field.endswith("sha") or field.endswith("sha256"):
                    with self.assertRaisesRegex(ValueError, field):
                        validate_release_input_manifest(tampered)
                else:
                    self.assertNotEqual(
                        release_input_manifest_sha256(manifest),
                        release_input_manifest_sha256(tampered),
                    )

    def test_protected_builder_and_publishable_validation_fixture_are_rejected(self):
        protected = dict(self._manifest(), release_trust_mode="protected_builder")
        with self.assertRaisesRegex(ValueError, "offline_operator"):
            validate_release_input_manifest(protected)
        fixture = dict(self._manifest(), production_publishable=True)
        with self.assertRaisesRegex(ValueError, "production_publishable"):
            validate_release_input_manifest(fixture)

    @staticmethod
    def _git_repo(root, files):
        os.makedirs(root)
        for relative, content in files.items():
            path = os.path.join(root, *relative.split("/"))
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(content)
        subprocess.check_call(["git", "init", "-q"], cwd=root)
        subprocess.check_call(
            ["git", "config", "user.email", "manifest-test@example.invalid"],
            cwd=root,
        )
        subprocess.check_call(
            ["git", "config", "user.name", "Release Manifest Test"], cwd=root
        )
        subprocess.check_call(["git", "add", "."], cwd=root)
        subprocess.check_call(["git", "commit", "-q", "-m", "fixture"], cwd=root)
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root
        ).decode("ascii").strip()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=root
        ).decode("ascii").strip()
        bundle = root + ".bundle"
        subprocess.check_call(["git", "bundle", "create", bundle, "HEAD"], cwd=root)
        return commit, tree, bundle

    def test_generic_operator_embeds_manifest_and_runs_without_github(self):
        from scripts.release.generate_flat_production_candidate_operator import generate

        with tempfile.TemporaryDirectory(prefix="generic-operator-") as root:
            source_commit, source_tree, source_bundle = self._git_repo(
                os.path.join(root, "source"), {"README.md": "source\n"}
            )
            tooling_files = {
                "app/release_trust_policy.py":
                    "CURRENT_PRODUCTION_RELEASE_TRUST_MODE = 'offline_operator'\n"
                    "CURRENT_PRODUCTION_RELEASE_TRUST_MODES = ('offline_operator',)\n",
                "scripts/release/build_production_candidate_artifact.py":
                    "release_trust_mode = 'offline_operator'\n",
                "scripts/release/build_flat_production_candidate_artifact.py":
                    "release_trust_mode = 'offline_operator'\n",
            }
            tooling_commit, tooling_tree, tooling_bundle = self._git_repo(
                os.path.join(root, "tooling"), tooling_files
            )
            manifest = self._manifest()
            manifest.update({
                "source_commit_sha": source_commit,
                "source_tree_sha": source_tree,
                "source_bundle_sha256": _sha256(source_bundle),
                "tooling_commit_sha": tooling_commit,
                "tooling_tree_sha": tooling_tree,
                "tooling_bundle_sha256": _sha256(tooling_bundle),
                "candidate_build_mode": "validation_fixture",
                "production_publishable": False,
            })
            manifest_path = os.path.join(root, "release_input_manifest.json")
            digest = write_release_input_manifest(
                manifest_path, build_release_input_manifest(manifest)
            )
            operator = os.path.join(root, "offline_operator.txt")
            result = generate(manifest_path, source_bundle, tooling_bundle, operator)
            self.assertEqual(result["release_input_manifest_sha256"], digest)
            with open(operator, encoding="utf-8") as stream:
                operator_text = stream.read()
            self.assertIn("RELEASE_INPUT_MANIFEST_SHA256", operator_text)
            self.assertNotIn("0f4009c9f8c253f42e60394841795522bbaaa24d", operator_text)
            subprocess.check_call(["bash", "-n", operator])
            environment = dict(os.environ)
            for key in ("GITHUB_TOKEN", "GH_TOKEN"):
                environment.pop(key, None)
            completed = subprocess.run(
                ["bash", operator, "--self-test"],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("OPERATOR_SELF_TEST=PASS", completed.stdout)
            self.assertIn("PRODUCTION_MUTATION=NONE", completed.stdout)

            tampered_operator = os.path.join(root, "tampered_operator.txt")
            with open(operator, encoding="utf-8") as stream:
                tampered_text = stream.read().replace(digest, "0" * 64, 1)
            with open(tampered_operator, "w", encoding="utf-8") as stream:
                stream.write(tampered_text)
            tampered = subprocess.run(
                ["bash", tampered_operator, "--self-test"],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertNotEqual(tampered.returncode, 0, tampered.stdout)
            self.assertNotIn("OPERATOR_SELF_TEST=PASS", tampered.stdout)

    def test_runtime_builder_command_carries_manifest_and_digest(self):
        from scripts.release.flat_candidate_operator_runtime import _builder_command

        manifest = dict(self._manifest(),
                        candidate_build_mode="production",
                        served_root="/srv/publish/CURRENT",
                        served_root_tree_sha256="5" * 64,
                        served_root_identity_sha256="6" * 64)
        manifest_sha = release_input_manifest_sha256(manifest)
        command = _builder_command(
            manifest, "/source", "/candidate", "/identity.json",
            "/evidence.json", "/source.bundle", "session-1", "/tooling",
            "/state/input/release_input_manifest.json", manifest_sha,
        )
        self.assertEqual(
            command[command.index("--release-input-manifest") + 1],
            "/state/input/release_input_manifest.json",
        )
        self.assertEqual(
            command[command.index("--release-input-manifest-sha256") + 1],
            manifest_sha,
        )

    def test_operator_rejects_non_python36_before_candidate_state_creation(self):
        from scripts.release.generate_flat_production_candidate_operator import generate

        with tempfile.TemporaryDirectory(prefix="operator-python36-gate-") as root:
            source_commit, source_tree, source_bundle = self._git_repo(
                os.path.join(root, "source"), {"README.md": "source\n"}
            )
            tooling_files = {
                "app/release_trust_policy.py":
                    "CURRENT_PRODUCTION_RELEASE_TRUST_MODE = 'offline_operator'\n"
                    "CURRENT_PRODUCTION_RELEASE_TRUST_MODES = ('offline_operator',)\n",
                "scripts/release/build_production_candidate_artifact.py":
                    "release_trust_mode = 'offline_operator'\n",
                "scripts/release/build_flat_production_candidate_artifact.py":
                    "release_trust_mode = 'offline_operator'\n",
            }
            tooling_commit, tooling_tree, tooling_bundle = self._git_repo(
                os.path.join(root, "tooling"), tooling_files
            )
            manifest = self._manifest()
            manifest.update({
                "source_commit_sha": source_commit,
                "source_tree_sha": source_tree,
                "source_bundle_sha256": _sha256(source_bundle),
                "tooling_commit_sha": tooling_commit,
                "tooling_tree_sha": tooling_tree,
                "tooling_bundle_sha256": _sha256(tooling_bundle),
                "candidate_parent": os.path.join(root, "candidate-parent"),
                "production_host": "local-validation",
                "candidate_build_mode": "production",
            })
            manifest_path = os.path.join(root, "release_input_manifest.json")
            write_release_input_manifest(
                manifest_path, build_release_input_manifest(manifest)
            )
            operator = os.path.join(root, "offline_operator.txt")
            generate(manifest_path, source_bundle, tooling_bundle, operator)
            fake_python = os.path.join(root, "python-not-36")
            with open(fake_python, "w", encoding="ascii") as stream:
                stream.write("#!/bin/sh\n")
                stream.write("if [ \"$1\" = \"-c\" ]; then echo 3.14; exit 0; fi\n")
                stream.write("exec python3 \"$@\"\n")
            os.chmod(fake_python, 0o750)
            environment = dict(os.environ)
            environment["FOS_OPERATOR_PYTHON"] = fake_python
            completed = subprocess.run(
                ["bash", operator], cwd=root, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("PYTHON36_COMPAT=FAIL", completed.stdout)
            self.assertFalse(os.path.exists(os.path.join(root, "candidate-parent")))


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
