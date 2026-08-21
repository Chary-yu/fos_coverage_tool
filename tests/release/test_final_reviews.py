import os
import subprocess
import tempfile
import unittest

from scripts.diagnostics.final_security_review import audit as audit_security
from scripts.diagnostics.final_source_review import audit as audit_source


class FinalReviewAuditTest(unittest.TestCase):
    def test_security_review_records_exact_revision_and_fail_closed_path_checks(self):
        result = audit_security(os.getcwd())
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["candidate_revision"], result["release_identity"]["commit_sha"])
        self.assertTrue(result["checks"]["path_boundary"]["parent_traversal_fail_closed"])
        self.assertTrue(result["checks"]["path_boundary"]["basename_ambiguity_fail_closed"])

    def test_source_review_rejects_a_dirty_exact_checkout(self):
        with tempfile.TemporaryDirectory(prefix="source-review-") as root:
            subprocess.check_call(["git", "init", "-q", root])
            path = os.path.join(root, "tracked.txt")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("one\n")
            subprocess.check_call(["git", "-C", root, "add", "tracked.txt"])
            subprocess.check_call([
                "git", "-C", root, "-c", "user.name=Audit", "-c",
                "user.email=audit@example.invalid", "commit", "-q", "-m", "fixture",
            ])
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("dirty\n")
            result = audit_source(root)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["worktree_clean"])
        self.assertTrue(any("clean worktree" in item for item in result["violations"]))


if __name__ == "__main__":
    unittest.main()
