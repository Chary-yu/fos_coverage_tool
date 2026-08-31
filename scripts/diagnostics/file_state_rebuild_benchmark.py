"""Measure full versus affected-file FileState rebuilds on a synthetic DB.

This benchmark is a local diagnostic only.  It exercises the same strict
ready gate used by mutations, but an in-memory SQLite database is not a
production-equivalent MariaDB workload and the result is never release
evidence by itself.
"""

from __future__ import print_function

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.db.repositories import (  # noqa: E402
    FileStateRepository, LineIndexRepository, ProjectRepository,
    ProjectStateRepository,
)
from app.services.file_state_service import FileStateService  # noqa: E402
from scripts.upgrade.migration_runner import create_sqlite_schema  # noqa: E402


def _median(values):
    return round(float(statistics.median(values)), 3) if values else 0.0


def _fixture(file_count, lines_per_file):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_sqlite_schema(connection)
    projects = ProjectRepository()
    lines = LineIndexRepository()
    states = ProjectStateRepository()
    file_states = FileStateRepository()
    service = FileStateService(file_states, states)
    project = projects.ensure_project(connection, "file-state-benchmark")
    scan = projects.create_scan(
        connection, project["id"], "file-state-benchmark-scan", "import", "full"
    )
    files = []
    for file_index in range(file_count):
        file_row = projects.ensure_file(
            connection, scan["id"], "repo", "{}{:032x}".format("f", file_index),
            "src/benchmark/file-{}.c".format(file_index),
            "file-{}.c".format(file_index),
        )
        files.append(file_row)
        lines.upsert_lines(connection, file_row["id"], [
            {
                "line_number": line_number,
                "line_text": "return {};".format(line_number),
                "coverage_state": "uncovered",
            }
            for line_number in range(1, lines_per_file + 1)
        ])
    states.ensure(
        connection, project["id"], current_scan_id=scan["id"]
    )
    first_state = states.advance(connection, project["id"])
    service.rebuild_validate_and_mark_ready_in_transaction(
        connection, project["id"], scan["id"], first_state["data_version"]
    )
    connection.commit()
    return connection, project, scan, files, service, states


def run_benchmark(file_count=100, lines_per_file=100, iterations=3):
    file_count = max(1, int(file_count))
    lines_per_file = max(1, int(lines_per_file))
    iterations = max(1, int(iterations))
    connection, project, scan, files, service, states = _fixture(
        file_count, lines_per_file
    )
    full_samples = []
    partial_samples = []
    try:
        for _ in range(iterations):
            state = states.advance(connection, project["id"])
            started = time.perf_counter()
            service.rebuild_validate_and_mark_ready_in_transaction(
                connection, project["id"], scan["id"], state["data_version"]
            )
            full_samples.append((time.perf_counter() - started) * 1000.0)
            connection.commit()

        for _ in range(iterations):
            state = states.advance(connection, project["id"])
            started = time.perf_counter()
            service.rebuild_validate_and_mark_ready_in_transaction(
                connection, project["id"], scan["id"], state["data_version"],
                affected_file_ids=[files[0]["id"]],
            )
            partial_samples.append((time.perf_counter() - started) * 1000.0)
            connection.commit()
    finally:
        connection.close()

    full_median = _median(full_samples)
    partial_median = _median(partial_samples)
    return {
        "status": "MEASURED",
        "evidence_class": "synthetic_file_state_rebuild",
        "release_eligible": False,
        "workload": {
            "database": "SQLite in-memory",
            "file_count": file_count,
            "lines_per_file": lines_per_file,
            "total_lines": file_count * lines_per_file,
            "affected_file_count": 1,
            "iterations": iterations,
        },
        "full_ready_gate": {
            "samples_ms": [round(value, 3) for value in full_samples],
            "median_ms": full_median,
        },
        "partial_ready_gate": {
            "samples_ms": [round(value, 3) for value in partial_samples],
            "median_ms": partial_median,
        },
        "partial_to_full_median_ratio": (
            round(partial_median / full_median, 4) if full_median else None
        ),
        "notes": [
            "Both paths execute completeness, pending conservation, and authoritative reconciliation.",
            "Synthetic SQLite timing is diagnostic only; run the production-equivalent MariaDB gate before release.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Measure full versus affected-file FileState rebuilds. "
            "Output is synthetic diagnostic evidence only."
        )
    )
    parser.add_argument("--files", type=int, default=100)
    parser.add_argument("--lines-per-file", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--output",
        default=os.path.join(ROOT, "benchmarks", "file_state_rebuild_baseline.json"),
    )
    args = parser.parse_args(argv)
    result = run_benchmark(args.files, args.lines_per_file, args.iterations)
    output = os.path.abspath(args.output)
    output_dir = os.path.dirname(output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))
    print("Wrote synthetic benchmark to {}".format(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
