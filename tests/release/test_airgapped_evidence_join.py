import json
import os
import tempfile
import unittest

from scripts.upgrade.airgapped_evidence_join import build_join, sha256_file


class AirgappedEvidenceJoinTest(unittest.TestCase):
    def _write(self, root, name, value):
        path = os.path.join(root, name)
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
        return path

    def _fixture(self, root):
        revision = '1' * 40
        baseline = '0' * 40
        session = 'r8-session'
        artifact = '2' * 64
        served = '3' * 64
        url = 'http://10.190.162.33:19529/coverage/report.html'
        env = {'browser_name': 'chromium', 'browser_version': 'fixture'}
        candidate_source = self._write(root, 'candidate.json', {
            'status': 'PASSED', 'evidence_class': 'release_performance_revision',
            'comparison_type': 'single_revision', 'revision': revision,
            'workload_id': 'w1', 'workload_hash': 'h1',
            'environment_identity': env, 'exit_code': 0,
            'coverage_virtual_scroll_100k': {'status': 'PASSED', 'elapsed_ms': 10.0},
            'tiers': {
                name: {'status': 'PASSED', 'measured_ms': value}
                for name, value in (
                    ('Tier_A_1k', 1.0), ('Tier_B_10k', 2.0),
                    ('Tier_C_50k', 3.0), ('Tier_D_100k', 4.0))
            },
        })
        observation = self._write(root, 'observation.json', {
            'status': 'PASSED',
            'evidence_class': 'airgapped_real_candidate_browser_observation',
            'real_http': True, 'synthetic': False, 'release_eligible': False,
            'requires_performance_join': True, 'candidate_revision': revision,
            'release_validation_session_id': session,
            'candidate_artifact_sha256': artifact, 'served_root_sha256': served,
            'candidate_url': url, 'report_path': 'reports/report.html',
            'report_sha256': '4' * 64,
            'release_identity': {'commit_sha': revision},
            'browser_functional': {'status': 'PASSED'},
        })
        auth = self._write(root, 'auth.json', {
            'status': 'PASSED', 'evidence_class': 'real_candidate_authenticated_mutation',
            'release_eligible': True, 'real_http': True, 'synthetic': False,
            'candidate_revision': revision, 'release_validation_session_id': session,
            'candidate_artifact_sha256': artifact, 'served_root_sha256': served,
            'candidate_url': url,
        })
        performance = self._write(root, 'performance.json', {
            'status': 'PASSED', 'evidence_class': 'release_performance_ab',
            'comparison_type': 'release_revision_ab', 'synthetic': False,
            'exit_code': 0, 'candidate_commit': revision,
            'baseline_commit': baseline, 'release_validation_session_id': session,
            'candidate_artifact_sha256': artifact, 'served_root_sha256': served,
            'workload_id': 'w1', 'workload_hash': 'h1',
            'coverage_virtual_scroll_100k': {'status': 'PASSED'},
            'source_artifacts': {
                'candidate': {'path': candidate_source,
                              'sha256': sha256_file(candidate_source),
                              'revision': revision},
            },
        })
        return observation, auth, performance, {
            'revision': revision, 'session_id': session,
            'candidate_artifact_sha256': artifact,
            'served_root_sha256': served, 'candidate_url': url,
        }

    def test_join_requires_all_three_exact_identities(self):
        with tempfile.TemporaryDirectory() as root:
            observation, auth, performance, expected = self._fixture(root)
            browser = os.path.join(root, 'browser.json')
            workload = os.path.join(root, 'workload.json')
            result = build_join(observation, auth, performance, workload, browser, expected)
            self.assertEqual('PASSED', result['status'])
            with open(browser, encoding='utf-8') as stream:
                payload = json.load(stream)
            self.assertTrue(payload['release_eligible'])
            self.assertEqual('real_http_chromium_browser', payload['evidence_class'])
            self.assertEqual('chromium', payload['coverage_virtual_scroll_100k']['environment_identity']['browser_name'])

    def test_join_rejects_session_drift(self):
        with tempfile.TemporaryDirectory() as root:
            observation, auth, performance, expected = self._fixture(root)
            with open(auth, encoding='utf-8') as stream:
                payload = json.load(stream)
            payload['release_validation_session_id'] = 'wrong'
            with open(auth, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream)
            with self.assertRaisesRegex(RuntimeError, 'release validation session identity mismatch'):
                build_join(observation, auth, performance,
                           os.path.join(root, 'workload.json'),
                           os.path.join(root, 'browser.json'), expected)

    def test_join_rejects_operator_observation_promoted_directly(self):
        with tempfile.TemporaryDirectory() as root:
            observation, auth, performance, expected = self._fixture(root)
            with open(observation, encoding='utf-8') as stream:
                payload = json.load(stream)
            payload['release_eligible'] = True
            with open(observation, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream)
            with self.assertRaisesRegex(RuntimeError, 'invalid evidence semantics'):
                build_join(observation, auth, performance,
                           os.path.join(root, 'workload.json'),
                           os.path.join(root, 'browser.json'), expected)


if __name__ == '__main__':
    unittest.main()
