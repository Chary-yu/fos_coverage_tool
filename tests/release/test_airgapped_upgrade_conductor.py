import os
import tempfile
import unittest
from unittest import mock

from scripts.upgrade import run_upgrade
from scripts.upgrade.run_airgapped_upgrade import (
    AirGappedUpgradeOrchestrator,
    PAUSE_LOG,
    _external_origin,
    _operator_page_url,
    _resolve_revision_source,
)


class AirgappedUpgradeConductorTest(unittest.TestCase):
    def test_pause_boundary_is_after_candidate_ready_and_before_phase_d(self):
        source_path = os.path.abspath(run_upgrade.__file__)
        with open(source_path, 'r', encoding='utf-8') as stream:
            source = stream.read()
        ready = source.index('self._write_upgrade_state("CANDIDATE_READY")')
        pause = source.index('self.log("{}")'.format(PAUSE_LOG))
        phase_d = source.index('self._phase_d_entered = True')
        self.assertLess(ready, pause)
        self.assertLess(pause, phase_d)

    def test_operator_page_reuses_candidate_gateway_origin(self):
        origin = 'http://10.190.162.33:19529'
        self.assertEqual(
            origin + '/api/coverage/release-validation/page.html',
            _operator_page_url(origin),
        )

    def test_gateway_origin_rejects_report_path(self):
        with self.assertRaisesRegex(
                RuntimeError, 'Candidate Gateway origin must not include a path'):
            _external_origin(
                'http://10.190.162.33:19529/coverage/a/b/report.gcov.html'
            )

    def test_flat_cutover_rechecks_deterministic_source_binding(self):
        orchestrator = AirGappedUpgradeOrchestrator(repo_root=os.getcwd())
        orchestrator._deployment_layout = run_upgrade.FLAT
        orchestrator._current_adoption_plan = {"status": "PASSED"}
        flat_binding = {
            "previous_release_commit_sha": "a" * 40,
            "legacy_source_root_realpath": "/srv/legacy-flat",
            "legacy_source_tree_sha256": "3" * 64,
            "legacy_source_file_count": 7,
            "legacy_source_total_size": 1234,
            "legacy_release_identity_sha256": "4" * 64,
        }
        orchestrator.candidate_preflight = {
            "source_provenance": dict(flat_binding)
        }
        orchestrator.publisher = mock.Mock()
        orchestrator.publisher.validate_current.return_value = {
            "status": "PASSED"
        }
        orchestrator.publisher.current_session_id.return_value = \
            "baseline-session"
        orchestrator.manifest = mock.Mock()
        previous = {"commit_sha": "a" * 40}
        upgrade = {
            "flat_current_adoption_on_cutover": True,
            "flat_release_identity_path": "/srv/identity.json",
            "flat_served_root": "/srv/legacy-flat",
            "flat_baseline_session_id": "baseline-session",
        }
        current_binding = {
            "previous_release_commit_sha": "a" * 40,
            "served_root_tree_sha256": "9" * 64,
            "served_root_identity_sha256": "8" * 64,
        }
        with mock.patch(
                "scripts.upgrade.run_airgapped_upgrade.core.bootstrap_flat_current",
                return_value={"status": "PASSED"}), mock.patch(
                "scripts.upgrade.run_airgapped_upgrade.core.current_served_root_binding",
                return_value=current_binding), mock.patch(
                "scripts.upgrade.run_airgapped_upgrade.current_flat_source_binding",
                return_value=flat_binding) as flat_probe:
            result = orchestrator._ensure_flat_current_baseline(
                upgrade, previous
            )
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(
            orchestrator._deployment_layout, run_upgrade.IMMUTABLE_CURRENT
        )
        flat_probe.assert_called_once_with(
            "/srv/legacy-flat", "/srv/identity.json", "a" * 40
        )
        self.assertEqual(
            orchestrator.candidate_preflight["current_served_root_binding"],
            current_binding,
        )
        orchestrator.manifest.record.assert_called_once()

    def test_revision_source_is_exact_placeholder_and_must_exist(self):
        revision = 'a' * 40
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, 'perf-{}.json'.format(revision))
            with open(target, 'w', encoding='utf-8') as stream:
                stream.write('{}')
            self.assertEqual(
                os.path.realpath(target),
                _resolve_revision_source(
                    root, 'perf-{commit_sha}.json', revision, 'performance source'
                ),
            )
            with self.assertRaisesRegex(RuntimeError, 'regular pre-staged file'):
                _resolve_revision_source(
                    root, 'missing-{commit_sha}.json', revision,
                    'performance source',
                )


if __name__ == '__main__':
    unittest.main()
