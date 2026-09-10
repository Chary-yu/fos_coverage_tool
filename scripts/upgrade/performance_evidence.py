"""Python 3.6-compatible exact-revision performance evidence join.

The production host deliberately does not need Node or npm.  Chromium runs
are performed on the evidence host and produce one immutable JSON artifact per
revision.  This module validates those inputs and binds their measurements to
the current release-validation session only after the Candidate publication
has supplied its artifact and Served Root hashes.
"""

from __future__ import print_function

import hashlib
import json
import math
import os


REQUIRED_TIERS = ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k")
EPHEMERAL_ENVIRONMENT_KEYS = frozenset(
    ("ci_run", "run_id", "workflow_run_id", "started_at", "finished_at", "timestamp")
)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _comparable_environment(value):
    if not isinstance(value, dict) or not value:
        return None
    return _stable_json({
        key: item for key, item in value.items()
        if key not in EPHEMERAL_ENVIRONMENT_KEYS
    })


def _finite_number(value):
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _load_json(path, label):
    path = os.path.abspath(str(path))
    if not os.path.isfile(path) or os.path.islink(path):
        raise ValueError("{} artifact is missing: {}".format(label, path))
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("{} artifact is unreadable: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ValueError("{} artifact must be a JSON object".format(label))
    return path, value


def validate_revision_artifact(path, expected_revision, expected_workload_hash,
                               role):
    """Validate one independently measured Chromium revision artifact."""
    path, payload = _load_json(path, role)
    if payload.get("status") != "PASSED":
        raise ValueError("{} artifact status is not PASSED".format(role))
    if payload.get("evidence_class") != "release_performance_revision" or \
            payload.get("comparison_type") != "single_revision":
        raise ValueError(
            "{} artifact is not release_performance_revision".format(role)
        )
    if payload.get("revision") != str(expected_revision):
        raise ValueError("{} artifact revision does not match expected SHA".format(role))
    if payload.get("workload_hash") != str(expected_workload_hash):
        raise ValueError("{} artifact workload hash does not match".format(role))
    if payload.get("synthetic") is True:
        raise ValueError("synthetic {} performance evidence is forbidden".format(role))
    if not payload.get("workload_id"):
        raise ValueError("{} artifact workload identity is missing".format(role))
    if not _comparable_environment(payload.get("environment_identity")):
        raise ValueError("{} artifact environment identity is missing".format(role))
    if payload.get("exit_code") not in (None, 0):
        raise ValueError("{} artifact has a non-zero exit code".format(role))

    tiers = payload.get("tiers") or payload
    for tier_name in REQUIRED_TIERS:
        tier = tiers.get(tier_name) if isinstance(tiers, dict) else None
        if not isinstance(tier, dict) or tier.get("status") != "PASSED" or \
                not _finite_number(tier.get("measured_ms")):
            raise ValueError(
                "{} artifact tier {} is incomplete".format(role, tier_name)
            )
    virtual = payload.get("coverage_virtual_scroll_100k") or {}
    if virtual.get("status") != "PASSED" or \
            not _finite_number(virtual.get("elapsed_ms")):
        raise ValueError(
            "{} artifact 100k virtual-scroll workload is incomplete".format(role)
        )
    return {
        "path": path,
        "sha256": _sha256(path),
        "payload": payload,
        "tiers": tiers,
        "environment_key": _comparable_environment(
            payload.get("environment_identity")
        ),
    }


def _regression_percent(baseline, candidate):
    if float(baseline) == 0:
        return 0.0 if float(candidate) == 0 else float("inf")
    return ((float(candidate) - float(baseline)) / float(baseline)) * 100.0


def _rounded(value):
    return round(float(value), 3) if math.isfinite(float(value)) else None


def join_release_performance(baseline_path, candidate_path, baseline_commit,
                             candidate_commit, workload_hash, output_path,
                             release_validation_session_id="",
                             candidate_artifact_sha256="",
                             served_root_sha256="",
                             max_regression_percent=20.0):
    """Join two exact-revision artifacts into a release A/B artifact."""
    baseline = validate_revision_artifact(
        baseline_path, baseline_commit, workload_hash, "baseline"
    )
    candidate = validate_revision_artifact(
        candidate_path, candidate_commit, workload_hash, "candidate"
    )
    if str(baseline_commit) == str(candidate_commit):
        raise ValueError("baseline and candidate revisions must differ")
    if baseline["path"] == candidate["path"]:
        raise ValueError("baseline and candidate artifacts must be separate files")
    if baseline["environment_key"] != candidate["environment_key"]:
        raise ValueError("baseline and candidate environment identities differ")
    if baseline["payload"].get("workload_id") != candidate["payload"].get("workload_id"):
        raise ValueError("baseline and candidate workload IDs differ")
    budget = float(max_regression_percent)
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("max_regression_percent must be finite and non-negative")

    violations = []
    tiers = {}
    for tier_name in REQUIRED_TIERS:
        baseline_ms = float(baseline["tiers"][tier_name]["measured_ms"])
        candidate_ms = float(candidate["tiers"][tier_name]["measured_ms"])
        regression = _regression_percent(baseline_ms, candidate_ms)
        tier = {
            "status": "PASSED" if regression <= budget else "FAILED",
            "baseline_ms": _rounded(baseline_ms),
            "candidate_ms": _rounded(candidate_ms),
            "regression_percent": _rounded(regression),
        }
        if regression > budget:
            violations.append(
                "{} exceeded the {}% regression budget".format(
                    tier_name, budget
                )
            )
        tiers[tier_name] = tier

    baseline_virtual = baseline["payload"]["coverage_virtual_scroll_100k"]
    candidate_virtual = candidate["payload"]["coverage_virtual_scroll_100k"]
    baseline_elapsed = float(baseline_virtual["elapsed_ms"])
    candidate_elapsed = float(candidate_virtual["elapsed_ms"])
    virtual_regression = _regression_percent(baseline_elapsed, candidate_elapsed)
    if virtual_regression > budget:
        violations.append(
            "coverage_virtual_scroll_100k exceeded the {}% regression budget".format(
                budget
            )
        )

    session = str(release_validation_session_id or "").strip()
    candidate_artifact_sha256 = str(candidate_artifact_sha256 or "").strip().lower()
    served_root_sha256 = str(served_root_sha256 or "").strip().lower()
    binding_values = (session, candidate_artifact_sha256, served_root_sha256)
    if any(binding_values) and not all(binding_values):
        raise ValueError(
            "publication binding requires session, Candidate artifact, and Served Root hashes"
        )

    baseline_payload = baseline["payload"]
    candidate_payload = candidate["payload"]
    result = {
        "schema_version": 1,
        "status": "PASSED" if not violations else "FAILED",
        "evidence_class": "release_performance_ab",
        "comparison_type": "release_revision_ab",
        "synthetic": False,
        "release_eligible": not violations,
        "baseline_commit": str(baseline_commit),
        "candidate_commit": str(candidate_commit),
        "workload_id": candidate_payload.get("workload_id"),
        "workload_hash": str(workload_hash),
        "environment_identity": candidate_payload.get("environment_identity"),
        "release_validation_session_id": session,
        "candidate_artifact_sha256": candidate_artifact_sha256,
        "served_root_sha256": served_root_sha256,
        "exit_code": 0 if not violations else 1,
        "violations": violations,
        "source_artifacts": {
            "baseline": {
                "path": baseline["path"],
                "sha256": baseline["sha256"],
                "revision": str(baseline_commit),
                "evidence_class": "release_performance_revision",
            },
            "candidate": {
                "path": candidate["path"],
                "sha256": candidate["sha256"],
                "revision": str(candidate_commit),
                "evidence_class": "release_performance_revision",
            },
        },
    }
    result.update(tiers)
    result["baseline_ms"] = result["Tier_D_100k"]["baseline_ms"]
    result["candidate_ms"] = result["Tier_D_100k"]["candidate_ms"]
    result["coverage_virtual_scroll_100k"] = {
        "status": "PASSED" if virtual_regression <= budget else "FAILED",
        "baseline_elapsed_ms": _rounded(baseline_elapsed),
        "candidate_elapsed_ms": _rounded(candidate_elapsed),
        "regression_percent": _rounded(virtual_regression),
        "baseline": baseline_virtual,
        "candidate": candidate_virtual,
    }
    result["artifact_path"] = os.path.abspath(str(output_path))
    parent = os.path.dirname(result["artifact_path"])
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = result["artifact_path"] + ".tmp-{}".format(os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, result["artifact_path"])
    return result
