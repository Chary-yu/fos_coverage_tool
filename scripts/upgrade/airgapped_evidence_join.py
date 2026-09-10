#!/usr/bin/env python3
"""Join air-gapped operator observation with exact-revision performance.

This adapter never upgrades an operator observation by itself. Release-
eligible browser evidence exists only when the observation, authenticated
mutation evidence, and release_performance_ab artifact all bind to the same
Candidate revision/publication attempt and the performance artifact retains
its independently hashed baseline/candidate source runs.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import tempfile


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path, label):
    path = os.path.realpath(os.path.abspath(path))
    if os.path.islink(path) or not os.path.isfile(path):
        raise RuntimeError("{} is missing or linked: {}".format(label, path))
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError("{} must be a JSON object".format(label))
    return path, value


def atomic_json(path, value):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    fd, temporary = tempfile.mkstemp(prefix=".airgapped-join-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def require_equal(label, *values):
    normalized = [str(value or "") for value in values]
    if not normalized[0] or any(value != normalized[0] for value in normalized[1:]):
        raise RuntimeError("{} identity mismatch".format(label))
    return normalized[0]


def candidate_source(performance):
    source = (performance.get("source_artifacts") or {}).get("candidate") or {}
    path = str(source.get("path") or "")
    expected_sha = str(source.get("sha256") or "")
    source_path, payload = load_json(path, "candidate performance source artifact")
    if sha256_file(source_path) != expected_sha:
        raise RuntimeError("candidate performance source SHA256 mismatch")
    if payload.get("status") != "PASSED" or \
            payload.get("evidence_class") != "release_performance_revision" or \
            payload.get("comparison_type") != "single_revision":
        raise RuntimeError("candidate performance source is not release_performance_revision")
    return source_path, expected_sha, payload


def validate_inputs(observation, auth, performance, expected):
    if observation.get("status") != "PASSED" or \
            observation.get("evidence_class") != "airgapped_real_candidate_browser_observation" or \
            observation.get("real_http") is not True or \
            observation.get("synthetic") is not False or \
            observation.get("release_eligible") is not False or \
            observation.get("requires_performance_join") is not True:
        raise RuntimeError("operator observation has invalid evidence semantics")
    if auth.get("status") != "PASSED" or \
            auth.get("evidence_class") != "real_candidate_authenticated_mutation" or \
            auth.get("release_eligible") is not True or \
            auth.get("real_http") is not True or auth.get("synthetic") is not False:
        raise RuntimeError("authenticated mutation evidence is invalid")
    if performance.get("status") != "PASSED" or \
            performance.get("evidence_class") != "release_performance_ab" or \
            performance.get("comparison_type") != "release_revision_ab" or \
            performance.get("synthetic") is True or performance.get("exit_code") != 0:
        raise RuntimeError("release performance A/B evidence is invalid")

    revision = require_equal(
        "candidate revision", expected["revision"],
        observation.get("candidate_revision"), auth.get("candidate_revision"),
        performance.get("candidate_commit"),
    )
    session_id = require_equal(
        "release validation session", expected["session_id"],
        observation.get("release_validation_session_id"),
        auth.get("release_validation_session_id"),
        performance.get("release_validation_session_id"),
    )
    artifact_sha = require_equal(
        "candidate artifact SHA256", expected["candidate_artifact_sha256"],
        observation.get("candidate_artifact_sha256"),
        auth.get("candidate_artifact_sha256"),
        performance.get("candidate_artifact_sha256"),
    )
    served_sha = require_equal(
        "served root SHA256", expected["served_root_sha256"],
        observation.get("served_root_sha256"), auth.get("served_root_sha256"),
        performance.get("served_root_sha256"),
    )
    candidate_url = require_equal(
        "Candidate browser URL", expected["candidate_url"],
        observation.get("candidate_url"), auth.get("candidate_url"),
    )
    if performance.get("baseline_commit") == revision:
        raise RuntimeError("performance baseline equals Candidate revision")
    virtual = performance.get("coverage_virtual_scroll_100k") or {}
    if virtual.get("status") != "PASSED":
        raise RuntimeError("release 100k virtual-scroll performance is not PASSED")
    return revision, session_id, artifact_sha, served_sha, candidate_url


def build_join(observation_path, auth_path, performance_path, workload_output,
               browser_output, expected):
    observation_path, observation = load_json(observation_path, "operator observation")
    auth_path, auth = load_json(auth_path, "authenticated mutation evidence")
    performance_path, performance = load_json(performance_path, "performance A/B evidence")
    revision, session_id, artifact_sha, served_sha, candidate_url = validate_inputs(
        observation, auth, performance, expected
    )
    candidate_source_path, candidate_source_sha, candidate_perf = candidate_source(performance)
    if candidate_perf.get("revision") != revision:
        raise RuntimeError("candidate performance source revision mismatch")
    if candidate_perf.get("workload_id") != performance.get("workload_id") or \
            candidate_perf.get("workload_hash") != performance.get("workload_hash"):
        raise RuntimeError("candidate performance source workload mismatch")
    virtual = dict(candidate_perf.get("coverage_virtual_scroll_100k") or {})
    if virtual.get("status") != "PASSED":
        raise RuntimeError("candidate performance source 100k workload is not PASSED")
    environment = candidate_perf.get("environment_identity") or {}
    if not isinstance(environment, dict) or environment.get("browser_name") != "chromium":
        raise RuntimeError("candidate performance source is not Chromium evidence")
    virtual["environment_identity"] = environment

    workload = {
        "schema_version": 1,
        "status": "PASSED",
        "evidence_class": "airgapped_browser_performance_join_workload",
        "candidate_revision": revision,
        "release_validation_session_id": session_id,
        "candidate_artifact_sha256": artifact_sha,
        "served_root_sha256": served_sha,
        "candidate_url": candidate_url,
        "operator_observation": {
            "path": observation_path,
            "sha256": sha256_file(observation_path),
            "evidence_class": observation.get("evidence_class"),
        },
        "authenticated_mutation": {
            "path": auth_path,
            "sha256": sha256_file(auth_path),
            "evidence_class": auth.get("evidence_class"),
        },
        "performance_ab": {
            "path": performance_path,
            "sha256": sha256_file(performance_path),
            "evidence_class": performance.get("evidence_class"),
            "baseline_commit": performance.get("baseline_commit"),
            "candidate_commit": performance.get("candidate_commit"),
        },
        "candidate_performance_source": {
            "path": candidate_source_path,
            "sha256": candidate_source_sha,
            "evidence_class": candidate_perf.get("evidence_class"),
        },
        "coverage_virtual_scroll_100k": virtual,
    }
    atomic_json(workload_output, workload)
    workload_path = os.path.realpath(os.path.abspath(workload_output))
    workload_sha = sha256_file(workload_path)

    browser = {
        "schema_version": 2,
        "status": "PASSED",
        "evidence_class": "real_http_chromium_browser",
        "release_eligible": True,
        "synthetic": False,
        "real_http": True,
        "candidate_revision": revision,
        "release_validation_session_id": session_id,
        "candidate_artifact_sha256": artifact_sha,
        "served_root_sha256": served_sha,
        "candidate_url": candidate_url,
        "page_url": candidate_url,
        "release_identity": observation.get("release_identity") or {},
        "browser_functional": observation.get("browser_functional") or {},
        "coverage_virtual_scroll_100k": virtual,
        "artifact_path": workload_path,
        "artifact_sha256": workload_sha,
        "report_artifact_path": workload_path,
        "report_artifact_sha256": workload_sha,
        "report_path": observation.get("report_path"),
        "report_sha256": observation.get("report_sha256"),
        "operator_observation_path": observation_path,
        "operator_observation_sha256": sha256_file(observation_path),
        "auth_evidence_path": auth_path,
        "auth_evidence_sha256": sha256_file(auth_path),
        "performance_evidence_path": performance_path,
        "performance_evidence_sha256": sha256_file(performance_path),
        "candidate_performance_source_path": candidate_source_path,
        "candidate_performance_source_sha256": candidate_source_sha,
        "join_semantics": "real operator Candidate observation + independently measured exact-revision Chromium performance",
        "exit_code": 0,
    }
    atomic_json(browser_output, browser)
    return {
        "status": "PASSED",
        "browser_evidence_path": os.path.realpath(os.path.abspath(browser_output)),
        "browser_evidence_sha256": sha256_file(browser_output),
        "workload_path": workload_path,
        "workload_sha256": workload_sha,
        "candidate_revision": revision,
        "release_validation_session_id": session_id,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-observation", required=True)
    parser.add_argument("--auth-evidence", required=True)
    parser.add_argument("--performance-evidence", required=True)
    parser.add_argument("--workload-output", required=True)
    parser.add_argument("--browser-output", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--release-validation-session-id", required=True)
    parser.add_argument("--candidate-artifact-sha256", required=True)
    parser.add_argument("--served-root-sha256", required=True)
    parser.add_argument("--candidate-url", required=True)
    args = parser.parse_args()
    result = build_join(
        args.operator_observation, args.auth_evidence, args.performance_evidence,
        args.workload_output, args.browser_output,
        {
            "revision": args.expected_revision,
            "session_id": args.release_validation_session_id,
            "candidate_artifact_sha256": args.candidate_artifact_sha256,
            "served_root_sha256": args.served_root_sha256,
            "candidate_url": args.candidate_url,
        },
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
