"""Measure LCOV parser and coverage-chunk behavior at large file sizes.

This is an intentionally synthetic benchmark.  It records parser RSS, parser
time, and the latency of converting one large LCOV record into the bounded
coverage-import chunks.  It is not release evidence and does not change the
production parser or database.
"""

from __future__ import print_function

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.incremental.lcov import iter_info_records
from app.inject.service import ScanImportService
from app.scan_import.coordinator import ScanImportCoordinator


def _peak_rss_bytes():
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Linux reports KiB; macOS reports bytes.
    return value * 1024 if sys.platform.startswith("linux") else value


def _write_lcov(path, line_count):
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("TN:\nSF:src/benchmark-large.c\n")
        for line_number in range(1, int(line_count) + 1):
            stream.write("DA:{},0\n".format(line_number))
        stream.write("end_of_record\n")


def _benchmark_case(line_count, max_lines=20000, max_est_bytes=16 * 1024 * 1024):
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="lcov-parser-benchmark-") as root:
        info_path = os.path.join(root, "large.info")
        _write_lcov(info_path, line_count)
        input_bytes = os.path.getsize(info_path)

        parse_started = time.perf_counter()
        parsed_files = 0
        parsed_lines = 0
        for record in iter_info_records(info_path):
            parsed_files += 1
            parsed_lines += len(record.get("lines") or {})
        parse_ms = (time.perf_counter() - parse_started) * 1000.0

        service = ScanImportService()
        _, records, stats = service.iter_info_file(info_path)
        chunk_started = time.perf_counter()
        batch_count = 0
        chunk_count = 0
        chunk_lines = 0
        for batch in ScanImportCoordinator._iter_coverage_batches(
                records, max_files=128, max_lines=max_lines,
                max_est_bytes=max_est_bytes):
            batch_count += 1
            chunk_count += len(batch)
            for item in batch:
                metadata = item.get("_coverage_chunk") or {}
                chunk_lines += int(metadata.get("line_count") or 0)
        chunk_ms = (time.perf_counter() - chunk_started) * 1000.0

        return {
            "line_count_requested": int(line_count),
            "input_bytes": int(input_bytes),
            "parsed_files": parsed_files,
            "parsed_lines": parsed_lines,
            "chunk_batches": batch_count,
            "coverage_chunks": chunk_count,
            "chunk_lines": chunk_lines,
            "parser_time_ms": round(parse_ms, 3),
            "chunk_generation_time_ms": round(chunk_ms, 3),
            "peak_rss_bytes": _peak_rss_bytes(),
            "stats": stats,
            "status": "MEASURED",
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }


def _run_case_isolated(line_count, max_lines, max_est_bytes):
    output = subprocess.check_output([
        sys.executable, os.path.abspath(__file__),
        "--case-lines", str(int(line_count)),
        "--max-lines", str(int(max_lines)),
        "--max-est-bytes", str(int(max_est_bytes)),
    ], cwd=ROOT)
    return json.loads(output.decode("utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark one large LCOV record at 100k/500k/1m DA lines. "
            "Results are synthetic and not release evidence."
        )
    )
    parser.add_argument(
        "--lines", default="100000,500000,1000000",
        help="comma-separated line counts for isolated benchmark cases",
    )
    parser.add_argument("--max-lines", type=int, default=20000)
    parser.add_argument("--max-est-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--case-lines", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--output",
        help="optional JSON output path; omitted means stdout only",
    )
    args = parser.parse_args(argv)

    if args.case_lines is not None:
        print(json.dumps(_benchmark_case(
            args.case_lines, args.max_lines, args.max_est_bytes,
        ), sort_keys=True))
        return 0

    line_counts = [int(item.strip()) for item in args.lines.split(",") if item.strip()]
    result = {
        "evidence_class": "synthetic_benchmark",
        "synthetic": True,
        "release_eligible": False,
        "workload_id": "lcov-parser-large-record-v1",
        "chunk_budget": {
            "max_lines": int(args.max_lines),
            "max_est_bytes": int(args.max_est_bytes),
        },
        "cases": [
            _run_case_isolated(count, args.max_lines, args.max_est_bytes)
            for count in line_counts
        ],
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = os.path.abspath(args.output)
        directory = os.path.dirname(output)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
