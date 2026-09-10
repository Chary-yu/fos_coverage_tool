"""Deployment-layout state machine for Flat Root to immutable CURRENT.

The normal upgrade path may inspect a Flat deployment but must not create a
pointer as a side effect of preflight.  The explicit cutover helper is the
only operation in this module that can bootstrap the immutable baseline.
"""

from __future__ import print_function

import json
import hashlib
import os
import shutil
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_publication import current_served_root_binding
from scripts.release.prepare_legacy_flat_adoption import (
    _bind_release_assets, _source_total_size, _source_tree_sha256,
    _validate_flat_root, _validate_release_identity,
    flat_source_binding,
    prepare_legacy_flat_adoption,
    verify_flat_source_binding,
)
from scripts.release.bootstrap_previous_release import bootstrap as bootstrap_baseline


FLAT = "FLAT"
IMMUTABLE_CURRENT = "IMMUTABLE_CURRENT"
UNKNOWN = "UNKNOWN"


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def _literal(path):
    return os.path.normpath(os.path.abspath(str(path)))


def _validate_identity(identity_path, expected_commit_sha):
    # Reuse the adoption tool's complete identity contract.  Checking only a
    # commit field would allow a partial/handwritten identity to pass
    # preflight and fail later after traffic has already been frozen.
    try:
        return _validate_release_identity(identity_path, expected_commit_sha)
    except ValueError:
        raise
    except (OSError, TypeError) as exc:
        raise ValueError("flat release identity is invalid: {}".format(exc))


def classify_deployment(publish_root, flat_served_root=""):
    """Classify without changing either the publication root or Flat root."""
    publish_root = _real(publish_root)
    current_path = os.path.join(publish_root, "CURRENT")
    if os.path.lexists(current_path):
        # Do not instantiate ImmutableReleasePublisher here: its constructor
        # creates ``releases/``.  Layout classification is a read-only
        # preflight and must not create publication state.
        binding = current_served_root_binding(publish_root)
        checked = {
            "status": "PASSED",
            "commit_sha": binding.get("previous_release_commit_sha"),
            "served_root": os.path.join(binding.get("realpath") or "", "reports"),
            "release_validation_session_id": binding.get(
                "release_validation_session_id"
            ),
            "violations": [],
        }
        return {
            "status": "PASSED", "deployment_layout": IMMUTABLE_CURRENT,
            "publish_root": publish_root, "current_path": _literal(current_path),
            "current": checked, "current_binding": binding,
            "bootstrap_required": False,
        }
    if flat_served_root and os.path.isdir(_real(flat_served_root)):
        return {
            "status": "PASSED", "deployment_layout": FLAT,
            "publish_root": publish_root,
            "flat_served_root": _real(flat_served_root),
            "current_path": _literal(current_path),
            "bootstrap_required": True,
        }
    return {
        "status": "FAILED", "deployment_layout": UNKNOWN,
        "publish_root": publish_root, "current_path": _literal(current_path),
        "bootstrap_required": False,
        "reason": "NO_CURRENT_OR_FLAT_ROOT",
    }


def current_flat_source_binding(flat_served_root, release_identity_path,
                                expected_commit_sha):
    """Return a deterministic, read-only identity for a legacy Flat root.

    Immutable CURRENT includes generated publication-control metadata, so its
    full tree digest is intentionally not reproducible across two independent
    bootstrap attempts.  A Flat-to-immutable upgrade instead binds the
    Candidate to the authoritative legacy source bytes plus the exact baseline
    release identity.  The same binding is recomputed immediately before the
    real bootstrap/cutover.
    """
    # Retain the strict compatibility API used by existing diagnostics, while
    # returning the complete binding used by Candidate provenance and the
    # cutover recheck.  The HTML/static-asset contract is still enforced here.
    _validate_flat_root(flat_served_root)
    return flat_source_binding(
        flat_served_root, release_identity_path, expected_commit_sha
    )


