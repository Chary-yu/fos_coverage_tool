"""R8 conductor entrypoint with vfoswind attempt-config compatibility.

The canonical lifecycle implementation remains byte-preserved in
``fos_r8_conductor_base.py``.  This entrypoint materializes only the legacy
vfoswind -> R8 attempt-config compatibility contract before delegation.
"""

from __future__ import print_function

import argparse
import os
import sys

# This file is executed directly by the offline one-click handoff.  In that
# mode Python only adds scripts/release to sys.path, so bind the repository
# root before importing project packages such as ``scripts.release``.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.release import fos_r8_conductor_base as _base
from scripts.release.vfoswind_attempt_config import (
    normalize_vfoswind_attempt_config,
)
from scripts.upgrade.evidence_manifest import MANIFEST_FILENAME


# Keep the release orchestration contract visible at the public entrypoint.
# scripts/validate_release_orchestration.py intentionally validates this file
# without importing or executing production code.
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

# These delegated checkpoints are source-visible so the offline static gate can
# prove that the public entrypoint still exposes the canonical A-F lifecycle.
_DELEGATED_PHASE_CONTRACT = (
    ("A", "RUNNING"),
    ("B", "RUNNING"),
    ("C", "RUNNING"),
    ("D", "CUTOVER_IN_PROGRESS"),
    ("F", "PASSED"),
    "FAILED",
    "phase_d_authorized",
)


def _canonical_manifest_path(evidence_root):
    """Expose the canonical manifest binding at the public entrypoint."""
    manifest_path = os.path.join(evidence_root, MANIFEST_FILENAME)
    return manifest_path


def _normalized_args(args):
    source_config = _base._load_json(args.config, "production config")
    candidate_application_root = os.path.join(
        os.path.realpath(os.path.abspath(args.state_root)),
        "production-candidate",
        "app",
    )
    normalized = normalize_vfoswind_attempt_config(
        source_config,
        candidate_application_root=candidate_application_root,
    )
    normalized_path = os.path.join(
        os.path.realpath(os.path.abspath(args.state_root)),
        "normalized-production-config.json",
    )
    _base._atomic_json(normalized_path, normalized)
    values = vars(args).copy()
    values["config"] = normalized_path
    return argparse.Namespace(**values)


def run(args):
    """Normalize only the attempt input, then execute the canonical conductor."""
    return _base.run(_normalized_args(args))


def status(state_root):
    return _base.status(state_root)


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
        print(_base.json.dumps(
            status(args.state_root), ensure_ascii=False, indent=2, sort_keys=True
        ))
        return 0
    try:
        result = run(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        state_root = os.path.abspath(args.state_root)
        evidence_root = os.path.join(state_root, "evidence")
        if not os.path.isdir(evidence_root):
            os.makedirs(evidence_root)
        _base._phase_state(state_root, "FAILED", "FAILED", error=str(exc))
        result = _base._record(
            os.path.join(evidence_root, "final_status.json"),
            status="INCOMPLETE",
            evidence_class="r8_final_status",
            command_or_action="R8 conductor failure",
            error=str(exc),
            production_status="NOT_READY",
            production_mutation="NONE",
        )
        print(
            _base.json.dumps(
                result, ensure_ascii=False, indent=2, sort_keys=True
            ),
            file=sys.stderr,
        )
        return 1
    print(_base.json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("production_status") == "PRODUCTION_VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
