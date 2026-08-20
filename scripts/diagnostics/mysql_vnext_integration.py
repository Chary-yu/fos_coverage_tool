"""Run a disposable, real-MariaDB VNext transaction and API integration.

This command is intentionally opt-in. It never targets the repository's
existing ``coverage`` database: ``--create-disposable`` generates a fresh
database name, applies the VNext schema, runs the checks, and drops that
database in ``finally``.
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import tempfile
import threading
import uuid
import urllib.request
from urllib.parse import quote

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract

import pymysql

from app.bootstrap import VNextRuntime, create_vnext_server
from app.code_detail.code_region import FunctionRange
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import (
    SourceContext,
    SourceLineDTO,
    calc_sidecar_file_key,
)
from app.db.manager import DatabaseManager
from app.db.repositories.base import fetchone
from scripts.upgrade.migration_runner import apply_schema


def _env(name, default=None):
    value = os.environ.get(name)
    return default if value in (None, "") else value


def _connect(host, port, user, password, database=None, autocommit=True):
    kwargs = {
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "charset": "utf8mb4",
        "connect_timeout": 5,
        "autocommit": bool(autocommit),
    }
    if database:
        kwargs["database"] = database
    return pymysql.connect(**kwargs)


def _database_name():
    return "coverage_vnext_audit_{}".format(uuid.uuid4().hex[:12])


def _line_records():
    return [{
        "line_number": number,
        "line_text": "audit_line_{}();".format(number),
        "coverage_state": "uncovered",
        "block_start_line": number,
        "block_end_line": number,
        "block_type": "single",
        "function_name": "",
        "function_hash": "",
        "code_line_hash": "audit-line-{}".format(number),
        "code_occurrence": 1,
        "suggested_reviewer": "git-audit" if number == 1 else "",
    } for number in (1, 2)]


def _source_context():
    lines = []
    for number in (1, 2):
        lines.append(SourceLineDTO(
            number,
            source="audit_line_{}();".format(number),
            coverage_state="uncovered",
            analysis_state="未确认",
            is_pending_analysis=True,
            block_start_line=number,
            block_end_line=number,
            block_type="single",
            suggested_reviewer="git-audit" if number == 1 else "",
            is_block_entry=True,
        ))
    return SourceContext(
        "MySQLAudit", "src/mysql_audit.c", lines,
        function_ranges=[FunctionRange(1, 2, "audit")],
        report_id="report_mysql_audit",
    )


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def run(args):
    admin = None
    manager = None
    runtime = None
    server = None
    server_thread = None
    database = _database_name()
    checks = {}
    root = tempfile.mkdtemp(prefix="vnext-mysql-audit-")
    report_root = os.path.join(root, "report")
    os.makedirs(report_root)
    project_name = "MySQLAudit_{}".format(os.getpid())

    try:
        admin = _connect(args.host, args.port, args.user, args.password)
        with admin.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci".format(
                    database
                )
            )
        admin.close()
        admin = None

        schema_connection = _connect(
            args.host, args.port, args.user, args.password, database=database,
            autocommit=False,
        )
        try:
            apply_schema(
                schema_connection,
                os.path.join(_REPO_ROOT, "scripts/upgrade/vnext_schema.sql"),
                release_sha="mysql-audit",
            )
            schema_connection.commit()
            schema_row = fetchone(
                schema_connection,
                "SELECT schema_version, release_sha FROM coverage_schema_meta "
                "WHERE schema_key = ?",
                ("coverage_vnext",),
            )
            _assert(schema_row and int(schema_row["schema_version"]) == 1,
                    "VNext schema marker was not applied")
            checks["schema_applied"] = True
        finally:
            schema_connection.close()

        config = {
            "project_name": project_name,
            "runtime_mode": "vnext",
            "schema_version": 1,
            "mysql": {
                "host": args.host,
                "port": int(args.port),
                "user": args.user,
                "password": args.password,
                "database": database,
                "charset": "utf8mb4",
                "connect_timeout": 5,
                "idle_ping_after_sec": 60,
                "retry_read_operations": True,
            },
            "auth": {"mode": "disabled"},
            "runtime_state": {"root": os.path.join(root, "state")},
            "report_roots": [report_root],
            "input_roots": [root],
            "jobs": {"max_workers": 1, "max_queue_size": 4},
        }
        manager = DatabaseManager(config)
        runtime = VNextRuntime(config, root, database_manager=manager)
        context = _source_context()
        file_path = "src/mysql_audit.c"
        file_key = calc_sidecar_file_key(file_path, "repo-a")
        SidecarStore([report_root], chunk_size=2).save_chunked_sidecar(
            report_root, "report_mysql_audit", file_key, context
        )

        with runtime.connection_context(read_only=False) as connection:
            scan = runtime.project_service.create_scan_and_ingest(
                connection,
                project_name,
                [{
                    "repository_name": "repo-a",
                    "file_path": file_path,
                    "file_path_hash": "",
                    "source_file_name": "mysql_audit.c",
                    "lines": _line_records(),
                }],
                info_file_name="mysql_audit.info",
                info_sha256="b" * 64,
                repositories=[{
                    "repository_name": "repo-a",
                    "repository_path": os.path.join(root, "repo-a"),
                    "branch_name": "main",
                    "verified": True,
                }],
                report={
                    "report_id": "report_mysql_audit",
                    "report_root": report_root,
                    "sidecar_schema": 2,
                    "asset_identity": "mysql-audit-v1",
                },
            )
            runtime.report_registry.register(
                "report_mysql_audit", [report_root], sidecar_required=True,
                report_root=report_root, scan_id=scan["id"],
            )
        _assert(str(scan["status"]) in ("ready", "sealed"),
                "scan was not sealed after MySQL ingest")
        checks["bulk_scan_ingest_and_seal"] = True

        application = runtime.application()
        status, layout = application.dispatch(
            "GET", "/api/coverage/code-layout",
            query={
                "scan_id": scan["id"], "report_id": "report_mysql_audit",
                "repository_name": "repo-a", "file_path": file_path,
            },
        )
        _assert(status == 200 and layout["total_lines"] == 2,
                "real MySQL code layout request failed")
        status, lines = application.dispatch(
            "POST", "/api/coverage/code-lines/batch",
            body={
                "scan_id": scan["id"], "report_id": "report_mysql_audit",
                "repository_name": "repo-a", "file_path": file_path,
                "ranges": [{"start_line": 1, "end_line": 2}],
            },
        )
        _assert(status == 200 and len(lines["batches"][0]["lines"]) == 2,
                "real MySQL code-lines batch request failed")
        checks["code_detail_http_contract"] = True

        status, saved = application.dispatch(
            "POST", "/api/coverage/analysis",
            body={
                "project_name": project_name,
                "scan_id": scan["id"],
                "repository_name": "repo-a",
                "file_path": file_path,
                "records": [{
                    "line_start": 1, "line_end": 1,
                    "file_path": file_path, "repository_name": "repo-a",
                    "status": "可覆盖", "reviewer": "mysql-reviewer",
                    "coverage_method": "mysql-test", "is_draft": False,
                }],
            },
        )
        _assert(status == 200 and int(saved["saved"]) == 1,
                "real MySQL bulk analysis save failed")
        checks["bulk_analysis_upsert"] = True

        with runtime.connection_context(read_only=False) as connection:
            runtime.progress_service.rebuild(connection, project_name, scan["id"])
        status, summary = application.dispatch(
            "GET", "/api/coverage/progress",
            query={"project": project_name, "scan_id": scan["id"]},
        )
        _assert(
            status == 200
            and summary.get("source") == "coverage_file_state"
            and int(summary.get("confirmed_total") or 0) == 1
            and int(summary.get("pending_total") or 0) == 1,
            "real MySQL SQL progress aggregate is incorrect",
        )
        checks["sql_progress_aggregate"] = True

        with runtime.connection_context(read_only=False) as connection:
            before = fetchone(
                connection,
                "SELECT data_version FROM coverage_project_state WHERE project_id = ?",
                (scan["project_id"],),
            )
            before_analysis = fetchone(
                connection,
                "SELECT COUNT(*) AS total FROM coverage_analyses",
            )
            try:
                runtime.analysis_service.save(
                    connection, project_name, scan["id"], [{
                        "file_path": file_path, "repository_name": "repo-a",
                        "line_number": 999, "status": "可覆盖",
                    }], reviewer="should-rollback",
                )
            except (KeyError, ValueError):
                pass
            else:
                raise AssertionError("invalid MySQL analysis write unexpectedly succeeded")
            after = fetchone(
                connection,
                "SELECT data_version FROM coverage_project_state WHERE project_id = ?",
                (scan["project_id"],),
            )
            after_analysis = fetchone(
                connection,
                "SELECT COUNT(*) AS total FROM coverage_analyses",
            )
        _assert(before["data_version"] == after["data_version"],
                "failed MySQL transaction changed data_version")
        _assert(before_analysis["total"] == after_analysis["total"],
                "failed MySQL transaction left analysis rows")
        checks["transaction_rollback"] = True

        # Exercise the actual stdlib HTTP transport with a second manager
        # reference so closing the server cannot close the first runtime's pool.
        http_manager = DatabaseManager(config)
        server = create_vnext_server(
            ("127.0.0.1", 0), config, repo_root=root,
            database_manager=http_manager,
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        url = "http://127.0.0.1:{}/api/coverage/progress?project={}&scan_id={}".format(
            server.server_address[1],
            quote(project_name),
            scan["id"],
        )
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        _assert(payload.get("source") == "coverage_file_state",
                "real MySQL HTTP progress response is incorrect")
        checks["real_http_transport"] = True
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
        server = None
        server_thread = None

        health = manager.health()
        checks["pool_runtime_health"] = (
            int(health.get("acquires") or 0) > 0
            and int(health.get("rollbacks") or 0) > 0
        )
        _assert(checks["pool_runtime_health"],
                "real MySQL pool health did not record request/rollback activity")

        return with_contract({
            "status": "PASSED",
            "evidence_class": "real_mariadb_vnext_integration",
            "database_engine": "MariaDB",
            "database_version": _database_version(args),
            "checks": checks,
            "database": database,
            "disposable": True,
        })
    finally:
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            if server_thread is not None:
                server_thread.join(timeout=5)
            try:
                server.server_close()
            except Exception:
                pass
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass
        if admin is not None:
            try:
                admin.close()
            except Exception:
                pass
        try:
            admin = _connect(args.host, args.port, args.user, args.password)
            with admin.cursor() as cursor:
                cursor.execute("DROP DATABASE IF EXISTS `{}`".format(database))
            admin.close()
        except Exception:
            # Preserve the original failure; cleanup failure is emitted by the
            # caller as a separate warning rather than masking the assertion.
            pass
        try:
            for dirpath, _, filenames in os.walk(root, topdown=False):
                for filename in filenames:
                    try:
                        os.remove(os.path.join(dirpath, filename))
                    except OSError:
                        pass
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
        except OSError:
            pass


def _database_version(args):
    connection = _connect(args.host, args.port, args.user, args.password)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            return str(cursor.fetchone()[0])
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=_env("COVERAGE_MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(_env("COVERAGE_MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=_env("COVERAGE_MYSQL_USER", "root"))
    parser.add_argument("--password", default=_env("COVERAGE_MYSQL_PASSWORD", ""))
    parser.add_argument(
        "--create-disposable", action="store_true", required=True,
        help="required safety acknowledgement; only a generated temporary database is used",
    )
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        result = with_contract({
            "status": "FAILED",
            "evidence_class": "real_mariadb_vnext_integration",
            "database_engine": "MariaDB",
            "violations": ["{}: {}".format(type(exc).__name__, exc)],
            "disposable": True,
        })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
