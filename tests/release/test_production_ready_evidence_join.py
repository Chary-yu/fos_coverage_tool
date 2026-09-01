import unittest

from scripts.diagnostics.production_ready_evidence_join import (
    backup_identity, browser_identity, candidate_build_identity,
    join_identities, performance_identity,
)


class ProductionReadyEvidenceJoinTest(unittest.TestCase):
    def _browser(self, commit="a" * 40, artifact="b" * 64,
                 served="c" * 64, session="attempt-1"):
        return {
            "status": "PASSED",
            "synthetic": False,
            "release_eligible": True,
            "candidate_revision": commit,
            "observed_publication": {
                "commit_sha": commit,
                "candidate_artifact_sha256": artifact,
                "served_root_sha256": served,
                "release_validation_session_id": session,
            },
        }

    def _performance(self, commit="a" * 40, artifact="b" * 64,
                     served="c" * 64, session="attempt-1"):
        return {
            "status": "PASSED",
            "synthetic": False,
            "release_eligible": True,
            "candidate_commit": commit,
            "candidate_artifact_sha256": artifact,
            "served_root_sha256": served,
            "release_validation_session_id": session,
        }

    def _backup(self, commit="a" * 40):
        return {
            "status": "PASSED",
            "synthetic": False,
            "candidate_revision": commit,
        }

    def test_join_requires_one_exact_candidate_publication(self):
        browser = browser_identity(self._browser())
        performance = performance_identity(self._performance())
        result = join_identities(
            candidate_build_identity("a" * 40, "b" * 64),
            browser, performance, backup_identity(self._backup()),
        )
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["identity"]["release_validation_session_id"], "attempt-1")

    def test_join_rejects_mixed_candidate_artifacts(self):
        browser = browser_identity(self._browser())
        performance = performance_identity(self._performance(artifact="d" * 64))
        with self.assertRaisesRegex(ValueError, "candidate_artifact_sha256"):
            join_identities(
                candidate_build_identity("a" * 40, "b" * 64),
                browser, performance, backup_identity(self._backup()),
            )

    def test_validation_fixture_can_never_supply_production_ready_build_identity(self):
        with self.assertRaisesRegex(ValueError, "validation fixture"):
            candidate_build_identity(
                "a" * 40, "b" * 64,
                artifact_role="validation_fixture",
                production_publishable=False,
                project_name="Coverage Candidate",
            )

    def test_browser_identity_requires_observed_not_expected_only_binding(self):
        payload = self._browser()
        payload.pop("observed_publication")
        payload["release_validation_session_id"] = "attempt-1"
        payload["candidate_artifact_sha256"] = "b" * 64
        payload["served_root_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "observed publication"):
            browser_identity(payload)


if __name__ == "__main__":
    unittest.main()
