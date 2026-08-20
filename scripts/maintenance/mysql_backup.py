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
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", str(restore_database)):
            return False, result, "restore database name is unsafe"
        cfg = dict(db_config or {})
        client = shutil.which("mariadb") or shutil.which("mysql")
        if not client:
            return False, result, "mariadb/mysql client is unavailable for restore smoke"
        env = os.environ.copy()
        if cfg.get("password"):
            env["MYSQL_PWD"] = str(cfg.get("password"))
        common = [
            "--host={}".format(cfg.get("backup_restore_host", cfg.get("host", "127.0.0.1"))),
            "--port={}".format(int(cfg.get("backup_restore_port", cfg.get("port", 3306)))),
            "--user={}".format(cfg.get("backup_restore_user", cfg.get("user", "root"))),
        ]
        restore_password = cfg.get("backup_restore_password", cfg.get("password", ""))
        if restore_password:
            env["MYSQL_PWD"] = str(restore_password)
        else:
            env.pop("MYSQL_PWD", None)
        create = subprocess.run(
            [client] + common + ["--execute", "CREATE DATABASE IF NOT EXISTS `{}`".format(restore_database)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        if create.returncode != 0:
            return False, result, "restore scratch database creation failed: {}".format(
                create.stderr.decode("utf-8", errors="replace")
            )
        restore = subprocess.Popen(
            [client] + common + ["--database={}".format(restore_database)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        try:
            with gzip.open(full_sql_gz, "rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    restore.stdin.write(chunk)
            restore.stdin.close()
            restore.stdin = None
            stdout, stderr = restore.communicate()
        except Exception:
            try:
                restore.kill()
            except OSError:
                pass
            raise
        if restore.returncode != 0:
            return False, result, "restore smoke failed: {}".format(stderr.decode("utf-8", errors="replace"))
        tables_check = subprocess.run(
            [client] + common + ["--database={}".format(restore_database), "--batch", "--skip-column-names",
                                  "--execute", "SHOW TABLES"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        if tables_check.returncode != 0:
            return False, result, "restored schema inspection failed"
        restored_tables = [line.strip() for line in tables_check.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
        result["restored_table_inventory"] = sorted(restored_tables)
        result["restore_smoke"] = "PASSED"
        # Cleanup is restricted to the caller-provided scratch database.
        subprocess.run(
            [client] + common + ["--execute", "DROP DATABASE `{}`".format(restore_database)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
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
    os.makedirs(backup_dir, exist_ok=True)
    
    host = db_config.get("host", "127.0.0.1")
    port = db_config.get("port", 3306)
    user = db_config.get("user", "root")
    password = db_config.get("password", "")
    database = db_config.get("database", "coverage_tool")
    
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
            snapshot = capture_database_snapshot(connection)
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
        db_config=db_config,
        restore_database=db_config.get("backup_restore_database"),
    )
    if not verified_dump:
        return False, {}, verification_error or "backup verification failed"

    manifest = {
        "status": "BACKUP_VERIFIED",
        "evidence_class": "mock" if use_mock or (allow_mock_in_test and not has_mysqldump) else "production_backup",
        "database": database,
        "backup_dir": backup_dir,
        "full_sql_gz_size": os.path.getsize(full_sql_gz),
        "full_sql_gz_sha256": gz_sha256,
        "schema_sql_size": os.path.getsize(schema_sql) if os.path.isfile(schema_sql) else 0,
        "snapshot": snapshot,
        "verification": verification,
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
