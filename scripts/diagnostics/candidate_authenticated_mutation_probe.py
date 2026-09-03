"""Exercise the external Candidate Gateway authentication-to-mutation chain.

This probe is intentionally operator-run.  It never stores credential/header
values in the evidence envelope.  The positive request is sent through the
external Candidate Gateway and the same mutation is first sent without the
credentials; a 401/403 negative control plus a successful positive request is
the minimum evidence consumed by the production upgrade controller.
"""

from __future__ import print_function

import argparse
import ipaddress
import json
import os
import platform
import socket
import sys
try:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
    from urllib.parse import urlparse
except ImportError:  # pragma: no cover - Python 2 compatibility for old tooling
    from urllib2 import HTTPError, URLError, Request, urlopen
    from urlparse import urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import is_valid_commit_sha
from app.time_utils import utc_iso


def _parse_headers(values):
    headers = {}
    for raw in values or []:
        name, separator, value = str(raw).partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError("--header must use Name=Value")
        headers[name] = value
    return headers


def _safe_headers(headers):
    # The keys are useful for auditing the route; the values are credentials
    # and must never be written to JSON, logs, or command evidence.
    return sorted(str(name) for name in (headers or {}).keys())


def _origin(value):
    parsed = urlparse(str(value or "").strip())
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if hostname in ("localhost", "127.0.0.1", "::1", "[::1]"):
        return None
    try:
        if ipaddress.ip_address(hostname.strip("[]")).is_loopback:
            return None
    except ValueError:
        pass
    return parsed.scheme.lower(), hostname, port


def _endpoint_errors(value, candidate_url, label):
    parsed = urlparse(str(value or "").strip())
    errors = []
    observed_origin = _origin(value)
    expected_origin = _origin(candidate_url)
    if observed_origin is None:
        errors.append("{} must be an external HTTP(S) URL".format(label))
        return errors
    if expected_origin is None or observed_origin != expected_origin:
        errors.append("{} must use the Candidate Gateway origin".format(label))
    if parsed.username or parsed.password:
        errors.append("{} must not embed credentials".format(label))
    if not str(parsed.path or "").lower().startswith("/api/"):
        errors.append("{} must point at a Candidate Gateway API path".format(label))
    return errors


def _request(url, method, headers, body=None, timeout=20):
    encoded = None
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=encoded, headers=dict(headers or {}), method=method)
    if encoded is not None:
        request.add_header("Content-Type", "application/json")
    try:
        response = urlopen(request, timeout=timeout)
        raw = response.read()
        status = int(getattr(response, "status", response.getcode()))
        payload = None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeError):
            pass
        return status, payload, ""
    except HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        payload = None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeError):
            pass
        return int(exc.code), payload, str(exc)
    except (URLError, OSError, ValueError) as exc:
        return 0, None, str(exc)


def _publication(payload):
    value = payload if isinstance(payload, dict) else {}
    if isinstance(value.get("publication"), dict):
        return value["publication"]
    if isinstance(value.get("data"), dict) and isinstance(
            value["data"].get("publication"), dict):
        return value["data"]["publication"]
    return {}


def _release(payload):
    value = payload if isinstance(payload, dict) else {}
    if isinstance(value.get("release"), dict):
        return value["release"]
    if isinstance(value.get("data"), dict) and isinstance(
            value["data"].get("release"), dict):
        return value["data"]["release"]
    return value


def _backend_identity_observed(payload):
    value = payload if isinstance(payload, dict) else {}
    for key in ("reviewed_by", "requested_by", "authenticated_user", "user"):
        if str(value.get(key) or "").strip():
            return True
    for nested_key in ("project", "scan", "rejection", "decision", "result"):
        nested = value.get(nested_key)
        if isinstance(nested, dict) and _backend_identity_observed(nested):
            return True
    return False


