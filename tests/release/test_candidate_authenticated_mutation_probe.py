import json
import unittest
from unittest import mock

from scripts.diagnostics import candidate_authenticated_mutation_probe as probe


class _Response(object):
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def getcode(self):
        return self.status


class CandidateAuthenticatedMutationProbeTest(unittest.TestCase):
    def _run(self, unauthenticated_status):
        responses = [
            _Response(200, {
                "release": {"commit_sha": "a" * 40},
                "publication": {
                    "release_validation_session_id": "attempt-a",
                    "candidate_artifact_sha256": "b" * 64,
                    "served_root_sha256": "c" * 64,
                },
            }),
            _Response(unauthenticated_status, {"error": "forbidden"}),
            _Response(200, {"project": {"project_name": "probe"}}),
        ]

        def fake_urlopen(_request, timeout=20):
            del timeout
            return responses.pop(0)

        with mock.patch.object(probe, "urlopen", fake_urlopen):
            return probe.run_probe(
                "https://candidate.example.invalid/coverage/report.html",
                "https://candidate.example.invalid/api/coverage/release",
                "https://candidate.example.invalid/api/coverage/projects",
                {"project_name": "probe"},
                {"Authorization": "secret-value"},
                "a" * 40, "attempt-a", "b" * 64, "c" * 64,
                "reverse_proxy", "X-Remote-User", "operator-selected-auth",
                "d" * 64,
            )

    def test_positive_and_negative_controls_produce_release_evidence_without_secret(self):
        evidence = self._run(401)
        self.assertEqual(evidence["status"], "PASSED")
        self.assertTrue(evidence["identity_propagated"])
        self.assertEqual(
            evidence["request_headers"], ["Authorization"]
        )
        self.assertNotIn("secret-value", json.dumps(evidence))

    def test_missing_auth_boundary_is_failed(self):
        evidence = self._run(200)
        self.assertEqual(evidence["status"], "FAILED")
        self.assertFalse(evidence["release_eligible"])
        self.assertTrue(any(
            "unauthenticated Candidate mutation" in item
            for item in evidence["violations"]
        ))


if __name__ == "__main__":
    unittest.main()
