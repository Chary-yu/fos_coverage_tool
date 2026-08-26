"""Traffic-closed, read-only verification for the final Candidate target.

The verifier intentionally exposes no write option.  Database checks use a
read-only transaction and HTTP checks issue only GET requests.  Mutation
evidence belongs to the rehearsal database and is never performed against the
final target.
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity
from scripts.upgrade.database_identity import fingerprint_connection
from scripts.upgrade.migration_runner import capture_vnext_semantic_snapshot, semantic_hash
from app.time_utils import utc_iso


RELEASE_IDENTITY_FIELDS = (
    "version", "commit_sha", "build_id", "asset_hash", "schema_version",
    "asset_manifest_version", "asset_count", "asset_manifest_hash",
    "asset_manifest",
)


def _release_identity_mismatches(actual, expected_release, label):
    """Compare only the canonical release identity contract fields."""
    violations = []
    for field in RELEASE_IDENTITY_FIELDS:
        actual_value = actual.get(field) if isinstance(actual, dict) else None
        expected_value = (
            expected_release.get(field)
            if isinstance(expected_release, dict) else None
        )
        if actual_value != expected_value:
            violations.append("{} mismatch: {}".format(label, field))
    return violations


def _verify_release_payload(actual, expected_release):
    """Return explicit release mismatches without touching other HTTP routes."""
    return _release_identity_mismatches(
        actual, expected_release, "release identity"
    )


def _load_config(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    return value


def _headers(items):
    result = {}
    for item in items or []:
        if ":" not in item:
            raise ValueError("header must be Name:Value")
        name, value = item.split(":", 1)
        result[name.strip()] = value.strip()
    return result


def _get_json(endpoint, path, query, headers):
    query_string = urllib.parse.urlencode(query or {})
    url = endpoint.rstrip("/") + path
    if query_string:
        url += "?" + query_string
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
        return int(getattr(response, "status", 200)), json.loads(body)


def _database_checks(config_path):
    try:
        import pymysql
        config = _load_config(config_path)
        db = config.get("mysql") or config
        connection = pymysql.connect(
            host=db.get("host", "127.0.0.1"), port=int(db.get("port", 3306)),
            user=db.get("user", "root"), password=str(db.get("password", "")),
            database=db.get("database"), charset=db.get("charset", "utf8mb4"),
            connect_timeout=float(db.get("connect_timeout", 5)),
            autocommit=False, cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            identity = fingerprint_connection(connection, db)
            with connection.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT schema_key, schema_version, release_sha, migration_id "
                    "FROM coverage_schema_meta ORDER BY schema_key"
                )
                schema = cursor.fetchall()
                cursor.execute("SELECT COUNT(*) AS total FROM coverage_projects")
                project_count = int(cursor.fetchone()["total"])
                cursor.execute("SELECT COUNT(*) AS total FROM coverage_lines")
                line_count = int(cursor.fetchone()["total"])
                cursor.execute("SELECT COUNT(*) AS total FROM coverage_analyses")
                analysis_count = int(cursor.fetchone()["total"])
                cursor.execute("SELECT COUNT(*) AS total FROM coverage_analysis_line_links")
                link_count = int(cursor.fetchone()["total"])
            semantic = capture_vnext_semantic_snapshot(connection)
            connection.rollback()
            return {
                "status": "PASSED",
                "database_runtime_identity": identity,
                "schema": schema,
                "counts": {
                    "projects": project_count, "lines": line_count,
                    "analyses": analysis_count, "analysis_line_links": link_count,
                },
                "semantic_hash": semantic_hash(semantic),
                "semantic_snapshot": semantic,
                "read_only_transaction": True,
            }
        finally:
            connection.close()
    except Exception as exc:
        return {"status": "INCOMPLETE",
                "violations": ["{}: {}".format(type(exc).__name__, exc)]}


def verify(args):
    violations = []
    config = _load_config(args.config)
    operator_release = None
    operator_release_loaded = False
    release_artifact_path = getattr(args, "release_identity", None)
    if release_artifact_path:
        try:
            with open(release_artifact_path, "r", encoding="utf-8") as stream:
                operator_release = json.load(stream)
            operator_release_loaded = True
        except Exception as exc:
            violations.append(
                "release identity artifact load failed: {}: {}".format(
                    type(exc).__name__, exc
                )
            )
        if operator_release_loaded and not isinstance(operator_release, dict):
            violations.append("release identity artifact must be a JSON object")

    canonical_release = None
    try:
        canonical_release = generate_release_identity(repo_root=ROOT)
    except Exception as exc:
        violations.append(
            "canonical release identity generation failed: {}: {}".format(
                type(exc).__name__, exc
            )
        )
    if canonical_release is not None and not isinstance(canonical_release, dict):
        violations.append("canonical release identity must be a JSON object")
    if isinstance(canonical_release, dict):
        if release_artifact_path and isinstance(operator_release, dict):
            violations.extend(_release_identity_mismatches(
                operator_release,
                canonical_release,
                "operator release identity",
            ))

    expected_release = canonical_release if isinstance(canonical_release, dict) else None
    db = _database_checks(args.config)
    if db.get("status") != "PASSED":
        violations.extend(db.get("violations") or ["database read-only verification unavailable"])

    headers = _headers(args.header)
    http = {"status": "PASSED", "requests": [], "responses": {}}
    query = {}
    if args.project:
        query["project"] = args.project
    if args.scan_id:
        query["scan_id"] = args.scan_id
    for path, request_query in (
            ("/api/coverage/health", {}),
            ("/api/coverage/release", {}),
            ("/api/coverage/projects", {}),
            ("/api/coverage/progress", query)):
        try:
            status, payload = _get_json(args.endpoint, path, request_query, headers)
            http["requests"].append({"method": "GET", "path": path})
            http["responses"][path] = {"status": status, "payload": payload}
            if status != 200:
                http["status"] = "INCOMPLETE"
                violations.append("{} returned HTTP {}".format(path, status))
            if path == "/api/coverage/release" and expected_release:
                actual = payload.get("release") if isinstance(payload, dict) else None
                release_violations = _verify_release_payload(
                    actual, expected_release
                )
                if release_violations:
                    http["status"] = "FAILED"
                    violations.extend(release_violations)
        except Exception as exc:
            http["status"] = "INCOMPLETE"
            violations.append("{}: {}".format(path, exc))

    return {
        "status": "PASSED" if not violations else "INCOMPLETE",
        "evidence_class": "final_target_read_only_verification",
        "synthetic": False,
        "checked_at": utc_iso(),
        "candidate_config": {
            "path": os.path.abspath(args.config),
            "database": (config.get("mysql") or config).get("database", ""),
            "server": config.get("server") or {},
        },
        "release_identity": expected_release or {},
        "database": db,
        "http": http,
        "write_routes_exercised": [],
        "violations": violations,
        "command_or_action": "python scripts/diagnostics/final_read_only_verification.py",
        "exit_code": 0 if not violations else 1,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument(
        "--release-identity",
        help=(
            "optional operator artifact; the Candidate is always compared "
            "with the exact checkout identity"
        ),
    )
    parser.add_argument("--project")
    parser.add_argument("--scan-id")
    parser.add_argument("--header", action="append")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = verify(args)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = os.path.abspath(args.output)
        directory = os.path.dirname(output)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
