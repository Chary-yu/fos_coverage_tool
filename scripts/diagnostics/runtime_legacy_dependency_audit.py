"""Audit whether VNext app runtime still imports root legacy business modules."""

import json
import os
import re
import sys


PATTERN = re.compile(
    r"^\s*(?:from|import)\s+(enhance_coverage|coverage_check|"
    r"code_detail_service|source_reader|code_region)\b",
    re.MULTILINE,
)


def audit(repo_root):
    findings = []
    app_root = os.path.join(repo_root, "app")
    for dirpath, _, filenames in os.walk(app_root):
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, "r", encoding="utf-8") as stream:
                text = stream.read()
            for match in PATTERN.finditer(text):
                findings.append({
                    "file": os.path.relpath(path, repo_root),
                    "module": match.group(1),
                    "line": text[:match.start()].count("\n") + 1,
                })
    return {
        "status": "PASSED" if not findings else "FAILED",
        "evidence_class": "architecture_audit",
        "legacy_imports": findings,
        "is_valid": not findings,
    }


if __name__ == "__main__":
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    result = audit(root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["status"] == "PASSED" else 1)
