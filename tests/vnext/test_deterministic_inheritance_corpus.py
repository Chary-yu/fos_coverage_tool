import os
import unittest

from scripts.diagnostics.deterministic_inheritance_corpus import (
    DEFAULT_FIXTURE, derived_reports, run,
)


class DeterministicInheritanceCorpusTest(unittest.TestCase):
    def test_local_corpus_has_no_known_false_positive(self):
        result = run(DEFAULT_FIXTURE)
        self.assertEqual(result["status"], "PASSED")
        self.assertTrue(result["synthetic"])
        self.assertFalse(result["release_eligible"])
        self.assertEqual(result["failed_cases"], 0)
        reports = derived_reports(result)
        self.assertEqual(
            reports["false_positive_check"]["known_false_positive_count"], 0
        )
        self.assertEqual(reports["parser_uncertainty_report"]["status"], "PASSED")
        self.assertEqual(reports["dependency_resolution_report"]["status"], "PASSED")

    def test_fixture_is_inside_repository_and_repeated_result_is_identical(self):
        self.assertTrue(os.path.isfile(DEFAULT_FIXTURE))
        self.assertEqual(run(DEFAULT_FIXTURE), run(DEFAULT_FIXTURE))


if __name__ == "__main__":
    unittest.main()
