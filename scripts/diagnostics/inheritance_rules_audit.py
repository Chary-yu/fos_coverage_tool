"""Validate the machine-authoritative R01-R83 contract."""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def audit(repo_root=ROOT):
    path = os.path.join(repo_root, "contracts", "inheritance_rules_v1.json")
    violations = []
    rules = []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        rules = payload.get("rules") or []
    except Exception as exc:
        return {"status": "FAILED", "evidence_class": "inheritance_rules_audit",
                "rules_contract_path": path, "violations": [str(exc)],
                "rule_count": 0, "contract_sha256": ""}
    expected = ["R{:02d}".format(index) for index in range(1, 84)]
    actual = [str(item.get("rule_id") or "") for item in rules]
    if actual != expected:
        violations.append("rules must contain exactly R01..R83 in order")
    for item in rules:
        for field in ("rule_id", "owner_module", "test_ids", "reason_codes", "status"):
            if not item.get(field):
                violations.append("{} missing {}".format(item.get("rule_id"), field))
        if item.get("status") == "TODO":
            violations.append("{} is still TODO".format(item.get("rule_id")))
    with open(path, "rb") as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
    return {"status": "PASSED" if not violations else "FAILED",
            "evidence_class": "inheritance_rules_audit",
            "rules_contract_path": os.path.relpath(path, repo_root),
            "rule_count": len(rules), "contract_sha256": digest,
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