def plan_flat_current_adoption(publish_root, flat_served_root,
                               release_identity_path, expected_commit_sha):
    """Validate the inputs for adoption while leaving the layout untouched."""
    classification = classify_deployment(publish_root, flat_served_root)
    if classification.get("deployment_layout") == IMMUTABLE_CURRENT:
        current = classification.get("current_binding") or {}
        if str(current.get("previous_release_commit_sha") or "").lower() != \
                str(expected_commit_sha or "").lower():
            raise ValueError("CURRENT baseline SHA does not match Flat identity")
        return dict(classification, adoption_action="NOOP_CURRENT_ALREADY_EXISTS")
    if classification.get("deployment_layout") != FLAT:
        raise ValueError("Flat adoption requires a real Flat Served Root")
    identity = _validate_identity(release_identity_path, expected_commit_sha)
    source_binding = flat_source_binding(
        classification["flat_served_root"], release_identity_path,
        expected_commit_sha, allow_incomplete=True,
    )
    # Keep preflight read-only, but report malformed/partial legacy roots as
    # INCOMPLETE instead of allowing a later staging step to become the first
    # place that discovers the missing HTML/static-asset contract.
    try:
        _validate_flat_root(classification["flat_served_root"])
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        source_binding = dict(source_binding)
        source_binding["binding_status"] = "INCOMPLETE"
        violations = list(source_binding.get("binding_violations") or [])
        if str(exc) not in violations:
            violations.append(str(exc))
        source_binding["binding_violations"] = violations
    return {
        "status": "PASSED" if source_binding.get("binding_status") == "PASSED"
        else "INCOMPLETE", "deployment_layout": FLAT,
        "adoption_action": "BOOTSTRAP_IMMUTABLE_BASELINE",
        "publish_root": classification["publish_root"],
        "flat_served_root": classification["flat_served_root"],
        "release_identity_path": _real(release_identity_path),
        "expected_commit_sha": str(expected_commit_sha).lower(),
        "release_identity_sha256": hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
        "flat_source_binding": source_binding,
        "current_path": classification["current_path"],
        "switch_performed": False,
    }


def bootstrap_flat_current(publish_root, flat_served_root,
                           release_identity_path, expected_commit_sha,
                           session_id, switch=False, api_contract_version="",
                           application_root="", expected_source_binding=None):
    """Adopt a Flat root only when the caller explicitly authorizes switching."""
    try:
        plan = plan_flat_current_adoption(
            publish_root, flat_served_root, release_identity_path,
            expected_commit_sha
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if expected_source_binding is None:
            raise
        raise ValueError(
            "legacy Flat Root source binding changed: {}".format(exc)
        )
    if plan.get("adoption_action") == "NOOP_CURRENT_ALREADY_EXISTS":
        return dict(plan, switch_performed=False)
    if expected_source_binding is not None:
        verify_flat_source_binding(
            plan["flat_served_root"], plan["release_identity_path"],
            plan["expected_commit_sha"], expected_source_binding,
        )
    if plan.get("status") != "PASSED":
        if not switch:
            return plan
        raise ValueError(
            "Flat adoption preflight is incomplete: {}".format(
                "; ".join(plan.get("flat_source_binding", {}).get(
                    "binding_violations", []
                ))
            )
        )
    if not switch:
        return plan

    publish_root = plan["publish_root"]
    parent = os.path.dirname(publish_root) or None
    temporary_parent = tempfile.mkdtemp(
        prefix="coverage-flat-adoption-", dir=parent
    )
    staging_root = os.path.join(temporary_parent, "staging")
    try:
        adoption = prepare_legacy_flat_adoption(
            plan["flat_served_root"], staging_root,
            plan["release_identity_path"], plan["expected_commit_sha"],
        )
        if expected_source_binding is not None:
            observed = adoption.get("flat_source_binding") or {}
            if observed != expected_source_binding:
                raise ValueError(
                    "legacy Flat Root source binding changed while staging"
                )
        result = bootstrap_baseline(
            staging_root, publish_root,
            os.path.join(staging_root, "release_identity.json"),
            session_id, switch=True, api_contract_version=api_contract_version,
            application_root=application_root,
        )
        result["adoption_plan"] = plan
        result["deployment_layout"] = FLAT
        result["switch_performed"] = True
        result["flat_source_binding"] = adoption.get("flat_source_binding")
        return result
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)


def validate_current_or_plan_flat(publish_root, flat_served_root="",
                                  release_identity_path="",
                                  expected_commit_sha=""):
    """Convenience preflight used by orchestration and diagnostics."""
    classification = classify_deployment(publish_root, flat_served_root)
    if classification.get("deployment_layout") == FLAT:
        if not release_identity_path or not expected_commit_sha:
            raise ValueError(
                "Flat deployment requires release identity and expected commit SHA"
            )
        return plan_flat_current_adoption(
            publish_root, flat_served_root, release_identity_path,
            expected_commit_sha,
        )
    return classification
