import os
import tempfile
import unittest

from scripts.upgrade import run_upgrade
from scripts.upgrade.run_airgapped_upgrade import (
    PAUSE_LOG,
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
        self.assertEqual(
            'http://10.190.162.33:19529/api/coverage/release-validation/page.html',
            _operator_page_url(
                'http://10.190.162.33:19529/coverage/a/b/report.gcov.html'
            ),
        )

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
