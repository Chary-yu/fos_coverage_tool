"""Runtime database identity and Source/Target separation checks.

Configuration equality is not enough: aliases and different credentials can
still reach the same MariaDB instance.  This module keeps the probe small and
DB-API based so it works with PyMySQL and deterministic SQLite tests.
"""

from __future__ import print_function

import hashlib
import json
import socket

from app.db.repositories.base import fetchone, is_sqlite


def _redact(value):
    text = str(value or "")
    return text if len(text) <= 2 else text[:1] + "***" + text[-1:]


def fingerprint_connection(connection, configured=None):
    configured = dict(configured or {})
    if is_sqlite(connection):
        result = {
            "engine": "sqlite",
            "configured_host": _redact(configured.get("host", "")),
            "configured_port": int(configured.get("port", 0) or 0),
            "configured_database": str(configured.get("database", "")),
            "database": "sqlite",
            "hostname": socket.gethostname(),
            "port": 0,
            "datadir": "",
            "server_uuid": "",
            "probe_status": "PASSED",
        }
        # SQLite has no server identity.  The connection object identity is
        # still useful for tests and for rejecting the exact same handle from
        # being presented as Source and Target.
        result["runtime_key"] = hashlib.sha256(
            "sqlite:{}".format(id(getattr(connection, "_connection", connection))).encode("utf-8")
        ).hexdigest()
        return result
    queries = {
        "database": "SELECT DATABASE() AS database_name",
        "hostname": "SELECT @@hostname AS hostname",
        "port": "SELECT @@port AS port",
        "datadir": "SELECT @@datadir AS datadir",
    }
    values = {}
    for name, query in queries.items():
        row = fetchone(connection, query)
        if not row:
            raise RuntimeError("database identity probe returned no row: {}".format(name))
        values[name] = row.get(list(row.keys())[0])
    try:
        row = fetchone(connection, "SELECT @@server_uuid AS server_uuid")
        values["server_uuid"] = (row or {}).get("server_uuid") or ""
    except Exception:
        values["server_uuid"] = ""
    fingerprint = {
        "engine": "mysql",
        "configured_host": _redact(configured.get("host", "")),
        "configured_port": int(configured.get("port", 3306) or 3306),
        "configured_database": str(configured.get("database", "")),
        "database": str(values.get("database") or ""),
        "hostname": str(values.get("hostname") or ""),
        "port": int(values.get("port") or 0),
        "datadir": str(values.get("datadir") or ""),
        "server_uuid": str(values.get("server_uuid") or ""),
        "probe_status": "PASSED",
    }
    fingerprint["runtime_key"] = hashlib.sha256(json.dumps({
        "database": fingerprint["database"],
        "hostname": fingerprint["hostname"],
        "port": fingerprint["port"],
        "datadir": fingerprint["datadir"],
        "server_uuid": fingerprint["server_uuid"],
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return fingerprint


def assert_separate_connections(source_connection, target_connection,
                                source_config=None, target_config=None):
    source = fingerprint_connection(source_connection, source_config)
    target = fingerprint_connection(target_connection, target_config)
    if source.get("runtime_key") == target.get("runtime_key"):
        raise ValueError("source and target database runtime fingerprints are identical")
    if (source.get("engine") == "mysql" and target.get("engine") == "mysql"
            and source.get("database") == target.get("database")):
        raise ValueError("source and target database names are identical on different-looking connections")
    return {"source": source, "target": target, "status": "PASSED"}
