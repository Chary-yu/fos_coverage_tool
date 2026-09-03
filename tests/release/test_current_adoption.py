import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from scripts.release.current_adoption import (
    FLAT, IMMUTABLE_CURRENT, bootstrap_flat_current,
    classify_deployment, plan_flat_current_adoption,
)


class CurrentAdoptionStateTest(unittest.TestCase):
    def test_flat_preflight_is_read_only_and_requires_explicit_switch(self):
        with tempfile.TemporaryDirectory(prefix="flat-adoption-state-") as root:
            publish = os.path.join(root, "publish")
            flat = os.path.join(root, "flat")
            identity_path = os.path.join(root, "flat-release.json")
            os.makedirs(flat)
            expected_sha = "a" * 40
            assets = [{
                "path": "coverage_progress.js",
                "size": 1,
                "sha256": "1" * 64,
            }]
            asset_hash = hashlib.sha256(json.dumps(
                assets, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            with open(identity_path, "w") as stream:
                json.dump({
                    "version": "legacy-baseline",
                    "commit_sha": expected_sha,
                    "build_id": "baseline",
                    "asset_hash": asset_hash,
                    "schema_version": 1,
                    "asset_manifest_version": 1,
                    "asset_count": len(assets),
                    "asset_manifest_hash": asset_hash,
                    "asset_manifest": assets,
                }, stream)

            classification = classify_deployment(publish, flat)
            self.assertEqual(classification["deployment_layout"], FLAT)
            self.assertFalse(os.path.lexists(os.path.join(publish, "CURRENT")))
            self.assertFalse(os.path.isdir(os.path.join(publish, "releases")))

            plan = plan_flat_current_adoption(
                publish, flat, identity_path, expected_sha
            )
            self.assertEqual(plan["adoption_action"], "BOOTSTRAP_IMMUTABLE_BASELINE")
            self.assertFalse(plan["switch_performed"])
            no_switch = bootstrap_flat_current(
                publish, flat, identity_path, expected_sha, "baseline-a", switch=False
            )
            self.assertEqual(no_switch["deployment_layout"], FLAT)
            self.assertFalse(os.path.lexists(os.path.join(publish, "CURRENT")))

    def test_existing_current_is_a_noop_and_is_not_bootstrapped_again(self):
        with tempfile.TemporaryDirectory(prefix="current-adoption-noop-") as root:
            publish = os.path.join(root, "publish")
            release = os.path.join(publish, "releases", "baseline-a")
            os.makedirs(release)
            os.symlink(os.path.join("releases", "baseline-a"),
                       os.path.join(publish, "CURRENT"))
            binding = {
                "previous_release_commit_sha": "b" * 40,
                "previous_release_sha": "b" * 40,
                "release_validation_session_id": "baseline-a",
                "realpath": release,
            }
            with mock.patch(
                    "scripts.release.current_adoption.current_served_root_binding",
                    return_value=binding):
                result = plan_flat_current_adoption(
                    publish, os.path.join(root, "missing-flat"), "", "b" * 40
                )
                self.assertEqual(
                    result["adoption_action"], "NOOP_CURRENT_ALREADY_EXISTS"
                )
                result = bootstrap_flat_current(
                    publish, os.path.join(root, "missing-flat"), "", "b" * 40,
                    "new-baseline", switch=True,
                )
                self.assertFalse(result["switch_performed"])
            self.assertEqual(
                os.path.realpath(os.path.join(publish, "CURRENT")),
                os.path.realpath(release),
            )


if __name__ == "__main__":
    unittest.main()
