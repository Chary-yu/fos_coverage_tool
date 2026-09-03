"""Create an isolated blue/green database target from a verified backup.

The active database is never used as a migration target.  This module owns
the small amount of privileged work needed to create a new database and
restore a previously verified dump into it.  It refuses to touch a database
that already existed before the call; on failure it may remove only the
database created by this call.

The target is intentionally retained after a successful restore.  The
upgrade controller needs that same target for schema migration, FileState
rebuild, Candidate runtime checks, and rollback rehearsal.
"""

from __future__ import print_function

import gzip
import os
import subprocess

from scripts.maintenance.mysql_backup import (
    _DATABASE_IDENTIFIER, _client_settings, _run_client, _runtime_identity,
    compute_file_sha256,
)


RESTORE_FROM_VERIFIED_BACKUP = "restore_from_verified_backup"
PRE_RESTORED_CONSISTENT_BACKUP = "pre_restored_consistent_backup"
EMPTY_NEW_TARGET = "empty_new_target"
DISPOSABLE_TARGET_MODES = (
    RESTORE_FROM_VERIFIED_BACKUP,
    PRE_RESTORED_CONSISTENT_BACKUP,
    EMPTY_NEW_TARGET,
)
_DISPOSABLE_PREFIXES = ("coverage_vnext_", "coverage_candidate_", "coverage_gate_")
_CANDIDATE_PRIVILEGES = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX",
    "CREATE TEMPORARY TABLES",
)
_PRIVILEGE_TOKENS = frozenset(_CANDIDATE_PRIVILEGES)


def _mysql_section(config):
    value = (config or {}).get("mysql") if isinstance(config, dict) else None
    return dict(value or (config or {}))


def _target_name(value):
    name = str(value or "").strip()
    if not _DATABASE_IDENTIFIER.match(name):
        raise ValueError("disposable target database name is missing or unsafe")
    if not any(name.lower().startswith(prefix) for prefix in _DISPOSABLE_PREFIXES):
        raise ValueError(
            "disposable target database must use a coverage_vnext_, "
            "coverage_candidate_ or coverage_gate_ prefix"
        )
    return name


def _sql_string(value):
    """Quote a non-secret MySQL string literal without accepting SQL syntax."""
    return "'{}'".format(
        str(value).replace("\\", "\\\\").replace("'", "''")
    )


def _candidate_account(target):
    user = str(target.get("user") or "").strip()
    if not user:
        raise ValueError("disposable target application user is required")
    if user.lower() == "root":
        raise ValueError("disposable target application user may not be root")
    grant_host = str(
        target.get("candidate_grant_host") or target.get("grant_host") or
        target.get("host") or "127.0.0.1"
    ).strip()
    if not grant_host:
        raise ValueError("disposable target application grant host is required")
    configured = target.get("candidate_privileges") or \
        target.get("required_privileges") or _CANDIDATE_PRIVILEGES
    if isinstance(configured, str):
        configured = [item.strip() for item in configured.split(",") if item.strip()]
    privileges = []
    for value in configured:
        privilege = str(value).strip().upper()
        if privilege not in _PRIVILEGE_TOKENS:
            raise ValueError(
                "unsupported disposable target application privilege: {}".format(
                    privilege
                )
            )
        if privilege not in privileges:
            privileges.append(privilege)
    missing = [item for item in _CANDIDATE_PRIVILEGES if item not in privileges]
    if missing:
        raise ValueError(
            "disposable target application privileges are incomplete: {}".format(
                ", ".join(missing)
            )
        )
    return user, grant_host, privileges


def _application_settings(target):
    """Return target connection settings without restore-admin overrides."""
    result = dict(target or {})
    for key in (
            "backup_restore_host", "backup_restore_port",
            "backup_restore_user", "backup_restore_password"):
        result.pop(key, None)
    return result


def _row_value(row, key=None, index=0):
    """Read a DB-API row from either DictCursor or tuple-style cursors."""
    if isinstance(row, dict):
        if key and key in row:
            return row.get(key)
        values = list(row.values())
        return values[index] if len(values) > index else None
    if isinstance(row, (list, tuple)):
        return row[index] if len(row) > index else None
    return row


