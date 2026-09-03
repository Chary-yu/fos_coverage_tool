"""
MySQL Full Backup and Integrity Check Module (Item 20)
Provides automated backup generation:
- full.sql.gz
- full.sql.gz.sha256
- schema.sql
- critical-counts.json
- critical-content-hashes.json
Enforces zero data loss: fails closed if mysqldump is missing.
"""

import os
import sys
import json
import gzip
import hashlib
import subprocess
import shutil
import re
from typing import Dict, Any, Optional, Tuple

from scripts.diagnostics.data_hash_gate import capture_database_snapshot
from app.time_utils import utc_iso


_DATABASE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _capture_generation_aware_snapshot(connection):
    """Capture a backup witness without assuming the Legacy schema.

    The old data-hash helper intentionally knows only the four Legacy tables.
    A live Existing-VNext database must be captured through the same
    authoritative, ID-independent snapshot used by its blue/green migration.
    The ``tables`` compatibility view keeps the backup manifest format useful
    to existing consumers while preserving the richer VNext semantic hashes.
    """
    from scripts.upgrade.database_generation import (
        LEGACY, UNKNOWN, VNEXT, inspect_database_generation,
    )

    generation = inspect_database_generation(connection)
    value = generation.get("generation")
    if value == LEGACY:
        snapshot = capture_database_snapshot(connection)
        snapshot["generation"] = LEGACY
        return snapshot
    if value == VNEXT:
        from scripts.upgrade.existing_vnext_upgrade import (
            capture_vnext_authoritative_snapshot,
        )
        authoritative = capture_vnext_authoritative_snapshot(connection)
        counts = authoritative.get("counts") or {}
        hashes = authoritative.get("component_hashes") or {}
        return {
            "snapshot_version": authoritative.get("snapshot_version"),
            "captured_at": utc_iso(),
            "generation": VNEXT,
            "semantic_hash": authoritative.get("semantic_hash", ""),
            "components": authoritative.get("components") or {},
            "counts": dict(counts),
            "component_hashes": dict(hashes),
            "tables": {
                "coverage_{}".format(component): {
                    "count": counts.get(component, 0),
                    "content_hash": hashes.get(component, ""),
                }
                for component in sorted(counts)
            },
        }
    raise RuntimeError(
        "database generation is {} ({})".format(
            UNKNOWN, generation.get("reason", "unclassified")
        )
    )


def _is_within(path: str, root: str) -> bool:
    """Return whether *path* is equal to or below *root* after resolution."""
    try:
        return os.path.commonpath([
            os.path.realpath(os.path.abspath(path)),
            os.path.realpath(os.path.abspath(root)),
        ]) == os.path.realpath(os.path.abspath(root))
    except (AttributeError, OSError, ValueError):
        return False


def _client_settings(config: Dict[str, Any]):
    """Return a CLI client, safe connection arguments, and its environment."""
    client = shutil.which("mariadb") or shutil.which("mysql")
    if not client:
        return None, [], None
    cfg = dict(config or {})
    env = os.environ.copy()
    password = cfg.get("backup_restore_password", cfg.get("password", ""))
    if password:
        env["MYSQL_PWD"] = str(password)
    else:
        env.pop("MYSQL_PWD", None)
    common = [
        "--host={}".format(cfg.get("backup_restore_host", cfg.get("host", "127.0.0.1"))),
        "--port={}".format(int(cfg.get("backup_restore_port", cfg.get("port", 3306)))),
        "--user={}".format(cfg.get("backup_restore_user", cfg.get("user", "root"))),
    ]
    return client, common, env


def _run_client(client, common, env, sql, database=None):
    command = [client] + list(common)
    if database:
        command.append("--database={}".format(database))
    command.extend(["--batch", "--skip-column-names", "--execute", sql])
    return subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )


def _runtime_identity(client, common, env, database):
    """Capture non-secret server identity for restore evidence."""
    result = _run_client(
        client, common, env,
        "SELECT VERSION(), @@hostname, @@port, DATABASE()",
        database=database,
    )
    if result.returncode != 0:
        return False, {}, result.stderr.decode("utf-8", errors="replace")
    line = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if not line or len(line[0].split("\t")) < 4:
        return False, {}, "database runtime identity query returned incomplete data"
    version, hostname, port, selected_database = line[0].split("\t", 3)
    return True, {
        "database": selected_database or database,
        "version": version,
        "hostname": hostname,
        "port": int(port) if str(port).isdigit() else port,
        "client": os.path.basename(client),
    }, ""


