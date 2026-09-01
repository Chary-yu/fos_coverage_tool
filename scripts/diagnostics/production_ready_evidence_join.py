"""Join Production READY evidence on one exact Candidate publication.

Individual CI jobs prove different properties.  A green job result is not
enough: the final gate must establish that the trusted build, live browser,
and performance evidence all refer to the same commit, artifact, Served Root,
and validation attempt.  This module is intentionally fail-closed and uses
the browser's observed publication object rather than its expected CLI flags.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import sys


_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
IDENTITY_FIELDS = (
    "commit_sha", "candidate_artifact_sha256", "served_root_sha256",
    "release_validation_session_id",
)


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("evidence is unreadable: {}".format(exc))
    if not isinstance(value, dict):
        raise ValueError("evidence must be a JSON object")
    return value


def _require_success(payload, role, require_release_eligible=True):
    if payload.get("status") != "PASSED":
        raise ValueError("{} evidence status is not PASSED".format(role))
    if payload.get("synthetic") is not False:
        raise ValueError("{} evidence must set synthetic=false".format(role))
    if require_release_eligible and payload.get("release_eligible") is not True:
        raise ValueError("{} evidence must set release_eligible=true".format(role))


def _require_commit(value, label):
    value = str(value or "").strip().lower()
    if not _COMMIT_SHA.fullmatch(value) or value == "0" * 40:
        raise ValueError("{} must be an exact non-zero commit SHA".format(label))
    return value


def _require_sha256(value, label):
    value = str(value or "").strip().lower()
    if not _SHA256.fullmatch(value) or value == "0" * 64:
        raise ValueError("{} must be an exact non-zero SHA256".format(label))
    return value


def _require_session(value, label):
    value = str(value or "").strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("{} must be a non-empty validation session ID".format(label))
    return value


def browser_identity(payload):
    """Return identity observed by the real browser's HTTP /release call."""
    _require_success(payload, "real browser")
    observed = payload.get("observed_publication")
    if not isinstance(observed, dict):
        raise ValueError("real browser evidence lacks observed publication identity")
    identity = {
        "commit_sha": _require_commit(
            observed.get("commit_sha"),
            "real browser observed commit_sha",
        ),
        "candidate_artifact_sha256": _require_sha256(
            observed.get("candidate_artifact_sha256"),
            "real browser observed candidate_artifact_sha256",
        ),
        "served_root_sha256": _require_sha256(
            observed.get("served_root_sha256"),
            "real browser observed served_root_sha256",
        ),
        "release_validation_session_id": _require_session(
            observed.get("release_validation_session_id"),
            "real browser observed release_validation_session_id",
        ),
    }
    if str(payload.get("candidate_revision") or "").lower() != identity["commit_sha"]:
        raise ValueError("real browser candidate_revision does not match observed commit")
    return identity


def performance_identity(payload):
    """Return identity from the release-eligible performance source JSON."""
    _require_success(payload, "cross-layer performance")
    candidate_commit = payload.get("candidate_commit") or payload.get(
        "candidate_revision"
    ) or payload.get("commit_sha")
    return {
        "commit_sha": _require_commit(
            candidate_commit, "cross-layer performance candidate commit"
        ),
        "candidate_artifact_sha256": _require_sha256(
            payload.get("candidate_artifact_sha256"),
            "cross-layer performance candidate_artifact_sha256",
        ),
        "served_root_sha256": _require_sha256(
            payload.get("served_root_sha256"),
            "cross-layer performance served_root_sha256",
        ),
        "release_validation_session_id": _require_session(
            payload.get("release_validation_session_id"),
            "cross-layer performance release_validation_session_id",
        ),
    }


def backup_identity(payload):
    """Return the source revision proven by the production DB rehearsal.

    The backup rehearsal does not serve the Candidate and therefore cannot
    truthfully observe a publication/session hash.  Its source-side contract
    is the exact Candidate commit; the publication fields are joined by the
    browser and performance lanes below.
    """
    _require_success(payload, "production backup", require_release_eligible=False)
    return {
        "commit_sha": _require_commit(
            payload.get("candidate_revision"),
            "production backup candidate_revision",
        ),
    }


def candidate_build_identity(commit_sha, artifact_sha256):
    return {
        "commit_sha": _require_commit(commit_sha, "trusted Candidate build commit_sha"),
        "candidate_artifact_sha256": _require_sha256(
            artifact_sha256, "trusted Candidate build candidate_artifact_sha256"
        ),
    }