def run_probe(candidate_url, release_url, mutation_url, body, headers,
              expected_revision, session_id, artifact_sha, served_sha,
              auth_mode, user_header, identity_source, gateway_sha,
              timeout=20):
    started = utc_iso()
    errors = []
    if _origin(candidate_url) is None:
        errors.append(
            "candidate_url must be an external non-loopback HTTP(S) HTML URL"
        )
    else:
        candidate_path = str(urlparse(candidate_url).path or "").lower()
        if not candidate_path.endswith((".html", ".htm")):
            errors.append("candidate_url must identify a static HTML document")
    errors.extend(_endpoint_errors(release_url, candidate_url, "release_url"))
    errors.extend(_endpoint_errors(mutation_url, candidate_url, "mutation_url"))
    release_status, release_payload, release_error = _request(
        release_url, "GET", headers, timeout=timeout
    )
    release = _release(release_payload)
    if release_status != 200 or release.get("commit_sha") != expected_revision:
        errors.append("Candidate release identity did not match expected revision")
    publication = _publication(release_payload)
    for field, expected in (
            ("release_validation_session_id", session_id),
            ("candidate_artifact_sha256", artifact_sha),
            ("served_root_sha256", served_sha)):
        if expected and publication.get(field) != expected:
            errors.append("Candidate publication {} did not match attempt".format(field))
    if release_error and release_status == 0:
        errors.append("Candidate release endpoint request failed: {}".format(release_error))

    # Remove both the identity header and common credential carriers for the
    # negative control.  A proxy that accepts this request has no meaningful
    # authentication boundary and must not certify production readiness.
    unauthenticated_headers = {
        key: value for key, value in headers.items()
        if key.lower() not in (
            str(user_header or "X-Remote-User").lower(),
            "authorization", "cookie", "proxy-authorization",
        )
    }
    unauth_status, _unauth_payload, _unauth_error = _request(
        mutation_url, "POST", unauthenticated_headers, body=body, timeout=timeout
    )
    if unauth_status not in (401, 403):
        errors.append(
            "unauthenticated Candidate mutation control returned HTTP {}".format(
                unauth_status or "no response"
            )
        )

    authenticated_status, authenticated_payload, authenticated_error = _request(
        mutation_url, "POST", headers, body=body, timeout=timeout
    )
    if authenticated_status < 200 or authenticated_status >= 300:
        errors.append(
            "authenticated Candidate mutation returned HTTP {}".format(
                authenticated_status or "no response"
            )
        )
    if authenticated_error and authenticated_status == 0:
        errors.append(
            "authenticated Candidate mutation request failed: {}".format(
                authenticated_error
            )
        )
    identity_propagated = (
        unauth_status in (401, 403) and
        200 <= authenticated_status < 300
    )
    if not identity_propagated:
        errors.append("Candidate Gateway did not prove identity propagation")
    finished = utc_iso()
    return {
        "schema_version": 1,
        "status": "PASSED" if not errors else "FAILED",
        "evidence_class": "real_candidate_authenticated_mutation",
        "synthetic": False,
        "release_eligible": not errors,
        "real_http": True,
        "candidate_url": candidate_url,
        "release_url": release_url,
        "mutation_url": mutation_url,
        "candidate_revision": expected_revision,
        "release_validation_session_id": session_id,
        "candidate_artifact_sha256": artifact_sha,
        "served_root_sha256": served_sha,
        "auth_mode": str(auth_mode or "").strip().lower(),
        "user_header": str(user_header or "X-Remote-User").strip(),
        "identity_source": str(identity_source or "").strip(),
        "identity_propagated": identity_propagated,
        "gateway_config_sha256": str(gateway_sha or "").strip(),
        "request_headers": _safe_headers(headers),
        "mutation_probe": {
            "status": "PASSED" if identity_propagated and
            200 <= authenticated_status < 300 else "FAILED",
            "method": "POST",
            "mutation_url": mutation_url,
            "unauthenticated_status_code": unauth_status,
            "authenticated_status_code": authenticated_status,
            "backend_identity_observed": _backend_identity_observed(
                authenticated_payload
            ),
            "credentials_recorded": False,
        },
        "release_probe": {
            "status": "PASSED" if release_status == 200 and
            release.get("commit_sha") == expected_revision else "FAILED",
            "status_code": release_status,
            "publication": publication,
        },
        "started_at": started,
        "finished_at": finished,
        "host": socket.gethostname(),
        "environment": "external_candidate_gateway",
        "runtime": platform.platform(),
        "command": "external Candidate Gateway release + unauthenticated/authenticated mutation probe",
        "exit_code": 0 if not errors else 1,
        "violations": errors,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="prove external Candidate Gateway authentication reaches mutation API"
    )
    parser.add_argument("--candidate-url", required=True)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--mutation-url", required=True)
    parser.add_argument("--mutation-body-file", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--release-validation-session-id", required=True)
    parser.add_argument("--candidate-artifact-sha256", required=True)
    parser.add_argument("--served-root-sha256", required=True)
    parser.add_argument("--auth-mode", default="reverse_proxy")
    parser.add_argument("--user-header", default="X-Remote-User")
    parser.add_argument("--identity-source", required=True)
    parser.add_argument("--gateway-config-sha256", required=True)
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if not is_valid_commit_sha(args.expected_revision):
        parser.error("--expected-revision must be an exact commit SHA")
    for field in ("candidate_artifact_sha256", "served_root_sha256",
                  "gateway_config_sha256"):
        value = getattr(args, field)
        if len(value) != 64 or set(value.lower()) == {"0"}:
            parser.error("--{} must be a non-zero SHA256".format(
                field.replace("_", "-")
            ))
    with open(args.mutation_body_file, "r", encoding="utf-8") as stream:
        body = json.load(stream)
    headers = _parse_headers(args.header)
    evidence = run_probe(
        args.candidate_url, args.release_url, args.mutation_url, body, headers,
        args.expected_revision, args.release_validation_session_id,
        args.candidate_artifact_sha256, args.served_root_sha256,
        args.auth_mode, args.user_header, args.identity_source,
        args.gateway_config_sha256, timeout=args.timeout,
    )
    output = os.path.abspath(args.output)
    parent = os.path.dirname(output)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({
        "status": evidence["status"],
        "release_eligible": evidence["release_eligible"],
        "output": output,
    }, sort_keys=True))
    return 0 if evidence["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
