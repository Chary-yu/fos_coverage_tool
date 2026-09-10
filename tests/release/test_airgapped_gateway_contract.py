import unittest
from unittest import mock

from scripts.upgrade import run_upgrade as core
from scripts.upgrade import airgapped_gateway_contract as contract


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


if __name__ == '__main__':
    unittest.main()
