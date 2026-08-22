"""VNext Code Detail API service bound to Scan and Report identities."""

import os
import json
import threading
from collections import OrderedDict

from app.code_detail.cache_budget import ByteBudget
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


def _estimate_cache_bytes(value):
    """Return a deterministic, conservative size estimate for JSON-shaped data."""
    try:
        return max(1, len(json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=repr,
        ).encode("utf-8")))
    except Exception:
        return max(1, len(repr(value).encode("utf-8", "replace")))


class _LinesBatchPlan(object):
    """Connection-free plan for a code-lines batch request.

    Identity and analysis overlay are resolved with a short-lived database
    lease.  The plan then lets the caller perform Sidecar metadata/chunk IO
    after that lease has been returned to the pool.
    """

    def __init__(self, scan_id, report_id, repository_name, file_path,
                 report, file_row, key, sidecar, ranges):
        self.scan_id = int(scan_id)
        self.report_id = report_id
        self.repository_name = str(repository_name or "")
        self.file_path = file_path
        self.report = report
        self.file_row = file_row
        self.key = key
        self.sidecar = sidecar
        self.ranges = tuple(ranges or ())
        self.meta = None
        self.normalized_ranges = None


class VNextCodeDetailService(object):
    def __init__(self, project_repo, analysis_repo, report_registry,
                 domain_repo=None,
                 max_sidecar_stores=32, max_identity_entries=256,
                 max_overlay_entries=512,
                 max_identity_bytes=4 * 1024 * 1024,
                 max_overlay_bytes=16 * 1024 * 1024,
                 max_cache_bytes=64 * 1024 * 1024,
                 max_cache_entry_bytes=4 * 1024 * 1024,
                 max_sidecar_cache_bytes=32 * 1024 * 1024,
                 max_sidecar_entry_bytes=4 * 1024 * 1024):
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
        self.max_identity_bytes = max(0, int(max_identity_bytes))
        self.max_overlay_bytes = max(0, int(max_overlay_bytes))
        self.max_cache_bytes = max(0, int(max_cache_bytes))
        self.max_cache_entry_bytes = max(0, int(max_cache_entry_bytes))
        self.max_sidecar_cache_bytes = max(0, int(max_sidecar_cache_bytes))
        self.max_sidecar_entry_bytes = max(0, int(max_sidecar_entry_bytes))
        self._sidecar_stores = OrderedDict()
        self._identity_cache = OrderedDict()
        self._overlay_cache = OrderedDict()
        self._identity_index = {}
        self._identity_inflight = {}
        self._overlay_inflight = {}
        self._cache_lock = threading.RLock()
        self._process_cache_budget = ByteBudget(self.max_cache_bytes)
        self._identity_cache_bytes = 0
        self._overlay_cache_bytes = 0
        self._metrics = {
            "overlay_db_queries": 0,
            "overlay_db_rows": 0,
            "overlay_cache_hits": 0,
            "overlay_cache_misses": 0,
            "overlay_singleflight_shared": 0,
            "identity_version_queries": 0,
            "identity_loads": 0,
            "identity_singleflight_shared": 0,
            "cache_evictions": 0,
            "cache_oversize_bypass": 0,
        }

    @staticmethod
    def _cache_entry(value):
        return value[0] if isinstance(value, tuple) and len(value) == 2 else value

    def _cache_bytes_for(self, cache_name):
        return (self._identity_cache_bytes if cache_name == "identity"
                else self._overlay_cache_bytes)

    def _set_cache_bytes_for(self, cache_name, value):
        if cache_name == "identity":
            self._identity_cache_bytes = max(0, int(value))
        else:
            self._overlay_cache_bytes = max(0, int(value))

    def _remove_service_entry_locked(self, cache, key, cache_name):
        entry = cache.pop(key, None)
        if entry is None:
            return None
        value, size = entry
        self._set_cache_bytes_for(
            cache_name, self._cache_bytes_for(cache_name) - int(size)
        )
        self._process_cache_budget.release(size)
        return value

    def _store_service_entry_locked(self, cache, key, value, cache_name,
                                    max_entries, max_bytes):
        size = _estimate_cache_bytes(value)
        if (max_bytes <= 0 or self.max_cache_entry_bytes <= 0 or
                size > max_bytes or size > self.max_cache_entry_bytes):
            self._metrics["cache_oversize_bypass"] += 1
            return False

        self._remove_service_entry_locked(cache, key, cache_name)
        while cache and (len(cache) >= max_entries or
                         self._cache_bytes_for(cache_name) + size > max_bytes or
                         self._identity_cache_bytes + self._overlay_cache_bytes + size > self.max_cache_bytes):
            old_key = next(iter(cache))
            self._remove_service_entry_locked(cache, old_key, cache_name)
            self._metrics["cache_evictions"] += 1
        if (self._identity_cache_bytes + self._overlay_cache_bytes + size >
                self.max_cache_bytes or
                not self._process_cache_budget.try_acquire(size)):
            self._metrics["cache_oversize_bypass"] += 1
            return False
        cache[key] = (value, size)
        cache.move_to_end(key)
        self._set_cache_bytes_for(
            cache_name, self._cache_bytes_for(cache_name) + size
        )
        return True

    def _sidecar_store(self, store_key, report_root, asset_identity):
        with self._cache_lock:
            sidecar = self._sidecar_stores.get(store_key)
            if sidecar is not None:
                self._sidecar_stores.move_to_end(store_key)
                return sidecar
        # Constructor/path normalization is outside the service cache lock.
        candidate = SidecarStore(
            search_dirs=[report_root],
            asset_identity=asset_identity,
            max_cache_bytes=self.max_sidecar_cache_bytes,
            max_entry_bytes=self.max_sidecar_entry_bytes,
            process_budget=self._process_cache_budget,
        )
        with self._cache_lock:
            sidecar = self._sidecar_stores.get(store_key)
            if sidecar is None:
                sidecar = candidate
                self._sidecar_stores[store_key] = sidecar
                while len(self._sidecar_stores) > self.max_sidecar_stores:
                    _, evicted = self._sidecar_stores.popitem(last=False)
                    evicted.clear_caches()
            else:
                self._sidecar_stores.move_to_end(store_key)
        return sidecar

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
        base_key = (int(scan_id), report_id, repository_name, file_path)
        while True:
            # Only map lookup/LRU mutation is protected.  Freshness validation
            # below deliberately performs its DB read after this lock is gone.
            with self._cache_lock:
                full_key = self._identity_index.get(base_key)
                entry = self._identity_cache.get(full_key) if full_key else None
                if entry is not None:
                    self._identity_cache.move_to_end(full_key)
                    cached_identity = entry[0]
                else:
                    cached_identity = None
            if cached_identity is not None:
                cached_report, cached_file, cached_key, cached_sidecar = cached_identity
                project_id = cached_file.get("project_id")
                current_version = int(cached_file.get("data_version") or 0)
                if project_id is not None:
                    version_row = fetchone(connection, """
                        SELECT data_version FROM coverage_project_state
                        WHERE project_id = ?
                    """, (int(project_id),))
                    with self._cache_lock:
                        self._metrics["identity_version_queries"] += 1
                    current_version = int((version_row or {}).get("data_version") or 0)
                if current_version == int(cached_file.get("data_version") or 0):
                    # CAS-style recheck prevents returning an entry that was
                    # evicted/replaced while the version query was running.
                    with self._cache_lock:
                        current = self._identity_cache.get(full_key)
                        if (current is not None and
                                self._identity_index.get(base_key) == full_key):
                            self._identity_cache.move_to_end(full_key)
                            return current[0]
                with self._cache_lock:
                    if self._identity_index.get(base_key) == full_key:
                        self._identity_index.pop(base_key, None)
                        self._remove_service_entry_locked(
                            self._identity_cache, full_key, "identity"
                        )

            with self._cache_lock:
                event = self._identity_inflight.get(base_key)
                if event is None:
                    event = threading.Event()
                    self._identity_inflight[base_key] = event
                    owner = True
                    self._metrics["identity_loads"] += 1
                else:
                    owner = False
                    self._metrics["identity_singleflight_shared"] += 1
            if not owner:
                event.wait()
                continue

            try:
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
                # Existing VNext fixtures/imports may have supplied an explicit
                # hash.  The fallback remains constrained by Scan/repository/path.
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
                asset_identity = str(report.get("asset_identity") or "")
                store_key = (report_root, asset_identity)
                sidecar = self._sidecar_store(
                    store_key, report_root, asset_identity
                )
                result = (
                    report, file_row, self._sidecar_key(file_path, repository_name),
                    sidecar,
                )
                full_key = (
                    base_key, int(file_row.get("data_version") or 0), asset_identity,
                )
                with self._cache_lock:
                    self._store_service_entry_locked(
                        self._identity_cache, full_key, result, "identity",
                        self.max_identity_entries, self.max_identity_bytes,
                    )
                    if (full_key in self._identity_cache or
                            self.max_identity_bytes <= 0):
                        self._identity_index[base_key] = full_key
                    current = self._identity_inflight.pop(base_key, None)
                    if current is not None:
                        current.set()
                return result
            except BaseException:
                with self._cache_lock:
                    current = self._identity_inflight.pop(base_key, None)
                    if current is not None:
                        current.set()
                raise

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
        relation_active = int(row.get("relation_is_active") or 0)
        rejected = bool(row.get("rejection_id")) and not relation_active
        if rejected:
            # A rejection deliberately removes the inherited relation from the
            # active overlay. Keep only its lineage/CAS metadata visible so a
            # current-scan UI can offer undo without resurrecting the content
            # as a confirmed analysis.
            return dict(row, status="", is_draft=1,
                        reviewer="", analysis_state="未确认",
                        coverage_method="", uncovered_reason="",
                        review_state="INHERITANCE_REJECTED",
                        relation_is_active=0)
        return dict(row, status=row.get("conclusion_status") or "",
                    is_draft=1 if state in (MANUAL_DRAFT, INHERITED_PENDING) else 0,
                    reviewer=row.get("reviewed_by") or "",
                    analysis_state=row.get("conclusion_status") or "",
                    review_state=state)

    def _overlay(self, connection, file_id, ranges=None, data_version=0,
                 scan_id=None, report_id=None, asset_identity=""):
        range_key = self._overlay_ranges(ranges)
        cache_key = (
            str(report_id or ""), int(scan_id or 0), int(file_id),
            int(data_version or 0), str(asset_identity or ""), range_key,
        )
        while True:
            with self._cache_lock:
                cached = self._overlay_cache.get(cache_key)
                if cached is not None:
                    self._overlay_cache.move_to_end(cache_key)
                    self._metrics["overlay_cache_hits"] += 1
                    return cached[0]
                event = self._overlay_inflight.get(cache_key)
                if event is None:
                    event = threading.Event()
                    self._overlay_inflight[cache_key] = event
                    owner = True
                    self._metrics["overlay_cache_misses"] += 1
                else:
                    owner = False
                    self._metrics["overlay_singleflight_shared"] += 1
            if not owner:
                event.wait()
                continue
            try:
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
                overlay = {
                    int(row["line_number"]): row
                    for row in rows
                }
                with self._cache_lock:
                    self._metrics["overlay_db_queries"] += 1
                    self._metrics["overlay_db_rows"] += len(rows)
                    self._store_service_entry_locked(
                        self._overlay_cache, cache_key, overlay, "overlay",
                        self.max_overlay_entries, self.max_overlay_bytes,
                    )
                    current = self._overlay_cache.get(cache_key)
                    result = current[0] if current is not None else overlay
                    current_event = self._overlay_inflight.pop(cache_key, None)
                    if current_event is not None:
                        current_event.set()
                    return result
            except BaseException:
                with self._cache_lock:
                    current_event = self._overlay_inflight.pop(cache_key, None)
                    if current_event is not None:
                        current_event.set()
                raise

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
            "chunk_inflight_waits": sum(
                int(item.get("chunk_inflight_waits") or 0) for item in stores
            ),
            "cache_bytes": sum(int(item.get("cache_bytes") or 0) for item in stores),
            "cache_evictions": sum(int(item.get("cache_evictions") or 0) for item in stores),
            "cache_oversize_bypass": sum(
                int(item.get("cache_oversize_bypass") or 0) for item in stores
            ),
        }
        with self._cache_lock:
            identity_bytes = self._identity_cache_bytes
            overlay_bytes = self._overlay_cache_bytes
            process_bytes = self._process_cache_budget.current_bytes()
            cache_metrics = dict(self._metrics)
        return {
            "identity_cache_entries": identity_count,
            "max_identity_entries": self.max_identity_entries,
            "identity_cache_bytes": identity_bytes,
            "max_identity_bytes": self.max_identity_bytes,
            "overlay_cache_entries": overlay_count,
            "max_overlay_entries": self.max_overlay_entries,
            "overlay_cache_bytes": overlay_bytes,
            "max_overlay_bytes": self.max_overlay_bytes,
            "cache_bytes": process_bytes,
            "max_cache_bytes": self.max_cache_bytes,
            "cache_entry_bytes": self.max_cache_entry_bytes,
            "cache_evictions": cache_metrics["cache_evictions"] + sidecar_metrics["cache_evictions"],
            "cache_oversize_bypass": (
                cache_metrics["cache_oversize_bypass"] +
                sidecar_metrics["cache_oversize_bypass"]
            ),
            "sidecar_store_count": len(self._sidecar_stores),
            "stores": stores,
            "max_sidecar_stores": self.max_sidecar_stores,
            "overlay_db_queries": service_metrics["overlay_db_queries"],
            "overlay_db_rows": service_metrics["overlay_db_rows"],
            "overlay_cache_hits": cache_metrics["overlay_cache_hits"],
            "overlay_cache_misses": cache_metrics["overlay_cache_misses"],
            "overlay_singleflight_shared": cache_metrics["overlay_singleflight_shared"],
            "identity_version_queries": cache_metrics["identity_version_queries"],
            "identity_loads": cache_metrics["identity_loads"],
            "identity_singleflight_shared": cache_metrics["identity_singleflight_shared"],
            "sidecar_metadata_reads": sidecar_metrics["metadata_reads"],
            "sidecar_metadata_cache_hits": sidecar_metrics["metadata_cache_hits"],
            "sidecar_decode_count": sidecar_metrics["chunk_reads"],
            "sidecar_decode_cache_hits": sidecar_metrics["chunk_cache_hits"],
            "sidecar_decode_inflight_waits": sidecar_metrics["chunk_inflight_waits"],
            "sidecar_cache_bytes": sidecar_metrics["cache_bytes"],
            "sidecar_cache_evictions": sidecar_metrics["cache_evictions"],
            "sidecar_cache_oversize_bypass": sidecar_metrics["cache_oversize_bypass"],
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
            scan_id=scan_id, report_id=report_id,
            asset_identity=report.get("asset_identity") or "",
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

    def resolve_lines_batch(self, connection, scan_id, report_id, repository_name="",
                            file_path=None, ranges=None):
        """Resolve DB identity without reading Sidecar files.

        This is the first phase of the batch API.  It intentionally does not
        call ``load_metadata`` so a pooled DB connection is not held while
        filesystem IO or JSON decoding is in progress.
        """
        if ranges is None:
            # Compatibility with the pre-repository identity call shape:
            # (connection, scan_id, report_id, file_path, ranges).
            ranges = file_path
            file_path, repository_name = repository_name, ""
        if not ranges:
            return None
        if len(ranges) > MAX_BATCH_RANGES:
            raise ValueError("too many line ranges")
        report, file_row, key, sidecar = self._identity(
            connection, scan_id, report_id, repository_name, file_path
        )
        return _LinesBatchPlan(
            scan_id, report_id, repository_name, file_path,
            report, file_row, key, sidecar, ranges,
        )

    @staticmethod
    def _normalize_batch_ranges(meta, ranges):
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
        return tuple(normalized)

    def prepare_lines_batch(self, plan):
        """Read and validate immutable Sidecar metadata without a DB lease."""
        if plan is None:
            return None
        meta = plan.sidecar.load_metadata(plan.report_id, plan.key)
        if not meta:
            raise FileNotFoundError("report sidecar metadata is unavailable")
        plan.meta = meta
        plan.normalized_ranges = self._normalize_batch_ranges(meta, plan.ranges)
        return plan

    def load_lines_batch_overlay(self, connection, plan):
        """Load the analysis overlay in a short DB-only phase."""
        if plan is None or plan.normalized_ranges is None:
            raise ValueError("line batch metadata has not been prepared")
        return self._overlay(
            connection, plan.file_row["id"], plan.normalized_ranges,
            data_version=plan.file_row.get("data_version", 0),
            scan_id=plan.scan_id, report_id=plan.report_id,
            asset_identity=plan.report.get("asset_identity") or "",
        )

    def render_lines_batch(self, plan, overlay):
        """Read/decode Sidecar chunks and compose the response without DB IO."""
        if plan is None:
            return []
        rows_batches = plan.sidecar.load_lines_ranges(
            plan.report_id, plan.key, plan.normalized_ranges
        )
        if rows_batches is None:
            raise FileNotFoundError("report sidecar lines are unavailable")
        batches = []
        for (start_line, end_line), rows in zip(plan.normalized_ranges, rows_batches):
            result = []
            for row in rows:
                item = dict(row)
                analysis = overlay.get(int(item.get("line_no") or item.get("line_number") or 0))
                if analysis:
                    item["analysis"] = analysis
                result.append(item)
            batches.append({
                "scan_id": plan.scan_id, "report_id": plan.report_id,
                "repository_name": plan.repository_name, "file_path": plan.file_path,
                "start_line": start_line, "end_line": end_line,
                "lines": result,
            })
        return batches

    def lines_batch(self, connection, scan_id, report_id, repository_name="", file_path=None,
                    ranges=None):
        """Resolve identity/overlay once and split shared sidecar chunks per range."""
        plan = self.resolve_lines_batch(
            connection, scan_id, report_id, repository_name, file_path, ranges
        )
        if plan is None:
            return []
        self.prepare_lines_batch(plan)
        overlay = self.load_lines_batch_overlay(connection, plan)
        return self.render_lines_batch(plan, overlay)

    @staticmethod
    def _project_id(connection, scan_id):
        row = fetchone(connection, """
            SELECT project_id FROM coverage_scans WHERE id = ?
        """, (int(scan_id),))
        if not row:
            raise KeyError("scan not found")
        return row["project_id"]
