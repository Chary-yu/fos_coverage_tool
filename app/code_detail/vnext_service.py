"""VNext Code Detail API service bound to Scan and Report identities."""

import os
import threading
from collections import OrderedDict

from app.code_detail.code_region import FunctionRange, build_code_regions
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import calc_sidecar_file_key, compute_db_file_path_hash
from app.db.repositories.base import fetchall, fetchone
from app.db.repositories.analysis_domain_repository import (
    AnalysisDomainRepository, INHERITED_PENDING, MANUAL_DRAFT,
)
from app.reports.identity import validate_report_id


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")
MAX_SINGLE_LINE_SPAN = 10000
MAX_BATCH_RANGES = 1000
MAX_BATCH_LOGICAL_LINES = 20000


class VNextCodeDetailService(object):
    def __init__(self, project_repo, analysis_repo, report_registry,
                 domain_repo=None,
                 max_sidecar_stores=32, max_identity_entries=256,
                 max_overlay_entries=512):
        self.projects = project_repo
        self.analyses = analysis_repo
        self.domain = domain_repo
        self.registry = report_registry
        # A runtime serves many ranges from the same immutable report. Keep
        # one SidecarStore per report root/asset so metadata and decoded
        # physical chunks can be reused across HTTP requests.
        self.max_sidecar_stores = max(1, int(max_sidecar_stores))
        self.max_identity_entries = max(1, int(max_identity_entries))
        self.max_overlay_entries = max(1, int(max_overlay_entries))
        self._sidecar_stores = OrderedDict()
        self._identity_cache = OrderedDict()
        self._overlay_cache = OrderedDict()
        self._cache_lock = threading.RLock()
        self._metrics = {
            "overlay_db_queries": 0,
            "overlay_db_rows": 0,
        }

    @staticmethod
    def _sidecar_key(file_path, repository_name=""):
        return calc_sidecar_file_key(file_path, repository_name)

    @staticmethod
    def _normalize_file_path(file_path):
        value = str(file_path or "").replace("\\", "/").strip()
        if not value or value.startswith("/") or ":" in value:
            raise ValueError("file_path must be a repository-relative path")
        parts = [part for part in value.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ValueError("file_path traversal is not allowed")
        return "/".join(parts)

    def _identity(self, connection, scan_id, report_id, repository_name, file_path):
        report_id = validate_report_id(report_id)
        repository_name = str(repository_name or "").strip()
        if len(repository_name) > 128 or ".." in repository_name:
            raise ValueError("invalid repository_name")
        file_path = self._normalize_file_path(file_path)
        identity_key = (int(scan_id), report_id, repository_name, file_path)
        with self._cache_lock:
            cached_identity = self._identity_cache.get(identity_key)
            if cached_identity is not None:
                self._identity_cache.move_to_end(identity_key)
                cached_report, cached_file, cached_key, cached_sidecar = cached_identity
                project_id = cached_file.get("project_id")
                if project_id is not None:
                    version_row = fetchone(connection, """
                        SELECT data_version FROM coverage_project_state
                        WHERE project_id = ?
                    """, (int(project_id),))
                    current_version = int((version_row or {}).get("data_version") or 0)
                    if current_version != int(cached_file.get("data_version") or 0):
                        cached_file = dict(cached_file)
                        cached_file["data_version"] = current_version
                        cached_identity = (
                            cached_report, cached_file, cached_key, cached_sidecar
                        )
                        self._identity_cache[identity_key] = cached_identity
                return cached_identity
        report = self.projects.get_report(connection, report_id)
        if not report or int(report["scan_id"]) != int(scan_id):
            raise KeyError("report_id is not bound to scan_id")
        file_hash = compute_db_file_path_hash(file_path, repository_name)
        file_query = """
            SELECT f.*, s.project_id,
                   COALESCE(ps.data_version, 0) AS data_version
            FROM coverage_files f
            JOIN coverage_scans s ON s.id = f.scan_id
            LEFT JOIN coverage_project_state ps ON ps.project_id = s.project_id
            WHERE f.scan_id = ? AND f.repository_name = ?
              AND f.file_path_hash = ? AND f.file_path = ?
        """
        file_row = fetchone(
            connection, file_query,
            (int(scan_id), repository_name, file_hash, file_path),
        )
        # Existing VNext fixtures/imports may have supplied an explicit hash.
        # Path identity is still constrained by scan and repository; this
        # fallback is only for the immutable rows created before scoped hashes.
        if not file_row:
            matches = fetchall(connection, file_query.replace(
                "AND f.file_path_hash = ? AND f.file_path = ?",
                "AND f.file_path = ?",
            ), (int(scan_id), repository_name, file_path))
            if len(matches) > 1:
                raise ValueError("file path is ambiguous within the Scan identity")
            file_row = matches[0] if matches else None
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
        with self._cache_lock:
            sidecar = self._sidecar_stores.get(store_key)
            if sidecar is None:
                sidecar = SidecarStore(
                    search_dirs=[report_root],
                    asset_identity=report.get("asset_identity") or "",
                )
                self._sidecar_stores[store_key] = sidecar
                while len(self._sidecar_stores) > self.max_sidecar_stores:
                    self._sidecar_stores.popitem(last=False)
            else:
                self._sidecar_stores.move_to_end(store_key)
        result = report, file_row, self._sidecar_key(file_path, repository_name), sidecar
        with self._cache_lock:
            self._identity_cache[identity_key] = result
            self._identity_cache.move_to_end(identity_key)
            while len(self._identity_cache) > self.max_identity_entries:
                self._identity_cache.popitem(last=False)
        return result

    @staticmethod
    def _overlay_ranges(ranges):
        normalized = sorted({
            (int(start_line), int(end_line))
            for start_line, end_line in (ranges or [])
            if int(end_line) >= int(start_line)
        })
        merged = []
        for start_line, end_line in normalized:
            if merged and start_line <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end_line))
            else:
                merged.append((start_line, end_line))
        return tuple(merged)

    @staticmethod
    def _domain_overlay_row(row):
        state = str(row.get("review_state") or "")
        return dict(row, status=row.get("conclusion_status") or "",
                    is_draft=1 if state in (MANUAL_DRAFT, INHERITED_PENDING) else 0,
                    reviewer=row.get("reviewed_by") or "",
                    analysis_state=row.get("conclusion_status") or "",
                    review_state=state)

    def _overlay(self, connection, file_id, ranges=None, data_version=0,
                 scan_id=None):
        range_key = self._overlay_ranges(ranges)
        cache_key = (int(scan_id or 0), int(file_id), int(data_version or 0), range_key)
        with self._cache_lock:
            cached = self._overlay_cache.get(cache_key)
            if cached is not None:
                self._overlay_cache.move_to_end(cache_key)
                return cached
        if self.domain is not None and scan_id is not None:
            rows = self.domain.read_file(connection, scan_id, file_id, range_key)
            if rows:
                rows = [self._domain_overlay_row(row) for row in rows]
            elif range_key:
                rows = self.analyses.get_by_file_ranges(connection, file_id, range_key)
            else:
                rows = self.analyses.get_by_file(connection, file_id)
        elif range_key:
            rows = self.analyses.get_by_file_ranges(connection, file_id, range_key)
        else:
            rows = self.analyses.get_by_file(connection, file_id)
        with self._cache_lock:
            self._metrics["overlay_db_queries"] += 1
            self._metrics["overlay_db_rows"] += len(rows)
        overlay = {
            int(row["line_number"]): row
            for row in rows
        }
        with self._cache_lock:
            self._overlay_cache[cache_key] = overlay
            self._overlay_cache.move_to_end(cache_key)
            while len(self._overlay_cache) > self.max_overlay_entries:
                self._overlay_cache.popitem(last=False)
        return overlay

    def metrics(self):
        """Expose bounded SidecarStore cache counters for diagnostics."""
        with self._cache_lock:
            stores = [store.cache_stats() for store in self._sidecar_stores.values()]
            identity_count = len(self._identity_cache)
            overlay_count = len(self._overlay_cache)
            service_metrics = dict(self._metrics)
        sidecar_metrics = {
            "metadata_reads": sum(int(item.get("metadata_reads") or 0) for item in stores),
            "metadata_cache_hits": sum(int(item.get("metadata_cache_hits") or 0) for item in stores),
            "chunk_reads": sum(int(item.get("chunk_reads") or 0) for item in stores),
            "chunk_cache_hits": sum(int(item.get("chunk_cache_hits") or 0) for item in stores),
        }
        return {
            "identity_cache_entries": identity_count,
            "max_identity_entries": self.max_identity_entries,
            "overlay_cache_entries": overlay_count,
            "max_overlay_entries": self.max_overlay_entries,
            "sidecar_store_count": len(self._sidecar_stores),
            "stores": stores,
            "max_sidecar_stores": self.max_sidecar_stores,
            "overlay_db_queries": service_metrics["overlay_db_queries"],
            "overlay_db_rows": service_metrics["overlay_db_rows"],
            "sidecar_metadata_reads": sidecar_metrics["metadata_reads"],
            "sidecar_metadata_cache_hits": sidecar_metrics["metadata_cache_hits"],
            "sidecar_decode_count": sidecar_metrics["chunk_reads"],
            "sidecar_decode_cache_hits": sidecar_metrics["chunk_cache_hits"],
        }

    def layout(self, connection, scan_id, report_id, repository_name="", file_path=None):
        if file_path is None:
            file_path, repository_name = repository_name, ""
        report, file_row, key, sidecar = self._identity(
            connection, scan_id, report_id, repository_name, file_path
        )
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
        overlay = self._overlay(
            connection, file_row["id"], data_version=file_row.get("data_version", 0),
            scan_id=scan_id,
        )
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
            "scan_id": int(scan_id), "report_id": report_id,
            "repository_name": str(repository_name or ""), "file_path": file_path,
            "total_lines": int(meta.get("total_lines") or 0),
            "total_uncovered_count": int(meta.get(
                "static_total_uncovered_count", len(meta.get("uncovered_lines") or [])
            )),
            "pending_line_count": len(pending), "confirmed_count": confirmed,
            "regions": [region.to_dict() for region in regions],
        }

    def lines(self, connection, scan_id, report_id, repository_name="", file_path=None,
              start_line=None, end_line=None):
        if end_line is None or not isinstance(file_path, str):
            # Compatibility with the pre-repository identity call shape:
            # (connection, scan_id, report_id, file_path, start, end).
            old_start, old_end = file_path, start_line
            if end_line is None:
                old_end = start_line
            end_line = old_end if end_line is None or not isinstance(file_path, str) else end_line
            start_line = old_start
            file_path, repository_name = repository_name, ""
        return self.lines_batch(
            connection, scan_id, report_id, repository_name, file_path,
            [(int(start_line), int(end_line))],
        )[0]

    def lines_batch(self, connection, scan_id, report_id, repository_name="", file_path=None,
                    ranges=None):
        if ranges is None:
            # Compatibility with the pre-repository identity call shape:
            # (connection, scan_id, report_id, file_path, ranges).
            ranges = file_path
            file_path, repository_name = repository_name, ""
        """Resolve identity/overlay once and split shared sidecar chunks per range."""
        if not ranges:
            return []
        if len(ranges) > MAX_BATCH_RANGES:
            raise ValueError("too many line ranges")
        report, file_row, key, sidecar = self._identity(
            connection, scan_id, report_id, repository_name, file_path
        )
        meta = sidecar.load_metadata(report_id, key)
        if not meta:
            raise FileNotFoundError("report sidecar metadata is unavailable")
        total_lines = int(meta.get("total_lines") or 0)
        normalized = []
        logical_lines = 0
        for item in ranges or []:
            if isinstance(item, dict):
                start_line = item.get("start_line") or 1
                end_line = item.get("end_line") or start_line
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                start_line, end_line = item
            else:
                raise ValueError("each line range must contain start_line and end_line")
            start_line, end_line = int(start_line), int(end_line)
            if (start_line < 1 or end_line < start_line or
                    end_line - start_line + 1 > MAX_SINGLE_LINE_SPAN or
                    (total_lines and end_line > total_lines)):
                raise ValueError("invalid line range")
            normalized.append((start_line, end_line))
            logical_lines += end_line - start_line + 1
        if logical_lines > MAX_BATCH_LOGICAL_LINES:
            raise ValueError("requested line span is too large")
        rows_batches = sidecar.load_lines_ranges(report_id, key, normalized)
        if rows_batches is None:
            raise FileNotFoundError("report sidecar lines are unavailable")
        overlay = self._overlay(
            connection, file_row["id"], normalized,
            data_version=file_row.get("data_version", 0),
            scan_id=scan_id,
        )
        batches = []
        for (start_line, end_line), rows in zip(normalized, rows_batches):
            result = []
            for row in rows:
                item = dict(row)
                analysis = overlay.get(int(item.get("line_no") or item.get("line_number") or 0))
                if analysis:
                    item["analysis"] = analysis
                result.append(item)
            batches.append({
                "scan_id": int(scan_id), "report_id": report_id,
                "repository_name": str(repository_name or ""), "file_path": file_path,
                "start_line": start_line, "end_line": end_line,
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
