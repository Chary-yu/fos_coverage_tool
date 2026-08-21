"""Run Gate A's real verified-backup -> empty-target rehearsal.

The disposable source database is restored from an operator-supplied dump,
then migrated into a separately-created empty VNext target.  This command is
intentionally stricter than the generated-fixture MariaDB integration:

* ``--create-disposable`` is required;
* the dump must be outside the active deployment tree and have an explicit
  SHA256 (argument or sidecar file);
* both databases must be absent before this command creates them;
* only databases created by this command are eligible for cleanup; and
* a successful result is never marked synthetic.

The command does not make Gate A pass by itself.  Its JSON output is an
operator evidence artifact that must be bound to the exact checkout revision
by ``scripts/diagnostics/gate_matrix.py``.
"""

from __future__ import print_function

import argparse
import gzip
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import uuid

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import pymysql
except ImportError:  # pragma: no cover - the command fails closed below.
    pymysql = None

from app.release_identity import generate_release_identity
from app.time_utils import utc_iso
from scripts.diagnostics.contract import with_contract
from scripts.upgrade.evidence_manifest import EvidenceManifestV2
from scripts.maintenance.mysql_backup import (
    _DATABASE_IDENTIFIER,
    _client_settings,
    _is_within,
    _run_client,
    _runtime_identity,
    compute_file_sha256,
)
from scripts.upgrade.domain_migration import apply_analysis_domain
from scripts.upgrade.migration_runner import (
    apply_schema,
    capture_legacy_semantic_snapshot,
    capture_vnext_semantic_snapshot,
    migrate_legacy,
    semantic_hash,
)


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        return ""


def _sha256_text(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mysql_config(config):
    value = config.get("mysql", config) if isinstance(config, dict) else {}
    return dict(value or {})


def _safe_database_name(value, label):
    value = str(value or "")
    if not _DATABASE_IDENTIFIER.match(value):
        raise ValueError("{} database name is missing or unsafe".format(label))
    if not value.startswith("coverage_gate_a_"):
        raise ValueError(
            "{} database must use the disposable coverage_gate_a_ prefix".format(label)
        )
    return value


def _database_exists(client, common, env, name):
    result = _run_client(
        client, common, env,
        "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
        "WHERE SCHEMA_NAME = '{}'".format(name),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "database existence query failed: {}".format(
                result.stderr.decode("utf-8", errors="replace")
            )
        )
    return bool(result.stdout.decode("utf-8", errors="replace").strip())


def _create_database(client, common, env, name):
    result = _run_client(
        client, common, env,
        "CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci".format(name),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "database creation failed for {}: {}".format(
                name, result.stderr.decode("utf-8", errors="replace")
            )
        )


def _drop_database(client, common, env, name):
    result = _run_client(client, common, env, "DROP DATABASE `{}`".format(name))
    if result.returncode != 0:
        raise RuntimeError(
            "database cleanup failed for {}: {}".format(
                name, result.stderr.decode("utf-8", errors="replace")
            )
        )


def _restore_dump(client, common, env, dump_path, database):
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
            "backup restore process failed: {} ({})".format(
                exc, stderr.decode("utf-8", errors="replace")
            )
        )
    if process.returncode != 0:
        raise RuntimeError(
            "backup restore failed: {}".format(
                stderr.decode("utf-8", errors="replace")
            )
        )


def _connect(config, database):
    if pymysql is None:
        raise RuntimeError("PyMySQL is unavailable")
    cfg = dict(config or {})
    return pymysql.connect(
        host=cfg.get("backup_restore_host", cfg.get("host", "127.0.0.1")),
        port=int(cfg.get("backup_restore_port", cfg.get("port", 3306))),
        user=cfg.get("backup_restore_user", cfg.get("user", "root")),
        password=str(cfg.get("backup_restore_password", cfg.get("password", ""))),
        database=database,
        charset=cfg.get("charset", "utf8mb4"),
        autocommit=False,
        connect_timeout=float(cfg.get("connect_timeout", 5)),
        cursorclass=pymysql.cursors.DictCursor,
    )


def _load_expected_sha(dump_path, explicit):
    if explicit:
        return str(explicit).strip().split()[0]
    sidecar = dump_path + ".sha256"
    if not os.path.isfile(sidecar):
        raise ValueError("explicit dump SHA256 or <dump>.sha256 sidecar is required")
    with open(sidecar, "r", encoding="utf-8") as stream:
        value = stream.read().strip().split()
    if not value:
        raise ValueError("dump SHA256 sidecar is empty")
    return value[0]


