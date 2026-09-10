"""Offline-first R8 release conductor used by the one-file handoff.

This module is intentionally a thin, auditable wrapper around the existing
upgrade orchestrator.  It owns the exact-input/session/evidence namespace and
never downloads anything.  Production mutations remain inside
``run_upgrade.py`` and are unreachable until its explicit ``APPLY R8`` gate.
"""

from __future__ import print_function

import argparse
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

try:
    from urllib.request import urlopen
except ImportError:  # pragma: no cover
    from urllib2 import urlopen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity, save_release_manifest
from app.release_publication import current_served_root_binding
from scripts.release.build_flat_production_candidate_artifact import (
    build_flat_production_candidate,
)
from scripts.release.build_production_candidate_artifact import (
    build_production_candidate,
)
from scripts.release.current_adoption import (
    FLAT, IMMUTABLE_CURRENT, classify_deployment, plan_flat_current_adoption,
)
from scripts.upgrade.performance_evidence import validate_revision_artifact
from scripts.upgrade.evidence_manifest import MANIFEST_FILENAME


EVIDENCE_NAMES = (
    "input_integrity.json", "readiness.json", "production_baseline.json",
    "release_identity.json", "candidate_artifact_manifest.json",
    "offline_operator_evidence.json", "release_manifest.json",
    "validation_session_manifest.json", "backup_evidence.json",
    "restore_rehearsal.json", "database_generation.json",
    "disposable_target.json", "migration_evidence.json",
    "candidate_gateway_preflight.json", "candidate_runtime_binding.json",
    "candidate_server_gate.json", "operator_browser_observation.json",
    "candidate_auth_probe_evidence.json", "release_performance_ab.json",
    "candidate_browser_evidence.json", "rollback_evidence.json",
    "pre_cutover_ready.json", "cutover_evidence.json",
    "post_open_verification.json", "final_status.json",
    "sha256_inventory.json",
)

_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA64 = re.compile(r"^[0-9a-fA-F]{64}$")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, value):
    path = os.path.abspath(str(path))
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = "{}.tmp-{}".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except OSError:
            pass
    os.replace(temporary, path)


