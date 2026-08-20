"""Canonical inject parsing and Scan import services."""

import hashlib
import os

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
        reject_relative_traversal(info_path)
        info_path = os.path.realpath(info_path)
        if self.allowed_info_roots and not realpath_within(info_path, self.allowed_info_roots):
            raise ValueError("info_path is outside configured input roots")
        if not os.path.isfile(info_path):
            raise FileNotFoundError(info_path)
        digest = hashlib.sha256()
        with open(info_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        info_sha256 = digest.hexdigest()
        parsed = load_info(info_path)
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
            for line_number, execution_count in sorted(item.get("lines", {}).items()):
                function = self._function_for_line(ranges, line_number) if not fallback else None
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
        if report and self.allowed_report_roots:
            report_directories = report.get("directories") or (
                [report.get("report_root")] if report.get("report_root") else []
            )
            for directory in report_directories:
                if not realpath_within(directory, self.allowed_report_roots):
                    raise ValueError("report root is outside configured report roots")
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
        for item in ranges:
            if item.get("start_line", 0) <= line_number <= item.get("end_line", 0):
                return item
        return None