def _validate_dump(dump_path, expected_sha, repo_root, deployment_roots):
    dump_path = os.path.realpath(os.path.abspath(dump_path))
    if not os.path.isfile(dump_path) or os.path.getsize(dump_path) <= 0:
        raise ValueError("backup dump is missing or empty")
    if _is_within(dump_path, repo_root) or any(
            _is_within(dump_path, root) for root in deployment_roots if root
    ):
        raise ValueError("verified backup must be stored outside deployment roots")
    actual = compute_file_sha256(dump_path)
    if actual != expected_sha:
        raise ValueError("backup dump SHA256 mismatch")
    uncompressed = 0
    try:
        with gzip.open(dump_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                uncompressed += len(chunk)
    except Exception as exc:
        raise ValueError("backup dump decompression failed: {}".format(exc))
    if uncompressed <= 0:
        raise ValueError("backup dump expands to an empty stream")
    return dump_path, actual, uncompressed


def _base_result(repo_root, revision, dump_path, dump_sha, output_path,
                 started_at, command):
    identity = generate_release_identity(repo_root=repo_root)
    return with_contract({
        "gate": "gate-a",
        "status": "INCOMPLETE",
        "evidence_class": "verified_production_backup_restore_rehearsal",
        "synthetic": False,
        "candidate_revision": revision,
        "release_identity": identity,
        "host_identity": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
        },
        "command_or_action": command,
        "started_at": started_at,
        "finished_at": started_at,
        "exit_code": 1,
        "artifact_path": dump_path if os.path.isfile(dump_path) else "",
        "artifact_sha256": dump_sha,
        "source_inputs_sha256": [dump_sha],
        "output_path": output_path,
        "violations": [],
    })


