"""Bounded, identity-rich VNext export service."""

import json
import os
import re
import tempfile
import zipfile
from datetime import datetime


class ExportService(object):
    def __init__(self, project_repo, output_root, release_identity=None):
        self.projects = project_repo
        self.output_root = os.path.realpath(output_root)
        self.release_identity = release_identity or {}

    def _safe_output_path(self, project_name, scan_id, output_path=None):
        if output_path:
            candidate = os.path.realpath(output_path)
        else:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(project_name or "project"))
            candidate = os.path.join(
                self.output_root, "{}_scan_{}.zip".format(safe_name, int(scan_id))
            )
            candidate = os.path.realpath(candidate)
        try:
            inside = os.path.commonpath((self.output_root, candidate)) == self.output_root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("export path escapes configured output root")
        os.makedirs(self.output_root, exist_ok=True)
        return candidate

    def export_scan(self, connection, project_name, scan_id, report_id="", output_path=None):
        project = self.projects.get_project_by_name(connection, project_name)
        if not project:
            raise KeyError("project not found: {}".format(project_name))
        scan = self.projects.get_scan(connection, int(scan_id))
        if not scan or int(scan["project_id"]) != int(project["id"]):
            raise KeyError("scan is not bound to project")
        report = self.projects.get_report_for_scan(connection, int(scan_id))
        if report_id and (not report or report.get("report_id") != report_id):
            raise KeyError("report is not bound to scan")
        snapshots = self.projects.list_repository_snapshots(connection, int(scan_id))
        metadata = {
            "project_name": project_name,
            "scan_id": int(scan_id),
            "report_id": (report or {}).get("report_id", ""),
            "scan": dict(scan),
            "report": dict(report or {}),
            "repositories": snapshots,
            "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "release": self.release_identity,
        }
        target = self._safe_output_path(project_name, scan_id, output_path)
        part_path = target + ".part"
        try:
            with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "metadata.json",
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
                )
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".jsonl", delete=False
                ) as rows_file:
                    rows_path = rows_file.name
                    for row in self.projects.iter_scan_export_rows(connection, int(scan_id)):
                        item = dict(row)
                        item["suggested_reviewer"] = item.get("suggested_reviewer") or ""
                        item["reviewer"] = item.get("reviewer") or ""
                        rows_file.write(json.dumps(
                            item, ensure_ascii=False, sort_keys=True, default=str
                        ))
                        rows_file.write("\n")
                try:
                    archive.write(rows_path, "coverage_lines.jsonl")
                finally:
                    try:
                        os.remove(rows_path)
                    except OSError:
                        pass
            os.replace(part_path, target)
            return target
        except Exception:
            try:
                os.remove(part_path)
            except OSError:
                pass
            raise