def _load_json(path, label):
    try:
        with open(os.path.abspath(str(path)), "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise RuntimeError("{} must be a JSON object".format(label))
    return value


def _command(argv, cwd=None, check=False):
    try:
        result = subprocess.Popen(
            list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = result.communicate()
    except (OSError, ValueError) as exc:
        if check:
            raise RuntimeError("command failed to start: {}".format(exc))
        return 127, "", str(exc)
    output = stdout.decode("utf-8", "replace").strip()
    error = stderr.decode("utf-8", "replace").strip()
    if check and result.returncode != 0:
        raise RuntimeError(
            "command failed ({}): {}".format(result.returncode, error or output)
        )
    return result.returncode, output, error


def _git(repo_root, *args, check=True):
    return _command(("git",) + tuple(args), cwd=repo_root, check=check)


def _inside(root, path):
    try:
        return os.path.commonpath((
            os.path.realpath(os.path.abspath(root)),
            os.path.realpath(os.path.abspath(path)),
        )) == os.path.realpath(os.path.abspath(root))
    except (AttributeError, OSError, ValueError):
        return False


def _record(path, status="INCOMPLETE", revision="", session_id="", **extra):
    payload = {
        "schema_version": 1,
        "status": status,
        "revision": revision,
        "release_validation_session_id": session_id,
        "synthetic": False,
        "started_at": _now(),
        "finished_at": _now(),
        "command_or_action": extra.pop("command_or_action", "R8 conductor"),
        "exit_code": 0 if status == "PASSED" else 1,
    }
    payload.update(extra)
    _atomic_json(path, payload)
    return payload


def _phase_state(state_root, phase, status, **extra):
    path = os.path.join(state_root, "state.json")
    current = {}
    if os.path.isfile(path):
        try:
            current = _load_json(path, "R8 state")
        except RuntimeError:
            current = {}
    current.update({
        "schema_version": 1,
        "phase": phase,
        "status": status,
        "updated_at": _now(),
    })
    current.update(extra)
    _atomic_json(path, current)
    return current


def _resolve(value, base):
    value = str(value or "")
    if not value:
        return ""
    return os.path.realpath(value if os.path.isabs(value) else os.path.join(base, value))


def _host_identity():
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }


def _read_only_http(url):
    if not url:
        return {"status": "INCOMPLETE", "reason": "endpoint is missing"}
    try:
        with urlopen(str(url), timeout=5) as response:
            return {
                "status": "PASSED" if int(getattr(response, "status", 200)) == 200 else "FAILED",
                "http_status": int(getattr(response, "status", 200)),
            }
    except Exception as exc:
        return {"status": "INCOMPLETE", "reason": str(exc)}


def verify_inputs(metadata, repo_root, source_bundle, baseline_perf,
                  candidate_perf):
    violations = []
    final_sha = str(metadata.get("final_r8_sha") or "").lower()
    final_tree = str(metadata.get("final_r8_tree") or "").lower()
    baseline_sha = str(metadata.get("production_baseline_sha") or "").lower()
    if not _SHA40.fullmatch(final_sha):
        violations.append("metadata final_r8_sha is not exact")
    if not _SHA40.fullmatch(final_tree):
        violations.append("metadata final_r8_tree is not exact")
    if not _SHA40.fullmatch(baseline_sha):
        violations.append("metadata production_baseline_sha is not exact")
    expected_host = str(metadata.get("production_host") or "vfoswind")
    observed_host = socket.gethostname()
    if observed_host != expected_host:
        violations.append(
            "production host identity does not match {} (observed {})".format(
                expected_host, observed_host
            )
        )
    try:
        usage = shutil.disk_usage(os.path.abspath(repo_root))
        disk_observation = {
            "path": os.path.abspath(repo_root),
            "free_bytes": int(usage.free),
            "total_bytes": int(usage.total),
        }
        if usage.free <= 0:
            violations.append("phase-A filesystem has no free space")
    except OSError as exc:
        disk_observation = {"status": "INCOMPLETE", "reason": str(exc)}
        violations.append("phase-A disk probe failed: {}".format(exc))
    if not os.path.isfile(source_bundle) or os.path.islink(source_bundle):
        violations.append("embedded Git bundle is missing")
    else:
        observed_bundle = _sha256(source_bundle)
        if observed_bundle != str(metadata.get("source_bundle_sha256") or "").lower():
            violations.append("embedded Git bundle SHA256 mismatch")
        code, _, error = _command(("git", "bundle", "verify", source_bundle))
        if code != 0:
            violations.append("git bundle verify failed: {}".format(error))
        try:
            head = _git(repo_root, "rev-parse", "HEAD")[1].lower()
            tree = _git(repo_root, "rev-parse", "HEAD^{tree}")[1].lower()
            if head != final_sha:
                violations.append("source checkout HEAD does not match FINAL_R8_SHA")
            if tree != final_tree:
                violations.append("source checkout tree does not match FINAL_R8_TREE")
            if _git(repo_root, "status", "--porcelain", "--untracked-files=all")[1]:
                violations.append("source checkout is not clean")
            if _git(repo_root, "cat-file", "-e", baseline_sha)[0] != 0:
                violations.append("production baseline object is absent from checkout")
        except RuntimeError as exc:
            violations.append(str(exc))

    workload_hash = str(metadata.get("workload_hash") or "")
    for path, revision, role in (
            (baseline_perf, baseline_sha, "baseline"),
            (candidate_perf, final_sha, "candidate")):
        try:
            validate_revision_artifact(path, revision, workload_hash, role)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            violations.append(str(exc))
    return {
        "status": "PASSED" if not violations else "FAILED",
        "evidence_class": "r8_input_integrity",
        "synthetic": False,
        "release_identity": {
            "commit_sha": final_sha,
            "tree_sha": final_tree,
            "production_baseline_sha": baseline_sha,
        },
        "source_bundle": {
            "path": os.path.abspath(source_bundle),
            "sha256": _sha256(source_bundle) if os.path.isfile(source_bundle) else "",
        },
        "performance_sources": {
            "baseline": os.path.abspath(baseline_perf),
            "candidate": os.path.abspath(candidate_perf),
            "workload_hash": workload_hash,
        },
        "host_identity": _host_identity(),
        "expected_production_host": expected_host,
        "disk": disk_observation,
        "command_or_action": "offline exact input verification",
        "exit_code": 0 if not violations else 1,
        "violations": violations,
    }


def collect_readiness(config, repo_root, metadata):
    """Perform a read-only vfoswind readiness inventory."""
    upgrade = dict(config.get("upgrade") or {})
    integration = dict(upgrade.get("production_integration") or {})
    violations = []
    observations = {
        "host_identity": _host_identity(),
        "required_host": str(metadata.get("production_host") or "vfoswind"),
        "python_runtime": platform.python_version(),
        "git_runtime": "",
        "mariadb_runtime": {},
        "paths": {},
        "layout": {},
        "read_only_endpoints": {},
    }
    if observations["host_identity"]["hostname"] != observations["required_host"]:
        violations.append("production host identity does not match vfoswind")
    if sys.version_info[:2] != (3, 6):
        violations.append("production conductor must run under Python 3.6")
    code, git_version, git_error = _command(("git", "--version"))
    observations["git_runtime"] = git_version or git_error
    if code != 0:
        violations.append("Git runtime is unavailable")

    publish_root_value = upgrade.get("publish_root") or ""
    flat_config_root = upgrade.get("flat_served_root") or \
        upgrade.get("legacy_flat_served_root") or \
        integration.get("legacy_served_root") or ""
    flat_transition = bool(
        flat_config_root and os.path.isdir(os.path.realpath(str(flat_config_root))) and
        publish_root_value and not os.path.lexists(os.path.join(
            os.path.realpath(str(publish_root_value)), "CURRENT"
        ))
    )
    required_paths = {
        "production_config": config.get("_config_path") or "",
        "production_app_root": integration.get("legacy_application_root") or
            "/home/zcyu/coverage/onesensor_code-coverage_tool",
        "nginx_config": integration.get("nginx_config_path") or
            "/etc/nginx/conf.d/coverage.conf",
        "systemd_unit_file": integration.get("systemd_unit_file") or
            "/etc/systemd/system/onesensor-api.service",
        "publish_root": upgrade.get("publish_root") or "",
        "served_root": upgrade.get("served_root_path") or "",
        "backup_root": upgrade.get("backup_root") or "",
    }
    for name, value in required_paths.items():
        path = str(value)
        exists = bool(path and os.path.exists(path))
        observations["paths"][name] = {
            "path": path,
            "exists": exists,
            "realpath": os.path.realpath(path) if exists else "",
        }
        deferred_creation = (name in ("publish_root", "served_root") and
                             flat_transition) or name == "backup_root"
        if not exists and deferred_creation:
            observations["paths"][name]["deferred_creation_allowed"] = True
        elif not exists:
            violations.append("required production path is missing: {}".format(name))

    publish_root = str(upgrade.get("publish_root") or "")
    flat_root = str(upgrade.get("flat_served_root") or
                    upgrade.get("legacy_flat_served_root") or "")
    flat_identity = str(upgrade.get("flat_release_identity_path") or
                        upgrade.get("legacy_flat_release_identity_path") or "")
    try:
        observations["layout"] = classify_deployment(publish_root, flat_root)
        if observations["layout"].get("status") != "PASSED":
            violations.append("deployment layout is not a verified Flat Root or CURRENT")
        elif observations["layout"].get("deployment_layout") == FLAT:
            if not flat_identity or not os.path.isfile(flat_identity):
                violations.append("Flat Root release identity is missing")
            else:
                observations["flat_identity_path"] = os.path.realpath(flat_identity)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        observations["layout"] = {"status": "FAILED", "reason": str(exc)}
        violations.append("deployment layout preflight failed: {}".format(exc))

    health = _read_only_http(upgrade.get("health_endpoint"))
    release = _read_only_http(upgrade.get("release_endpoint"))
    observations["read_only_endpoints"] = {"health": health, "release": release}
    if health.get("status") != "PASSED":
        violations.append("production health endpoint was not verified")
    if release.get("status") != "PASSED":
        violations.append("production release endpoint was not verified")

    db_config = dict(config.get("mysql") or {})
    db_identity = {}
    try:
        import pymysql
        connection = pymysql.connect(
            host=db_config.get("host", "127.0.0.1"),
            port=int(db_config.get("port", 3306)),
            user=db_config.get("user", ""),
            password=str(db_config.get("password", "")),
            database=db_config.get("database", ""),
            autocommit=True,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION(), CURRENT_USER(), DATABASE()")
                row = cursor.fetchone()
            if isinstance(row, dict):
                values = list(row.values())
            else:
                values = list(row or ())
            db_identity = {
                "version": str(values[0] if len(values) > 0 else ""),
                "current_user": str(values[1] if len(values) > 1 else ""),
                "database": str(values[2] if len(values) > 2 else ""),
            }
        finally:
            connection.close()
        observations["mariadb_runtime"] = db_identity
        if not db_identity.get("version", "").startswith("5.5"):
            violations.append("MariaDB runtime is not 5.5")
        expected_user = str(db_config.get("user") or "")
        if expected_user and not db_identity.get("current_user", "").startswith(
                expected_user + "@"):
            violations.append("application CURRENT_USER() does not match configured user")
        if str(db_config.get("database") or "") and \
                db_identity.get("database") != str(db_config.get("database")):
            violations.append("application DATABASE() does not match configured database")
    except Exception as exc:
        violations.append("MariaDB read-only identity probe failed: {}".format(exc))

    disk_root = str(upgrade.get("publish_root") or repo_root)
    try:
        usage = shutil.disk_usage(disk_root)
        observations["disk"] = {
            "path": disk_root,
            "free_bytes": int(usage.free),
            "total_bytes": int(usage.total),
        }
    except OSError as exc:
        violations.append("disk capacity probe failed: {}".format(exc))

    return {
        "status": "PASSED" if not violations else "INCOMPLETE",
        "evidence_class": "production_readiness",
        "synthetic": False,
        "host_identity": observations["host_identity"],
        "observations": observations,
        "violations": violations,
        "command_or_action": "read-only production readiness inventory",
        "exit_code": 0 if not violations else 1,
    }


def _baseline_identity(config, layout):
    upgrade = config.get("upgrade") or {}
    if layout.get("deployment_layout") == IMMUTABLE_CURRENT:
        manifest_path = os.path.join(
            os.path.realpath(str(upgrade.get("publish_root"))), "CURRENT",
            "release_manifest.json"
        )
        manifest = _load_json(manifest_path, "CURRENT release manifest")
        identity = manifest.get("release_identity")
        if not isinstance(identity, dict):
            raise RuntimeError("CURRENT release identity is missing")
        return identity
    identity_path = str(upgrade.get("flat_release_identity_path") or
                        upgrade.get("legacy_flat_release_identity_path") or "")
    identity = _load_json(identity_path, "Flat Root release identity")
    return identity


def _write_placeholders(evidence_root, revision, session_id):
    for name in EVIDENCE_NAMES:
        path = os.path.join(evidence_root, name)
        if not os.path.exists(path):
            _record(path, revision=revision, session_id=session_id,
                    evidence_class="r8_evidence_placeholder",
                    command_or_action="awaiting the corresponding release phase")


def _project_manifest(evidence_root, manifest_path, revision, session_id):
    if not os.path.isfile(manifest_path):
        return
    try:
        manifest = _load_json(manifest_path, "production evidence manifest")
    except RuntimeError:
        return
    mapping = {
        "release_identity": "release_identity.json",
        "candidate_release_prepared": "release_manifest.json",
        "backup_evidence": "backup_evidence.json",
        "database_generation": "database_generation.json",
        "disposable_target": "disposable_target.json",
        "schema_migration": "migration_evidence.json",
        "candidate_gateway_preflight": "candidate_gateway_preflight.json",
        "production_validation_runtime_binding": "candidate_runtime_binding.json",
        "candidate_release_endpoint": "candidate_server_gate.json",
        "operator_browser_observation": "operator_browser_observation.json",
        "candidate_authenticated_mutation": "candidate_auth_probe_evidence.json",
        "performance_benchmark": "release_performance_ab.json",
        "candidate_browser_evidence": "candidate_browser_evidence.json",
        "rollback_evidence": "rollback_evidence.json",
        "pre_cutover_ready": "pre_cutover_ready.json",
        "file_cutover": "cutover_evidence.json",
        "post_open_serving": "post_open_verification.json",
    }
    for section, name in mapping.items():
        value = manifest.get(section)
        if isinstance(value, dict):
            value = dict(value)
            value.setdefault("revision", revision)
            value.setdefault("release_validation_session_id", session_id)
            _atomic_json(os.path.join(evidence_root, name), value)
    backup = manifest.get("backup_evidence") or {}
    verification = backup.get("verification") or {}
    restore_status = verification.get("restore_smoke")
    _atomic_json(os.path.join(evidence_root, "restore_rehearsal.json"), {
        "schema_version": 1,
        "status": "PASSED" if restore_status == "PASSED" else "INCOMPLETE",
        "evidence_class": "verified_backup_restore_rehearsal",
        "synthetic": False,
        "revision": revision,
        "release_validation_session_id": session_id,
        "restore_smoke": restore_status or "NOT_VERIFIED",
        "backup_artifact_sha256": backup.get("full_sql_gz_sha256", ""),
        "verification": verification,
        "command_or_action": "derived from explicit verified-backup restore smoke",
        "exit_code": 0 if restore_status == "PASSED" else 1,
    })


def _inventory(evidence_root, revision, session_id):
    files = []
    for name in sorted(os.listdir(evidence_root)):
        path = os.path.join(evidence_root, name)
        if name == "sha256_inventory.json" or not os.path.isfile(path):
            continue
        files.append({
            "path": name,
            "size": int(os.path.getsize(path)),
            "sha256": _sha256(path),
        })
    payload = {
        "schema_version": 1,
        "status": "PASSED",
        "evidence_class": "sha256_inventory",
        "revision": revision,
        "release_validation_session_id": session_id,
        "files": files,
        "command_or_action": "SHA256 inventory of R8 attempt evidence",
        "exit_code": 0,
    }
    _atomic_json(os.path.join(evidence_root, "sha256_inventory.json"), payload)
    return payload


def _finalize(evidence_root, state_root, revision, session_id, run_success):
    try:
        manifest = _load_json(
            os.path.join(evidence_root, MANIFEST_FILENAME),
            "production evidence manifest",
        )
    except RuntimeError:
        manifest = {}
    post_open = manifest.get("post_open_serving") or {}
    ready = manifest.get("pre_cutover_ready") or {}
    status = "PRODUCTION_VERIFIED" if run_success and \
        post_open.get("status") == "PASSED" else "NOT_READY"
    mutation = "AUTHORIZED" if manifest.get("production_mutation_boundary", {}).get(
        "production_mutation") == "AUTHORIZED" else "NONE"
    final = _record(
        os.path.join(evidence_root, "final_status.json"),
        status="PASSED" if status == "PRODUCTION_VERIFIED" else "INCOMPLETE",
        revision=revision, session_id=session_id,
        evidence_class="r8_final_status",
        command_or_action="R8 final adjudication",
        production_status=status,
        production_mutation=mutation,
        pre_cutover_ready=ready.get("status") == "PASSED",
        post_open_verified=post_open.get("status") == "PASSED",
    )
    final["production_status"] = status
    final["production_mutation"] = mutation
    _atomic_json(os.path.join(evidence_root, "final_status.json"), final)
    _inventory(evidence_root, revision, session_id)
    return final


def run(args):
    state_root = os.path.realpath(os.path.abspath(args.state_root))
    evidence_root = os.path.join(state_root, "evidence")
    input_root = os.path.join(state_root, "input")
    if not os.path.isdir(evidence_root):
        os.makedirs(evidence_root)
    metadata = _load_json(args.metadata, "release metadata")
    revision = str(metadata.get("final_r8_sha") or "").lower()
    session_id = ""
    state_path = os.path.join(state_root, "state.json")
    if args.resume and os.path.isfile(state_path):
        prior = _load_json(state_path, "R8 state")
        session_id = str(prior.get("release_validation_session_id") or "")
    if not session_id:
        session_id = "candidate-{}-{}".format(revision, uuid.uuid4().hex[:16])
    _write_placeholders(evidence_root, revision, session_id)

    _phase_state(state_root, "A", "RUNNING", release_validation_session_id=session_id)
    inputs = verify_inputs(
        metadata, args.repo_root, args.source_bundle,
        args.baseline_performance, args.candidate_performance,
    )
    _atomic_json(os.path.join(evidence_root, "input_integrity.json"), inputs)
    if inputs.get("status") != "PASSED":
        _phase_state(state_root, "A", "FAILED", release_validation_session_id=session_id)
        return _finalize(evidence_root, state_root, revision, session_id, False)
    _phase_state(state_root, "A", "PASSED", release_validation_session_id=session_id)

    config = _load_json(args.config, "production config")
    config["_config_path"] = os.path.abspath(args.config)
    _phase_state(state_root, "B", "RUNNING", release_validation_session_id=session_id)
    readiness = collect_readiness(config, args.repo_root, metadata)
    _atomic_json(os.path.join(evidence_root, "readiness.json"), readiness)
    _atomic_json(os.path.join(evidence_root, "production_baseline.json"), readiness)
    if readiness.get("status") != "PASSED":
        _phase_state(state_root, "B", "FAILED", release_validation_session_id=session_id)
        return _finalize(evidence_root, state_root, revision, session_id, False)
    _phase_state(state_root, "B", "PASSED", release_validation_session_id=session_id)

    _phase_state(state_root, "C", "RUNNING", release_validation_session_id=session_id)
    identity = generate_release_identity(args.repo_root, build_provenance="release-build")
    if identity.get("commit_sha") != revision:
        raise RuntimeError("generated release identity does not match FINAL_R8_SHA")
    _atomic_json(os.path.join(evidence_root, "release_identity.json"), identity)
    _atomic_json(os.path.join(state_root, "release_identity.json"), identity)
    upgrade = dict(config.get("upgrade") or {})
    publish_root = _resolve(upgrade.get("publish_root"), args.repo_root)
    layout = classify_deployment(publish_root, _resolve(
        upgrade.get("flat_served_root") or upgrade.get("legacy_flat_served_root"),
        args.repo_root,
    ))
    baseline = _baseline_identity(config, layout)
    baseline_sha = str(baseline.get("commit_sha") or "").lower()
    if baseline_sha != str(metadata.get("production_baseline_sha") or "").lower():
        raise RuntimeError("production baseline identity does not match embedded metadata")
    baseline_evidence = {
        "schema_version": 1,
        "status": "PASSED",
        "evidence_class": "production_baseline_identity",
        "synthetic": False,
        "revision": revision,
        "production_baseline_sha": baseline_sha,
        "release_identity": baseline,
        "deployment_layout": layout,
        "readiness_status": readiness.get("status"),
        "command_or_action": "read-only baseline release identity binding",
        "exit_code": 0,
    }
    _atomic_json(
        os.path.join(evidence_root, "production_baseline.json"),
        baseline_evidence,
    )
    candidate_root = os.path.join(state_root, "production-candidate")
    offline_evidence = os.path.join(state_root, "offline-operator-evidence.json")
    if layout.get("deployment_layout") == IMMUTABLE_CURRENT:
        binding = current_served_root_binding(publish_root)
        build_result = build_production_candidate(
            publish_root, args.repo_root, candidate_root,
            os.path.join(state_root, "target-release-identity.json"),
            "offline-r8-operator", "", "", "",
            expected_previous_release_sha=binding["previous_release_commit_sha"],
            expected_served_root_tree_sha256=binding["served_root_tree_sha256"],
            expected_current_identity_sha256=binding["current_identity_sha256"],
            publish_root=publish_root,
            release_trust_mode="offline_operator",
            offline_operator_evidence_output=offline_evidence,
            offline_operator_source_bundle=args.source_bundle,
            offline_operator_repository=str(metadata.get("repository") or "Chary-yu/fos_coverage_tool"),
            production_host=str(metadata.get("production_host") or "vfoswind"),
            production_baseline_sha=baseline_sha,
            validation_session_id=session_id,
        )
    elif layout.get("deployment_layout") == FLAT:
        flat_root = _resolve(
            upgrade.get("flat_served_root") or upgrade.get("legacy_flat_served_root"),
            args.repo_root,
        )
        flat_identity = _resolve(
            upgrade.get("flat_release_identity_path") or
            upgrade.get("legacy_flat_release_identity_path"), args.repo_root,
        )
        plan = plan_flat_current_adoption(
            publish_root, flat_root, flat_identity, baseline_sha
        )
        _atomic_json(os.path.join(state_root, "flat-source-binding.json"),
                     plan["flat_source_binding"])
        build_result = build_flat_production_candidate(
            flat_root, flat_identity, args.repo_root, candidate_root,
            os.path.join(state_root, "target-release-identity.json"),
            expected_previous_release_sha=baseline_sha,
            expected_source_binding=plan["flat_source_binding"],
            release_trust_mode="offline_operator",
            offline_operator_evidence_output=offline_evidence,
            offline_operator_source_bundle=args.source_bundle,
            offline_operator_repository=str(metadata.get("repository") or "Chary-yu/fos_coverage_tool"),
            production_host=str(metadata.get("production_host") or "vfoswind"),
            production_baseline_sha=baseline_sha,
            validation_session_id=session_id,
            flat_baseline_session_id=str(upgrade.get("flat_baseline_session_id") or ""),
        )
    else:
        raise RuntimeError("deployment layout is neither IMMUTABLE_CURRENT nor FLAT")
    _atomic_json(os.path.join(evidence_root, "candidate_artifact_manifest.json"),
                 _load_json(build_result["candidate_artifact_manifest"],
                            "Candidate artifact manifest"))
    _atomic_json(os.path.join(evidence_root, "offline_operator_evidence.json"),
                 _load_json(offline_evidence, "offline operator evidence"))
    _atomic_json(os.path.join(evidence_root, "release_manifest.json"), build_result)
    _atomic_json(os.path.join(evidence_root, "release_identity.json"), identity)

    # The production config is never edited in place.  All attempt-scoped
    # paths are owned by this state directory and the performance join is
    # performed later by run_upgrade.py after publication hashes are known.
    attempt_config = copy.deepcopy(config)
    attempt_config.pop("_config_path", None)
    attempt_upgrade = dict(attempt_config.get("upgrade") or {})
    attempt_upgrade.update({
        "production_candidate_root": candidate_root,
        "production_candidate_artifact_manifest": build_result[
            "candidate_artifact_manifest"
        ],
        "release_trust_mode": "offline_operator",
        "offline_operator_evidence": offline_evidence,
        "offline_operator_source_bundle": args.source_bundle,
        "offline_operator_repository": str(
            metadata.get("repository") or "Chary-yu/fos_coverage_tool"
        ),
        "production_host": str(metadata.get("production_host") or "vfoswind"),
        "production_baseline_sha": baseline_sha,
        "release_validation_session_id": session_id,
        "validation_session_manifest": os.path.join(
            state_root, "validation-session-{attempt_id}.json"
        ),
        "validation_teardown_evidence_path": os.path.join(
            state_root, "validation-teardown-{attempt_id}.json"
        ),
        "candidate_browser_evidence_path": os.path.join(
            evidence_root, "candidate-browser-{attempt_id}.json"
        ),
        "candidate_auth_probe_evidence_path": os.path.join(
            evidence_root, "candidate-auth-{attempt_id}.json"
        ),
        "performance_evidence_path": os.path.join(
            evidence_root, "release-performance-{attempt_id}.json"
        ),
        "rollback_evidence_path": os.path.join(
            evidence_root, "rollback-{attempt_id}.json"
        ),
        "performance_source_artifacts": {
            "baseline": args.baseline_performance,
            "candidate": args.candidate_performance,
            "workload_hash": str(metadata.get("workload_hash") or ""),
            "max_regression_percent": float(
                metadata.get("performance_max_regression_percent", 20)
            ),
        },
        "operator_browser_pause": True,
        "require_apply_confirmation": True,
    })
    attempt_config["upgrade"] = attempt_upgrade
    attempt_config_path = os.path.join(state_root, "attempt-config.json")
    _atomic_json(attempt_config_path, attempt_config)
    deployment_manifest = os.path.join(state_root, "deployment-manifest.json")
    _atomic_json(deployment_manifest, {
        "schema_version": 1,
        "actions": [{
            "op": "ADD", "source": "README.md", "destination": "README.md",
            "source_sha256": _sha256(os.path.join(args.repo_root, "README.md")),
            "backup_required": True,
        }],
    })
    _phase_state(
        state_root, "C", "PASSED", release_validation_session_id=session_id,
        candidate_artifact_sha256=build_result.get("artifact_sha256", ""),
    )

    # The actual run is deliberately delegated to the existing fail-closed
    # lifecycle.  It performs Candidate validation, waits for BROWSER READY,
    # records PRE_CUTOVER_READY, then waits for APPLY R8 before Phase D.
    os.environ["COVERAGE_EVIDENCE_DIR"] = evidence_root
    command = [
        sys.executable, os.path.join(args.repo_root, "scripts", "upgrade", "run_upgrade.py"),
        "--mode", "production", "--manifest", deployment_manifest,
        "--target-release", os.path.join(state_root, "target-release-identity.json"),
        "--config", attempt_config_path,
    ]
    _phase_state(state_root, "D", "RUNNING", release_validation_session_id=session_id)
    code, output, error = _command(command, cwd=args.repo_root)
    _atomic_json(os.path.join(state_root, "run-upgrade-result.json"), {
        "status": "PASSED" if code == 0 else "FAILED",
        "exit_code": code,
        "stdout_tail": output[-12000:],
        "stderr_tail": error[-12000:],
        "command": "python3.6 scripts/upgrade/run_upgrade.py --mode production",
    })
    manifest_path = os.path.join(evidence_root, "evidence_manifest.json")
    _project_manifest(evidence_root, manifest_path, revision, session_id)
    if code != 0:
        _phase_state(
            state_root, "E",
            "ROLLBACK_REQUIRED" if "Phase D" in output else "NOT_REQUIRED",
            release_validation_session_id=session_id,
        )
    else:
        _phase_state(
            state_root, "E", "NOT_REQUIRED",
            release_validation_session_id=session_id,
        )
    _phase_state(
        state_root, "F", "PASSED" if code == 0 else "FAILED",
        release_validation_session_id=session_id,
    )
    return _finalize(evidence_root, state_root, revision, session_id, code == 0)


def status(state_root):
    path = os.path.join(os.path.abspath(state_root), "state.json")
    if not os.path.isfile(path):
        return {"status": "NOT_STARTED", "state_root": os.path.abspath(state_root)}
    return _load_json(path, "R8 state")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fos_r8_conductor.py")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--source-bundle", required=True)
    parser.add_argument("--baseline-performance", required=True)
    parser.add_argument("--candidate-performance", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(status(args.state_root), ensure_ascii=False, indent=2,
                          sort_keys=True))
        return 0
    try:
        result = run(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        state_root = os.path.abspath(args.state_root)
        evidence_root = os.path.join(state_root, "evidence")
        if not os.path.isdir(evidence_root):
            os.makedirs(evidence_root)
        _phase_state(state_root, "FAILED", "FAILED", error=str(exc))
        result = _record(
            os.path.join(evidence_root, "final_status.json"),
            status="INCOMPLETE", evidence_class="r8_final_status",
            command_or_action="R8 conductor failure", error=str(exc),
            production_status="NOT_READY", production_mutation="NONE",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("production_status") == "PRODUCTION_VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
