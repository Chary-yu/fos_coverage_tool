import json
import os
import tempfile
import unittest

from app.config.runtime_config import load_application_config


class RuntimeConfigTest(unittest.TestCase):
    def test_candidate_roots_are_resolved_relative_to_their_declared_base(self):
        with tempfile.TemporaryDirectory(prefix="vnext-config-") as root:
            path = os.path.join(root, "coverage.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "runtime_mode": "vnext",
                    "schema_version": 1,
                    "runtime_state": {
                        "root": "state",
                        "exports_dir": "exports",
                    },
                    "input_roots": ["inputs"],
                    "report_roots": ["reports"],
                }, stream)

            config = load_application_config(path, base_dir=root)

            self.assertEqual(
                config["runtime_state"]["exports_dir"],
                os.path.realpath(os.path.join(root, "state", "exports")),
            )
            self.assertEqual(
                config["input_roots"],
                [os.path.realpath(os.path.join(root, "inputs"))],
            )
            self.assertEqual(
                config["report_roots"],
                [os.path.realpath(os.path.join(root, "reports"))],
            )


if __name__ == "__main__":
    unittest.main()
