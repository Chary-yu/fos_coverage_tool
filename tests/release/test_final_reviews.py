import os
import subprocess
import tempfile
import unittest

from scripts.diagnostics.final_security_review import audit as audit_security
from scripts.diagnostics.final_source_review import (
    _audit_release_identity,
    audit as audit_source,
)


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
        release_check = next(
            item for item in result["checks"]
            if item["name"] == "release_identity"
        )
        self.assertIn(release_check["status"], ("FAILED", "INCOMPLETE"))
        self.assertIsInstance(release_check["result"], dict)

    def test_source_review_reports_missing_release_assets_structurally(self):
        with tempfile.TemporaryDirectory(prefix="source-review-missing-") as root:
            subprocess.check_call(["git", "init", "-q", root])
            path = os.path.join(root, "tracked.txt")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("one\n")
            subprocess.check_call(["git", "-C", root, "add", "tracked.txt"])
            subprocess.check_call([
                "git", "-C", root, "-c", "user.name=Audit", "-c",
                "user.email=audit@example.invalid", "commit", "-q", "-m", "fixture",
            ])
            result = audit_source(root)

        self.assertEqual(result["status"], "INCOMPLETE")
        release_check = next(
            item for item in result["checks"]
            if item["name"] == "release_identity"
        )
        self.assertEqual(release_check["status"], "FAILED")
        self.assertTrue(any("required release asset" in item
                            for item in release_check["result"]["violations"]))

    def test_complete_release_asset_tree_passes_release_identity_check(self):
        from app.release_identity import DEFAULT_RELEASE_ASSET_RELATIVE_PATHS

        with tempfile.TemporaryDirectory(prefix="source-review-complete-") as root:
            subprocess.check_call(["git", "init", "-q", root])
            for relative in DEFAULT_RELEASE_ASSET_RELATIVE_PATHS:
                path = os.path.join(root, *relative.split("/"))
                directory = os.path.dirname(path)
                if directory and not os.path.isdir(directory):
                    os.makedirs(directory)
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(relative + "\n")
            subprocess.check_call(["git", "add", "."], cwd=root)
            subprocess.check_call([
                "git", "-C", root, "-c", "user.name=Audit", "-c",
                "user.email=audit@example.invalid", "commit", "-q", "-m", "fixture",
            ])
            result = _audit_release_identity(root)

        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["identity"]["asset_count"],
                         len(DEFAULT_RELEASE_ASSET_RELATIVE_PATHS))


if __name__ == "__main__":
    unittest.main()
