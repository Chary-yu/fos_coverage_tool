"""Unified bounded performance evidence contract.

The collector stores counters and aggregate timings only. It intentionally
does not retain source text, absolute paths, credentials, or unbounded request
labels. A snapshot is safe to attach to a release/scan/workload evidence
record and cheap enough to leave enabled in normal VNext operation.
"""

from __future__ import absolute_import

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
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


class InstrumentedCursor(object):
    """DB-API cursor proxy covering direct execute/executemany paths."""

    _performance_instrumented = True

    def __init__(self, cursor, collector=None):
        self._cursor = cursor
        self._collector = collector

    def _active_collector(self):
        return current_collector() or self._collector

    def _query(self, method, *args, **kwargs):
        started = time.perf_counter()
        try:
            result = getattr(self._cursor, method)(*args, **kwargs)
            collector = self._active_collector()
            rowcount = getattr(self._cursor, "rowcount", -1)
            affected_rows = self._valid_rowcount(rowcount)
            if (collector is not None and self._is_affected_statement(args) and
                    affected_rows is not None):
                collector.record_db_rows_affected(affected_rows)
            return result
        finally:
            collector = self._active_collector()
            if collector is not None:
                collector.record_db_query(
                    (time.perf_counter() - started) * 1000.0
                )

    def execute(self, *args, **kwargs):
        return self._query("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._query("executemany", *args, **kwargs)

    @staticmethod
    def _valid_rowcount(rowcount):
        try:
            value = int(rowcount)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def _is_affected_statement(self, args=()):
        """Return false for buffered result sets whose rowcount is a read.

        MySQL-compatible buffered cursors may expose SELECT's full row count
        immediately after ``execute``.  Counting that value as affected rows
        and then counting ``fetch*`` would double-count the same records.
        DB-API ``description`` is the portable result-set signal, so only a
        cursor without a result set contributes execute-time rowcount.
        """
        if bool(getattr(self._cursor, "description", None)):
            return False
        statement = args[0] if args else ""
        if isinstance(statement, bytes):
            statement = statement.decode("utf-8", "ignore")
        match = re.match(r"\s*(?:/\*.*?\*/\s*)?([A-Za-z]+)", str(statement))
        return not match or match.group(1).upper() not in (
            "SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH",
        )

    def _rows(self, method, *args, **kwargs):
        rows = getattr(self._cursor, method)(*args, **kwargs)
        collector = self._active_collector()
        if collector is not None:
            collector.record_db_rows_read(
                len(rows) if method != "fetchone" else int(rows is not None)
            )
        return rows

    def fetchone(self):
        return self._rows("fetchone")

    def fetchmany(self, *args, **kwargs):
        return self._rows("fetchmany", *args, **kwargs)

    def fetchall(self):
        return self._rows("fetchall")

    def __iter__(self):
        return self

    def __next__(self):
        try:
            row = next(self._cursor)
        except StopIteration:
            raise
        collector = self._active_collector()
        if collector is not None:
            collector.record_db_rows_read(1)
        return row

    next = __next__

    def close(self):
        return self._cursor.close()

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._cursor.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class InstrumentedConnection(object):
    """DB-API connection proxy that instruments every cursor it creates."""

    _performance_instrumented = True

    def __init__(self, connection, collector=None):
        self._connection = connection
        self._performance_collector = collector

    def _active_collector(self):
        return current_collector() or self._performance_collector

    def cursor(self, *args, **kwargs):
        cursor = self._connection.cursor(*args, **kwargs)
        if getattr(cursor, "_performance_instrumented", False):
            return cursor
        return InstrumentedCursor(cursor, self._active_collector())

    def execute(self, *args, **kwargs):
        cursor = self.cursor()
        cursor.execute(*args, **kwargs)
        return cursor

    def executemany(self, *args, **kwargs):
        cursor = self.cursor()
        cursor.executemany(*args, **kwargs)
        return cursor

    def executescript(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            result = self._connection.executescript(*args, **kwargs)
        finally:
            collector = self._active_collector()
            if collector is not None:
                collector.record_db_query(
                    (time.perf_counter() - started) * 1000.0
                )
        if getattr(result, "_performance_instrumented", False):
            return result
        return InstrumentedCursor(result, self._active_collector())

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def instrument_connection(connection, collector=None):
    """Return an idempotent DB-API proxy with dynamic collector binding."""
    if connection is None or getattr(connection, "_performance_instrumented", False):
        return connection
    return InstrumentedConnection(connection, collector or current_collector())


class PerformanceEvidenceCollector(object):
    SCHEMA_VERSION = 2

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
        self._started_at = time.time()
        self._finished_at = None
        self._status = "RUNNING"
        self._error_class = ""
        self._counters = {
            "request_count": 0, "request_bytes": 0, "response_bytes": 0,
            "db_query_count": 0, "db_rows": 0, "db_rows_read": 0,
            "db_rows_affected": 0, "db_time_ms": 0.0,
            "git_subprocess_count": 0, "git_bytes_read": 0,
            "bytes_read": 0, "bytes_written": 0,
            "cache_hits": 0, "cache_misses": 0, "cache_evictions": 0,
            "cache_bytes": 0, "sidecar_decode_count": 0,
            "payload_bytes": 0, "dom_nodes": 0, "long_tasks": 0,
        }
        self._durations = {}
        self._phase_durations = {}
        self._scan_evidence = OrderedDict()
        self._completed_scan_evidence = OrderedDict()
        self._max_scan_evidence = 64
        self._max_completed_scan_evidence = 64

    def bind(self, project_id=None, scan_id=None, workload_id=None):
        active = current_collector()
        if active is not None and active is not self:
            active.bind(project_id, scan_id, workload_id)
            return
        with self._lock:
            if project_id is not None:
                self.project_id = int(project_id)
            if scan_id is not None:
                self.scan_id = int(scan_id)
            if workload_id is not None:
                self.workload_id = str(workload_id)

    def child(self, project_id=None, scan_id=None, workload_id=None):
        """Create an isolated collector for one request/job/scan identity."""
        return PerformanceEvidenceCollector(
            {"commit_sha": self.release_sha, "build_id": self.build_id},
            project_id=project_id, scan_id=scan_id,
            workload_id=workload_id or self.workload_id,
        )

    @contextmanager
    def child_context(self, project_id=None, scan_id=None, workload_id=None):
        child = self.child(project_id, scan_id, workload_id)
        with bind_collector(child):
            try:
                yield child
            except BaseException as exc:
                status = (
                    "INTERRUPTED" if isinstance(
                        exc, (KeyboardInterrupt, SystemExit, GeneratorExit)
                    ) else "FAILED"
                )
                self._merge_child(child, status=status, error=exc)
                raise
            else:
                self._merge_child(child, status="COMPLETED")

    def _merge_child(self, child, status="COMPLETED", error=None):
        child._finish(status, error)
        child_snapshot = child.snapshot("scan")
        identity = child_snapshot.get("identity") or {}
        evidence_key = (
            identity.get("project_id"), identity.get("scan_id"),
            identity.get("workload_id"), child_snapshot.get("status"),
        )
        key = (
            identity.get("project_id"), identity.get("scan_id"),
            identity.get("workload_id"),
        )
        with child._lock:
            child_counters = dict(child._counters)
            child_durations = json.loads(json.dumps(child._durations))
            child_phases = json.loads(json.dumps(child._phase_durations))
        with self._lock:
            for field, value in child_counters.items():
                if field == "cache_bytes":
                    self._counters[field] = max(
                        int(self._counters.get(field) or 0), int(value or 0)
                    )
                else:
                    self._counters[field] = (
                        self._counters.get(field, 0) + value
                    )
            self._merge_timed_values(self._durations, child_durations)
            self._merge_timed_values(self._phase_durations, child_phases)
            self._scan_evidence[evidence_key] = child_snapshot
            self._scan_evidence.move_to_end(evidence_key)
            while len(self._scan_evidence) > self._max_scan_evidence:
                self._scan_evidence.popitem(last=False)
            if child_snapshot.get("status") == "COMPLETED":
                self._completed_scan_evidence[key] = child_snapshot
                self._completed_scan_evidence.move_to_end(key)
                while (len(self._completed_scan_evidence) >
                       self._max_completed_scan_evidence):
                    self._completed_scan_evidence.popitem(last=False)

    @staticmethod
    def _merge_timed_values(target, source):
        for operation, item in source.items():
            current = target.setdefault(operation, {
                "count": 0, "total_ms": 0.0, "max_ms": 0.0,
            })
            current["count"] += int(item.get("count") or 0)
            current["total_ms"] += float(item.get("total_ms") or 0.0)
            current["max_ms"] = max(
                current["max_ms"], float(item.get("max_ms") or 0.0)
            )

    def increment(self, field, amount=1):
        active = current_collector()
        if active is not None and active is not self:
            active.increment(field, amount)
            return
        with self._lock:
            self._counters[field] = self._counters.get(field, 0) + amount

    def observe(self, field, value):
        active = current_collector()
        if active is not None and active is not self:
            active.observe(field, value)
            return
        with self._lock:
            self._counters[field] = value

    def observe_duration(self, operation, elapsed_ms):
        active = current_collector()
        if active is not None and active is not self:
            active.observe_duration(operation, elapsed_ms)
            return
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
        active = current_collector()
        if active is not None and active is not self:
            with active.timed(operation):
                yield active
            return
        started = time.perf_counter()
        try:
            yield self
        finally:
            self.observe_duration(operation, (time.perf_counter() - started) * 1000.0)

    @contextmanager
    def phase(self, phase):
        active = current_collector()
        if active is not None and active is not self:
            with active.phase(phase):
                yield active
            return
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
        """Backward-compatible alias for rows read by repository helpers."""
        self.record_db_rows_read(count)

    def record_db_rows_read(self, count):
        value = int(count or 0)
        self.increment("db_rows_read", value)
        self.increment("db_rows", value)

    def record_db_rows_affected(self, count):
        value = int(count or 0)
        self.increment("db_rows_affected", value)
        self.increment("db_rows", value)

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

    def _finish(self, status, error=None):
        error_class = ""
        if error is not None:
            error_class = "{}.{}".format(
                getattr(error.__class__, "__module__", "builtins"),
                getattr(error.__class__, "__name__", "Exception"),
            )[:128]
        with self._lock:
            self._status = str(status or "FAILED")
            self._error_class = error_class
            self._finished_at = time.time()

    def snapshot(self, operation="runtime"):
        with self._lock:
            counters = dict(self._counters)
            durations = json.loads(json.dumps(self._durations))
            phases = json.loads(json.dumps(self._phase_durations))
            project_id = self.project_id
            scan_id = self.scan_id
            workload_id = self.workload_id
            status = self._status
            error_class = self._error_class
            started_at = self._started_at
            finished_at = self._finished_at
            scan_evidence = list(self._scan_evidence.values())
            completed_scans = list(self._completed_scan_evidence.values())
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
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "error_class": error_class,
            "operation": str(operation or "runtime"),
            "elapsed_ms": elapsed_ms,
            "peak_rss_bytes": _peak_rss_bytes(),
            "counters": counters,
            "durations": durations,
            "scan_phases": phases,
            "scan_evidence": scan_evidence,
            "completed_scan_evidence": completed_scans,
        }
