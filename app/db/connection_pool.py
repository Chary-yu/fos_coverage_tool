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

class PooledConnectionWrapper:
    """Wrapper around a DB-API connection tracking creation time and usage."""
    def __init__(self, raw_conn, pool: 'MySQLConnectionPool'):
        self.raw_conn = raw_conn
        self.pool = pool
        self.created_at = time.time()
        self.last_used_at = time.time()
        self.is_closed = False

    def is_alive(self) -> bool:
        if self.is_closed or not self.raw_conn:
            return False
        try:
            if hasattr(self.raw_conn, "ping"):
                self.raw_conn.ping(reconnect=True)
            return True
        except Exception:
            return False

    def rollback_if_dirty(self) -> None:
        try:
            if self.raw_conn and hasattr(self.raw_conn, "rollback"):
                self.raw_conn.rollback()
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
        max_lifetime_sec: float = 1800.0
    ):
        self.db_config = dict(db_config or {})
        self.min_connections = max(1, min_connections)
        self.max_connections = max(self.min_connections, max_connections)
        self.borrow_timeout = borrow_timeout
        self.max_lifetime_sec = max_lifetime_sec
        
        self._pool: Queue = Queue(maxsize=self.max_connections)
        self._active_count = 0
        self._lock = threading.Lock()
        self._is_shutdown = False
        self._metrics = {"acquires": 0, "acquire_timeouts": 0, "reconnects": 0}

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
        self._metrics["acquires"] += 1
            
        deadline = time.time() + self.borrow_timeout
        while True:
            # 1. Try get from idle queue
            try:
                wrapper = self._pool.get_nowait()
                # Check lifetime and liveness
                if (time.time() - wrapper.created_at) > self.max_lifetime_sec or not wrapper.is_alive():
                    self._metrics["reconnects"] += 1
                    wrapper.close()
                    with self._lock:
                        self._active_count -= 1
                    continue
                wrapper.last_used_at = time.time()
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
                self._metrics["acquire_timeouts"] += 1
                raise TimeoutError("Connection pool exhausted ({}/{} active).".format(self._active_count, self.max_connections))
            try:
                wrapper = self._pool.get(timeout=min(remaining, 0.5))
                if (time.time() - wrapper.created_at) > self.max_lifetime_sec or not wrapper.is_alive():
                    self._metrics["reconnects"] += 1
                    wrapper.close()
                    with self._lock:
                        self._active_count -= 1
                    continue
                wrapper.last_used_at = time.time()
                return wrapper
            except Empty:
                continue

    def return_connection(self, wrapper: Optional[PooledConnectionWrapper]) -> None:
        """Return a connection to the pool after rolling back uncommitted state."""
        if not wrapper or wrapper.is_closed:
            return
            
        if self._is_shutdown:
            wrapper.close()
            with self._lock:
                self._active_count -= 1
            return
            
        wrapper.rollback_if_dirty()
        try:
            self._pool.put_nowait(wrapper)
        except Exception:
            # Queue full, close it
            wrapper.close()
            with self._lock:
                self._active_count -= 1

    @contextmanager
    def connection(self):
        """Context manager for acquiring and safely returning connection."""
        conn_wrapper = self.borrow_connection()
        try:
            yield conn_wrapper.raw_conn
        finally:
            self.return_connection(conn_wrapper)

    def close_all(self) -> None:
        """Close all idle and active connections in the pool."""
        self._is_shutdown = True
        while not self._pool.empty():
            try:
                wrapper = self._pool.get_nowait()
                wrapper.close()
            except Empty:
                break
        with self._lock:
            self._active_count = 0

    def metrics(self) -> Dict[str, int]:
        with self._lock:
            active = self._active_count
        return dict(self._metrics, active=active, idle=self._pool.qsize(), waiters=0)

_GLOBAL_POOL: Optional[MySQLConnectionPool] = None
_GLOBAL_POOL_LOCK = threading.Lock()

def get_global_pool(db_config: Optional[Dict[str, Any]] = None) -> Optional[MySQLConnectionPool]:
    global _GLOBAL_POOL
    if _GLOBAL_POOL is not None:
        return _GLOBAL_POOL
    with _GLOBAL_POOL_LOCK:
        if _GLOBAL_POOL is None and db_config:
            _GLOBAL_POOL = MySQLConnectionPool(db_config)
    return _GLOBAL_POOL

def close_global_pool() -> None:
    global _GLOBAL_POOL
    with _GLOBAL_POOL_LOCK:
        if _GLOBAL_POOL is not None:
            _GLOBAL_POOL.close_all()
            _GLOBAL_POOL = None
