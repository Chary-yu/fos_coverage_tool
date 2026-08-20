"""Exercise the Scan construction/seal barrier against a real SQLite schema."""

import json
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.db.repositories import LineIndexRepository, ProjectRepository
from app.services.project_service import ProjectService
from scripts.upgrade.migration_runner import create_sqlite_schema
try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract


def audit():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        create_sqlite_schema(connection)
        service = ProjectService()
        scan = service.create_scan_and_ingest(
            connection, "immutability-audit", [{
                "repository_name": "repo-a",
                "file_path": "src/a.c",
                "file_path_hash": "a" * 32,
                "lines": [{"line_number": 1, "line_text": "return 0;",
                           "coverage_state": "uncovered"}],
            }], info_sha256="a" * 64,
        )
        failures = []
        projects = ProjectRepository()
        lines = LineIndexRepository()
        try:
            projects.ensure_file(connection, scan["id"], "repo-a", "a" * 32,
                                 "src/changed.c", "changed.c")
        except ValueError:
            pass
        else:
            failures.append("sealed scan accepted a file identity mutation")
        file_id = connection.execute(
            "SELECT id FROM coverage_files WHERE scan_id = ?", (scan["id"],)
        ).fetchone()[0]
        try:
            lines.upsert_line(connection, file_id, {
                "line_number": 1, "line_text": "changed", "coverage_state": "covered",
            })
        except ValueError:
            pass
        else:
            failures.append("sealed scan accepted a physical line mutation")
        status = connection.execute(
            "SELECT status FROM coverage_scans WHERE id = ?", (scan["id"],)
        ).fetchone()[0]
        if status not in ("ready", "sealed"):
            failures.append("scan was not sealed: {}".format(status))
        return with_contract({"status": "PASSED" if not failures else "FAILED",
                "evidence_class": "runtime_audit", "scan_status": status,
                "violations": failures})
    finally:
        connection.close()


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
