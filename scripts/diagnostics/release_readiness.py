"""Evaluate the final Gate F release decision without optimistic defaults.

The v1.2 plan distinguishes a release decision from individual Gate status:
``READY``, ``READY_WITH_ACCEPTED_RISK`` and ``NOT_READY``.  This audit binds
that decision to one exact-SHA Gate Matrix, the per-task status artifact and an
explicit risk register.  Missing evidence, stale artifacts, or any unresolved
P0/P1 finding can only produce ``NOT_READY``.
"""

from __future__ import print_function

import argparse
import json
import os
import platform
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity
from app.time_utils import utc_iso
from scripts.diagnostics.contract import with_contract
from scripts.diagnostics.task_manifest_audit import EXPECTED_TASKS
from scripts.diagnostics.dod_status import EXPECTED_DOD_IDS


DECISIONS = ("READY", "READY_WITH_ACCEPTED_RISK", "NOT_READY")
SEVERITIES = {"P0", "P1", "P2", "INFO"}
RISK_STATUSES = {"OPEN", "APPROVED", "CLOSED"}


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return ""


def _load(path):
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _gate_blockers(matrix):
    blockers = []
    gates = matrix.get("gates") or {}
    for gate in "ABCDEF":
        if gate not in gates:
            blockers.append({"type": "gate", "gate": gate,
                             "status": "INCOMPLETE",
                             "reason": "gate result is missing"})
    for gate, payload in sorted(gates.items()):
        status = str((payload or {}).get("status") or "INCOMPLETE").upper()
        if status == "PASSED":
            continue
        blockers.append({
            "type": "gate",
            "gate": gate,
            "status": status,
            "missing_evidence": (payload or {}).get("missing_evidence") or [],
        })
    return blockers


def _task_blockers(task_status, candidate_revision):
    blockers = []
    if not isinstance(task_status, dict):
        return [{"type": "task_status", "reason": "task status artifact is missing"}]
    if str(task_status.get("candidate_revision") or "") != str(candidate_revision or ""):
        blockers.append({
            "type": "task_status",
            "reason": "candidate_revision does not match Gate Matrix",
        })
    if task_status.get("task_count") != len(EXPECTED_TASKS):
        blockers.append({
            "type": "task_status",
            "reason": "task_count must be {}".format(len(EXPECTED_TASKS)),
        })
    observed_ids = []
    for item in task_status.get("tasks") or []:
        if not isinstance(item, dict):
            blockers.append({"type": "task", "reason": "task entry is invalid"})
            continue
        observed_ids.append(item.get("task_id"))
        status = str(item.get("status") or "INCOMPLETE").upper()
        if status != "PASSED":
            blockers.append({
                "type": "task",
                "task_id": item.get("task_id", ""),
                "status": status,
                "blockers": item.get("blockers") or [],
            })
    missing = sorted(set(EXPECTED_TASKS) - set(observed_ids))
    unexpected = sorted(set(observed_ids) - set(EXPECTED_TASKS))
    if missing:
        blockers.append({"type": "task_status", "reason": "missing task IDs",
                         "task_ids": missing})
    if unexpected or len(observed_ids) != len(set(observed_ids)):
        blockers.append({"type": "task_status", "reason": "duplicate or unexpected task IDs",
                         "task_ids": unexpected})
    if task_status.get("status") != "PASSED":
        blockers.append({
            "type": "task_status",
            "status": str(task_status.get("status") or "INCOMPLETE").upper(),
        })
    return blockers