def run(args):
    repo_root = os.path.abspath(args.repo_root)
    revision = _revision(repo_root)
    started_at = utc_iso()
    dump_path = os.path.abspath(args.backup)
    expected_sha = ""
    try:
        expected_sha = _load_expected_sha(dump_path, args.backup_sha256)
    except Exception as exc:
        expected_sha = ""
        sha_error = str(exc)
    else:
        sha_error = ""
    command = "python scripts/upgrade/run_verified_backup_rehearsal.py --create-disposable"
    result = _base_result(
        repo_root, revision, dump_path, expected_sha,
        os.path.abspath(args.output), started_at, command,
    )
    source_database = None
    target_database = None
    source_created = False
    target_created = False
    source_connection = None
    target_connection = None
    client = common = env = None
    try:
        if not args.create_disposable:
            raise ValueError("--create-disposable is required")
        if not revision:
            raise RuntimeError("unable to resolve exact checkout revision")
        if sha_error:
            raise ValueError(sha_error)
        deployment_roots = list(args.deployment_root or [])
        deployment_roots.append(repo_root)
        dump_path, dump_sha, uncompressed_size = _validate_dump(
            dump_path, expected_sha, repo_root, deployment_roots,
        )
        result["artifact_path"] = dump_path
        result["artifact_sha256"] = dump_sha
        result["source_inputs_sha256"] = [
            dump_sha,
            _sha256_text(os.path.join(repo_root, "scripts", "upgrade", "vnext_schema.sql")),
        ]
        with open(os.path.abspath(args.config), "r", encoding="utf-8") as stream:
            config = json.load(stream)
        mysql_config = _mysql_config(config)
        client, common, env = _client_settings(mysql_config)
        if not client:
            raise RuntimeError("mariadb/mysql client is unavailable")
        identity_ok, server_identity, identity_error = _runtime_identity(
            client, common, env, None,
        )
        if not identity_ok:
            raise RuntimeError("database runtime identity failed: {}".format(identity_error))
        result["database_runtime_identity"] = server_identity
        required_prefix = str(args.require_version_prefix or "").strip()
        if required_prefix and not str(server_identity.get("version", "")).startswith(required_prefix):
            raise RuntimeError(
                "database version {} does not match required prefix {}".format(
                    server_identity.get("version"), required_prefix
                )
            )
        suffix = uuid.uuid4().hex[:12]
        source_database = _safe_database_name(
            args.source_database or "coverage_gate_a_source_{}".format(suffix),
            "source",
        )
        target_database = _safe_database_name(
            args.target_database or "coverage_gate_a_target_{}".format(suffix),
            "target",
        )
        if source_database.lower() == target_database.lower():
            raise ValueError("source and target database names must differ")
        if _database_exists(client, common, env, source_database):
            raise RuntimeError("disposable source database already exists")
        if _database_exists(client, common, env, target_database):
            raise RuntimeError("disposable target database already exists")
        _create_database(client, common, env, source_database)
        source_created = True
        _create_database(client, common, env, target_database)
        target_created = True
        _restore_dump(client, common, env, dump_path, source_database)

        source_connection = _connect(mysql_config, source_database)
        target_connection = _connect(mysql_config, target_database)
        source_identity_ok, source_identity, source_identity_error = _runtime_identity(
            client, common, env, source_database,
        )
        target_identity_ok, target_identity, target_identity_error = _runtime_identity(
            client, common, env, target_database,
        )
        if not source_identity_ok or not target_identity_ok:
            raise RuntimeError(
                "disposable database identity query failed: {} {}".format(
                    source_identity_error, target_identity_error,
                )
            )
        result["source_database_runtime_identity"] = source_identity
        result["target_database_runtime_identity"] = target_identity
        source_semantic = capture_legacy_semantic_snapshot(source_connection)
        schema_path = os.path.join(repo_root, "scripts", "upgrade", "vnext_schema.sql")
        first_schema = apply_schema(target_connection, schema_path, release_sha=revision)
        first_domain = apply_analysis_domain(target_connection, release_sha=revision)
        first_migration = migrate_legacy(
            source_connection, target_connection, release_sha=revision,
        )
        target_semantic = capture_vnext_semantic_snapshot(target_connection)
        second_migration = migrate_legacy(
            source_connection, target_connection, release_sha=revision,
        )
        target_semantic_second = capture_vnext_semantic_snapshot(target_connection)
        second_schema = apply_schema(target_connection, schema_path, release_sha=revision)
        second_domain = apply_analysis_domain(target_connection, release_sha=revision)
        semantic_match = bool(first_migration.get("authoritative_semantic_match"))
        idempotent = bool(second_migration.get("authoritative_semantic_match")) \
            and target_semantic == target_semantic_second
        result["checks"] = {
            "backup_sha256_verified": True,
            "backup_gzip_verified": True,
            "restore_into_empty_source": True,
            "source_target_database_separation": True,
            "core_schema_first_apply": first_schema,
            "core_schema_second_apply": second_schema,
            "analysis_domain_first_apply": first_domain,
            "analysis_domain_second_apply": second_domain,
            "migration_first_run": first_migration,
            "migration_second_run": second_migration,
            "authoritative_semantic_match": semantic_match,
            "migration_idempotent": idempotent,
        }
        result["source_database"] = source_database
        result["target_database"] = target_database
        result["source_semantic_hash"] = semantic_hash(source_semantic)
        result["target_semantic_hash"] = semantic_hash(target_semantic)
        result["target_semantic_hash_second"] = semantic_hash(target_semantic_second)
        result["uncompressed_backup_bytes"] = uncompressed_size
        if not semantic_match:
            raise RuntimeError("restored Legacy -> VNext semantic hash mismatch")
        if not idempotent:
            raise RuntimeError("second migration changed the target semantic snapshot")
        result["status"] = "PASSED"
        result["exit_code"] = 0
    except Exception as exc:
        result.setdefault("violations", []).append(
            "{}: {}".format(type(exc).__name__, exc)
        )
    finally:
        if source_connection is not None:
            source_connection.close()
        if target_connection is not None:
            target_connection.close()
        if client and common is not None and env is not None:
            for name, created in (
                    (target_database, target_created),
                    (source_database, source_created)):
                if not created or not name:
                    continue
                try:
                    _drop_database(client, common, env, name)
                except Exception as exc:
                    result["status"] = "INCOMPLETE"
                    result["exit_code"] = 1
                    result.setdefault("violations", []).append(str(exc))
        result["finished_at"] = utc_iso()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=_ROOT)
    parser.add_argument("--config", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--backup-sha256", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest-output", default="")
    parser.add_argument("--deployment-root", action="append", default=[])
    parser.add_argument("--source-database", default="")
    parser.add_argument("--target-database", default="")
    parser.add_argument("--require-version-prefix", default="")
    parser.add_argument("--create-disposable", action="store_true")
    args = parser.parse_args(argv)
    result = run(args)
    output = os.path.abspath(args.output)
    directory = os.path.dirname(output)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    if args.manifest_output:
        manifest_path = os.path.abspath(args.manifest_output)
        try:
            manifest = EvidenceManifestV2(
                os.path.abspath(args.repo_root), "gate-a",
                candidate_revision=result.get("candidate_revision") or "",
                release_identity=result.get("release_identity") or {},
                database_runtime_identity=result.get("database_runtime_identity") or {},
                manifest_path=manifest_path,
            )
            artifact_path = result.get("artifact_path") or ""
            manifest.record(
                "verified-backup-restore-rehearsal",
                result.get("evidence_class") or "verified_production_backup_restore_rehearsal",
                result.get("status") or "INCOMPLETE",
                command_or_action=result.get("command_or_action") or "",
                exit_code=result.get("exit_code"),
                artifact_path=artifact_path if os.path.isfile(artifact_path) else "",
                source_inputs_sha256=result.get("source_inputs_sha256") or [],
                candidate_revision=result.get("candidate_revision") or "",
                host_identity=result.get("host_identity") or {},
                database_runtime_identity=result.get("database_runtime_identity") or {},
                release_identity=result.get("release_identity") or {},
                started_at=result.get("started_at") or "",
                finished_at=result.get("finished_at") or "",
                synthetic=False,
                checks=result.get("checks") or {},
                violations=result.get("violations") or [],
            )
            manifest_valid, manifest_errors = manifest.validate()
            result["manifest_path"] = manifest_path
            result["manifest_sha256"] = manifest.data.get("manifest_sha256", "")
            if not manifest_valid:
                result["status"] = "INCOMPLETE"
                result["exit_code"] = 1
                result.setdefault("violations", []).extend(manifest_errors)
        except Exception as exc:
            result["status"] = "INCOMPLETE"
            result["exit_code"] = 1
            result.setdefault("violations", []).append(
                "evidence manifest generation failed: {}".format(exc)
            )
        with open(output, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
