"""Run a forced file-cutover rollback rehearsal and emit bound evidence."""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from scripts.upgrade.cutover_controller import CutoverController
from scripts.diagnostics.data_hash_gate import capture_database_snapshot, verify_data_integrity


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_release_identity(endpoint):
    with urllib.request.urlopen(endpoint, timeout=5) as response:
        if int(getattr(response, "status", 200)) != 200:
            raise RuntimeError("release endpoint returned HTTP {}".format(response.status))
        payload = json.loads(response.read().decode("utf-8"))
    identity = payload.get("release") if isinstance(payload, dict) else None
    if not isinstance(identity, dict):
        raise RuntimeError("release endpoint did not return a release identity")
    return identity


def run(output, revision, config_path=None):
    root = tempfile.mkdtemp(prefix="coverage-rollback-rehearsal-")
    try:
        candidate = os.path.join(root, "candidate.txt")
        live = os.path.join(root, "live.txt")
        backup = os.path.join(root, "backup")
        with open(candidate, "w", encoding="utf-8") as stream:
            stream.write("candidate release\n")
        with open(live, "w", encoding="utf-8") as stream:
            stream.write("previous release\n")
        controller = CutoverController(root, backup)
        db_connection = None
        before_db = after_db = None
        api_endpoint = None
        api_before = api_after = None
        if config_path:
            import pymysql
            with open(config_path, "r", encoding="utf-8") as stream:
                config = json.load(stream)
            api_endpoint = ((config.get("upgrade") or {}).get("release_endpoint"))
            if api_endpoint:
                api_before = fetch_release_identity(api_endpoint)
            db = config.get("mysql", config)
            db_connection = pymysql.connect(
                host=db.get("host", "127.0.0.1"), port=int(db.get("port", 3306)),
                user=db.get("user", "root"), password=str(db.get("password", "")),
                database=db.get("database", "coverage_tool"), autocommit=False,
                cursorclass=pymysql.cursors.DictCursor,
            )
            before_db = capture_database_snapshot(db_connection, {"commit_sha": revision})
        before = sha256(live)
        controller.apply([{
            "op": "MODIFY",
            "source": "candidate.txt",
            "destination": "live.txt",
            "source_sha256": sha256(candidate),
            "backup_required": True,
        }])
        changed = sha256(live) != before
        controller.rollback()
        restored = sha256(live) == before
        if db_connection is not None:
            after_db = capture_database_snapshot(db_connection, {"commit_sha": revision})
            db_unchanged, db_errors = verify_data_integrity(before_db, after_db)
            db_connection.close()
        else:
            db_unchanged, db_errors = True, []
        if api_endpoint:
            api_after = fetch_release_identity(api_endpoint)
        api_unchanged = api_endpoint is None or api_before == api_after
        evidence = {
            "status": "PASSED" if changed and restored and db_unchanged and api_unchanged else "FAILED",
            "rehearsal_verified": bool(changed and restored and db_unchanged and api_unchanged),
            "revision": revision,
            "evidence_class": "staging_cutover",
            "traffic_opened": False,
            "command": "run_rollback_rehearsal",
            "exit_code": 0 if changed and restored and db_unchanged and api_unchanged else 1,
            "artifact_path": os.path.abspath(output),
            "api_endpoint": api_endpoint or "not_checked",
            "api_identity_before": api_before or "not_checked",
            "api_identity_after": api_after or "not_checked",
            "api_identity_verified": api_unchanged,
            "authoritative_data_hash_before": (before_db or {}).get("tables", {}).get("coverage_analysis", {}).get("content_hash", "not_checked"),
            "authoritative_data_hash_after": (after_db or {}).get("tables", {}).get("coverage_analysis", {}).get("content_hash", "not_checked"),
            "database_integrity_errors": db_errors,
            "live_hash_before": before,
            "live_hash_after_rollback": sha256(live),
        }
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        with open(output, "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, indent=2, sort_keys=True)
        if evidence["status"] != "PASSED":
            raise RuntimeError("rollback rehearsal failed")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    run(args.output, args.revision, args.config)
