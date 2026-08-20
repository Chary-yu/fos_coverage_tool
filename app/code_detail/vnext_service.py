"""VNext Code Detail API service bound to Scan and Report identities."""

import os

from app.code_detail.code_region import FunctionRange, build_code_regions
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import calc_sidecar_file_key, compute_db_file_path_hash
from app.db.repositories.base import fetchone
from app.reports.identity import validate_report_id


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")


class VNextCodeDetailService(object):
    def __init__(self, project_repo, analysis_repo, report_registry):
        self.projects = project_repo
        self.analyses = analysis_repo
        self.registry = report_registry
        # A runtime serves many ranges from the same immutable report. Keep
        # one SidecarStore per report root/asset so metadata and decoded
        # physical chunks can be reused across HTTP requests.
        self._sidecar_stores = {}

    @staticmethod
    def _sidecar_key(file_path):
        return calc_sidecar_file_key(file_path)

    @staticmethod
    def _normalize_file_path(file_path):
        value = str(file_path or "").replace("\\", "/").strip()
        if not value or value.startswith("/") or ":" in value:
            raise ValueError("file_path must be a repository-relative path")
        parts = [part for part in value.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ValueError("file_path traversal is not allowed")
        return "/".join(parts)

    def _identity(self, connection, scan_id, report_id, file_path):
        report_id = validate_report_id(report_id)
        file_path = self._normalize_file_path(file_path)
        report = self.projects.get_report(connection, report_id)
        if not report or int(report["scan_id"]) != int(scan_id):
            raise KeyError("report_id is not bound to scan_id")
        file_hash = compute_db_file_path_hash(file_path)
        file_row = fetchone(connection, """
            SELECT f.* FROM coverage_files f
            WHERE f.scan_id = ? AND f.file_path_hash = ? AND f.file_path = ?
        """, (int(scan_id), file_hash, file_path))
        if not file_row:
            raise KeyError("file identity not found")
        registry_root = self.registry.resolve_exact_root(report_id)
        declared_root = report.get("report_root") or ""
        if declared_root:
            declared_root = os.path.realpath(declared_root)
            if not os.path.isdir(declared_root):
                declared_root = ""
        if registry_root and declared_root and registry_root != declared_root:
            raise KeyError("report root identity mismatch")
        report_root = registry_root or declared_root
        if not report_root:
            raise FileNotFoundError("report root is unavailable")
        store_key = (report_root, str(report.get("asset_identity") or ""))
        sidecar = self._sidecar_stores.get(store_key)
        if sidecar is None:
            sidecar = SidecarStore(
                search_dirs=[report_root],
                asset_identity=report.get("asset_identity") or "",
            )
            self._sidecar_stores[store_key] = sidecar
        return report, file_row, self._sidecar_key(file_path), sidecar

    def _overlay(self, connection, file_id):
        return {
            int(row["line_number"]): row
            for row in self.analyses.get_by_file(connection, file_id)
        }

    def layout(self, connection, scan_id, report_id, file_path):
        report, file_row, key, sidecar = self._identity(connection, scan_id, report_id, file_path)
        meta = sidecar.load_metadata(report_id, key)
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
        return self.lines_batch(
            connection, scan_id, report_id, file_path,
            [(int(start_line), int(end_line))],
        )[0]

    def lines_batch(self, connection, scan_id, report_id, file_path, ranges):
        """Resolve identity/overlay once and split shared sidecar chunks per range."""
        if not ranges:
            return []
        report, file_row, key, sidecar = self._identity(connection, scan_id, report_id, file_path)
        normalized = []
        for item in ranges or []:
            if isinstance(item, dict):
                start_line = item.get("start_line") or 1
                end_line = item.get("end_line") or start_line
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                start_line, end_line = item
            else:
                raise ValueError("each line range must contain start_line and end_line")
            start_line, end_line = int(start_line), int(end_line)
            if start_line < 1 or end_line < start_line:
                raise ValueError("invalid line range")
            normalized.append((start_line, end_line))
        rows_batches = sidecar.load_lines_ranges(report_id, key, normalized)
        if rows_batches is None:
            raise FileNotFoundError("report sidecar lines are unavailable")
        overlay = self._overlay(connection, file_row["id"])
        batches = []
        for rows in rows_batches:
            result = []
            for row in rows:
                item = dict(row)
                analysis = overlay.get(int(item.get("line_no") or item.get("line_number") or 0))
                if analysis:
                    item["analysis"] = analysis
                result.append(item)
            batches.append({
                "scan_id": int(scan_id), "report_id": report_id, "file_path": file_path,
                "lines": result,
            })
        return batches

    @staticmethod
    def _project_id(connection, scan_id):
        row = fetchone(connection, """
            SELECT project_id FROM coverage_scans WHERE id = ?
        """, (int(scan_id),))
        if not row:
            raise KeyError("scan not found")
        return row["project_id"]
