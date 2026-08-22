"""Run the explicit public compatibility surfaces and emit provenance."""

from __future__ import absolute_import

import argparse
import importlib
import json
import os
import platform
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.time_utils import utc_iso
from scripts.diagnostics.contract import with_contract


SURFACES = (
    ("enhance_coverage", "app.legacy_runtime"),
    ("coverage_check", "app.incremental.legacy"),
    ("code_detail_service", "app.code_detail.service"),
    ("code_region", "app.code_detail.code_region"),
    ("source_reader", "app.code_detail.source_reader"),
)


# ``--help`` exercises the public CLI dispatch without starting a server,
# opening a database, or reading a user supplied report.  Keep this separate
# from the import list so the evidence makes it explicit which compatibility
# contract was actually exercised.
CLI_SURFACES = (
    "enhance_coverage.py",
    "coverage_check.py",
)

# ``inherit`` is intentionally no longer a compatibility writer.  Exercise
# the public command itself so a future refactor cannot accidentally restore
# the old project-name/hash inheritance path behind the shim.
RETIRED_CLI_SURFACES = (
    ("enhance_coverage.py", ("inherit", "--from", "v1", "--to", "v2"),
     2, "legacy inherit is retired"),
)


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            stderr=subprocess.STDOUT,
        ).decode("ascii").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return ""


def audit(repo_root=ROOT):
    results = []
    failures = []
    for public_name, owner in SURFACES:
        try:
            module = importlib.import_module(public_name)
            results.append({
                "surface": public_name,
                "owner": owner,
                "status": "PASSED" if module is not None else "FAILED",
            })
        except Exception as exc:  # pragma: no cover - exercised on broken deployments
            failures.append("{} import failed: {}".format(public_name, exc))
            results.append({
                "surface": public_name, "owner": owner,
                "status": "FAILED", "error": str(exc),
            })
    cli_results = []
    for script_name in CLI_SURFACES:
        script_path = os.path.join(repo_root, script_name)
        try:
            completed = subprocess.run(
                [sys.executable, script_path, "--help"],
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=30,
            )
            status = "PASSED" if completed.returncode == 0 else "FAILED"
            result = {
                "surface": script_name,
                "operation": "--help",
                "status": status,
                "exit_code": completed.returncode,
            }
            if status != "PASSED":
                result["stderr"] = (completed.stderr or "")[-1000:]
                failures.append(
                    "{} --help exited with {}".format(
                        script_name, completed.returncode
                    )
                )
            cli_results.append(result)
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append("{} --help failed: {}".format(script_name, exc))
            cli_results.append({
                "surface": script_name,
                "operation": "--help",
                "status": "FAILED",
                "exit_code": None,
                "error": str(exc),
            })
    retired_cli_results = []
    for script_name, args, expected_exit, marker in RETIRED_CLI_SURFACES:
        script_path = os.path.join(repo_root, script_name)
        try:
            completed = subprocess.run(
                [sys.executable, script_path] + list(args),
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=30,
            )
            output = ((completed.stdout or "") + (completed.stderr or "")).lower()
            status = "PASSED" if (
                completed.returncode == expected_exit and marker.lower() in output
            ) else "FAILED"
            result = {
                "surface": script_name,
                "operation": "retired-inherit",
                "status": status,
                "exit_code": completed.returncode,
                "expected_exit_code": expected_exit,
                "marker": marker,
            }
            if status != "PASSED":
                result["output"] = output[-1000:]
                failures.append(
                    "{} retired-inherit did not fail closed with exit {}".format(
                        script_name, expected_exit
                    )
                )
            retired_cli_results.append(result)
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append("{} retired-inherit failed: {}".format(script_name, exc))
            retired_cli_results.append({
                "surface": script_name,
                "operation": "retired-inherit",
                "status": "FAILED",
                "exit_code": None,
                "expected_exit_code": expected_exit,
                "error": str(exc),
            })
    started = utc_iso()
    return with_contract({
        "status": "PASSED" if not failures else "FAILED",
        "evidence_class": "compatibility_surface_smoke",
        "candidate_revision": _revision(repo_root),
        "host_identity": {
            "hostname": platform.node(), "platform": platform.platform(),
        },
        "command_or_action": "python scripts/diagnostics/legacy_compatibility_smoke.py",
        "started_at": started, "finished_at": utc_iso(),
        "exit_code": 0 if not failures else 1,
        "synthetic": False,
        "surfaces": results,
        "cli_surfaces": cli_results,
        "retired_cli_surfaces": retired_cli_results,
        "violations": failures,
        "legacy_implementation_status": "TRANSITIONAL_LEGACY",
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = audit(os.path.abspath(args.repo_root))
    if args.output:
        output = args.output if os.path.isabs(args.output) else os.path.join(os.getcwd(), args.output)
        directory = os.path.dirname(os.path.abspath(output))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
