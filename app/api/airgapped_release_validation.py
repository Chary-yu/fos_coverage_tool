"""Air-gapped operator-browser observation for one immutable Candidate.

The endpoint is available only from the isolated Candidate runtime behind the
reverse-proxy identity boundary. Candidate identity and the selected real
report are derived from the immutable release manifest; browser-supplied
identity is never authoritative. The operator observation is later joined by
the air-gapped upgrade conductor with independently measured exact-revision
100k performance evidence before the canonical browser gate is evaluated.
"""

from __future__ import print_function

import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from app.api.auth import AUTH_MUTATION_PROBE_PATH
from app.release_identity import is_valid_commit_sha
from app.release_publication import validate_release_manifest


PAGE_PATH = "/api/coverage/release-validation/page.html"
CONTEXT_PATH = "/api/coverage/release-validation/context"
SUBMIT_PATH = "/api/coverage/release-validation/browser-observation"
EVIDENCE_DIRECTORY = "validation_evidence"
STATE_NAME = "airgapped_operator_state.json"
OPERATOR_EVIDENCE_NAME = "operator_browser_observation.json"
OPERATOR_WORKLOAD_NAME = "operator_browser_workload.json"
AUTH_EVIDENCE_NAME = "candidate_auth_probe_evidence.json"
DEFAULT_GATEWAY_CONFIG = "/etc/nginx/conf.d/coverage-candidate.conf"
HTML_RELATIVE_PATH = os.path.join("web", "airgapped_release_validation.html")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root, path):
    try:
        return os.path.commonpath((_real(root), _real(path))) == _real(root)
    except (AttributeError, OSError, ValueError):
        return False


