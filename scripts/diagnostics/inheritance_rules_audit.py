"""Validate the machine-authoritative R01-R83 contract."""

from __future__ import print_function

import argparse
import ast
import collections
import hashlib
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
MATRIX_RELATIVE_PATH = os.path.join("contracts", "inheritance_test_matrix.json")
PLAN_RELATIVE_PATH = os.path.join(
    "docs", "FOS_Coverage_Gate_A-F_详细开发与验证总方案_v1.2.md"
)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _module_path(repo_root, module_name):
    parts = str(module_name or "").split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        return ""
    source_path = os.path.join(repo_root, *parts) + ".py"
    if os.path.isfile(source_path):
        return source_path
    package_path = os.path.join(repo_root, *parts, "__init__.py")
    return package_path if os.path.isfile(package_path) else ""


def _selector_parts(selector):
    parts = str(selector or "").split(".")
    if len(parts) < 3 or any(not part.isidentifier() for part in parts):
        return "", "", ""
    return ".".join(parts[:-2]), parts[-2], parts[-1]


def _test_selector_exists(repo_root, selector):
    module_name, class_name, method_name = _selector_parts(selector)
    path = _module_path(repo_root, module_name)
    if not path:
        return False, "test module does not exist"
    try:
        with open(path, "r", encoding="utf-8") as stream:
            tree = ast.parse(stream.read(), filename=path)
    except (OSError, SyntaxError, UnicodeError) as exc:
        return False, "cannot parse test module: {}".format(exc)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    child.name == method_name:
                return True, ""
        return False, "test method does not exist"
    return False, "test class does not exist"


