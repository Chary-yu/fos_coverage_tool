"""Unified bounded performance evidence contract.

The collector stores counters and aggregate timings only. It intentionally
does not retain source text, absolute paths, credentials, or unbounded request
labels. A snapshot is safe to attach to a release/scan/workload evidence
record and cheap enough to leave enabled in normal VNext operation.
"""

from __future__ import absolute_import

import hashlib
import json
import threading
import time
from contextlib import contextmanager

try:
    import resource
except ImportError:  # pragma: no cover
    resource = None


_ACTIVE_COLLECTOR = threading.local()


def current_collector():
    """Return the collector bound to the current request/job context."""
    return getattr(_ACTIVE_COLLECTOR, "value", None)


@contextmanager
def bind_collector(collector):
    """Bind one bounded collector across repository calls in this context."""
    previous = getattr(_ACTIVE_COLLECTOR, "value", None)
    _ACTIVE_COLLECTOR.value = collector
    try:
        yield collector
    finally:
        _ACTIVE_COLLECTOR.value = previous


def _peak_rss_bytes():
    if resource is None:
        return 0
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0
    return value if _is_macos() else value * 1024


def _is_macos():
    import sys
    return sys.platform == "darwin"


def stable_identity(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


class PerformanceEvidenceCollector(object):
    SCHEMA_VERSION = 1

    def __init__(self, release_identity=None, project_id=None, scan_id=None,
                 workload_id="runtime"):
        identity = release_identity or {}
        self.release_sha = str(identity.get("commit_sha") or "")
        self.build_id = str(identity.get("build_id") or "")
        self.project_id = int(project_id) if project_id is not None else None
        self.scan_id = int(scan_id) if scan_id is not None else None
        self.workload_id = str(workload_id or "runtime")
        self._lock = threading.Lock()
        self._started = time.perf_counter()
        self._counters = {
            "request_count": 0, "request_bytes": 0, "response_bytes": 0,
            "db_query_count": 0, "db_rows": 0, "db_time_ms": 0.0,
            "git_subprocess_count": 0, "git_bytes_read": 0,
            "bytes_read": 0, "bytes_written": 0,
            "cache_hits": 0, "cache_misses": 0, "cache_evictions": 0,
            "cache_bytes": 0, "sidecar_decode_count": 0,
            "payload_bytes": 0, "dom_nodes": 0, "long_tasks": 0,
        }
        self._durations = {}
        self._phase_durations = {}

    def bind(self, project_id=None, scan_id=None, workload_id=None):
        with self._lock:
            if project_id is not None:
                self.project_id = int(project_id)
            if scan_id is not None:
                self.scan_id = int(scan_id)
            if workload_id is not None:
                self.workload_id = str(workload_id)

    def increment(self, field, amount=1):
        with self._lock:
            self._counters[field] = self._counters.get(field, 0) + amount

    def observe(self, field, value):
        with self._lock:
            self._counters[field] = value

    def observe_duration(self, operation, elapsed_ms):
        key = str(operation or "unknown")
        with self._lock:
            value = self._durations.setdefault(key, {
                "count": 0, "total_ms": 0.0, "max_ms": 0.0,
            })
            value["count"] += 1
            value["total_ms"] += float(elapsed_ms)
            value["max_ms"] = max(value["max_ms"], float(elapsed_ms))

    @contextmanager
    def timed(self, operation):
        started = time.perf_counter()
        try:
            yield self
        finally:
            self.observe_duration(operation, (time.perf_counter() - started) * 1000.0)

    @contextmanager
    def phase(self, phase):
        started = time.perf_counter()
        try:
            yield self
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            with self._lock:
                item = self._phase_durations.setdefault(str(phase), {
                    "count": 0, "total_ms": 0.0, "max_ms": 0.0,
                })
                item["count"] += 1
                item["total_ms"] += elapsed
                item["max_ms"] = max(item["max_ms"], elapsed)

    def record_request(self, request_bytes=0, response_bytes=0, elapsed_ms=0.0):
        self.increment("request_count")
        self.increment("request_bytes", int(request_bytes or 0))
        self.increment("response_bytes", int(response_bytes or 0))
        self.observe_duration("http_request", float(elapsed_ms or 0.0))

    def record_db_query(self, elapsed_ms=0.0):
        self.increment("db_query_count")
        self.increment("db_time_ms", float(elapsed_ms or 0.0))

    def record_db_rows(self, count):
        self.increment("db_rows", int(count or 0))

    def record_git_subprocess(self, bytes_read=0):
        self.increment("git_subprocess_count")
        self.increment("git_bytes_read", int(bytes_read or 0))

    def record_cache(self, hit=False, miss=False, evictions=0,
                     current_bytes=None):
        """Record bounded cache evidence without exposing cache keys."""
        if hit:
            self.increment("cache_hits")
        if miss:
            self.increment("cache_misses")
        if evictions:
            self.increment("cache_evictions", int(evictions))
        if current_bytes is not None:
            self.observe("cache_bytes", max(0, int(current_bytes or 0)))

    def record_bytes_read(self, count):
        self.increment("bytes_read", max(0, int(count or 0)))

    def snapshot(self, operation="runtime"):
        with self._lock:
            counters = dict(self._counters)
            durations = json.loads(json.dumps(self._durations))
            phases = json.loads(json.dumps(self._phase_durations))
            project_id = self.project_id
            scan_id = self.scan_id
            workload_id = self.workload_id
        elapsed_ms = (time.perf_counter() - self._started) * 1000.0
        return {
            "evidence_schema_version": self.SCHEMA_VERSION,
            "release": {
                "commit_sha": self.release_sha, "build_id": self.build_id,
            },
            "identity": {
                "project_id": project_id, "scan_id": scan_id,
                "workload_id": workload_id,
                "workload_hash": stable_identity(workload_id),
            },
            "operation": str(operation or "runtime"),
            "elapsed_ms": elapsed_ms,
            "peak_rss_bytes": _peak_rss_bytes(),
            "counters": counters,
            "durations": durations,
            "scan_phases": phases,
        }
