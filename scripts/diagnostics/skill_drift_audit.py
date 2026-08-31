"""Validate the release-window Skill Drift evidence contract.

The skills themselves are maintained outside this repository.  This checker
therefore consumes an operator-produced manifest and verifies that every
required routing/helper/test-selector/audit ownership claim is explicit.
"""

from __future__ import print_function

import argparse
import json
import os
import sys

# Support the documented repository-root invocation as well as module import.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.diagnostics.contract import with_contract
from app.time_utils import utc_iso


REQUIRED_SKILLS = (
    "fos-coverage-maintainer",
    "fos-coverage-change-review",
    "fos-coverage-release-governance",
    "fos-coverage-runtime-reliability",
    "fos-coverage-performance-ui",
)
REQUIRED_FIELDS = (
    "routing_current", "helpers_current", "test_selector_current",
    "audits_current", "root_owner", "capability_manifest_current",
    "http_vhost_current", "semantic_identity_current",
    "redaction_modes_current", "canonical_analysis_authority_current",
    "served_root_registry_sidecar_cursor_current",
    "validation_session_teardown_current",
)


def audit(payload, candidate_revision=""):
    payload = dict(payload or {})
    violations = []
    observed_revision = str(payload.get("candidate_revision") or "")
    if not observed_revision:
        violations.append("candidate_revision is missing")
    if candidate_revision and observed_revision != str(candidate_revision):
        violations.append("candidate_revision does not match the release")
    skills = payload.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        violations.append("skills object is missing")
    checks = {}
    for name in REQUIRED_SKILLS:
        item = skills.get(name)
        missing = []
        if not isinstance(item, dict):
            missing = list(REQUIRED_FIELDS)
        else:
            missing = [field for field in REQUIRED_FIELDS if not item.get(field)]
        checks[name] = {"present": isinstance(item, dict), "missing": missing}
        if missing:
            violations.append("{} missing: {}".format(name, ", ".join(missing)))
    capability_manifest = payload.get("capability_manifest")
    if capability_manifest is not None:
        if not isinstance(capability_manifest, dict):
            violations.append("capability_manifest must be an object")
        else:
            manifest_revision = str(
                capability_manifest.get("candidate_revision") or ""
            )
            if observed_revision and manifest_revision != observed_revision:
                violations.append(
                    "capability_manifest candidate_revision does not match"
                )
            manifest_skills = capability_manifest.get("skills")
            if not isinstance(manifest_skills, dict):
                violations.append("capability_manifest skills object is missing")
            else:
                for name in REQUIRED_SKILLS:
                    item = manifest_skills.get(name)
                    if not isinstance(item, dict) or not item.get("capabilities"):
                        violations.append(
                            "capability_manifest missing capabilities: {}".format(name)
                        )
            if capability_manifest.get("status") != "PASSED":
                violations.append("capability_manifest is not PASSED")
    return with_contract({
        "status": "PASSED" if not violations else "INCOMPLETE",
        "evidence_class": "skill_drift_audit",
        "synthetic": False,
        "candidate_revision": observed_revision,
        "checked_at": utc_iso(),
        "checks": checks,
        "violations": violations,
        "command_or_action": "python scripts/diagnostics/skill_drift_audit.py",
        "exit_code": 0 if not violations else 1,
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--candidate-revision", default="")
    parser.add_argument("--capability-manifest", default="")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        with open(args.input, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if args.capability_manifest:
            with open(args.capability_manifest, "r", encoding="utf-8") as stream:
                payload["capability_manifest"] = json.load(stream)
        result = audit(payload, args.candidate_revision)
    except (OSError, TypeError, ValueError) as exc:
        result = audit({}, args.candidate_revision)
        result["violations"].insert(
            0, "skill-drift input could not be read: {}".format(exc)
        )
        result["exit_code"] = 1
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
