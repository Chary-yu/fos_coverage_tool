import hashlib
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.upgrade.build_deployment_manifest import build
from scripts.upgrade.cutover_controller import CutoverController


class TestUpgradeManifest(unittest.TestCase):
    def test_production_upgrade_uses_immutable_publication_only(self):
        with open(
                os.path.join(ROOT, "scripts", "upgrade", "run_upgrade.py"),
                encoding="utf-8") as stream:
            source = stream.read()
        self.assertIn("ImmutableReleasePublisher", source)
        self.assertIn("self.publisher.prepare", source)
        self.assertIn("self.publisher.switch_current", source)
        self.assertNotIn("from scripts.upgrade.cutover_controller import", source)
        self.assertNotIn("self.cutover.apply", source)

    def test_explicit_manifest_hash_and_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "candidate.txt")
            with open(path, "w") as stream:
                stream.write("candidate")
            manifest_path = os.path.join(root, "manifest.json")
            build(root, ["candidate.txt"], manifest_path)
            with open(manifest_path) as stream:
                manifest = json.load(stream)
            self.assertEqual(manifest["actions"][0]["op"], "ADD")
            controller = CutoverController(root, os.path.join(root, "backup"))
            controller.apply(manifest["actions"])
            with open(path) as stream:
                self.assertEqual(stream.read(), "candidate")

    def test_source_hash_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "candidate.txt")
            with open(path, "w") as stream:
                stream.write("one")
            manifest = {"actions": [{"op": "ADD", "source": "candidate.txt",
                                     "destination": "candidate.txt", "source_sha256": "bad",
                                     "backup_required": True}]}
            with self.assertRaises(RuntimeError):
                CutoverController(root, os.path.join(root, "backup")).apply(manifest["actions"])


if __name__ == "__main__":
    unittest.main()