def _restore_into_empty_database(
    full_sql_gz: str,
    schema_tables,
    config: Dict[str, Any],
    restore_database: str,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """Restore a verified dump into a newly-created, isolated database.

    The target must not exist before this function starts.  It is dropped in a
    finally block only when this function created it, so an operator typo can
    never make the verifier delete an unrelated existing database.
    """
    result = {
        "restore_database": restore_database,
        "restore_target_empty_before_restore": False,
        "restore_smoke": "NOT_REQUESTED",
    }
    source_database = str((config or {}).get("database") or "")
    if not _DATABASE_IDENTIFIER.match(restore_database):
        return False, result, "restore database name is unsafe"
    if not source_database or not _DATABASE_IDENTIFIER.match(source_database):
        return False, result, "source database name is missing or unsafe"
    if restore_database.lower() == source_database.lower():
        return False, result, "restore target must differ from source database"

    client, common, env = _client_settings(config)
    if not client:
        return False, result, "mariadb/mysql client is unavailable for restore smoke"

    created = False
    success = False
    error = None
    try:
        exists = _run_client(
            client, common, env,
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE SCHEMA_NAME = '{}'".format(restore_database),
        )
        if exists.returncode != 0:
            error = "restore target existence check failed: {}".format(
                exists.stderr.decode("utf-8", errors="replace")
            )
        elif exists.stdout.decode("utf-8", errors="replace").strip():
            error = "restore target database already exists; an empty target is required"
        else:
            create = _run_client(
                client, common, env,
                "CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci".format(
                    restore_database
                ),
            )
            if create.returncode != 0:
                error = "restore target database creation failed: {}".format(
                    create.stderr.decode("utf-8", errors="replace")
                )
            else:
                created = True
                result["restore_target_empty_before_restore"] = True

        if error is None:
            source_ok, source_identity, source_error = _runtime_identity(
                client, common, env, source_database
            )
            if not source_ok:
                error = "source database identity query failed: {}".format(source_error)
            else:
                result["source_database_runtime_identity"] = source_identity

        if error is None:
            restore = subprocess.Popen(
                [client] + common + ["--database={}".format(restore_database)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env,
            )
            try:
                with gzip.open(full_sql_gz, "rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        restore.stdin.write(chunk)
                restore.stdin.close()
                restore.stdin = None
                _stdout, stderr = restore.communicate()
            except Exception as exc:
                try:
                    restore.kill()
                except OSError:
                    pass
                _stdout, stderr = restore.communicate()
                error = "restore smoke process failed: {} ({})".format(
                    exc, stderr.decode("utf-8", errors="replace")
                )
            if error is None and restore.returncode != 0:
                error = "restore smoke failed: {}".format(
                    stderr.decode("utf-8", errors="replace")
                )

        if error is None:
            tables_check = _run_client(
                client, common, env, "SHOW TABLES", database=restore_database,
            )
            if tables_check.returncode != 0:
                error = "restored schema inspection failed: {}".format(
                    tables_check.stderr.decode("utf-8", errors="replace")
                )
            else:
                restored_tables = sorted(set(
                    line.strip() for line in tables_check.stdout.decode(
                        "utf-8", errors="replace"
                    ).splitlines() if line.strip()
                ))
                result["restored_table_inventory"] = restored_tables
                result["schema_table_inventory"] = sorted(schema_tables)
                missing_tables = sorted(set(schema_tables) - set(restored_tables))
                result["missing_restored_tables"] = missing_tables
                if missing_tables:
                    error = "restored schema is missing expected tables: {}".format(
                        ", ".join(missing_tables)
                    )

        if error is None:
            target_ok, target_identity, target_error = _runtime_identity(
                client, common, env, restore_database
            )
            if not target_ok:
                error = "restored database identity query failed: {}".format(target_error)
            else:
                result["restore_database_runtime_identity"] = target_identity
                result["restore_smoke"] = "PASSED"
                success = True
    except Exception as exc:
        error = "restore verification failed: {}: {}".format(type(exc).__name__, exc)
    finally:
        if created:
            dropped = _run_client(
                client, common, env,
                "DROP DATABASE `{}`".format(restore_database),
            )
            result["restore_target_cleanup"] = "PASSED" if dropped.returncode == 0 else "FAILED"
            if dropped.returncode != 0:
                cleanup_error = "restore target cleanup failed: {}".format(
                    dropped.stderr.decode("utf-8", errors="replace")
                )
                error = "{}{}".format(
                    (error + "; ") if error else "", cleanup_error
                )
                success = False
    return success, result, error

def compute_file_sha256(filepath: str) -> str:
    """Compute SHA256 checksum of any file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_mysql_backup(
    full_sql_gz: str,
    schema_sql: str,
    expected_sha256: Optional[str] = None,
    db_config: Optional[Dict[str, Any]] = None,
    restore_database: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """Verify a dump and optionally restore it into an explicit scratch DB.

    The default path is non-destructive: it validates checksum, gzip framing,
    non-empty schema, and a table inventory.  Restore smoke is opt-in and only
    accepts a separately named database matching the safe identifier grammar.
    """
    if not os.path.isfile(full_sql_gz) or os.path.getsize(full_sql_gz) <= 0:
        return False, {}, "backup dump is missing or empty"
    actual_sha = compute_file_sha256(full_sql_gz)
    if expected_sha256 and actual_sha != expected_sha256:
        return False, {}, "backup dump SHA256 mismatch"
    try:
        with gzip.open(full_sql_gz, "rb") as stream:
            uncompressed_size = 0
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                uncompressed_size += len(chunk)
    except Exception as exc:
        return False, {}, "backup dump decompression failed: {}".format(exc)
    if not os.path.isfile(schema_sql) or os.path.getsize(schema_sql) <= 0:
        return False, {}, "schema-only dump is missing or empty"
    with open(schema_sql, "r", encoding="utf-8", errors="replace") as stream:
        schema_text = stream.read()
    tables = sorted(set(re.findall(r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[`'\"]?([A-Za-z0-9_]+)", schema_text, re.IGNORECASE)))
    result = {
        "dump_sha256": actual_sha,
        "compressed_size": os.path.getsize(full_sql_gz),
        "uncompressed_size": uncompressed_size,
        "schema_size": os.path.getsize(schema_sql),
        "table_inventory": tables,
        "restore_smoke": "NOT_REQUESTED",
    }

    if restore_database:
        restored, restore_result, restore_error = _restore_into_empty_database(
            full_sql_gz, tables, dict(db_config or {}), str(restore_database),
        )
        result.update(restore_result)
        if not restored:
            return False, result, restore_error or "backup restore verification failed"
    return True, result, None

def perform_database_backup(
    db_config: Dict[str, Any],
    backup_dir: str,
    connection=None,
    allow_mock_in_test: bool = False
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Execute full backup workflow and verify all safety gates.
    Returns (success, backup_manifest, error_message).
    """
    config = dict(db_config or {})
    deployment_roots = list(config.get("deployment_roots") or [])
    for deployment_root in deployment_roots:
        if deployment_root and _is_within(backup_dir, deployment_root):
            return False, {}, (
                "backup root must be outside the Current/Candidate deployment root: {}"
                .format(os.path.abspath(backup_dir))
            )
    os.makedirs(backup_dir, exist_ok=True)
    
    host = config.get("host", "127.0.0.1")
    port = config.get("port", 3306)
    user = config.get("user", "root")
    password = config.get("password", "")
    database = config.get("database", "coverage_tool")
    
    full_sql_gz = os.path.join(backup_dir, "full.sql.gz")
    schema_sql = os.path.join(backup_dir, "schema.sql")
    sha256_file = os.path.join(backup_dir, "full.sql.gz.sha256")
    counts_file = os.path.join(backup_dir, "critical-counts.json")
    hashes_file = os.path.join(backup_dir, "critical-content-hashes.json")
    manifest_file = os.path.join(backup_dir, "backup-manifest.json")
    
    # 1. Capture snapshot if connection provided
    snapshot = None
    if connection:
        try:
            snapshot = _capture_generation_aware_snapshot(connection)
            counts = {k: v.get("count", 0) for k, v in snapshot.get("tables", {}).items()}
            with open(counts_file, "w", encoding="utf-8") as f:
                json.dump(counts, f, indent=2)
            hashes = {k: v.get("content_hash", "") for k, v in snapshot.get("tables", {}).items()}
            with open(hashes_file, "w", encoding="utf-8") as f:
                json.dump(hashes, f, indent=2)
        except Exception as e:
            return False, {}, f"Failed to capture pre-backup data snapshot: {e}"
            
    # 2. Check mysqldump binary
    has_mysqldump = False
    try:
        res = subprocess.run(["which", "mysqldump"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        has_mysqldump = (res.returncode == 0)
    except Exception:
        pass
        
    # A unit-test harness that did not provide a live connection must remain a
    # mock even when a system mysqldump happens to be installed.  Production
    # callers always pass a live connection and therefore cannot enter this
    # branch.
    use_mock = bool(allow_mock_in_test and connection is None)
    if not has_mysqldump or use_mock:
        if not allow_mock_in_test:
            return False, {}, "CRITICAL: mysqldump utility is not installed or not in PATH. Full database backup failed; stopping upgrade to prevent data risk."
        else:
            # Only allowed in mock test harness
            dummy_sql = f"-- Test Mock Backup for {database}\n"
            with gzip.open(full_sql_gz, "wt", encoding="utf-8") as gz_out:
                gz_out.write(dummy_sql)
            with open(schema_sql, "w", encoding="utf-8") as f:
                f.write(f"-- Test Mock Schema for {database}\n")
    else:
        try:
            env = os.environ.copy()
            if password:
                env["MYSQL_PWD"] = str(password)
            dump_cmd = [
                "mysqldump",
                f"--host={host}",
                f"--port={port}",
                f"--user={user}",
                "--single-transaction",
                "--quick",
                database
            ]
            with gzip.open(full_sql_gz, "wb") as gz_out:
                proc = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    gz_out.write(chunk)
                proc.wait()
                if proc.returncode != 0:
                    err = proc.stderr.read().decode("utf-8", errors="ignore")
                    return False, {}, f"mysqldump failed with code {proc.returncode}: {err}"
                    
            schema_cmd = [
                "mysqldump",
                f"--host={host}",
                f"--port={port}",
                f"--user={user}",
                "--no-data",
                database
            ]
            with open(schema_sql, "wb") as schema_out:
                proc_schema = subprocess.run(schema_cmd, stdout=schema_out, stderr=subprocess.PIPE, env=env)
                if proc_schema.returncode != 0:
                    err = proc_schema.stderr.decode("utf-8", errors="ignore")
                    return False, {}, f"mysqldump schema-only failed: {err}"
        except Exception as e:
            return False, {}, f"Exception during mysqldump: {e}"
            
    # 3. Compute SHA256 of full.sql.gz
    if not os.path.isfile(full_sql_gz) or os.path.getsize(full_sql_gz) == 0:
        return False, {}, "full.sql.gz was not created or is 0 bytes"
    try:
        with gzip.open(full_sql_gz, "rb") as gz_in:
            while gz_in.read(65536):
                pass
    except Exception as exc:
        return False, {}, "full.sql.gz failed decompression verification: {}".format(exc)
        
    gz_sha256 = compute_file_sha256(full_sql_gz)
    with open(sha256_file, "w", encoding="utf-8") as f:
        f.write(f"{gz_sha256}  full.sql.gz\n")
        
    verified_dump, verification, verification_error = verify_mysql_backup(
        full_sql_gz,
        schema_sql,
        expected_sha256=gz_sha256,
        db_config=config,
        restore_database=config.get("backup_restore_database"),
    )
    if not verified_dump:
        return False, {}, verification_error or "backup verification failed"

    source_environment = str(
        config.get("backup_source_environment") or
        os.environ.get("COVERAGE_BACKUP_SOURCE_ENVIRONMENT") or
        os.environ.get("COVERAGE_ENV") or
        ""
    ).strip().lower()
    operator = str(os.environ.get("COVERAGE_BACKUP_OPERATOR") or "").strip()
    manifest = {
        "status": "BACKUP_VERIFIED",
        "evidence_class": "mock" if use_mock or (allow_mock_in_test and not has_mysqldump) else "production_backup",
        "synthetic": bool(use_mock or (allow_mock_in_test and not has_mysqldump)),
        "database": database,
        "backup_dir": backup_dir,
        "backup_root_external": not any(
            _is_within(backup_dir, root) for root in deployment_roots if root
        ),
        "full_sql_gz_size": os.path.getsize(full_sql_gz),
        "full_sql_gz_sha256": gz_sha256,
        "schema_sql_size": os.path.getsize(schema_sql) if os.path.isfile(schema_sql) else 0,
        "snapshot": snapshot,
        "verification": verification,
        # Gate A's external rehearsal requires an explicit operator attestation
        # in addition to the technical backup checks.  Empty values are kept in
        # test/development manifests so they fail closed instead of being
        # silently interpreted as production provenance.
        "provenance": {
            "source_environment": source_environment,
            "operator": operator,
            "attested_at": utc_iso() if operator else "",
        },
    }
    if has_mysqldump:
        try:
            version_res = subprocess.run(["mysqldump", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            manifest["mysqldump_version"] = (version_res.stdout or version_res.stderr).decode("utf-8", errors="replace").strip()
        except Exception:
            manifest["mysqldump_version"] = "unknown"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        
    return True, manifest, None
