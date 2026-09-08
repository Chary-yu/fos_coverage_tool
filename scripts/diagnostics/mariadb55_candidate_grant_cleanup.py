"""Execute the Candidate GRANT -> scoped REVOKE lifecycle on real MariaDB.

This is a narrow regression for the R7 production failure.  It uses the SQL
builder owned by scripts.upgrade.disposable_target and proves that revoking the
Candidate database privileges leaves an unrelated sentinel database grant
unchanged.  All objects are disposable and are removed in a finally block.
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pymysql

from scripts.upgrade.disposable_target import (
    _CANDIDATE_PRIVILEGES, _candidate_revoke_sql,
)


def _privileges(cursor, database, user, host):
    grantee = "'{}'@'{}'".format(user, host)
    cursor.execute(
        "SELECT PRIVILEGE_TYPE FROM INFORMATION_SCHEMA.SCHEMA_PRIVILEGES "
        "WHERE GRANTEE=%s AND TABLE_SCHEMA=%s ORDER BY PRIVILEGE_TYPE",
        (grantee, database),
    )
    return sorted(set(str(row[0]).upper() for row in cursor.fetchall()))


def run(host, port, user, password, required_version_prefix="5.5"):
    suffix = "{}_{}".format(os.getpid(), int(time.time()))
    candidate_db = "coverage_vnext_r8cleanup_{}".format(suffix)
    sentinel_db = "coverage_gate_r8sentinel_{}".format(suffix)
    app_user = "covr8_{}".format(os.getpid())
    app_host = "127.0.0.1"
    connection = pymysql.connect(
        host=host, port=int(port), user=user, password=password,
        charset="utf8mb4", autocommit=True,
    )
    cursor = connection.cursor()
    created_user = False
    created_candidate = False
    created_sentinel = False
    try:
        cursor.execute("SELECT VERSION()")
        version = str(cursor.fetchone()[0])
        if required_version_prefix and not version.startswith(required_version_prefix):
            raise RuntimeError(
                "database version {} does not match required prefix {}".format(
                    version, required_version_prefix
                )
            )
        cursor.execute("CREATE DATABASE `{}`".format(candidate_db))
        created_candidate = True
        cursor.execute("CREATE DATABASE `{}`".format(sentinel_db))
        created_sentinel = True
        cursor.execute(
            "CREATE USER '{}'@'{}' IDENTIFIED BY %s".format(app_user, app_host),
            ("r8-cleanup-fixture",),
        )
        created_user = True
        cursor.execute(
            "GRANT SELECT ON `{}`.* TO '{}'@'{}'".format(
                sentinel_db, app_user, app_host
            )
        )
        sentinel_before = _privileges(
            cursor, sentinel_db, app_user, app_host
        )
        cursor.execute(
            "GRANT {} ON `{}`.* TO '{}'@'{}'".format(
                ", ".join(_CANDIDATE_PRIVILEGES), candidate_db,
                app_user, app_host,
            )
        )
        candidate_before = _privileges(
            cursor, candidate_db, app_user, app_host
        )
        revoke_sql = _candidate_revoke_sql(
            candidate_db, app_user, app_host, _CANDIDATE_PRIVILEGES
        )
        if "ALL PRIVILEGES" in revoke_sql.upper() or \
                "GRANT OPTION" in revoke_sql.upper():
            raise RuntimeError("R8 revoke SQL regressed to unsupported ALL/GRANT OPTION shape")
        cursor.execute(revoke_sql)
        candidate_after = _privileges(
            cursor, candidate_db, app_user, app_host
        )
        sentinel_after = _privileges(
            cursor, sentinel_db, app_user, app_host
        )
        if candidate_after:
            raise RuntimeError(
                "Candidate privileges remain after scoped revoke: {}".format(
                    ", ".join(candidate_after)
                )
            )
        if sentinel_before != sentinel_after or sentinel_after != ["SELECT"]:
            raise RuntimeError("unrelated sentinel grant changed during Candidate cleanup")
        return {
            "status": "PASSED",
            "evidence_class": "mariadb55_candidate_grant_cleanup",
            "database_version": version,
            "required_version_prefix": required_version_prefix,
            "candidate_privileges_before": candidate_before,
            "candidate_privileges_after": candidate_after,
            "candidate_privileges_zero": True,
            "sentinel_privileges_before": sentinel_before,
            "sentinel_privileges_after": sentinel_after,
            "non_candidate_grants_conserved": True,
            "revoke_uses_explicit_privileges": True,
            "credentials_recorded": False,
        }
    finally:
        if created_candidate:
            try:
                cursor.execute("DROP DATABASE IF EXISTS `{}`".format(candidate_db))
            except Exception:
                pass
        if created_sentinel:
            try:
                cursor.execute("DROP DATABASE IF EXISTS `{}`".format(sentinel_db))
            except Exception:
                pass
        if created_user:
            try:
                cursor.execute("DROP USER '{}'@'{}'".format(app_user, app_host))
            except Exception:
                pass
        try:
            cursor.close()
        finally:
            connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--require-version-prefix", default="5.5")
    args = parser.parse_args(argv)
    try:
        result = run(
            args.host, args.port, args.user, args.password,
            required_version_prefix=args.require_version_prefix,
        )
    except Exception as exc:
        result = {
            "status": "FAILED",
            "evidence_class": "mariadb55_candidate_grant_cleanup",
            "error_class": type(exc).__name__,
            "error": str(exc),
            "credentials_recorded": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