def audit(repo_root=ROOT):
    path = os.path.join(repo_root, "contracts", "inheritance_rules_v1.json")
    matrix_path = os.path.join(repo_root, MATRIX_RELATIVE_PATH)
    plan_path = os.path.join(repo_root, PLAN_RELATIVE_PATH)
    violations = []
    rules = []
    matrix = {}
    try:
        payload = _read_json(path)
        rules = payload.get("rules") or []
    except Exception as exc:
        return {"status": "FAILED", "evidence_class": "inheritance_rules_audit",
                "rules_contract_path": path, "violations": [str(exc)],
                "rule_count": 0, "contract_sha256": "", "test_matrix_path": matrix_path}
    try:
        matrix = _read_json(matrix_path)
    except Exception as exc:
        violations.append("cannot load test matrix: {}".format(exc))
    expected = ["R{:02d}".format(index) for index in range(1, 84)]
    actual = [str(item.get("rule_id") or "") for item in rules]
    if actual != expected:
        violations.append("rules must contain exactly R01..R83 in order")
    contract_test_ids = []
    for item in rules:
        for field in ("rule_id", "owner_module", "test_ids", "reason_codes", "status"):
            if not item.get(field):
                violations.append("{} missing {}".format(item.get("rule_id"), field))
        if item.get("status") == "TODO":
            violations.append("{} is still TODO".format(item.get("rule_id")))
        test_ids = item.get("test_ids") or []
        if not isinstance(test_ids, list):
            violations.append("{} test_ids must be a list".format(item.get("rule_id")))
            test_ids = []
        contract_test_ids.extend(str(test_id) for test_id in test_ids)

    owner_modules = sorted(set(str(item.get("owner_module") or "")
                               for item in rules if item.get("owner_module")))
    missing_owner_modules = []
    for module_name in owner_modules:
        if not _module_path(repo_root, module_name):
            missing_owner_modules.append(module_name)
            violations.append("owner module does not exist: {}".format(module_name))

    targeted_suites = matrix.get("targeted_suites") if isinstance(matrix, dict) else []
    if not isinstance(targeted_suites, list) or not targeted_suites:
        violations.append("test matrix targeted_suites must be a non-empty list")
        targeted_suites = []
    cases = matrix.get("cases") if isinstance(matrix, dict) else []
    if not isinstance(cases, list):
        violations.append("test matrix cases must be a list")
        cases = []
    mapped_test_ids = []
    invalid_selectors = []
    for case in cases:
        if not isinstance(case, dict):
            violations.append("test matrix contains a non-object case")
            continue
        selector = str(case.get("selector") or "")
        test_ids = case.get("test_ids") or []
        if not selector or not isinstance(test_ids, list) or not test_ids:
            violations.append("test matrix cases require selector and non-empty test_ids")
            continue
        module_name, _, _ = _selector_parts(selector)
        if module_name not in targeted_suites:
            violations.append("test selector is outside Gate D suites: {}".format(selector))
        valid, reason = _test_selector_exists(repo_root, selector)
        if not valid:
            invalid_selectors.append({"selector": selector, "reason": reason})
            violations.append("invalid test selector {}: {}".format(selector, reason))
        mapped_test_ids.extend(str(test_id) for test_id in test_ids)
    contract_counts = collections.Counter(contract_test_ids)
    mapped_counts = collections.Counter(mapped_test_ids)
    duplicate_contract_ids = sorted(item for item, count in contract_counts.items()
                                    if count != 1)
    duplicate_mapped_ids = sorted(item for item, count in mapped_counts.items()
                                  if count > 1)
    missing_test_ids = sorted(set(contract_test_ids) - set(mapped_test_ids))
    unexpected_test_ids = sorted(set(mapped_test_ids) - set(contract_test_ids))
    if duplicate_contract_ids:
        violations.append("contract test_ids must be unique: {}".format(
            ", ".join(duplicate_contract_ids)
        ))
    if duplicate_mapped_ids:
        violations.append("test matrix maps a test_id more than once: {}".format(
            ", ".join(duplicate_mapped_ids)
        ))
    if missing_test_ids:
        violations.append("test matrix is missing test_ids: {}".format(
            ", ".join(missing_test_ids)
        ))
    if unexpected_test_ids:
        violations.append("test matrix has unknown test_ids: {}".format(
            ", ".join(unexpected_test_ids)
        ))
    with open(path, "rb") as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
    document_sha256 = ""
    document_sha256_match = False
    try:
        with open(plan_path, "r", encoding="utf-8") as stream:
            document = stream.read()
        marker = "rules_contract_sha256 ="
        document_sha256 = document.split(marker, 1)[1].splitlines()[0].strip()
        document_sha256_match = document_sha256 == digest
        if not document_sha256_match:
            violations.append(
                "Gate A-F plan rules_contract_sha256 does not match canonical JSON"
            )
    except (OSError, IndexError, UnicodeError) as exc:
        violations.append("cannot read Gate A-F plan rules_contract_sha256: {}".format(exc))
    matrix_digest = ""
    if os.path.isfile(matrix_path):
        with open(matrix_path, "rb") as stream:
            matrix_digest = hashlib.sha256(stream.read()).hexdigest()
    return {"status": "PASSED" if not violations else "FAILED",
            "evidence_class": "inheritance_rules_audit",
            "rules_contract_path": os.path.relpath(path, repo_root),
            "rule_count": len(rules), "contract_sha256": digest,
            "test_matrix_path": os.path.relpath(matrix_path, repo_root),
            "test_matrix_sha256": matrix_digest,
            "plan_path": os.path.relpath(plan_path, repo_root),
            "plan_rules_contract_sha256": document_sha256,
            "plan_sha256_match": document_sha256_match,
            "owner_module_count": len(owner_modules),
            "missing_owner_modules": missing_owner_modules,
            "test_id_count": len(contract_test_ids),
            "mapped_test_id_count": len(mapped_test_ids),
            "missing_test_ids": missing_test_ids,
            "unexpected_test_ids": unexpected_test_ids,
            "invalid_test_selectors": invalid_selectors,
            "targeted_suites": targeted_suites,
            "violations": violations}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=ROOT)
    args = parser.parse_args(argv)
    result = audit(args.repo_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