def probe_candidate_connection_access(connection, target_config):
    """Verify the already-open Candidate connection owns the target database.

    The disposable-target CLI path grants and probes the application account
    before returning a connection.  A caller may also supply a pre-restored
    target connection, so that path must receive the same fail-closed check;
    merely being able to connect as an administrator is not Candidate access.
    """
    target = _mysql_section(target_config)
    target_database = _target_name(target.get("database"))
    user, grant_host, privileges = _candidate_account(target)
    cursor = None
    probe_table = "__coverage_upgrade_privilege_probe_{}".format(os.getpid())
    created = False
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT DATABASE() AS database_name, CURRENT_USER() AS current_user"
        )
        identity_row = cursor.fetchone()
        observed_database = str(
            _row_value(identity_row, "database_name", 0) or ""
        ).strip()
        current_user = str(
            _row_value(identity_row, "current_user", 1) or ""
        ).strip()
        if observed_database.lower() != target_database.lower():
            raise RuntimeError(
                "Candidate connection selected database {} instead of {}".format(
                    observed_database or "<none>", target_database
                )
            )
        if user.lower() not in current_user.lower():
            raise RuntimeError(
                "Candidate connection current user does not match configured account"
            )

        cursor.execute("SHOW GRANTS")
        grants = cursor.fetchall() or []
        grants_text = "\n".join(str(_row_value(row)) for row in grants).upper()
        if target_database.upper() not in grants_text or user.upper() not in grants_text:
            raise RuntimeError(
                "Candidate connection SHOW GRANTS does not identify the target account/database"
            )
        missing = [
            privilege for privilege in privileges
            if privilege.upper() not in grants_text
        ]
        if missing:
            raise RuntimeError(
                "Candidate connection grants are incomplete: {}".format(
                    ", ".join(missing)
                )
            )

        cursor.execute(
            "CREATE TEMPORARY TABLE `{}` (probe_id INT)".format(probe_table)
        )
        created = True
        cursor.execute(
            "INSERT INTO `{}` (probe_id) VALUES (1)".format(probe_table)
        )
        cursor.execute(
            "UPDATE `{}` SET probe_id = 2 WHERE probe_id = 1".format(probe_table)
        )
        cursor.execute("SELECT COUNT(*) FROM `{}`".format(probe_table))
        cursor.fetchone()
        cursor.execute(
            "DELETE FROM `{}` WHERE probe_id = 2".format(probe_table)
        )
        cursor.execute(
            "ALTER TABLE `{}` ADD COLUMN probe_marker VARCHAR(1)".format(
                probe_table
            )
        )
        cursor.execute("DROP TEMPORARY TABLE `{}`".format(probe_table))
        created = False
        return {
            "status": "PASSED",
            "application_user": user,
            "application_grant_host": grant_host,
            "configured_privileges": list(privileges),
            "database": target_database,
            "current_user": current_user,
            "show_grants": {
                "status": "PASSED",
                "database": target_database,
                "account": "{}@{}".format(user, grant_host),
                "grant_count": len(grants),
            },
            "capability_probe": {
                "status": "PASSED",
                "operations": [
                    "SELECT", "INSERT", "UPDATE", "DELETE",
                    "CREATE TEMPORARY TABLES", "ALTER TEMPORARY TABLE",
                ],
                "table": probe_table,
            },
        }
    except Exception as exc:
        raise RuntimeError("Candidate application access probe failed: {}".format(exc))
    finally:
        if created and cursor is not None:
            try:
                cursor.execute("DROP TEMPORARY TABLE `{}`".format(probe_table))
            except Exception:
                pass
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass


