"""Audit the Gate A--F task manifest against Appendix B's task inventory."""

from __future__ import absolute_import

import hashlib
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.diagnostics.contract import with_contract


MANIFEST_PATH = os.path.join(ROOT, "docs", "gate_task_manifest.json")
PLAN_PATH = os.path.join(ROOT, "docs", "FOS_Coverage_Gate_A-F_详细开发与验证总方案_v1.2.md")
SKILLS = {
    "fos-coverage-maintainer",
    "fos-coverage-change-review",
    "fos-coverage-release-governance",
    "fos-coverage-runtime-reliability",
    "fos-coverage-performance-ui",
}
EXPECTED_TASKS = [
    "{}-{:02d}".format(gate, number)
    for gate, maximum in (("A", 10), ("B", 12), ("C", 12),
                          ("D", 19), ("E", 14), ("F", 13))
    for number in range(1, maximum + 1)
]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit(repo_root=ROOT):
    repo_root = os.path.abspath(repo_root)
    path = os.path.join(repo_root, "docs", "gate_task_manifest.json")
    plan = os.path.join(repo_root, "docs", "FOS_Coverage_Gate_A-F_详细开发与验证总方案_v1.2.md")
    violations = []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        return with_contract({
            "status": "FAILED", "evidence_class": "task_manifest_audit",
            "manifest_path": os.path.relpath(path, repo_root),
            "violations": ["cannot load task manifest: {}".format(exc)],
        })
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        tasks = []
        violations.append("tasks must be an array")
    observed_ids = [item.get("task_id") for item in tasks if isinstance(item, dict)]
    if len(observed_ids) != len(set(observed_ids)):
        violations.append("task IDs are duplicated")
    missing = sorted(set(EXPECTED_TASKS) - set(observed_ids))
    unexpected = sorted(set(observed_ids) - set(EXPECTED_TASKS))
    if missing:
        violations.append("missing task IDs: {}".format(", ".join(missing)))
    if unexpected:
        violations.append("unexpected task IDs: {}".format(", ".join(unexpected)))
    known = set(EXPECTED_TASKS)
    for item in tasks:
        if not isinstance(item, dict):
            violations.append("task entry must be an object")
            continue
        task_id = item.get("task_id") or "<missing>"
        root_owner = item.get("root_owner_skill")
        secondary = item.get("secondary_owner_skill")
        if root_owner not in SKILLS:
            violations.append("{} has invalid root_owner_skill".format(task_id))
        if secondary is not None and secondary not in SKILLS:
            violations.append("{} has invalid secondary_owner_skill".format(task_id))
        if not item.get("required_evidence_class"):
            violations.append("{} has no required_evidence_class".format(task_id))
        dependencies = item.get("upstream_gate_dependencies")
        if not isinstance(dependencies, list):
            violations.append("{} dependencies must be an array".format(task_id))
            dependencies = []
        for dependency in dependencies:
            if dependency not in known:
                violations.append("{} references unknown dependency {}".format(task_id, dependency))
            if dependency == task_id:
                violations.append("{} depends on itself".format(task_id))
    expected_hash = payload.get("plan_sha256") if isinstance(payload, dict) else ""
    actual_hash = _sha256(plan) if os.path.isfile(plan) else ""
    if not expected_hash or expected_hash != actual_hash:
        violations.append("task manifest plan_sha256 does not match v1.2 plan")
    return with_contract({
        "status": "PASSED" if not violations else "FAILED",
        "evidence_class": "task_manifest_audit",
        "manifest_path": os.path.relpath(path, repo_root),
        "plan_path": os.path.relpath(plan, repo_root),
        "plan_sha256": actual_hash,
        "expected_task_count": len(EXPECTED_TASKS),
        "observed_task_count": len(observed_ids),
        "missing_task_ids": missing,
        "unexpected_task_ids": unexpected,
        "violations": violations,
    })


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0 if result["status"] == "PASSED" else 1)
