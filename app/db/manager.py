"""Canonical pooled database manager used by request and job services."""

from contextlib import contextmanager
from typing import Any, Dict, Optional

from app.db.connection_pool import get_global_pool, release_global_pool


class DatabaseManager:
    """Canonical manager boundary for request/job/runtime callers.

    The legacy root entrypoint is still supported while its large method set
    is being retired.  Construction from the root module is redirected through
    this boundary, so new callers do not import or name the legacy class.
    """
    def __init__(self, config: Optional[Dict[str, Any]] = None, pool=None):
        cfg = (config or {}).get("mysql", config or {})
        self.config = dict(cfg)
        pool_config = dict(self.config)
        pool_config.update((self.config.get("pool") or {}))
        self._owns_pool = pool is not None
        self.pool = pool or get_global_pool(pool_config)
        if self.pool is None:
            raise RuntimeError("database pool is not configured")

    @contextmanager
    def connection(self, read_only: bool = False):
        with self.pool.connection(read_only=read_only) as conn:
            yield conn

    def close(self):
        if self._owns_pool:
            self.pool.close_all()
        else:
            release_global_pool(self.pool)

    def health(self):
        return self.pool.metrics()


class LegacyManagerAdapter:
    """Thin compatibility class bound to the legacy method surface once.

    Binding happens after the root module has defined its historical methods;
    the root name remains this adapter, while new code uses the pooled manager
    above.  Copying the method descriptors also keeps old ``object.__new__``
    test/extension usage working without a second independent implementation.
    """

    _legacy_class = None

    @classmethod
    def bind_legacy(cls, legacy_class):
        cls._legacy_class = legacy_class
        for name, value in legacy_class.__dict__.items():
            if name not in ("__dict__", "__weakref__", "__module__", "__doc__"):
                setattr(cls, name, value)

    def __init__(self, *args, **kwargs):
        if self._legacy_class is None:
            raise RuntimeError("legacy database manager has not been bound")
        self._legacy_class.__init__(self, *args, **kwargs)