def _grant_and_probe_candidate_access(
        admin_client, admin_common, admin_env, target, target_database):
    """Grant and verify the exact account used by the Candidate runtime.

    The database administrator is used only for the grant.  All subsequent
    probes run through the configured application account, so a successful
    admin connection can never masquerade as Candidate database access.
    """
    user, grant_host, privileges = _candidate_account(target)
    grant = (
        "GRANT {} ON `{}`.* TO {}@{}"
    ).format(
        ", ".join(privileges), target_database,
        _sql_string(user), _sql_string(grant_host),
    )
    grant_result = _run_client(admin_client, admin_common, admin_env, grant)
    if grant_result.returncode != 0:
        raise RuntimeError(
            "disposable target application grant failed: {}".format(
                grant_result.stderr.decode("utf-8", errors="replace")
            )
        )

    application = _application_settings(target)
    app_client, app_common, app_env = _client_settings(application)
    if not app_client:
        raise RuntimeError(
            "mariadb/mysql client is unavailable for Candidate access probe"
        )
    grants = _run_client(
        app_client, app_common, app_env, "SHOW GRANTS", database=target_database
    )
    grants_text = grants.stdout.decode("utf-8", errors="replace").strip()
    if grants.returncode != 0 or not grants_text:
        raise RuntimeError(
            "Candidate application SHOW GRANTS probe failed: {}".format(
                grants.stderr.decode("utf-8", errors="replace")
            )
        )
    normalized_grants = grants_text.upper()
    if target_database.upper() not in normalized_grants or \
            user.upper() not in normalized_grants:
        raise RuntimeError(
            "Candidate application SHOW GRANTS does not identify the target account/database"
        )
    missing_grants = [
        privilege for privilege in privileges
        if privilege.upper() not in normalized_grants
    ]
    if missing_grants:
        raise RuntimeError(
            "Candidate application grants are incomplete: {}".format(
                ", ".join(missing_grants)
            )
        )

    probe_table = "__coverage_upgrade_privilege_probe"
    probe_sql = (
        "CREATE TEMPORARY TABLE `{}` (probe_id INT); "
        "INSERT INTO `{}` (probe_id) VALUES (1); "
        "UPDATE `{}` SET probe_id = 2 WHERE probe_id = 1; "
        "SELECT COUNT(*) FROM `{}`; "
        "DELETE FROM `{}` WHERE probe_id = 2; "
        "ALTER TABLE `{}` ADD COLUMN probe_marker VARCHAR(1); "
        "DROP TEMPORARY TABLE `{}`"
    ).format(*([probe_table] * 7))
    probe = _run_client(
        app_client, app_common, app_env, probe_sql, database=target_database
    )
    if probe.returncode != 0:
        raise RuntimeError(
            "Candidate application capability probe failed: {}".format(
                probe.stderr.decode("utf-8", errors="replace")
            )
        )
    return {
        "status": "PASSED",
        "application_user": user,
        "application_grant_host": grant_host,
        "granted_privileges": list(privileges),
        "grant": {
            "status": "PASSED",
            "database": target_database,
            "privileges": list(privileges),
            "account": "{}@{}".format(user, grant_host),
            "command": "GRANT <candidate_privileges> ON `{}`.* TO <candidate_account>".format(
                target_database
            ),
            "exit_code": grant_result.returncode,
        },
        "show_grants": {
            "status": "PASSED",
            "database": target_database,
            "account": "{}@{}".format(user, grant_host),
            "output": grants_text[-4000:],
            "exit_code": grants.returncode,
        },
        "capability_probe": {
            "status": "PASSED",
            "operations": [
                "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE TEMPORARY TABLES",
                "ALTER TEMPORARY TABLE",
            ],
            "exit_code": probe.returncode,
        },
    }


def validate_disposable_target_config(source_config, target_config):
    """Validate names and server settings before any CREATE DATABASE call."""
    source = _mysql_section(source_config)
    target = _mysql_section(target_config)
    source_database = str(source.get("database") or "").strip()
    target_database = _target_name(target.get("database"))
    if not source_database or not _DATABASE_IDENTIFIER.match(source_database):
        raise ValueError("source database name is missing or unsafe")
    if source_database.lower() == target_database.lower():
        raise ValueError("disposable target database must differ from source database")
    application_user, grant_host, privileges = _candidate_account(target)
    return {
        "source_database": source_database,
        "target_database": target_database,
        "source_host": str(source.get("host", "127.0.0.1")),
        "source_port": int(source.get("port", 3306) or 3306),
        "target_host": str(target.get("host", "127.0.0.1")),
        "target_port": int(target.get("port", 3306) or 3306),
        "application_user": application_user,
        "application_grant_host": grant_host,
        "candidate_privileges": privileges,
    }


