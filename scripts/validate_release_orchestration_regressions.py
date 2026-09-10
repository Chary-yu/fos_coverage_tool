#!/usr/bin/env python3
"""Run only the release-orchestration regression lane required by R8."""

from __future__ import print_function

import argparse
import json
import os
import subprocess
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="validate_release_orchestration_regressions.py"
    )
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    repo_root = os.path.realpath(os.path.abspath(args.repo_root))
    modules = [
        "tests.release.test_fos_r8_orchestration",
        "tests.release.test_legacy_flat_adoption",
        "tests.release.test_current_adoption",
        "tests.release.test_production_candidate_build",
        "tests.release.test_offline_operator_trust",
        "tests.release.test_performance_ab",
        "tests.release.test_upgrade_manifest",
        "tests.release.test_vfoswind_production_lifecycle",
    ]
    command = [sys.executable, "-m", "unittest"] + modules
    result = subprocess.Popen(
        command, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    stdout, _ = result.communicate()
    output = stdout.decode("utf-8", "replace")
    payload = {
        "status": "PASSED" if result.returncode == 0 else "FAILED",
        "evidence_class": "release_orchestration_regression",
        "modules": modules,
        "command": "python3 -m unittest " + " ".join(modules),
        "exit_code": result.returncode,
        "output_tail": output[-20000:],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        parent = os.path.dirname(os.path.abspath(args.output))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
    print(encoded)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
