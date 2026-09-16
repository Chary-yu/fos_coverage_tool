#!/usr/bin/env python3
"""Report the current production release authority contract.

This diagnostic is deliberately source/configuration-only.  It never contacts
an external service and does not inspect or mutate a production host.
"""

from __future__ import print_function

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_trust_policy import authority_contract


CONTRACT_ORDER = (
    "PRODUCTION_RELEASE_AUTHORITY",
    "GITHUB_SOURCE_REPOSITORY_ONLY",
    "GITHUB_ACTIONS_REQUIRED",
    "GITHUB_CI_REQUIRED",
    "GITHUB_RUNNER_REQUIRED",
    "GITHUB_ENVIRONMENT_REQUIRED",
    "GITHUB_SECRETS_REQUIRED_FOR_RELEASE",
    "GITHUB_ARTIFACT_REQUIRED",
    "GITHUB_ATTESTATION_REQUIRED",
    "GITHUB_API_REQUIRED_FOR_READINESS",
    "PROTECTED_BUILDER_LEGACY_VERIFY_ONLY",
    "PROTECTED_BUILDER_NEW_PUBLICATION_ALLOWED",
    "VALIDATION_FIXTURE_PRODUCTION_PUBLISHABLE",
    "DUAL_PRODUCTION_AUTHORITY",
)


def _workflows(repo_root):
    directory = os.path.join(os.path.abspath(repo_root), ".github", "workflows")
    if not os.path.isdir(directory) or os.path.islink(directory):
        return []
    result = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and not os.path.islink(path):
            result.append(os.path.relpath(path, repo_root).replace(os.sep, "/"))
    return result


def evaluate(repo_root):
    """Return the static authority contract and residue diagnostics."""
    repo_root = os.path.abspath(repo_root)
    contract = dict(authority_contract())
    for key, value in list(contract.items()):
        if isinstance(value, bool):
            contract[key] = "YES" if value else "NO"
    # Workflow files are legacy infrastructure residue.  They do not gain
    # authority, but their presence prevents the strict local-first readiness
    # result until the local replacements have been verified and the files are
    # retired in a later commit.
    workflows = _workflows(repo_root)
    contract.update({
        "GITHUB_SOURCE_REPOSITORY_ONLY": "YES",
        "PROTECTED_BUILDER_LEGACY_VERIFY_ONLY": "YES",
        "PROTECTED_BUILDER_NEW_PUBLICATION_ALLOWED": "NO",
        "VALIDATION_FIXTURE_PRODUCTION_PUBLISHABLE": "NO",
        "DUAL_PRODUCTION_AUTHORITY": "NO",
        "GITHUB_WORKFLOW_RESIDUE": "YES" if workflows else "NO",
        "GITHUB_WORKFLOW_RESIDUE_FILES": workflows,
        "LOCAL_FIRST_AUTHORITY_READY": "YES" if not workflows else "NO",
        "LOCAL_FIRST_AUTHORITY_BLOCKER": (
            "legacy workflow files remain"
            if workflows else ""
        ),
        "repository_root": repo_root,
    })
    return contract


def _value(value):
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if value is None:
        return ""
    return str(value)


def render_contract(contract):
    lines = []
    for key in CONTRACT_ORDER:
        lines.append("{}={}".format(key, _value(contract.get(key))))
    lines.extend([
        "GITHUB_WORKFLOW_RESIDUE={}".format(_value(
            contract.get("GITHUB_WORKFLOW_RESIDUE")
        )),
        "LOCAL_FIRST_AUTHORITY_READY={}".format(_value(
            contract.get("LOCAL_FIRST_AUTHORITY_READY")
        )),
        "LOCAL_FIRST_AUTHORITY_BLOCKER={}".format(_value(
            contract.get("LOCAL_FIRST_AUTHORITY_BLOCKER")
        )),
    ])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="production_release_authority.py")
    parser.add_argument("--repo-root", default=os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../..")
    ))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = evaluate(args.repo_root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_contract(result), end="")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
