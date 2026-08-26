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
from app.upgrade.lifecycle import UpgradeLifecycle


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


def load_identity(value):
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return None
    with open(str(value), "r", encoding="utf-8") as stream:
        identity = json.load(stream)
    if not isinstance(identity, dict):
        raise RuntimeError("release identity artifact must contain an object")
    return identity


def release_identity_id(identity):
    identity = identity or {}
    value = identity.get("commit_sha") or identity.get("build_id") or identity.get("version")
    if not value:
        raise RuntimeError("release identity has no stable id")
    return str(value)


RELEASE_IDENTITY_FIELDS = (
    "version", "commit_sha", "build_id", "asset_hash", "schema_version",
    "asset_manifest_version", "asset_count", "asset_manifest_hash",
    "asset_manifest",
)


def release_identity_matches(observed, expected):
    observed = observed or {}
    expected = expected or {}
    return bool(
        all(observed.get(field) == expected.get(field)
            for field in RELEASE_IDENTITY_FIELDS)
    )


def run(output, revision, config_path=None, before_release=None, target_release=None):
    config = {}
    if config_path:
        with open(config_path, "r", encoding="utf-8") as stream:
            config = json.load(stream)
    upgrade_config = config.get("upgrade") or {}
    before_release = load_identity(
        before_release if before_release is not None else upgrade_config.get("previous_release")
    )
    target_release = load_identity(
        target_release if target_release is not None else upgrade_config.get("target_release")
    )
    if not before_release or not target_release:
        raise RuntimeError("real before and target release identity artifacts are required")
    before_release_id = release_identity_id(before_release)
    target_release_id = release_identity_id(target_release)
    if before_release_id == target_release_id:
        raise RuntimeError("before and target release identities must differ")
    if str(target_release.get("commit_sha") or "") != str(revision):
        raise RuntimeError("target release commit does not match --revision")

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
        rollback_control = None
        if config_path:
            import pymysql
            api_endpoint = ((config.get("upgrade") or {}).get("release_endpoint"))
            if not api_endpoint:
                raise RuntimeError("release_endpoint is required for rollback rehearsal")
            if not ((config.get("upgrade") or {}).get("previous_release_endpoint")):
                raise RuntimeError("previous_release_endpoint is required for rollback rehearsal")
            api_before = fetch_release_identity(api_endpoint)
            if not release_identity_matches(api_before, target_release):
                raise RuntimeError("release endpoint before identity is not the target release")
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
        if api_endpoint:
            # A file restore alone is not a rollback rehearsal.  The
            # configured lifecycle must stop the Candidate and start the
            # independently identified previous release; its endpoint is
            # checked again below.  Missing/failed commands stay fail-closed.
            lifecycle = UpgradeLifecycle(ROOT, config, "staging", before_release)
            lifecycle.api_started = True
            rollback_control = lifecycle.abort()
        if db_connection is not None:
            after_db = capture_database_snapshot(db_connection, {"commit_sha": revision})
            db_unchanged, db_errors = verify_data_integrity(before_db, after_db)
            db_connection.close()
        else:
            db_unchanged, db_errors = True, []
        if api_endpoint:
            api_after = fetch_release_identity(api_endpoint)
        api_identity_verified = bool(api_after and
                                     release_identity_matches(api_after, before_release))
        rollback_release_id = release_identity_id(api_after) if api_after else ""
        rehearsal_ok = bool(
            changed and restored and db_unchanged and api_identity_verified
            and rollback_release_id == before_release_id
        )
        evidence = {
            "status": "PASSED" if rehearsal_ok else "FAILED",
            "rehearsal_verified": rehearsal_ok,
            "revision": revision,
            "evidence_class": "staging_cutover",
            "traffic_opened": False,
            "command": "run_rollback_rehearsal",
            "exit_code": 0 if rehearsal_ok else 1,
            "artifact_path": os.path.abspath(output),
            "api_endpoint": api_endpoint or "not_checked",
            "api_identity_before": api_before or "not_checked",
            "api_identity_after": api_after or "not_checked",
            "api_identity_verified": api_identity_verified,
            "rollback_control": rollback_control or "not_checked",
            "before_release_id": before_release_id,
            "target_release_id": target_release_id,
            "rollback_release_id": rollback_release_id,
            "before_release": before_release,
            "target_release": target_release,
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
    parser.add_argument("--before-release", help="JSON artifact for the real previous release")
    parser.add_argument("--target-release", help="JSON artifact for the release being tested")
    args = parser.parse_args()
    run(args.output, args.revision, args.config, args.before_release, args.target_release)
