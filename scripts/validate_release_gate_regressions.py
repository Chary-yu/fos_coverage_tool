#!/usr/bin/env python3
"""Validate lineage-aware release-gate regression cases."""
from __future__ import print_function

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.upgrade.release_gate_mode import classify_release_gate_mode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets", "release-gate-regression-cases.json"
        ),
    )
    args = parser.parse_args(argv)
    with open(os.path.abspath(args.cases), "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        print("ERROR missing regression cases")
        return 1

    errors = []
    seen = set()
    for item in cases:
        case_id = str(item.get("id") or "").strip()
        if not case_id or case_id in seen:
            errors.append("invalid/duplicate case id: {}".format(case_id or "<empty>"))
            continue
        seen.add(case_id)
        actual = classify_release_gate_mode(item)
        checks = {
            "path_mapping_gate": item.get("expected_path_mapping_gate"),
            "vnext_report_gate": item.get("expected_vnext_report_gate"),
            "blocked": item.get("expected_blocked"),
        }
        for key, expected in checks.items():
            if actual.get(key) != expected:
                errors.append(
                    "{} {} expected {!r} got {!r}".format(
                        case_id, key, expected, actual.get(key)
                    )
                )
        if actual.get("report_identity_fabricated") is not False:
            errors.append(
                "{} report_identity_fabricated must always be false".format(
                    case_id
                )
            )

    if errors:
        for error in errors:
            print("ERROR {}".format(error))
        return 1
    print("VALID {} release-gate regression cases".format(len(cases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