def _atomic_json(path, payload):
    path = os.path.abspath(str(path))
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory, 0o700)
    if os.path.islink(directory):
        raise RuntimeError("validation evidence directory may not be a symlink")
    temporary = "{}.part-{}-{}".format(path, os.getpid(), secrets.token_hex(4))
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            if os.path.lexists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def _load_json(path, label):
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            raise RuntimeError("{} is missing or linked".format(label))
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("{} is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise RuntimeError("{} must be a JSON object".format(label))
    return value


def _candidate_release_root(application):
    runtime = getattr(application, "runtime", None)
    application_root = _real(getattr(runtime, "repo_root", ""))
    if not application_root or os.path.basename(application_root) != "app":
        raise RuntimeError("Candidate runtime must execute from immutable release/app")
    release_root = _real(os.path.dirname(application_root))
    manifest_path = os.path.join(release_root, "release_manifest.json")
    if not os.path.isfile(manifest_path) or os.path.islink(manifest_path):
        raise RuntimeError("immutable Candidate release_manifest.json is unavailable")
    return release_root, manifest_path


def _validated_report_entries(release_root, entries):
    reports_root = _real(os.path.join(release_root, "reports"))
    if not isinstance(entries, list):
        raise RuntimeError("Candidate release report inventory is invalid")
    result = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise RuntimeError("Candidate release report entry is invalid")
        relative = str(raw.get("path") or "").replace("\\", "/").strip()
        sha256 = str(raw.get("sha256") or "").strip().lower()
        if not relative.startswith("reports/") or not relative.lower().endswith(
                (".html", ".htm")):
            raise RuntimeError("Candidate report path is invalid: {}".format(relative))
        if not _SHA256_RE.fullmatch(sha256):
            raise RuntimeError("Candidate report SHA256 is invalid: {}".format(relative))
        absolute = _real(os.path.join(release_root, *relative.split("/")))
        if not _inside(reports_root, absolute) or not os.path.isfile(absolute) or \
                os.path.islink(absolute):
            raise RuntimeError("Candidate report escapes immutable reports root")
        try:
            size = int(raw.get("size"))
        except (TypeError, ValueError):
            raise RuntimeError("Candidate report size is invalid: {}".format(relative))
        if size != os.path.getsize(absolute):
            raise RuntimeError("Candidate report size changed: {}".format(relative))
        item = dict(raw)
        item.update({"path": relative, "sha256": sha256, "size": size})
        result.append(item)
    return sorted(
        result,
        key=lambda item: (
            0 if str(item.get("file_path") or "").strip() else 1,
            -int(item.get("size") or 0),
            str(item.get("path") or ""),
        ),
    )


def _validated_publication(application):
    release_root, manifest_path = _candidate_release_root(application)
    manifest = _load_json(manifest_path, "Candidate release manifest")
    session_id = str(manifest.get("release_validation_session_id") or "").strip()
    if not _SESSION_RE.fullmatch(session_id) or os.path.basename(release_root) != session_id:
        raise RuntimeError("Candidate release directory/session identity mismatch")
    checked = validate_release_manifest(
        release_root, manifest, expected_session_id=session_id
    )
    if checked.get("status") != "PASSED":
        raise RuntimeError(
            "immutable Candidate release manifest failed validation: {}".format(
                "; ".join(str(item) for item in (checked.get("violations") or []))
            )
        )
    release = manifest.get("release_identity") or {}
    candidate = manifest.get("candidate_artifact_manifest") or {}
    served = manifest.get("served_root") or {}
    candidate_sha = str(release.get("commit_sha") or "").strip().lower()
    artifact_sha = str(
        candidate.get("candidate_artifact_sha256") or
        candidate.get("artifact_sha256") or ""
    ).strip().lower()
    served_sha = str(served.get("sha256") or "").strip().lower()
    if not is_valid_commit_sha(candidate_sha):
        raise RuntimeError("Candidate commit identity is unavailable")
    if not _SHA256_RE.fullmatch(artifact_sha) or not _SHA256_RE.fullmatch(served_sha):
        raise RuntimeError("Candidate publication SHA256 identity is unavailable")
    reports = _validated_report_entries(release_root, manifest.get("reports") or [])
    if not reports:
        raise RuntimeError("immutable Candidate manifest contains no report HTML")
    selected = reports[0]
    selected_path = _real(os.path.join(
        release_root, *str(selected["path"]).split("/")
    ))
    if _sha256_file(selected_path) != selected["sha256"]:
        raise RuntimeError("selected Candidate report bytes changed after validation")
    source = candidate.get("source_provenance") or {}
    publication = {
        "release_validation_session_id": session_id,
        "candidate_artifact_sha256": artifact_sha,
        "served_root_sha256": served_sha,
        "commit_sha": candidate_sha,
        "previous_release_commit_sha": str(source.get("previous_release_commit_sha") or ""),
        "served_root_tree_sha256": str(source.get("served_root_tree_sha256") or ""),
        "served_root_identity_sha256": str(source.get("served_root_identity_sha256") or ""),
        "artifact_role": str(candidate.get("artifact_role") or ""),
        "production_publishable": candidate.get("production_publishable") is True,
        "project_name": str(candidate.get("project_name") or ""),
    }
    return release_root, release, publication, reports


def _evidence_paths(release_root, session_id):
    releases_root = _real(os.path.dirname(release_root))
    if os.path.basename(releases_root) != "releases":
        raise RuntimeError("Candidate release root is not under publish_root/releases")
    publish_root = _real(os.path.dirname(releases_root))
    configured = str(os.environ.get("COVERAGE_VALIDATION_EVIDENCE_ROOT") or "").strip()
    evidence_parent = _real(configured or os.path.join(publish_root, EVIDENCE_DIRECTORY))
    if evidence_parent == publish_root or not _inside(publish_root, evidence_parent):
        raise RuntimeError("validation evidence root must stay inside publish_root")
    attempt_root = _real(os.path.join(evidence_parent, session_id))
    if not _inside(evidence_parent, attempt_root):
        raise RuntimeError("validation evidence session escapes its root")
    return {
        "publish_root": publish_root,
        "attempt_root": attempt_root,
        "state": os.path.join(attempt_root, STATE_NAME),
        "operator": os.path.join(attempt_root, OPERATOR_EVIDENCE_NAME),
        "workload": os.path.join(attempt_root, OPERATOR_WORKLOAD_NAME),
        "auth": os.path.join(attempt_root, AUTH_EVIDENCE_NAME),
    }


def _state(paths, session_id, timeout_sec):
    path = paths["state"]
    if os.path.isfile(path) and not os.path.islink(path):
        value = _load_json(path, "air-gapped validation state")
        if value.get("session_id") != session_id or not value.get("nonce"):
            raise RuntimeError("air-gapped validation state identity mismatch")
        return value
    now = time.time()
    value = {
        "schema_version": 1,
        "session_id": session_id,
        "nonce": secrets.token_hex(32),
        "created_at": now,
        "expires_at": now + timeout_sec,
        "submitted": False,
    }
    _atomic_json(path, value)
    return value


def _gateway_origin(headers):
    host = str(headers.get("Host") or "").strip()
    proto = str(headers.get("X-Forwarded-Proto") or "http").strip().lower()
    if proto not in ("http", "https") or not host or any(
            char in host for char in ("/", "\\", "@", "\r", "\n", "\t", " ")):
        raise RuntimeError("Candidate Gateway origin is invalid")
    parsed = urlparse("{}://{}".format(proto, host))
    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname or parsed.username or parsed.password:
        raise RuntimeError("Candidate Gateway origin is invalid")
    try:
        if ipaddress.ip_address(hostname.strip("[]")).is_loopback:
            raise RuntimeError("operator browser must use the external Candidate Gateway")
    except ValueError:
        if hostname == "localhost":
            raise RuntimeError("operator browser must use the external Candidate Gateway")
    return "{}://{}".format(proto, host)


def _report_url(origin, report):
    relative = str(report.get("path") or "")
    if not relative.startswith("reports/"):
        raise RuntimeError("Candidate report path is outside reports/")
    encoded = "/".join(
        quote(part, safe="") for part in relative[len("reports/"):].split("/")
    )
    return origin.rstrip("/") + "/coverage/" + encoded


def _gateway_config_sha256():
    path = str(os.environ.get("COVERAGE_CANDIDATE_GATEWAY_CONFIG") or
               DEFAULT_GATEWAY_CONFIG).strip()
    if not os.path.isabs(path) or os.path.islink(path) or not os.path.isfile(path):
        raise RuntimeError("Candidate Gateway config is unavailable")
    return _sha256_file(path)


def enabled(application):
    config = application.config or {}
    return str(config.get("environment") or "").strip().lower() == "candidate" and \
        str((config.get("auth") or {}).get("mode") or "").strip().lower() == "reverse_proxy"


def _public_context(application, headers):
    release_root, release, publication, reports = _validated_publication(application)
    session_id = publication["release_validation_session_id"]
    paths = _evidence_paths(release_root, session_id)
    timeout_sec = int(os.environ.get("COVERAGE_OPERATOR_BROWSER_TIMEOUT_SEC") or 1800)
    if timeout_sec < 60 or timeout_sec > 7200:
        raise RuntimeError("operator browser timeout must be between 60 and 7200 seconds")
    state = _state(paths, session_id, timeout_sec)
    report = reports[0]
    auth = application.config.get("auth") or {}
    return {
        "schema_version": 2,
        "mode": "airgapped_operator_browser",
        "candidate_url": _report_url(_gateway_origin(headers), report),
        "candidate_revision": str(release.get("commit_sha") or ""),
        "release_validation_session_id": session_id,
        "candidate_artifact_sha256": publication["candidate_artifact_sha256"],
        "served_root_sha256": publication["served_root_sha256"],
        "previous_release_commit_sha": publication["previous_release_commit_sha"],
        "served_root_tree_sha256": publication["served_root_tree_sha256"],
        "served_root_identity_sha256": publication["served_root_identity_sha256"],
        "release_identity": release,
        "publication": publication,
        "report_path": report["path"],
        "report_sha256": report["sha256"],
        "report_size": report["size"],
        "report_mode": str(report.get("report_mode") or ""),
        "report_id": str(report.get("report_id") or ""),
        "scan_id": report.get("scan_id"),
        "repository_name": str(report.get("repository_name") or ""),
        "file_path": str(report.get("file_path") or ""),
        "auth_mode": str(auth.get("mode") or "").strip().lower(),
        "user_header": str(auth.get("user_header") or "X-Remote-User").strip(),
        "gateway_config_sha256": _gateway_config_sha256(),
        "nonce": state["nonce"],
        "submitted": bool(state.get("submitted")),
        "expires_at": float(state.get("expires_at") or 0),
        "_paths": paths,
    }


def _permission_status(exc):
    raw = str(exc or "")
    for value in (401, 403, 503):
        if raw.startswith("{}:".format(value)):
            return value
    return 403


def _require_operator(application, headers, remote_address, mutation=False):
    try:
        identity = application._require_mutation(headers, remote_address) if mutation \
            else application._require_operator(headers, remote_address)
        return identity, None
    except PermissionError as exc:
        return "", (_permission_status(exc), {
            "error": "forbidden",
            "message": "release validation requires authenticated operator access",
        })


def _negative_mutation_probe(candidate_url):
    parsed = urlparse(candidate_url)
    request = urllib.request.Request(
        "{}://{}{}".format(parsed.scheme, parsed.netloc, AUTH_MUTATION_PROBE_PATH),
        data=b"{}", method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError, urllib.error.URLError):
        return 0


def _validate_submission(context, body, operator):
    errors = []
    if not isinstance(body, dict):
        return ["browser observation must be a JSON object"]
    for field in (
            "nonce", "candidate_revision", "release_validation_session_id",
            "candidate_artifact_sha256", "served_root_sha256", "candidate_url",
            "report_path", "report_sha256"):
        if body.get(field) != context.get(field):
            errors.append("{} does not match immutable Candidate context".format(field))
    functional = body.get("browser_functional") or {}
    workload = body.get("operator_browser_workload") or {}
    environment = workload.get("environment_identity") or {}
    if functional.get("status") != "PASSED" or functional.get("report_loaded") is not True:
        errors.append("real Candidate report functional validation did not pass")
    if functional.get("release_identity_verified") is not True:
        errors.append("browser did not verify Candidate release identity")
    if workload.get("status") != "PASSED" or \
            workload.get("workload_id") != "airgapped-real-candidate-report-v1":
        errors.append("operator browser workload did not pass")
    if environment.get("browser_name") != "chromium":
        errors.append("operator browser must be Chromium/Chrome/Edge based")
    if int(workload.get("visible_content_units") or 0) < 1:
        errors.append("operator browser observed no report content")
    if workload.get("js_errors"):
        errors.append("operator browser observed JavaScript errors")
    auth = body.get("authenticated_probe") or {}
    status = auth.get("status_code")
    probe = auth.get("payload") or {}
    if type(status) is not int or status < 200 or status >= 300:
        errors.append("authenticated mutation probe HTTP status is not successful")
    if probe.get("mutation_probe") is not True or \
            probe.get("probe_path") != AUTH_MUTATION_PROBE_PATH or \
            probe.get("database_mutation") is not False:
        errors.append("authenticated zero-write mutation probe contract was not observed")
    authenticated_user = str(probe.get("authenticated_user") or "").strip()
    if not authenticated_user:
        errors.append("authenticated mutation probe did not expose backend identity")
    if authenticated_user and operator and authenticated_user != operator:
        errors.append("browser operator identity does not match backend mutation identity")
    return errors


def _write_evidence(context, body, operator, negative_status):
    paths = context["_paths"]
    workload = dict(body.get("operator_browser_workload") or {})
    workload.update({
        "candidate_revision": context["candidate_revision"],
        "release_validation_session_id": context["release_validation_session_id"],
        "candidate_artifact_sha256": context["candidate_artifact_sha256"],
        "served_root_sha256": context["served_root_sha256"],
        "candidate_url": context["candidate_url"],
        "report_path": context["report_path"],
        "report_sha256": context["report_sha256"],
        "recorded_at": time.time(),
    })
    _atomic_json(paths["workload"], workload)
    workload_sha = _sha256_file(paths["workload"])
    observation = {
        "schema_version": 2,
        "status": "PASSED",
        "evidence_class": "airgapped_real_candidate_browser_observation",
        "synthetic": False,
        "real_http": True,
        "release_eligible": False,
        "requires_performance_join": True,
        "candidate_revision": context["candidate_revision"],
        "release_validation_session_id": context["release_validation_session_id"],
        "candidate_artifact_sha256": context["candidate_artifact_sha256"],
        "served_root_sha256": context["served_root_sha256"],
        "candidate_url": context["candidate_url"],
        "page_url": context["candidate_url"],
        "report_path": context["report_path"],
        "report_sha256": context["report_sha256"],
        "report_sha256_verified_server_side": True,
        "release_identity": context["release_identity"],
        "observed_publication": context["publication"],
        "browser_functional": dict(body.get("browser_functional") or {}),
        "operator_browser_workload": workload,
        "artifact_path": paths["workload"],
        "artifact_sha256": workload_sha,
        "operator_identity": operator,
        "credentials_recorded": False,
        "exit_code": 0,
        "recorded_at": time.time(),
    }
    parsed = urlparse(context["candidate_url"])
    origin = "{}://{}".format(parsed.scheme, parsed.netloc)
    authenticated = body.get("authenticated_probe") or {}
    probe = authenticated.get("payload") or {}
    auth = {
        "schema_version": 2,
        "status": "PASSED",
        "evidence_class": "real_candidate_authenticated_mutation",
        "release_eligible": True,
        "synthetic": False,
        "real_http": True,
        "candidate_revision": context["candidate_revision"],
        "release_validation_session_id": context["release_validation_session_id"],
        "candidate_artifact_sha256": context["candidate_artifact_sha256"],
        "served_root_sha256": context["served_root_sha256"],
        "candidate_url": context["candidate_url"],
        "page_url": context["candidate_url"],
        "report_path": context["report_path"],
        "report_sha256": context["report_sha256"],
        "release_url": origin + "/api/coverage/release",
        "mutation_url": origin + AUTH_MUTATION_PROBE_PATH,
        "auth_mode": context["auth_mode"],
        "user_header": context["user_header"],
        "identity_source": "candidate_gateway_authenticated_operator",
        "identity_propagated": bool(str(probe.get("authenticated_user") or "").strip()),
        "gateway_config_sha256": context["gateway_config_sha256"],
        "mutation_probe": {
            "status": "PASSED", "method": "POST",
            "endpoint": AUTH_MUTATION_PROBE_PATH,
            "probe_contract_observed": probe.get("mutation_probe") is True,
            "backend_identity_observed": bool(str(probe.get("authenticated_user") or "").strip()),
            "authenticated_user_present": bool(str(probe.get("authenticated_user") or "").strip()),
            "database_mutation": probe.get("database_mutation"),
            "authenticated_status_code": int(authenticated.get("status_code") or 0),
            "unauthenticated_status_code": int(negative_status or 0),
            "credentials_recorded": False,
        },
        "operator_identity": operator,
        "credentials_recorded": False,
        "exit_code": 0,
        "recorded_at": time.time(),
    }
    _atomic_json(paths["operator"], observation)
    _atomic_json(paths["auth"], auth)
    return observation, auth


def _submit(application, body, headers, remote_address):
    operator, denied = _require_operator(application, headers, remote_address, mutation=True)
    if denied:
        return denied
    try:
        context = _public_context(application, headers)
    except RuntimeError as exc:
        return 409, {"error": "validation_context_unavailable", "message": str(exc)}
    state = _load_json(context["_paths"]["state"], "air-gapped validation state")
    if state.get("submitted"):
        operator_path = context["_paths"]["operator"]
        auth_path = context["_paths"]["auth"]
        if os.path.isfile(operator_path) and os.path.isfile(auth_path):
            return 200, {
                "status": "PASSED", "already_submitted": True,
                "release_validation_session_id": context["release_validation_session_id"],
                "operator_evidence_sha256": _sha256_file(operator_path),
                "auth_evidence_sha256": _sha256_file(auth_path),
                "credentials_recorded": False,
            }
        return 409, {"error": "validation_state_incomplete"}
    if time.time() > float(state.get("expires_at") or 0):
        return 409, {"error": "validation_expired"}
    errors = _validate_submission(context, body, operator)
    if errors:
        return 400, {"error": "invalid_browser_observation", "violations": errors}
    negative_status = _negative_mutation_probe(context["candidate_url"])
    if negative_status not in (401, 403):
        return 409, {
            "error": "auth_negative_control_failed",
            "observed_status": negative_status,
        }
    _write_evidence(context, body, operator, negative_status)
    state.update({
        "submitted": True,
        "submitted_at": time.time(),
        "operator_evidence_sha256": _sha256_file(context["_paths"]["operator"]),
        "auth_evidence_sha256": _sha256_file(context["_paths"]["auth"]),
    })
    _atomic_json(context["_paths"]["state"], state)
    return 200, {
        "status": "PASSED",
        "release_validation_session_id": context["release_validation_session_id"],
        "operator_evidence_sha256": state["operator_evidence_sha256"],
        "auth_evidence_sha256": state["auth_evidence_sha256"],
        "report_path": context["report_path"],
        "report_sha256": context["report_sha256"],
        "performance_join_required": True,
        "credentials_recorded": False,
    }


def _public_payload(context, operator):
    value = dict(context)
    value.pop("_paths", None)
    value["operator_identity"] = operator
    return value


def _validation_html(application):
    runtime = getattr(application, "runtime", None)
    root = _real(getattr(runtime, "repo_root", ""))
    path = _real(os.path.join(root, HTML_RELATIVE_PATH))
    if not _inside(root, path) or os.path.islink(path) or not os.path.isfile(path):
        raise RuntimeError("air-gapped validation HTML is unavailable")
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()


def dispatch(application, method, path, query, body, headers, remote_address):
    """Return ``None`` when this transport does not own the request path."""
    del query
    if path not in (PAGE_PATH, CONTEXT_PATH, SUBMIT_PATH):
        return None
    if not enabled(application):
        return 404, {"error": "not_found", "message": "resource not found"}
    if method == "GET" and path in (PAGE_PATH, CONTEXT_PATH):
        operator, denied = _require_operator(application, headers, remote_address)
        if denied:
            return denied
        try:
            context = _public_context(application, headers)
            if path == CONTEXT_PATH:
                return 200, _public_payload(context, operator)
            return 200, {"__html__": _validation_html(application)}
        except RuntimeError as exc:
            return 409, {"error": "validation_context_unavailable", "message": str(exc)}
    if method == "POST" and path == SUBMIT_PATH:
        return _submit(application, body, headers, remote_address)
    return 405, {"error": "method_not_allowed", "message": "method not allowed"}
