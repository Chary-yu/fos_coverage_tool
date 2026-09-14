import argparse
import json
import os
import tempfile
import unittest

from scripts.release import fos_r8_conductor


class VfoswindConductorLegacyConfigTest(unittest.TestCase):
    def _args(self, root, config_path, metadata_path):
        return argparse.Namespace(
            repo_root=root,
            config=config_path,
            state_root=os.path.join(root, "state"),
            metadata=metadata_path,
            source_bundle=os.path.join(root, "source.bundle"),
            baseline_performance=os.path.join(root, "baseline.json"),
            candidate_performance=os.path.join(root, "candidate.json"),
            resume=False,
            status=False,
        )

    def test_vfoswind_metadata_materializes_adapterless_legacy_config(self):
        with tempfile.TemporaryDirectory(prefix="r8-conductor-legacy-") as root:
            config_path = os.path.join(root, "coverage_config.json")
            metadata_path = os.path.join(root, "metadata.json")
            with open(config_path, "w", encoding="utf-8") as stream:
                json.dump({"auth": {"mode": "disabled"}, "upgrade": {}}, stream)
            with open(metadata_path, "w", encoding="utf-8") as stream:
                json.dump({"production_host": "vfoswind"}, stream)

            normalized_args = fos_r8_conductor._normalized_args(
                self._args(root, config_path, metadata_path)
            )
            with open(normalized_args.config, "r", encoding="utf-8") as stream:
                normalized = json.load(stream)

        upgrade = normalized["upgrade"]
        self.assertEqual(upgrade["lifecycle_adapter"], "vfoswind")
        self.assertEqual(
            upgrade["production_integration"]["adapter"], "vfoswind"
        )
        self.assertEqual(upgrade["publish_root"], "/home/zcyu/coverage_published")
        self.assertEqual(
            upgrade["served_root_path"],
            "/home/zcyu/coverage_published/CURRENT/reports",
        )
        self.assertEqual(
            upgrade["health_endpoint"],
            "http://127.0.0.1:9528/api/coverage/health",
        )
        self.assertEqual(
            upgrade["release_endpoint"],
            "http://127.0.0.1:9528/api/coverage/release",
        )
        self.assertEqual(normalized["auth"]["mode"], "reverse_proxy")

    def test_non_vfoswind_metadata_does_not_force_adapterless_config(self):
        source = {"auth": {"mode": "disabled"}, "upgrade": {}}
        result = fos_r8_conductor._bind_legacy_vfoswind_adapter(
            source, {"production_host": "other-host"}
        )
        self.assertEqual(result, source)

    def test_explicit_conflicting_adapter_fails_closed(self):
        source = {
            "upgrade": {"lifecycle_adapter": "local"},
        }
        with self.assertRaisesRegex(RuntimeError, "conflicts with lifecycle adapter"):
            fos_r8_conductor._bind_legacy_vfoswind_adapter(
                source, {"production_host": "vfoswind"}
            )


if __name__ == "__main__":
    unittest.main()
