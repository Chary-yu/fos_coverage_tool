"""Canonical inject parsing and Scan import services."""

import hashlib
import os
from bisect import bisect_right

from app.inject.parse_once import parse_gcov_source_once
from app.incremental.lcov import load_info
from app.services.project_service import ProjectService
from app.config.path_policy import realpath_within, reject_relative_traversal


class InjectService:
    parse_once = staticmethod(parse_gcov_source_once)


class ScanImportService(object):
    """Create one immutable Scan and populate its physical line identities."""

    def __init__(self, project_service=None, report_registry=None,
                 allowed_info_roots=None, allowed_report_roots=None):
        self.projects = project_service or ProjectService()
        self.report_registry = report_registry
        self.allowed_info_roots = [os.path.realpath(root) for root in (allowed_info_roots or [])]
        self.allowed_report_roots = [os.path.realpath(root) for root in
                                     (allowed_report_roots or [])]

    def import_info(self, connection, project_name, info_path, review_scope="full",
                    repositories=None, report=None, info_file_name=""):
        info_path = self._validate_info_path(info_path)
        digest = hashlib.sha256()
        with open(info_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        info_sha256 = digest.hexdigest()
        parsed = load_info(info_path)
        files = self.build_files(parsed, repositories or [])
        scan = self.projects.create_scan_and_ingest(
            connection, project_name, files,
            info_file_name=info_file_name or os.path.basename(info_path),
            info_sha256=info_sha256, review_scope=review_scope,
            repositories=repositories or [], report=report,
        )
        if report and self.report_registry:
            report_root = report.get("report_root") or ""
            directories = report.get("directories") or ([report_root] if report_root else [])
            self.report_registry.register(
                report["report_id"], directories,
                sidecar_required=bool(report.get("sidecar_required", True)),
                report_root=report_root,
                scan_id=scan["id"],
                source_signature=report.get("source_signature", ""),
            )
        return {
            "scan": scan, "files": len(files),
            "line_count": sum(len(item["lines"]) for item in files),
            "function_range_fallback_files": sum(
                1 for item in parsed.values() if item.get("function_range_fallback")
            ),
        }

    def _validate_info_path(self, info_path):
        reject_relative_traversal(info_path)
        info_path = os.path.realpath(info_path)
        if self.allowed_info_roots and not realpath_within(info_path, self.allowed_info_roots):
            raise ValueError("info_path is outside configured input roots")
        if not os.path.isfile(info_path):
            raise FileNotFoundError(info_path)
        return info_path

    def parse_info_file(self, info_path, repositories=None, expected_sha256="",
                        verify=False):
        """Parse a trusted staged artifact into immutable physical line facts."""
        info_path = os.path.realpath(str(info_path or ""))
        if not os.path.isfile(info_path):
            raise FileNotFoundError(info_path)
        digest = None
        if verify:
            digest = hashlib.sha256()
            with open(info_path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            observed = digest.hexdigest()
            expected = str(expected_sha256 or "").strip().lower()
            if expected and observed.lower() != expected:
                raise ValueError("STAGED_ARTIFACT_CHANGED")
        parsed = load_info(info_path)
        return (digest.hexdigest() if digest is not None else
                str(expected_sha256 or "")), parsed, self.build_files(parsed, repositories or [])

    def build_files(self, parsed, repositories=None):
        files = []
        for file_path, item in parsed.items():
            normalized = str(file_path or "").replace("\\", "/")
            repository_name, normalized = self._repository_name(
                normalized, repositories or []
            )
            if normalized.startswith("/") or ":" in normalized:
                raise ValueError(
                    "LCOV path is not safely namespaced by one repository: {}".format(file_path)
                )
            ranges = item.get("function_ranges") or []
            fallback = bool(item.get("function_range_fallback"))
            line_records = []
            sorted_lines = sorted(item.get("lines", {}).items())
            function_lookup = self._function_lookup(ranges) if not fallback else None
            for line_number, execution_count in sorted_lines:
                function = (function_lookup(line_number)
                            if function_lookup is not None else None)
                line_records.append({
                    "line_number": line_number,
                    "line_text": "",
                    "coverage_state": "uncovered" if int(execution_count) == 0 else "covered",
                    "block_start_line": function["start_line"] if function else line_number,
                    "block_end_line": function["end_line"] if function else line_number,
                    "block_type": "function" if function else "single",
                    "function_name": function.get("name", "") if function else "",
                    "function_hash": "",
                    "code_line_hash": "",
                    "code_occurrence": 1,
                })
            files.append({
                "repository_name": repository_name,
                "file_path": normalized,
                "file_path_hash": hashlib.md5(
                    "{}\0{}".format(repository_name, normalized).encode("utf-8")
                ).hexdigest(),
                "source_file_name": os.path.basename(normalized),
                "lines": line_records,
            })
        return files

    @staticmethod
    def _repository_name(path, repositories):
        matches = []
        for item in repositories:
            name = str(item.get("repository_name") or "")
            root = str(item.get("repository_path") or "").replace("\\", "/").rstrip("/")
            if root and (path == root or path.startswith(root + "/")):
                matches.append((name, root))
        if len(matches) == 1:
            name, root = matches[0]
            if root and path.startswith(root + "/"):
                path = path[len(root) + 1:]
            elif root and path == root:
                path = os.path.basename(path)
            return name, path
        if len(repositories) > 1:
            raise ValueError(
                "LCOV path cannot be assigned uniquely to a repository: {}".format(path)
            )
        if len(repositories) == 1:
            item = repositories[0] or {}
            name = str(item.get("repository_name") or "")
            if name:
                return name, path
        return "", path

    @staticmethod
    def _function_for_line(ranges, line_number):
        """Resolve one line without making the normal path O(lines*functions).

        LCOV ranges are expected to be non-overlapping after parser
        validation.  Keep the historical first-match behaviour for a direct
        compatibility call, but make malformed overlaps fail closed when the
        bulk lookup is built.  The bulk resolver uses a monotonic sweep, so
        sorted line numbers and ranges are O(L+F).
        """
        lookup = ScanImportService._function_lookup(ranges)
        return lookup(line_number) if lookup is not None else None

    @staticmethod
    def _function_lookup(ranges):
        normalized = []
        for index, item in enumerate(ranges or []):
            try:
                start = int(item.get("start_line", 0))
                end = int(item.get("end_line", 0))
            except (AttributeError, TypeError, ValueError):
                return None
            if start < 1 or end < start:
                return None
            normalized.append((start, end, index, item))
        normalized.sort(key=lambda value: (value[0], value[1], value[2]))
        for previous, current in zip(normalized, normalized[1:]):
            if current[0] <= previous[1]:
                # Crossing/nested ranges have no unique physical ownership.
                return None
        starts = [item[0] for item in normalized]
        pointer = [0]

        def resolve(line_number):
            try:
                line_number = int(line_number)
            except (TypeError, ValueError):
                return None
            while pointer[0] < len(normalized) and normalized[pointer[0]][1] < line_number:
                pointer[0] += 1
            if pointer[0] >= len(normalized):
                return None
            candidate = normalized[pointer[0]]
            if candidate[0] <= line_number <= candidate[1]:
                return candidate[3]
            # A caller may use the helper with a non-monotonic line.  Locate
            # the only possible range without mutating the sweep pointer.
            index = bisect_right(starts, line_number) - 1
            if index >= 0:
                candidate = normalized[index]
                if candidate[0] <= line_number <= candidate[1]:
                    return candidate[3]
            return None

        return resolve
