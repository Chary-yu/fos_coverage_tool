import unittest

from scripts.upgrade.run_rollback_rehearsal import (
    RELEASE_IDENTITY_FIELDS,
    release_identity_matches,
)


class RollbackRehearsalContractTest(unittest.TestCase):
    def setUp(self):
        self.identity = {
            "version": "v11.7",
            "commit_sha": "target",
            "build_id": "target-build",
            "asset_hash": "asset-target",
            "schema_version": 2,
            "asset_manifest_version": 1,
            "asset_count": 1,
            "asset_manifest_hash": "asset-target",
            "asset_manifest": [{
                "path": "coverage_progress.js", "size": 1, "sha256": "asset-target",
            }],
        }

    def test_rollback_identity_requires_every_release_field(self):
        self.assertTrue(release_identity_matches(self.identity, dict(self.identity)))
        for field in RELEASE_IDENTITY_FIELDS:
            changed = dict(self.identity)
            changed[field] = "different"
            self.assertFalse(release_identity_matches(changed, self.identity))

    def test_missing_release_field_fails_closed(self):
        incomplete = dict(self.identity)
        incomplete.pop("asset_hash")
        self.assertFalse(release_identity_matches(incomplete, self.identity))


if __name__ == "__main__":
    unittest.main()
