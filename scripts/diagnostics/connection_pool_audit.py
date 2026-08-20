"""Executable transaction/liveness audit for the pooled DB boundary."""

import json
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract

from app.db.connection_pool import (
    MySQLConnectionPool,
    PooledConnectionWrapper,
    get_global_pool,
    release_global_pool,
)


class _Cursor(object):
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def execute(self, query, params=()):
        if self.connection.dead:
            raise ConnectionError("connection was closed by the server")
        return 1

    def close(self):
        self.closed = True


class _Connection(object):
    __module__ = "pymysql.connections"

    def __init__(self, autocommit=False, dead=False):
        self.autocommit = autocommit
        self.dead = dead
        self.rollback_count = 0
        self.ping_count = 0

    def get_autocommit(self):
        return self.autocommit

    def rollback(self):
        self.rollback_count += 1

    def ping(self, reconnect=False):
        self.ping_count += 1
        self.dead = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        pass


def audit():
    failures = []

    pool = MySQLConnectionPool(
        {"idle_ping_after_sec": 60, "retry_read_operations": True},
        min_connections=1, max_connections=1,
    )
    writer = _Connection(autocommit=False)
    pool.return_connection(PooledConnectionWrapper(writer, pool))
    with pool.connection(read_only=True):
        pass
    if writer.rollback_count != 2:
        failures.append("autocommit=False read-only return did not rollback")

    autocommit_reader = _Connection(autocommit=True)
    autocommit_wrapper = PooledConnectionWrapper(autocommit_reader, pool)
    autocommit_wrapper.read_only = True
    pool.return_connection(autocommit_wrapper)
    with pool.connection(read_only=True):
        pass
    if autocommit_reader.rollback_count != 0:
        failures.append("autocommit read-only return performed an unnecessary rollback")

    dead_pool = MySQLConnectionPool(
        {"idle_ping_after_sec": 60, "retry_read_operations": True},
        min_connections=1, max_connections=1,
    )
    dead = _Connection(dead=True)
    dead_pool.return_connection(PooledConnectionWrapper(dead, dead_pool))
    with dead_pool.connection(read_only=True) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
    if dead.ping_count != 1 or dead_pool.metrics().get("read_reconnects") != 1:
        failures.append("read-only dead connection was not reconnected exactly once")
    pool.close_all()
    dead_pool.close_all()

    first = get_global_pool({"host": "127.0.0.1", "port": 13306, "database": "audit_a"})
    second = get_global_pool({"host": "127.0.0.1", "port": 13306, "database": "audit_b"})
    if first is second:
        failures.append("different database identities shared one global pool")
    release_global_pool(first)
    if second._is_shutdown:
        failures.append("releasing one runtime closed another runtime's pool")
    release_global_pool(second)

    return with_contract({
        "status": "PASSED" if not failures else "FAILED",
        "evidence_class": "runtime_reliability_audit",
        "checks": {
            "rollback_on_return": not bool(failures),
            "read_reconnect": not bool(failures),
            "config_keyed_pool": not bool(failures),
        },
        "violations": failures,
    })


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
