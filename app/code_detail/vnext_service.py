"""VNext Code Detail API service bound to Scan and Report identities."""

import hashlib

from app.code_detail.code_region import FunctionRange, build_code_regions
from app.code_detail.sidecar_store import SidecarStore
from app.db.repositories.base import fetchone
from app.reports.identity import validate_report_id


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")


class VNextCodeDetailService(object):
    def __init__(self, project_repo, analysis_repo, report_registry):
        self.projects = project_repo
        self.analyses = analysis_repo
        self.registry = report_registry
        self.sidecar = SidecarStore()

    @staticmethod
    def _sidecar_key(file_path):
        return hashlib.sha256(str(file_path).replace("\\", "/").encode("utf-8")).hexdigest()

    def _identity(self, connection, scan_id, report_id, file_path):
        report_id = validate_report_id(report_id)
        report = self.projects.get_report(connection, report_id)
        if not report or int(report["scan_id"]) != int(scan_id):
            raise KeyError("report_id is not bound to scan_id")
        file_hash = hashlib.md5(file_path.encode("utf-8")).hexdigest()
        file_row = fetchone(connection, """
            SELECT f.* FROM coverage_files f
            WHERE f.scan_id = ? AND f.file_path_hash = ? AND f.file_path = ?
        """, (int(scan_id), file_hash, file_path))
        if not file_row:
            raise KeyError("file identity not found")
        report_root = report.get("report_root") or self.registry.resolve_exact_root(report_id)
        if report_root:
            self.sidecar.add_search_dir(report_root)
        return report, file_row, self._sidecar_key(file_path)

    def _overlay(self, connection, file_id):
        return {
            int(row["line_number"]): row
            for row in self.analyses.get_by_file(connection, file_id)
        }

    def layout(self, connection, scan_id, report_id, file_path):
        report, file_row, key = self._identity(connection, scan_id, report_id, file_path)
        meta = self.sidecar.load_metadata(report_id, key)
        if not meta:
            raise FileNotFoundError("report sidecar metadata is unavailable")
        raw_ranges = meta.get("function_ranges") or []
        ranges = []
        for item in raw_ranges:
            if isinstance(item, dict):
                ranges.append(FunctionRange(
                    item["start_line"], item["end_line"], item.get("name", "")
                ))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                ranges.append(FunctionRange(item[0], item[1], item[2] if len(item) > 2 else ""))
        overlay = self._overlay(connection, file_row["id"])
        pending = []
        confirmed = 0
        for line_number in meta.get("uncovered_lines") or []:
            row = overlay.get(int(line_number))
            if row and not int(row.get("is_draft") or 0) and row.get("status") in CONFIRMED_STATUSES:
                confirmed += 1
            else:
                pending.append(int(line_number))
        regions = build_code_regions(
            int(meta.get("total_lines") or 0), pending, ranges
        )
        return {
            "project_name": self.projects.get_project(
                connection, self._project_id(connection, scan_id)
            ).get("project_name"),
            "scan_id": int(scan_id), "report_id": report_id, "file_path": file_path,
            "total_lines": int(meta.get("total_lines") or 0),
            "total_uncovered_count": int(meta.get(
                "static_total_uncovered_count", len(meta.get("uncovered_lines") or [])
            )),
            "pending_line_count": len(pending), "confirmed_count": confirmed,
            "regions": [region.to_dict() for region in regions],
        }

    def lines(self, connection, scan_id, report_id, file_path, start_line, end_line):
        report, file_row, key = self._identity(connection, scan_id, report_id, file_path)
        if int(start_line) < 1 or int(end_line) < int(start_line):
            raise ValueError("invalid line range")
        rows = self.sidecar.load_lines_range(
            report_id, key, int(start_line), int(end_line)
        )
        if rows is None:
            raise FileNotFoundError("report sidecar lines are unavailable")
        overlay = self._overlay(connection, file_row["id"])
        result = []
        for row in rows:
            item = dict(row)
            analysis = overlay.get(int(item.get("line_no") or item.get("line_number") or 0))
            if analysis:
                item["analysis"] = analysis
            result.append(item)
        return {
            "scan_id": int(scan_id), "report_id": report_id, "file_path": file_path,
            "lines": result,
        }

    @staticmethod
    def _project_id(connection, scan_id):
        row = fetchone(connection, """
            SELECT project_id FROM coverage_scans WHERE id = ?
        """, (int(scan_id),))
        if not row:
            raise KeyError("scan not found")
        return row["project_id"]