def _risk_findings(risk_register, candidate_revision):
    blockers = []
    accepted = []
    if not isinstance(risk_register, dict):
        return [{"type": "risk_register", "reason": "risk register is missing"}], accepted
    observed_revision = str(risk_register.get("candidate_revision") or "")
    if not observed_revision:
        blockers.append({"type": "risk_register", "reason": "candidate_revision is missing"})
    elif observed_revision != str(candidate_revision or ""):
        blockers.append({
            "type": "risk_register",
            "reason": "candidate_revision does not match Gate Matrix",
        })
    risks = risk_register.get("risks")
    if not isinstance(risks, list):
        return blockers + [{"type": "risk_register", "reason": "risks must be an array"}], accepted
    for index, risk in enumerate(risks):
        if not isinstance(risk, dict):
            blockers.append({"type": "risk", "index": index, "reason": "risk must be an object"})
            continue
        risk_id = str(risk.get("id") or "risk-{}".format(index))
        severity = str(risk.get("severity") or "").upper()
        status = str(risk.get("status") or "OPEN").upper()
        if severity not in SEVERITIES:
            blockers.append({"type": "risk", "id": risk_id, "reason": "unknown severity"})
            continue
        if status not in RISK_STATUSES:
            blockers.append({"type": "risk", "id": risk_id, "reason": "unknown status"})
            continue
        if severity in ("P0", "P1"):
            if status != "CLOSED":
                blockers.append({
                    "type": "risk", "id": risk_id, "severity": severity,
                    "status": status,
                    "reason": "unresolved P0/P1 cannot be accepted",
                })
            elif not risk.get("evidence_ref") or not risk.get("closed_at"):
                blockers.append({
                    "type": "risk", "id": risk_id, "severity": severity,
                    "reason": "closed P0/P1 needs closed_at and evidence_ref",
                })
        elif status == "OPEN":
            blockers.append({
                "type": "risk", "id": risk_id, "severity": severity,
                "reason": "open P2/Info risk has no approved disposition",
            })
        elif status == "APPROVED":
            required = ("owner", "approved_by", "approved_at", "evidence_ref")
            missing = [name for name in required if not risk.get(name)]
            if missing:
                blockers.append({
                    "type": "risk", "id": risk_id, "severity": severity,
                    "reason": "approved risk is missing: {}".format(", ".join(missing)),
                })
            else:
                accepted.append(dict(risk, id=risk_id, severity=severity, status=status))
        # CLOSED P2/Info findings are resolved and do not affect the decision.
    return blockers, accepted


def _dod_blockers(dod_status, candidate_revision):
    blockers = []
    if not isinstance(dod_status, dict):
        return [{"type": "dod_status", "reason": "DoD status artifact is missing"}]
    if str(dod_status.get("candidate_revision") or "") != str(candidate_revision or ""):
        blockers.append({
            "type": "dod_status",
            "reason": "candidate_revision does not match Gate Matrix",
        })
    if str(dod_status.get("current_revision") or "") != str(candidate_revision or ""):
        blockers.append({
            "type": "dod_status",
            "reason": "current_revision does not match Gate Matrix",
        })
    if dod_status.get("dod_count") != len(EXPECTED_DOD_IDS):
        blockers.append({
            "type": "dod_status",
            "reason": "dod_count must be {}".format(len(EXPECTED_DOD_IDS)),
        })
    observed_ids = []
    for item in dod_status.get("items") or []:
        if not isinstance(item, dict):
            blockers.append({"type": "dod", "reason": "DoD entry is invalid"})
            continue
        dod_id = item.get("dod_id")
        observed_ids.append(dod_id)
        status = str(item.get("status") or "INCOMPLETE").upper()
        if status != "PASSED":
            blockers.append({
                "type": "dod", "dod_id": dod_id, "status": status,
                "blockers": item.get("blockers") or [],
            })
    missing = sorted(set(EXPECTED_DOD_IDS) - set(observed_ids))
    unexpected = sorted(set(observed_ids) - set(EXPECTED_DOD_IDS))
    if missing:
        blockers.append({"type": "dod_status", "reason": "missing DoD IDs",
                         "dod_ids": missing})
    if unexpected or len(observed_ids) != len(set(observed_ids)):
        blockers.append({
            "type": "dod_status",
            "reason": "duplicate or unexpected DoD IDs",
            "dod_ids": unexpected,
        })
    if observed_ids != EXPECTED_DOD_IDS:
        blockers.append({
            "type": "dod_status",
            "reason": "DoD IDs are not in canonical order",
        })
    if str(dod_status.get("status") or "INCOMPLETE").upper() != "PASSED":
        blockers.append({
            "type": "dod_status",
            "status": str(dod_status.get("status") or "INCOMPLETE").upper(),
        })
    return blockers