def _restore_process(client, common, env, dump_path, database):
    process = subprocess.Popen(
        [client] + common + ["--database={}".format(database)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env,
    )
    try:
        with gzip.open(dump_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                process.stdin.write(chunk)
        process.stdin.close()
        process.stdin = None
        _stdout, stderr = process.communicate()
    except Exception as exc:
        try:
            process.kill()
        except OSError:
            pass
        _stdout, stderr = process.communicate()
        raise RuntimeError(
            "disposable target restore process failed: {} ({})".format(
                exc, stderr.decode("utf-8", errors="replace")
            )
        )
    if process.returncode != 0:
        raise RuntimeError(
            "disposable target restore failed: {}".format(
                stderr.decode("utf-8", errors="replace")
            )
        )


def create_disposable_target_from_backup(
        backup_manifest, source_config, target_config,
        collation="utf8mb4_unicode_ci"):
    """Create and restore one never-before-existing blue/green target.

    ``backup_manifest`` must be the manifest returned by
    :func:`perform_database_backup`; both its declared dump SHA and the bytes
    on disk are checked before the restore starts.  The returned evidence is
    safe to persist in the upgrade manifest and contains no credentials.
    """
    backup_manifest = dict(backup_manifest or {})
    source = _mysql_section(source_config)
    target = _mysql_section(target_config)
    identity = validate_disposable_target_config(source, target)
    dump_path = str(
        backup_manifest.get("artifact_path") or
        os.path.join(str(backup_manifest.get("backup_dir") or ""), "full.sql.gz")
    ).strip()
    if not dump_path or not os.path.isfile(dump_path) or os.path.islink(dump_path):
        raise ValueError("verified backup dump for disposable target is missing")
    expected_sha = str(backup_manifest.get("full_sql_gz_sha256") or "").strip().lower()
    actual_sha = compute_file_sha256(dump_path).lower()
    if not expected_sha or expected_sha != actual_sha:
        raise ValueError("verified backup dump SHA256 does not match the manifest")
    declared_database = str(backup_manifest.get("database") or "").strip()
    if declared_database and declared_database.lower() != identity["source_database"].lower():
        raise ValueError("verified backup source database does not match source config")

    schema_inventory = ((backup_manifest.get("verification") or {}).get(
        "table_inventory") or backup_manifest.get("table_inventory") or [])
    schema_inventory = sorted(set(str(item) for item in schema_inventory if str(item)))
    if not schema_inventory:
        raise ValueError("verified backup has no schema table inventory")

    admin = dict(target)
    # A target may live on a separate host, but its administrative credentials
    # are still explicitly supplied by the target section.  The source
    # backup/restore credentials are a documented fallback for same-host
    # staging, never a reason to connect to the source database as target.
    for key in (
            "backup_restore_host", "backup_restore_port",
            "backup_restore_user", "backup_restore_password"):
        if key not in admin and key in source:
            admin[key] = source[key]
    source_client, source_common, source_env = _client_settings(source)
    client, common, env = _client_settings(admin)
    if not source_client or not client:
        raise RuntimeError("mariadb/mysql client is unavailable for disposable target")

    target_database = identity["target_database"]
    created = False
    try:
        exists = _run_client(
            client, common, env,
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE SCHEMA_NAME = '{}'".format(target_database),
        )
        if exists.returncode != 0:
            raise RuntimeError(
                "disposable target existence check failed: {}".format(
                    exists.stderr.decode("utf-8", errors="replace")
                )
            )
        if exists.stdout.decode("utf-8", errors="replace").strip():
            raise RuntimeError(
                "disposable target database already exists; refusing to reuse it"
            )

        source_ok, source_identity, source_error = _runtime_identity(
            source_client, source_common, source_env, identity["source_database"]
        )
        if not source_ok:
            raise RuntimeError(
                "source database identity query failed: {}".format(source_error)
            )

        create = _run_client(
            client, common, env,
            "CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE {}".format(
                target_database, collation
            ),
        )
        if create.returncode != 0:
            raise RuntimeError(
                "disposable target database creation failed: {}".format(
                    create.stderr.decode("utf-8", errors="replace")
                )
            )
        created = True
        _restore_process(client, common, env, dump_path, target_database)

        tables = _run_client(
            client, common, env, "SHOW TABLES", database=target_database,
        )
        if tables.returncode != 0:
            raise RuntimeError(
                "disposable target schema inspection failed: {}".format(
                    tables.stderr.decode("utf-8", errors="replace")
                )
            )
        observed_tables = sorted(set(
            line.strip() for line in tables.stdout.decode(
                "utf-8", errors="replace"
            ).splitlines() if line.strip()
        ))
        missing = sorted(set(schema_inventory) - set(observed_tables))
        if missing:
            raise RuntimeError(
                "disposable target restore is missing tables: {}".format(
                    ", ".join(missing)
                )
            )
        target_ok, target_identity, target_error = _runtime_identity(
            client, common, env, target_database
        )
        if not target_ok:
            raise RuntimeError(
                "disposable target identity query failed: {}".format(target_error)
            )
        candidate_access = _grant_and_probe_candidate_access(
            client, common, env, target, target_database
        )
        return {
            "status": "PASSED",
            "preparation_mode": RESTORE_FROM_VERIFIED_BACKUP,
            "source_database": identity["source_database"],
            "target_database": target_database,
            "target_database_created_by_this_run": True,
            "target_database_existed_before": False,
            "restore_completed": True,
            "source_database_untouched": True,
            "backup_artifact_path": os.path.abspath(dump_path),
            "backup_sha256": actual_sha,
            "schema_table_inventory": schema_inventory,
            "restored_table_inventory": observed_tables,
            "source_database_runtime_identity": source_identity,
            "target_database_runtime_identity": target_identity,
            "candidate_access": candidate_access,
            "target_retained_for_candidate": True,
        }
    except Exception as exc:
        if created:
            cleanup = _run_client(
                client, common, env,
                "DROP DATABASE `{}`".format(target_database),
            )
            if cleanup.returncode != 0:
                raise RuntimeError(
                    "{}; cleanup of newly-created target failed: {}".format(
                        exc, cleanup.stderr.decode("utf-8", errors="replace")
                    )
                )
        raise


def create_empty_disposable_target(source_config, target_config,
                                   collation="utf8mb4_unicode_ci"):
    """Create an empty blue/green target for the Legacy-to-VNext route.

    This is the companion to :func:`create_disposable_target_from_backup`:
    Legacy data is read from the source connection by the migration runner, so
    restoring the Legacy dump into the target would make the target look like
    Legacy and would bypass the empty-target gate.
    """
    source = _mysql_section(source_config)
    target = _mysql_section(target_config)
    identity = validate_disposable_target_config(source, target)
    admin = dict(target)
    for key in (
            "backup_restore_host", "backup_restore_port",
            "backup_restore_user", "backup_restore_password"):
        if key not in admin and key in source:
            admin[key] = source[key]
    client, common, env = _client_settings(admin)
    if not client:
        raise RuntimeError("mariadb/mysql client is unavailable for disposable target")

    target_database = identity["target_database"]
    created = False
    try:
        exists = _run_client(
            client, common, env,
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE SCHEMA_NAME = '{}'".format(target_database),
        )
        if exists.returncode != 0:
            raise RuntimeError(
                "disposable target existence check failed: {}".format(
                    exists.stderr.decode("utf-8", errors="replace")
                )
            )
        if exists.stdout.decode("utf-8", errors="replace").strip():
            raise RuntimeError(
                "disposable target database already exists; refusing to reuse it"
            )
        create = _run_client(
            client, common, env,
            "CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE {}".format(
                target_database, collation
            ),
        )
        if create.returncode != 0:
            raise RuntimeError(
                "disposable target database creation failed: {}".format(
                    create.stderr.decode("utf-8", errors="replace")
                )
            )
        created = True
        target_ok, target_identity, target_error = _runtime_identity(
            client, common, env, target_database
        )
        if not target_ok:
            raise RuntimeError(
                "empty disposable target identity query failed: {}".format(
                    target_error
                )
            )
        candidate_access = _grant_and_probe_candidate_access(
            client, common, env, target, target_database
        )
        return {
            "status": "PASSED",
            "preparation_mode": EMPTY_NEW_TARGET,
            "source_database": identity["source_database"],
            "target_database": target_database,
            "target_database_created_by_this_run": True,
            "target_database_existed_before": False,
            "restore_completed": False,
            "source_database_untouched": True,
            "target_database_runtime_identity": target_identity,
            "candidate_access": candidate_access,
            "target_retained_for_candidate": True,
        }
    except Exception as exc:
        if created:
            cleanup = _run_client(
                client, common, env,
                "DROP DATABASE `{}`".format(target_database),
            )
            if cleanup.returncode != 0:
                raise RuntimeError(
                    "{}; cleanup of newly-created target failed: {}".format(
                        exc, cleanup.stderr.decode("utf-8", errors="replace")
                    )
                )
        raise
