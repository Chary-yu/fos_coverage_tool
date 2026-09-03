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
    return {
        "source_database": source_database,
        "target_database": target_database,
        "source_host": str(source.get("host", "127.0.0.1")),
        "source_port": int(source.get("port", 3306) or 3306),
        "target_host": str(target.get("host", "127.0.0.1")),
        "target_port": int(target.get("port", 3306) or 3306),
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
