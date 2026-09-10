import json
import os
import tempfile
import unittest
from unittest import mock

from scripts.upgrade import run_upgrade as core
from scripts.upgrade import airgapped_gateway_contract as contract
from scripts.upgrade.run_airgapped_manifest_upgrade import _normalized_private_config


class AirgappedGatewayContractTest(unittest.TestCase):
    def tearDown(self):
        contract.restore_canonical_gateway_contract()

    def test_origin_only_contract_is_accepted_for_airgapped_preflight(self):
        self.assertEqual(
            [],
            contract._validate_airgapped_browser_contract(
                'http://10.190.162.33:19529'
            ),
        )

    def test_origin_contract_rejects_loopback(self):
        errors = contract._validate_airgapped_browser_contract(
            'http://127.0.0.1:19529'
        )
        self.assertTrue(errors)

    def test_real_candidate_url_must_be_html_on_same_origin(self):
        origin = 'http://10.190.162.33:19529'
        self.assertTrue(contract._real_candidate_html_url(
            origin + '/coverage/FOS_V6R2/report.gcov.html', origin
        ))
        self.assertFalse(contract._real_candidate_html_url(
            'http://10.190.162.34:19529/coverage/report.gcov.html', origin
        ))
        self.assertFalse(contract._real_candidate_html_url(
            origin + '/api/coverage/release', origin
        ))

    def test_browser_wrapper_delegates_using_observed_manifest_url(self):
        origin = 'http://10.190.162.33:19529'
        observed = origin + '/coverage/actual/report.gcov.html'
        payload = {'candidate_url': observed}
        with mock.patch.object(
                contract, '_ORIGINAL_VALIDATE_BROWSER_EVIDENCE',
                return_value=([], {'status': 'PASSED'})) as validator:
            errors, normalized = contract._validate_airgapped_browser_evidence(
                '/tmp/browser.json', payload, {'commit_sha': 'a' * 40}, origin,
                expected_session_id='session-1',
                expected_candidate_artifact_sha256='b' * 64,
                expected_served_root_sha256='c' * 64,
            )
        self.assertEqual([], errors)
        self.assertEqual('PASSED', normalized['status'])
        self.assertEqual(observed, validator.call_args.args[3])

    def test_auth_wrapper_rejects_cross_origin_before_canonical_validation(self):
        origin = 'http://10.190.162.33:19529'
        payload = {
            'candidate_url':
                'http://10.190.162.34:19529/coverage/actual/report.gcov.html'
        }
        with mock.patch.object(
                contract, '_ORIGINAL_VALIDATE_AUTH_EVIDENCE') as validator:
            errors, normalized = contract._validate_airgapped_auth_evidence(
                '/tmp/auth.json', payload, {'commit_sha': 'a' * 40}, origin
            )
        self.assertTrue(errors)
        self.assertEqual({}, normalized)
        validator.assert_not_called()

    def test_install_is_process_scoped_and_reversible(self):
        original_browser = core._validate_external_candidate_browser_url
        original_lifecycle = core.VfoswindProductionLifecycle
        contract.install_airgapped_gateway_contract()
        self.assertIs(
            core._validate_external_candidate_browser_url,
            contract._validate_airgapped_browser_contract,
        )
        self.assertIs(
            core.VfoswindProductionLifecycle,
            contract.ManifestDerivedGatewayLifecycle,
        )
        contract.restore_canonical_gateway_contract()
        self.assertIs(core._validate_external_candidate_browser_url, original_browser)
        self.assertIs(core.VfoswindProductionLifecycle, original_lifecycle)

    def test_private_config_replaces_placeholder_report_with_gateway_origin(self):
        origin = 'http://10.190.162.33:19529'
        source = {
            'mysql': {'database': 'coverage', 'user': 'coverage_user'},
            'upgrade': {
                'candidate_browser_url': origin + '/coverage/guessed.html',
                'candidate_gateway_origin': origin,
                'airgapped_operator_browser': {
                    'enabled': True,
                    'candidate_gateway_origin': origin,
                },
                'production_integration': {
                    'candidate_gateway': {
                        'browser_url': origin + '/coverage/guessed.html',
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as root:
            source_path = os.path.join(root, 'source.json')
            with open(source_path, 'w', encoding='utf-8') as stream:
                json.dump(source, stream)
            normalized_path = _normalized_private_config(source_path)
            self.addCleanup(lambda: os.path.exists(normalized_path) and os.remove(normalized_path))
            with open(normalized_path, 'r', encoding='utf-8') as stream:
                normalized = json.load(stream)
            upgrade = normalized['upgrade']
            self.assertEqual(origin, upgrade['candidate_browser_url'])
            self.assertEqual(
                origin,
                upgrade['production_integration']['candidate_gateway']['browser_url'],
            )
            self.assertEqual(
                origin,
                upgrade['production_integration']['candidate_gateway']['origin'],
            )
            self.assertEqual(0o600, os.stat(normalized_path).st_mode & 0o777)


if __name__ == '__main__':
    unittest.main()