def build(repo_root=ROOT, matrix=None, task_status=None, risk_register=None,
          dod_status=None, matrix_path="", task_status_path="",
          dod_status_path="", risk_register_path=""):
    repo_root = os.path.abspath(repo_root)
    current_revision = _revision(repo_root)
    matrix = matrix if isinstance(matrix, dict) else {}
    candidate_revision = str(matrix.get("candidate_revision") or "")
    blockers = []
    if not candidate_revision:
        blockers.append({"type": "provenance", "reason": "Gate Matrix candidate_revision is missing"})
    if current_revision and candidate_revision and candidate_revision != current_revision:
        blockers.append({"type": "provenance", "reason": "Gate Matrix candidate_revision does not match HEAD"})
    identity = matrix.get("release_identity") or {}
    if not isinstance(identity, dict) or identity.get("commit_sha") != candidate_revision:
        blockers.append({"type": "provenance", "reason": "release identity revision does not match Gate Matrix"})

    blockers.extend(_gate_blockers(matrix))
    blockers.extend(_task_blockers(task_status, candidate_revision))
    blockers.extend(_dod_blockers(dod_status, candidate_revision))
    risk_blockers, accepted_risks = _risk_findings(risk_register, candidate_revision)
    blockers.extend(risk_blockers)

    if blockers:
        decision = "NOT_READY"
    elif accepted_risks:
        decision = "READY_WITH_ACCEPTED_RISK"
    else:
        decision = "READY"

    return with_contract({
        "status": decision,
        "decision": decision,
        "evidence_class": "release_readiness_audit",
        "synthetic": False,
        "candidate_revision": candidate_revision,
        "current_revision": current_revision,
        "release_identity": identity or generate_release_identity(repo_root=repo_root),
        "host_identity": {"hostname": platform.node(), "platform": platform.platform()},
        "evaluated_at": utc_iso(),
        "matrix_path": os.path.abspath(matrix_path) if matrix_path else "",
        "task_status_path": os.path.abspath(task_status_path) if task_status_path else "",
        "dod_status_path": os.path.abspath(dod_status_path) if dod_status_path else "",
        "risk_register_path": os.path.abspath(risk_register_path) if risk_register_path else "",
        "accepted_risks": accepted_risks,
        "blockers": blockers,
        "gate_statuses": {
            gate: str((payload or {}).get("status") or "INCOMPLETE").upper()
            for gate, payload in sorted((matrix.get("gates") or {}).items())
        },
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--task-status", required=True)
    parser.add_argument("--dod-status", required=True)
    parser.add_argument("--risk-register", required=True)
    parser.add_argument("--output", default=".artifacts/vnext/release-readiness.json")
    parser.add_argument("--allow-not-ready", action="store_true")
    args = parser.parse_args(argv)
    repo_root = os.path.abspath(args.repo_root)
    matrix_path = os.path.abspath(args.matrix)
    task_path = os.path.abspath(args.task_status)
    dod_path = os.path.abspath(args.dod_status)
    risk_path = os.path.abspath(args.risk_register)
    try:
        result = build(
            repo_root,
            matrix=_load(matrix_path),
            task_status=_load(task_path),
            dod_status=_load(dod_path),
            risk_register=_load(risk_path),
            matrix_path=matrix_path,
            task_status_path=task_path,
            dod_status_path=dod_path,
            risk_register_path=risk_path,
        )
    except (OSError, ValueError, TypeError) as exc:
        result = with_contract({
            "status": "NOT_READY",
            "decision": "NOT_READY",
            "evidence_class": "release_readiness_audit",
            "synthetic": False,
            "candidate_revision": _revision(repo_root),
            "blockers": [{"type": "input", "reason": str(exc)}],
        })
    output = args.output if os.path.isabs(args.output) else os.path.join(repo_root, args.output)
    directory = os.path.dirname(output)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["decision"] == "READY":
        return 0
    if result["decision"] == "READY_WITH_ACCEPTED_RISK":
        return 0
    return 0 if args.allow_not_ready else 1


if __name__ == "__main__":
    sys.exit(main())