def join_identities(candidate_build, browser, performance, backup):
    """Fail unless all available lanes identify one exact publication."""
    errors = []
    for field in ("commit_sha", "candidate_artifact_sha256"):
        if candidate_build.get(field) != browser.get(field):
            errors.append("trusted Candidate build and browser {} mismatch".format(field))
        if candidate_build.get(field) != performance.get(field):
            errors.append(
                "trusted Candidate build and performance {} mismatch".format(field)
            )
    if browser != performance:
        for field in IDENTITY_FIELDS:
            if browser.get(field) != performance.get(field):
                errors.append("browser and performance {} mismatch".format(field))
    if backup.get("commit_sha") != candidate_build.get("commit_sha"):
        errors.append("production backup and Candidate build commit_sha mismatch")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "status": "PASSED",
        "evidence_class": "production-ready-identity-join",
        "synthetic": False,
        "release_eligible": True,
        "identity": {
            "commit_sha": browser["commit_sha"],
            "candidate_artifact_sha256": browser["candidate_artifact_sha256"],
            "served_root_sha256": browser["served_root_sha256"],
            "release_validation_session_id": browser[
                "release_validation_session_id"
            ],
        },
        "lanes": {
            "candidate_build": dict(candidate_build),
            "real_browser_candidate": dict(browser),
            "cross_layer_performance": dict(performance),
            "verified_production_mariadb55": dict(backup),
        },
        "checks": {
            "candidate_build_artifact_matches_browser": True,
            "candidate_build_artifact_matches_performance": True,
            "browser_matches_performance_publication": True,
            "backup_matches_candidate_commit": True,
        },
    }


def _emit(kind, path):
    payload = _load(path)
    if kind == "browser":
        identity = browser_identity(payload)
    elif kind == "performance":
        identity = performance_identity(payload)
    elif kind == "backup":
        identity = backup_identity(payload)
    else:
        raise ValueError("unsupported evidence kind: {}".format(kind))
    for key, value in sorted(identity.items()):
        output_key = "candidate_commit_sha" if key == "commit_sha" else key
        print("{}={}".format(output_key, value))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="production_ready_evidence_join.py")
    parser.add_argument("--kind", choices=("browser", "performance", "backup"))
    parser.add_argument("--path", help="source evidence JSON for --kind")
    parser.add_argument("--github-output", action="store_true")
    parser.add_argument("--candidate-build-commit", default="")
    parser.add_argument("--candidate-build-artifact", default="")
    parser.add_argument("--browser-commit", default="")
    parser.add_argument("--browser-artifact", default="")
    parser.add_argument("--browser-served-root", default="")
    parser.add_argument("--browser-session", default="")
    parser.add_argument("--performance-commit", default="")
    parser.add_argument("--performance-artifact", default="")
    parser.add_argument("--performance-served-root", default="")
    parser.add_argument("--performance-session", default="")
    parser.add_argument("--backup-commit", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    try:
        if args.github_output:
            if not args.kind or not args.path:
                raise ValueError("--kind and --path are required with --github-output")
            _emit(args.kind, args.path)
            return 0
        result = join_identities(
            candidate_build_identity(
                args.candidate_build_commit, args.candidate_build_artifact
            ),
            {
                "commit_sha": _require_commit(args.browser_commit, "browser commit_sha"),
                "candidate_artifact_sha256": _require_sha256(
                    args.browser_artifact, "browser candidate_artifact_sha256"
                ),
                "served_root_sha256": _require_sha256(
                    args.browser_served_root, "browser served_root_sha256"
                ),
                "release_validation_session_id": _require_session(
                    args.browser_session, "browser release_validation_session_id"
                ),
            },
            {
                "commit_sha": _require_commit(
                    args.performance_commit, "performance commit_sha"
                ),
                "candidate_artifact_sha256": _require_sha256(
                    args.performance_artifact,
                    "performance candidate_artifact_sha256",
                ),
                "served_root_sha256": _require_sha256(
                    args.performance_served_root, "performance served_root_sha256"
                ),
                "release_validation_session_id": _require_session(
                    args.performance_session,
                    "performance release_validation_session_id",
                ),
            },
            {
                "commit_sha": _require_commit(
                    args.backup_commit, "backup candidate_revision"
                ),
            },
        )
        if args.output:
            output = os.path.abspath(args.output)
            directory = os.path.dirname(output)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            with open(output, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
