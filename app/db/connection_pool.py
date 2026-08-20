"""
MySQL Connection Pool Module (Item 4)
Thread-safe, bounded connection pool compatible with PyMySQL and Python 3.6+.
Features:
- Min / Max connection boundaries with borrow timeout
- Auto ping / reconnect on stale or dead connections
- Transaction safety: automatic rollback on return if connection is dirty
- Thread-safe queue management
- Clean pool shutdown
"""

import time
import threading
import logging
from queue import Queue, Empty
from contextlib import contextmanager
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

try:
    import pymysql
    HAVE_PYMYSQL = True
except ImportError:
    pymysql = None
    HAVE_PYMYSQL = False


def _is_connection_error(error):
    """Identify errors for which a read-only operation can be retried safely."""
    if isinstance(error, (ConnectionError, OSError)):
        return True
    if HAVE_PYMYSQL:
        try:
            if isinstance(error, (pymysql.err.OperationalError, pymysql.err.InterfaceError)):
                return True
        except AttributeError:
            pass
    try:
        return int(error.args[0]) in {2006, 2013, 2055}
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


class _RetryingReadOnlyCursor:
    """DB-API cursor proxy that retries one verified MySQL disconnect."""

    def __init__(self, connection, raw_cursor):
        self._connection = connection
        self._raw_cursor = raw_cursor

    def _run(self, method_name, *args, **kwargs):
        method = getattr(self._raw_cursor, method_name)
        try:
            return method(*args, **kwargs)
        except Exception as error:
            if not _is_connection_error(error) or not self._connection._reconnect():
                raise
            try:
                self._raw_cursor.close()
            except Exception:
                pass
            self._raw_cursor = self._connection._new_cursor()
            return getattr(self._raw_cursor, method_name)(*args, **kwargs)

    def execute(self, *args, **kwargs):
        return self._run("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._run("executemany", *args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._raw_cursor, name)


class _RetryingReadOnlyConnection:
    """Transparent PyMySQL connection proxy used only for read-only scopes."""

    def __init__(self, raw_connection, pool):
        self._connection = raw_connection
        self._pool = pool

    def _new_cursor(self, *args, **kwargs):
        return self._connection.cursor(*args, **kwargs)

    def _reconnect(self):
        try:
            self._connection.ping(reconnect=True)
            self._pool._record_metric("read_reconnects")
            return True
        except Exception:
            return False

    def cursor(self, *args, **kwargs):
        return _RetryingReadOnlyCursor(self, self._new_cursor(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._connection, name)

class PooledConnectionWrapper:
    """Wrapper around a DB-API connection tracking creation time and usage."""
    def __init__(self, raw_conn, pool: 'MySQLConnectionPool'):
        self.raw_conn = raw_conn
        self.pool = pool
        self.created_at = time.time()
        self.last_used_at = time.time()
        self.last_returned_at = time.time()
        self.is_closed = False
        self.read_only = False

    def is_alive(self, force_ping: bool = False) -> bool:
        if self.is_closed or not self.raw_conn:
            return False
        idle_for = time.time() - self.last_returned_at
        ping_after = self.pool.idle_ping_after_sec
        if not force_ping and idle_for < ping_after:
            self.pool._record_metric("ping_skips")
            return True
        try:
            if hasattr(self.raw_conn, "ping"):
                self.raw_conn.ping(reconnect=True)
                self.pool._record_metric("pings")
            return True
        except Exception:
            return False

    def rollback_if_dirty(self) -> None:
        if not self.pool.rollback_on_return:
            self.pool._record_metric("rollback_skips")
            return
        # read_only is only a Python-side hint. Skip cleanup only when the
        # DB-API connection itself confirms autocommit/read-transaction
        # semantics; ordinary PyMySQL connections use autocommit=False and
        # must still be rolled back for transaction hygiene.
        autocommit = False
        if self.read_only and self.raw_conn is not None:
            try:
                get_autocommit = getattr(self.raw_conn, "get_autocommit", None)
                autocommit = bool(get_autocommit()) if get_autocommit else False
            except Exception:
                autocommit = False
        if self.read_only and autocommit:
            self.pool._record_metric("rollback_skips")
            return
        try:
            if self.raw_conn and hasattr(self.raw_conn, "rollback"):
                self.raw_conn.rollback()
                self.pool._record_metric("rollbacks")
        except Exception as e:
            logger.warning(f"Error rolling back connection on pool return: {e}")

    def close(self) -> None:
        self.is_closed = True
        try:
            if self.raw_conn and hasattr(self.raw_conn, "close"):
                self.raw_conn.close()
        except Exception:
            pass

class MySQLConnectionPool:
    def __init__(
        self,
        db_config: Dict[str, Any],
        min_connections: int = 2,
        max_connections: int = 10,
        borrow_timeout: float = 10.0,
        max_lifetime_sec: float = 1800.0,
        idle_ping_after_sec: float = 60.0,
        rollback_on_return: bool = True,
    ):
        self.db_config = dict(db_config or {})
        self.min_connections = max(1, min_connections)
        self.max_connections = max(self.min_connections, max_connections)
        self.borrow_timeout = borrow_timeout
        self.max_lifetime_sec = max_lifetime_sec
        self.idle_ping_after_sec = max(0.0, float(
            self.db_config.get("idle_ping_after_sec", idle_ping_after_sec)
        ))
        self.rollback_on_return = bool(self.db_config.get(
            "rollback_on_return", rollback_on_return
        ))
        self.retry_read_operations = bool(self.db_config.get(
            "retry_read_operations", True
        ))
        
        self._pool: Queue = Queue(maxsize=self.max_connections)
        self._active_count = 0
        self._lock = threading.Lock()
        self._is_shutdown = False
        self._metrics = {
            "acquires": 0, "acquire_timeouts": 0, "reconnects": 0,
            "pings": 0, "ping_skips": 0, "rollbacks": 0,
            "rollback_skips": 0, "read_only_rollbacks": 0,
            "read_reconnects": 0,
        }

    def _record_metric(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._metrics[name] = self._metrics.get(name, 0) + int(amount)

    def _discard_wrapper(self, wrapper: PooledConnectionWrapper) -> None:
        wrapper.close()
        with self._lock:
            self._active_count = max(0, self._active_count - 1)

    def _create_raw_connection(self):
        if not HAVE_PYMYSQL:
            raise RuntimeError("PyMySQL is not installed. Connection pool cannot create live MySQL connection.")
        return pymysql.connect(
            host=self.db_config.get("host", "127.0.0.1"),
            port=int(self.db_config.get("port", 3306)),
            user=self.db_config.get("user", "root"),
            password=str(self.db_config.get("password", "")),
            database=self.db_config.get("database", "coverage_tool"),
            charset=self.db_config.get("charset", "utf8mb4"),
            autocommit=False,
            connect_timeout=float(self.db_config.get("connect_timeout", 5.0))
        )

    def borrow_connection(self) -> PooledConnectionWrapper:
        """Borrow a healthy connection from the pool."""
        if self._is_shutdown:
            raise RuntimeError("Connection pool has been shut down.")
        self._record_metric("acquires")
            
        deadline = time.time() + self.borrow_timeout
        while True:
            # 1. Try get from idle queue
            try:
                wrapper = self._pool.get_nowait()
                # Check lifetime and liveness
                if (time.time() - wrapper.created_at) > self.max_lifetime_sec or not wrapper.is_alive():
                    self._record_metric("reconnects")
                    self._discard_wrapper(wrapper)
                    continue
                wrapper.last_used_at = time.time()
                wrapper.read_only = False
                return wrapper
            except Empty:
                pass
                
            # 2. Try create new if below max
            with self._lock:
                if self._active_count < self.max_connections:
                    raw = self._create_raw_connection()
                    self._active_count += 1
                    wrapper = PooledConnectionWrapper(raw, self)
                    return wrapper
                    
            # 3. Wait for returned connection
            remaining = deadline - time.time()
            if remaining <= 0:
                self._record_metric("acquire_timeouts")
                raise TimeoutError("Connection pool exhausted ({}/{} active).".format(self._active_count, self.max_connections))
            try:
                wrapper = self._pool.get(timeout=min(remaining, 0.5))
                if (time.time() - wrapper.created_at) > self.max_lifetime_sec or not wrapper.is_alive():
                    self._record_metric("reconnects")
                    self._discard_wrapper(wrapper)
                    continue
                wrapper.last_used_at = time.time()
                wrapper.read_only = False
                return wrapper
            except Empty:
                continue

    def return_connection(self, wrapper: Optional[PooledConnectionWrapper]) -> None:
        """Return a connection to the pool after rolling back uncommitted state."""
        if not wrapper or wrapper.is_closed:
            return
            
        if self._is_shutdown:
            self._discard_wrapper(wrapper)
            return
            
        wrapper.rollback_if_dirty()
        wrapper.last_returned_at = time.time()
        try:
            self._pool.put_nowait(wrapper)
        except Exception:
            # Queue full, close it
            self._discard_wrapper(wrapper)

    @contextmanager
    def connection(self, read_only: bool = False):
        """Acquire a connection and always restore transaction hygiene.

        ``read_only`` is observational metadata for metrics; it never weakens
        rollback-on-return because PyMySQL connections use autocommit=False.
        """
        conn_wrapper = self.borrow_connection()
        conn_wrapper.read_only = bool(read_only)
        try:
            raw_connection = conn_wrapper.raw_conn
            module_name = str(getattr(raw_connection.__class__, "__module__", ""))
            if (read_only and self.retry_read_operations
                    and module_name.startswith("pymysql")
                    and hasattr(raw_connection, "cursor")
                    and hasattr(raw_connection, "ping")):
                yield _RetryingReadOnlyConnection(raw_connection, self)
            else:
                yield raw_connection
        finally:
            self.return_connection(conn_wrapper)

    def close_all(self) -> None:
        """Close all idle and active connections in the pool."""
        self._is_shutdown = True
        while not self._pool.empty():
            try:
                wrapper = self._pool.get_nowait()
                wrapper.close()
                with self._lock:
                    self._active_count = max(0, self._active_count - 1)
            except Empty:
                break

    def metrics(self) -> Dict[str, int]:
        with self._lock:
            active = self._active_count
            metrics = dict(self._metrics)
        return dict(metrics, active=active, idle=self._pool.qsize(), waiters=0)

_GLOBAL_POOLS = {}
_GLOBAL_POOL_REFS = {}
# Kept as a compatibility alias for older diagnostics/importers.  New code
# must use the config-keyed registry below rather than one process-global DB.
_GLOBAL_POOL: Optional[MySQLConnectionPool] = None
_GLOBAL_POOL_LOCK = threading.Lock()

def _normalized_pool_config(db_config):
    raw = dict(db_config or {})
    mysql = raw.get("mysql")
    if isinstance(mysql, dict):
        merged = dict(mysql)
        merged.update(raw.get("pool") or {})
        return merged
    return raw


def _pool_key(db_config):
    config = _normalized_pool_config(db_config)
    # Include credentials in the identity so a rotated password never reuses
    # a live connection authenticated with the previous secret.  The key is
    # process-local and is never emitted in metrics/logs.
    fields = (
        "host", "port", "user", "password", "database", "charset",
        "connect_timeout", "idle_ping_after_sec", "rollback_on_return",
        "min_connections", "max_connections", "borrow_timeout",
        "max_lifetime_sec",
    )
    return tuple((name, str(config.get(name, ""))) for name in fields)


def get_global_pool(db_config: Optional[Dict[str, Any]] = None) -> Optional[MySQLConnectionPool]:
    global _GLOBAL_POOL
    if not db_config:
        return _GLOBAL_POOL
    normalized = _normalized_pool_config(db_config)
    key = _pool_key(normalized)
    with _GLOBAL_POOL_LOCK:
        pool = _GLOBAL_POOLS.get(key)
        if pool is None or pool._is_shutdown:
            pool = MySQLConnectionPool(normalized)
            _GLOBAL_POOLS[key] = pool
            _GLOBAL_POOL_REFS[key] = 0
        _GLOBAL_POOL_REFS[key] = int(_GLOBAL_POOL_REFS.get(key, 0)) + 1
        _GLOBAL_POOL = pool
        return pool


def release_global_pool(pool) -> None:
    """Release one manager reference without closing a shared pool early."""
    global _GLOBAL_POOL
    if pool is None:
        return
    with _GLOBAL_POOL_LOCK:
        for key, candidate in list(_GLOBAL_POOLS.items()):
            if candidate is not pool:
                continue
            refs = max(0, int(_GLOBAL_POOL_REFS.get(key, 0)) - 1)
            _GLOBAL_POOL_REFS[key] = refs
            if refs == 0:
                candidate.close_all()
                _GLOBAL_POOLS.pop(key, None)
                _GLOBAL_POOL_REFS.pop(key, None)
                if _GLOBAL_POOL is candidate:
                    _GLOBAL_POOL = next(iter(_GLOBAL_POOLS.values()), None)
            break

def close_global_pool() -> None:
    global _GLOBAL_POOL, _GLOBAL_POOLS, _GLOBAL_POOL_REFS
    with _GLOBAL_POOL_LOCK:
        for pool in list(_GLOBAL_POOLS.values()):
            pool.close_all()
        _GLOBAL_POOLS = {}
        _GLOBAL_POOL_REFS = {}
        _GLOBAL_POOL = None
