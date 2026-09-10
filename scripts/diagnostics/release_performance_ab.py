#!/usr/bin/env python3
"""Combine two exact-revision Chromium performance artifacts.

This module is the production-host implementation of the release performance
A/B join.  It intentionally performs no browser work and requires two
independently produced ``release_performance_revision`` JSON files.  Keeping
this combiner in Python avoids introducing a Node.js runtime dependency on the
CentOS 7 / Python 3.6 production host.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import math
import os
import tempfile


REQUIRED_TIERS = ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k")
EPHEMERAL_ENV_KEYS = frozenset((
    "ci_run", "run_id", "workflow_run_id", "started_at", "finished_at",
    "timestamp",
))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _real_regular_file(path, label):
    absolute = os.path.realpath(os.path.abspath(str(path)))
    if os.path.islink(os.path.abspath(str(path))) or not os.path.isfile(absolute):
        raise RuntimeError("{} must be a regular file: {}".format(label, absolute))
    return absolute


def _load_json(path, label):
    absolute = _real_regular_file(path, label)
    with open(absolute, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError("{} must be a JSON object".format(label))
    return absolute, value


def _atomic_json(path, value):
    absolute = os.path.abspath(str(path))
    directory = os.path.dirname(absolute)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    fd, temporary = tempfile.mkstemp(prefix=".release-perf-ab-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(temporary, absolute)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
    return absolute


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stable_environment(environment):
    if not isinstance(environment, dict):
        return None
    filtered = dict(
        (key, value) for key, value in environment.items()
        if key not in EPHEMERAL_ENV_KEYS
    )
    if not filtered:
        return None
    return json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _read_revision_artifact(path, expected_revision, expected_workload_hash, role):
    absolute, artifact = _load_json(path, "{} performance artifact".format(role))
    if artifact.get("status") != "PASSED":
        raise RuntimeError("{} artifact status is not PASSED".format(role))
    if artifact.get("evidence_class") != "release_performance_revision" or \
            artifact.get("comparison_type") != "single_revision":
        raise RuntimeError("{} artifact is not an independent release_performance_revision".format(role))
    if not expected_revision or artifact.get("revision") != expected_revision:
        raise RuntimeError("{} artifact revision mismatch".format(role))
    if not artifact.get("workload_id") or artifact.get("workload_hash") != expected_workload_hash:
        raise RuntimeError("{} artifact workload identity mismatch".format(role))
    if artifact.get("exit_code", 0) != 0:
        raise RuntimeError("{} artifact has a non-zero exit_code".format(role))
    if artifact.get("baseline_commit") or artifact.get("candidate_commit") or \
            artifact.get("comparison_type") == "synthetic_same_run" or \
            artifact.get("evidence_class") == "synthetic_dom_microbenchmark":
        raise RuntimeError("{} artifact contains same-run/synthetic A/B identity".format(role))
    environment_key = _stable_environment(artifact.get("environment_identity"))
    if not environment_key:
        raise RuntimeError("{} artifact has no environment_identity".format(role))
    tiers = artifact.get("tiers") or artifact
    for tier_name in REQUIRED_TIERS:
        tier = tiers.get(tier_name) if isinstance(tiers, dict) else None
        if not isinstance(tier, dict) or tier.get("status") != "PASSED" or \
                not _finite_number(tier.get("measured_ms")):
            raise RuntimeError("{} artifact tier {} lacks measured_ms/PASSED".format(role, tier_name))
    virtual = artifact.get("coverage_virtual_scroll_100k")
    if not isinstance(virtual, dict) or virtual.get("status") != "PASSED" or \
            not _finite_number(virtual.get("elapsed_ms")):
        raise RuntimeError("{} artifact lacks the 100k virtual-scroll workload".format(role))
    return {
        "artifact": artifact,
        "path": absolute,
        "sha256": sha256_file(absolute),
        "tiers": tiers,
        "environment_key": environment_key,
    }


def _regression_percent(baseline, candidate):
    if baseline == 0:
        return 0.0 if candidate == 0 else float("inf")
    return ((candidate - baseline) / baseline) * 100.0


def _rounded(value):
    return round(value, 3) if _finite_number(value) else None


def build_release_performance_ab(
        baseline_artifact, candidate_artifact, baseline_commit,
        candidate_commit, workload_hash, output_path,
        release_validation_session_id="", candidate_artifact_sha256="",
        served_root_sha256="", max_regression_percent=20.0):
    if not baseline_commit or not candidate_commit or baseline_commit == candidate_commit:
        raise RuntimeError("baseline and candidate commits must be different")
    try:
        budget = float(max_regression_percent)
    except (TypeError, ValueError):
        raise RuntimeError("max regression percent must be numeric")
    if not math.isfinite(budget) or budget < 0:
        raise RuntimeError("max regression percent must be finite and non-negative")
    bindings = (
        str(release_validation_session_id or ""),
        str(candidate_artifact_sha256 or ""),
        str(served_root_sha256 or ""),
    )
    populated = sum(1 for item in bindings if item)
    if populated not in (0, 3):
        raise RuntimeError("attempt publication binding requires all three identity fields")

    baseline = _read_revision_artifact(
        baseline_artifact, baseline_commit, workload_hash, "baseline"
    )
    candidate = _read_revision_artifact(
        candidate_artifact, candidate_commit, workload_hash, "candidate"
    )
    if baseline["path"] == candidate["path"]:
        raise RuntimeError("baseline and candidate artifacts must be separate files")
    if baseline["environment_key"] != candidate["environment_key"]:
        raise RuntimeError("baseline and candidate environment_identity do not match")
    if baseline["artifact"].get("workload_id") != candidate["artifact"].get("workload_id"):
        raise RuntimeError("baseline and candidate workload_id do not match")

    violations = []
    tiers = {}
    for tier_name in REQUIRED_TIERS:
        baseline_ms = baseline["tiers"][tier_name]["measured_ms"]
        candidate_ms = candidate["tiers"][tier_name]["measured_ms"]
        regression = _regression_percent(baseline_ms, candidate_ms)
        within_budget = math.isfinite(regression) and regression <= budget
        if not within_budget:
            violations.append("{} exceeded the {}% regression budget".format(tier_name, budget))
        tiers[tier_name] = {
            "status": "PASSED" if within_budget else "FAILED",
            "baseline_ms": _rounded(baseline_ms),
            "candidate_ms": _rounded(candidate_ms),
            "regression_percent": _rounded(regression),
            "regression_budget_percent": budget,
            "workload_id": baseline["artifact"].get("workload_id"),
            "baseline_revision": baseline_commit,
            "candidate_revision": candidate_commit,
        }

    baseline_virtual = baseline["artifact"]["coverage_virtual_scroll_100k"]
    candidate_virtual = candidate["artifact"]["coverage_virtual_scroll_100k"]
    virtual_regression = _regression_percent(
        baseline_virtual["elapsed_ms"], candidate_virtual["elapsed_ms"]
    )
    virtual_passed = math.isfinite(virtual_regression) and virtual_regression <= budget
    if not virtual_passed:
        violations.append("coverage_virtual_scroll_100k exceeded the {}% regression budget".format(budget))
    virtual = dict(candidate_virtual)
    virtual.update({
        "status": "PASSED" if virtual_passed else "FAILED",
        "baseline_elapsed_ms": _rounded(baseline_virtual["elapsed_ms"]),
        "candidate_elapsed_ms": _rounded(candidate_virtual["elapsed_ms"]),
        "regression_percent": _rounded(virtual_regression),
        "regression_budget_percent": budget,
        "baseline_revision": baseline_commit,
        "candidate_revision": candidate_commit,
    })

    result = {
        "status": "PASSED" if not violations else "FAILED",
        "evidence_class": "release_performance_ab",
        "comparison_type": "release_revision_ab",
        "workload_id": baseline["artifact"].get("workload_id"),
        "workload_hash": workload_hash,
        "baseline_commit": baseline_commit,
        "candidate_commit": candidate_commit,
        "release_validation_session_id": bindings[0],
        "candidate_artifact_sha256": bindings[1],
        "served_root_sha256": bindings[2],
        "environment_identity": baseline["artifact"].get("environment_identity"),
        "baseline_artifact": {
            "path": baseline["path"], "sha256": baseline["sha256"],
            "revision": baseline_commit,
        },
        "candidate_artifact": {
            "path": candidate["path"], "sha256": candidate["sha256"],
            "revision": candidate_commit,
        },
        "source_inputs_sha256": [baseline["sha256"], candidate["sha256"]],
        "regression_budget_percent": budget,
        "baseline_ms": tiers["Tier_B_10k"]["baseline_ms"],
        "candidate_ms": tiers["Tier_B_10k"]["candidate_ms"],
        "regression_percent": tiers["Tier_B_10k"]["regression_percent"],
        "tiers": tiers,
        "Tier_A_1k": tiers["Tier_A_1k"],
        "Tier_B_10k": tiers["Tier_B_10k"],
        "Tier_C_50k": tiers["Tier_C_50k"],
        "Tier_D_100k": tiers["Tier_D_100k"],
        "coverage_virtual_scroll_100k": virtual,
        "source_artifacts": {
            "baseline": {
                "path": baseline["path"], "sha256": baseline["sha256"],
                "revision": baseline_commit,
            },
            "candidate": {
                "path": candidate["path"], "sha256": candidate["sha256"],
                "revision": candidate_commit,
            },
        },
        "producer": "scripts/diagnostics/release_performance_ab.py",
        "exit_code": 0 if not violations else 1,
        "violations": violations,
    }
    _atomic_json(output_path, result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-artifact", required=True)
    parser.add_argument("--candidate-artifact", required=True)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--workload-hash", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--release-validation-session-id", default="")
    parser.add_argument("--candidate-artifact-sha256", default="")
    parser.add_argument("--served-root-sha256", default="")
    parser.add_argument("--max-regression-percent", type=float, default=20.0)
    args = parser.parse_args()
    try:
        result = build_release_performance_ab(
            args.baseline_artifact, args.candidate_artifact,
            args.baseline_commit, args.candidate_commit, args.workload_hash,
            args.output,
            release_validation_session_id=args.release_validation_session_id,
            candidate_artifact_sha256=args.candidate_artifact_sha256,
            served_root_sha256=args.served_root_sha256,
            max_regression_percent=args.max_regression_percent,
        )
    except Exception as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
