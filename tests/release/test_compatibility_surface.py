import os
import subprocess
import tempfile
import unittest


class CompatibilitySurfaceTest(unittest.TestCase):
    @staticmethod
    def _commit(root, message):
        subprocess.check_call(["git", "add", "."], cwd=root)
        subprocess.check_call(["git", "commit", "-q", "-m", message], cwd=root)
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root
        ).decode("ascii").strip()

    @classmethod
    def _repo(cls, baseline_files, changed_files):
        root = tempfile.mkdtemp(prefix="compatibility-surface-")
        subprocess.check_call(["git", "init", "-q"], cwd=root)
        subprocess.check_call(
            ["git", "config", "user.email", "compatibility@example.invalid"],
            cwd=root,
        )
        subprocess.check_call(
            ["git", "config", "user.name", "Compatibility Surface Test"],
            cwd=root,
        )
        for relative, contents in baseline_files.items():
            path = os.path.join(root, *relative.split("/"))
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(contents)
        baseline = cls._commit(root, "baseline")
        for relative, contents in changed_files.items():
            path = os.path.join(root, *relative.split("/"))
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(contents)
        source = cls._commit(root, "candidate")
        return root, baseline, source

    def test_changed_production_python_is_classified_without_db_surface(self):
        from scripts.release.compatibility_surface import (
            classify_compatibility_surface,
        )

        root, baseline, source = self._repo(
            {"app/release_trust_policy.py": "MODE = 'legacy'\n"},
            {"app/release_trust_policy.py": "MODE = 'offline_operator'\n"},
        )
        try:
            result = classify_compatibility_surface(root, source, baseline)
        finally:
            import shutil
            shutil.rmtree(root)
        self.assertEqual(result["python"]["status"], "PRODUCTION_PYTHON_CHANGED")
        self.assertEqual(
            result["python"]["changed_paths"], ["app/release_trust_policy.py"]
        )
        self.assertEqual(result["mariadb"]["status"], "NOT_DB_SENSITIVE")
        self.assertEqual(result["mariadb"]["changed_paths"], [])

    def test_documentation_only_change_has_no_compatibility_surface(self):
        from scripts.release.compatibility_surface import (
            classify_compatibility_surface,
        )

        root, baseline, source = self._repo(
            {"README.md": "baseline\n"},
            {"README.md": "candidate\n"},
        )
        try:
            result = classify_compatibility_surface(root, source, baseline)
        finally:
            import shutil
            shutil.rmtree(root)
        self.assertEqual(result["python"]["status"], "NOT_PRODUCTION_PYTHON")
        self.assertEqual(result["mariadb"]["status"], "NOT_DB_SENSITIVE")
        self.assertEqual(result["changed_paths"], ["README.md"])

    def test_sql_and_db_module_change_is_db_sensitive(self):
        from scripts.release.compatibility_surface import (
            classify_compatibility_surface,
        )

        root, baseline, source = self._repo(
            {"app/db/manager.py": "return connection\n"},
            {"app/db/manager.py": "return connection.cursor()\n"},
        )
        try:
            result = classify_compatibility_surface(root, source, baseline)
        finally:
            import shutil
            shutil.rmtree(root)
        self.assertEqual(result["mariadb"]["status"], "DB_SENSITIVE_CHANGED")
        self.assertEqual(result["mariadb"]["changed_paths"], ["app/db/manager.py"])

    def test_sql_content_in_non_db_path_is_db_sensitive(self):
        from scripts.release.compatibility_surface import (
            classify_compatibility_surface,
        )

        root, baseline, source = self._repo(
            {"scripts/release/build_release.py": "def build():\n    return True\n"},
            {"scripts/release/build_release.py": "def build():\n    return 'GRANT SELECT'\n"},
        )
        try:
            result = classify_compatibility_surface(root, source, baseline)
        finally:
            import shutil
            shutil.rmtree(root)
        self.assertEqual(result["python"]["status"], "PRODUCTION_PYTHON_CHANGED")
        self.assertEqual(result["mariadb"]["status"], "DB_SENSITIVE_CHANGED")

    def test_compatibility_tool_sql_is_not_production_db_surface(self):
        from scripts.release.compatibility_surface import (
            classify_compatibility_surface,
        )

        root, baseline, source = self._repo(
            {"scripts/release/local_compatibility_gate.py":
             "def check():\n    return True\n"},
            {"scripts/release/local_compatibility_gate.py":
             "def check():\n    return 'GRANT SELECT'\n"},
        )
        try:
            result = classify_compatibility_surface(root, source, baseline)
        finally:
            import shutil
            shutil.rmtree(root)
        self.assertEqual(result["python"]["status"], "NOT_PRODUCTION_PYTHON")
        self.assertEqual(result["mariadb"]["status"], "NOT_DB_SENSITIVE")

    def test_documentation_about_mariadb_is_not_db_surface(self):
        from scripts.release.compatibility_surface import (
            classify_compatibility_surface,
        )

        root, baseline, source = self._repo(
            {"docs/release.md": "baseline\n"},
            {"docs/release.md": "MariaDB GRANT/REVOKE compatibility notes\n"},
        )
        try:
            result = classify_compatibility_surface(root, source, baseline)
        finally:
            import shutil
            shutil.rmtree(root)
        self.assertEqual(result["mariadb"]["status"], "NOT_DB_SENSITIVE")


if __name__ == "__main__":
    unittest.main()
