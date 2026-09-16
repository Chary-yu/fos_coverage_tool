"""Canonical production release trust policy.

The repository still understands the historical protected-builder records so
old Candidate evidence can be audited.  The current production authority is
deliberately narrower: only an offline operator may create evidence accepted
by the current publication path.

This module contains no network or production integration code and is safe to
import from Python 3.6-compatible release tooling.
"""

from __future__ import print_function


RELEASE_TRUST_MODE_PROTECTED_BUILDER = "protected_builder"
RELEASE_TRUST_MODE_OFFLINE_OPERATOR = "offline_operator"

# Compatibility parsing remains intentionally broader than the current
# authority.  Callers handling historical artifacts may still recognize both
# values, but current production code must use the tuple below.
RELEASE_TRUST_MODES = (
    RELEASE_TRUST_MODE_PROTECTED_BUILDER,
    RELEASE_TRUST_MODE_OFFLINE_OPERATOR,
)
CURRENT_PRODUCTION_RELEASE_TRUST_MODE = RELEASE_TRUST_MODE_OFFLINE_OPERATOR
CURRENT_PRODUCTION_RELEASE_TRUST_MODES = (
    CURRENT_PRODUCTION_RELEASE_TRUST_MODE,
)
PRODUCTION_RELEASE_AUTHORITY = "OFFLINE_OPERATOR_ONLY"
PROTECTED_BUILDER_LEGACY_VERIFY_ONLY = True
PROTECTED_BUILDER_NEW_PUBLICATION_ALLOWED = False


def normalize_release_trust_mode(value, default=None):
    """Return a normalized historical/current trust mode or fail closed."""
    mode = str(value or default or "").strip()
    if mode not in RELEASE_TRUST_MODES:
        raise ValueError("unsupported release_trust_mode")
    return mode


def assert_current_production_trust_mode(value=None, operation="production release"):
    """Require the sole trust mode accepted by current production paths."""
    mode = str(value or CURRENT_PRODUCTION_RELEASE_TRUST_MODE).strip()
    if mode not in CURRENT_PRODUCTION_RELEASE_TRUST_MODES:
        raise ValueError(
            "PROTECTED_BUILDER_CURRENT_PUBLICATION_ALLOWED=NO: {} requires "
            "release_trust_mode=offline_operator".format(operation)
        )
    return mode


def verify_current_production_candidate_trust(
        release_trust_mode, candidate_root=None, release_identity=None,
        candidate_manifest=None, source_repo_root=None, **kwargs):
    """Verify a Candidate under the current offline-only authority.

    The import is intentionally lazy: ``app.candidate_artifact`` re-exports
    this entry point for compatibility, while this policy module must remain
    importable without creating a module cycle.
    """
    assert_current_production_trust_mode(
        release_trust_mode, operation="current production Candidate"
    )
    if candidate_root is None or release_identity is None or \
            candidate_manifest is None or source_repo_root is None:
        raise ValueError(
            "current production Candidate trust requires Candidate, identity, "
            "manifest, and source checkout"
        )
    from app.candidate_artifact import verify_offline_operator_trust
    return verify_offline_operator_trust(
        candidate_root, release_identity, candidate_manifest, source_repo_root,
        **kwargs
    )


def verify_legacy_protected_builder_trust(provenance, workflow_identity,
                                         workflow_sha):
    """Verify historical protected-builder provenance without authorizing it."""
    from app.candidate_artifact import verify_trusted_build_policy
    return verify_trusted_build_policy(
        provenance, workflow_identity, workflow_sha
    )


def authority_contract():
    """Return the stable machine-readable current authority contract."""
    return {
        "PRODUCTION_RELEASE_AUTHORITY": PRODUCTION_RELEASE_AUTHORITY,
        "GITHUB_SOURCE_REPOSITORY_ONLY": True,
        "GITHUB_ACTIONS_REQUIRED": False,
        "GITHUB_CI_REQUIRED": False,
        "GITHUB_RUNNER_REQUIRED": False,
        "GITHUB_ENVIRONMENT_REQUIRED": False,
        "GITHUB_SECRETS_REQUIRED_FOR_RELEASE": False,
        "GITHUB_ARTIFACT_REQUIRED": False,
        "GITHUB_ATTESTATION_REQUIRED": False,
        "GITHUB_API_REQUIRED_FOR_READINESS": False,
        "PROTECTED_BUILDER_NEW_PUBLICATION_ALLOWED": False,
        "VALIDATION_FIXTURE_PRODUCTION_PUBLISHABLE": False,
        "DUAL_PRODUCTION_AUTHORITY": False,
    }
