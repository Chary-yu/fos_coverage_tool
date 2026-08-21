"""Validate the plan's frozen contract and schema ownership artifacts."""

from __future__ import absolute_import

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.diagnostics.contract import with_contract


REQUIRED_FILES = (
    "docs/FOS_Coverage_Gate_A-F_详细开发与验证总方案.md",
    "docs/FOS_Coverage_Gate_A-F_详细开发与验证总方案_v1.2.md",
    "docs/deterministic_inheritance_contract_v1.md",
    "docs/deterministic_inheritance_contract_v1.json",
    "docs/migration_matrix.md",
    "docs/migration_matrix.json",
    "docs/api_contract.md",
    "docs/api_contract.json",
    "docs/contracts/evidence_manifest_v2.schema.json",
    "docs/release_identity_contract_v2.md",
    "schema/vnext_core/README.md",
    "schema/analysis_domain/README.md",
    "schema/inheritance_domain/README.md",
    "evidence/README.md",
)


def _load(repo_root, relative_path, violations):
    path = os.path.join(repo_root, relative_path)
    if not os.path.isfile(path):
        violations.append("missing contract artifact: {}".format(relative_path))
        return {}
    if not relative_path.endswith(".json"):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        violations.append("invalid JSON contract {}: {}".format(relative_path, exc))
        return {}
    if not isinstance(value, dict):
        violations.append("JSON contract must be an object: {}".format(relative_path))
        return {}
    return value


def _route_fragments(route):
    value = str(route or "").split(" ", 1)[-1].split("?", 1)[0]
    # The router uses a regular expression with ``([^/]+)`` while the frozen
    # contract uses ``{id}``.  Compare stable path segments instead of
    # pretending those two placeholder syntaxes are byte-identical.
    return [segment for segment in value.split("/")
            if segment and not (segment.startswith("{") and segment.endswith("}"))]


def audit(repo_root=ROOT):
    repo_root = os.path.abspath(repo_root)
    violations = []
    for relative_path in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(repo_root, relative_path)):
            violations.append("missing contract artifact: {}".format(relative_path))
    deterministic = _load(
        repo_root, "docs/deterministic_inheritance_contract_v1.json", violations
    )
    migration = _load(repo_root, "docs/migration_matrix.json", violations)
    api = _load(repo_root, "docs/api_contract.json", violations)
    evidence_schema = _load(
        repo_root, "docs/contracts/evidence_manifest_v2.schema.json", violations
    )
    if deterministic.get("algorithm_version") != "inheritance-v1":
        violations.append("deterministic inheritance algorithm version is not frozen")
    if deterministic.get("authoritative_rules_contract") != \
            "contracts/inheritance_rules_v1.json":
        violations.append("deterministic inheritance rule authority is incorrect")
    if len(migration.get("mappings") or []) < 4:
        violations.append("migration matrix is missing required source mappings")
    if migration.get("target", {}).get("core_schema") != \
            "scripts/upgrade/vnext_schema.sql":
        violations.append("migration matrix core schema owner is incorrect")
    if api.get("pagination", {}).get("maximum_limit") != 500:
        violations.append("API pagination maximum must remain 500")
    route_text = ""
    application_path = os.path.join(repo_root, "app", "api", "application.py")
    try:
        with open(application_path, "r", encoding="utf-8") as stream:
            route_text = stream.read()
    except OSError as exc:
        violations.append("cannot read canonical API application: {}".format(exc))
    missing_routes = []
    for name, route in sorted((api.get("routes") or {}).items()):
        fragments = _route_fragments(route)
        if fragments and not all(fragment in route_text for fragment in fragments):
            missing_routes.append(name)
    if missing_routes:
        violations.append("API contract routes missing from canonical owner: {}".format(
            ", ".join(missing_routes)
        ))
    if evidence_schema.get("properties", {}).get("evidence_schema_version", {}).get(
            "const") != 2:
        violations.append("Evidence Manifest v2 schema is not version 2")
    return with_contract({
        "status": "PASSED" if not violations else "FAILED",
        "evidence_class": "contract_artifact_audit",
        "required_file_count": len(REQUIRED_FILES),
        "missing_routes": missing_routes,
        "violations": violations,
    })


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0 if result["status"] == "PASSED" else 1)
