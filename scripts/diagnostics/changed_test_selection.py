"""Map changed ownership boundaries to specialist regression suites.

The diff source is part of the evidence. A missing base/before revision is an
error, not an empty change set, because an empty set would silently select the
fallback suite and create false confidence.
"""

import argparse
import fnmatch
import json
import os
import subprocess
import sys

try:
    from scripts.diagnostics.contract import CONTRACT_VERSION, with_contract
except ModuleNotFoundError:
    from contract import CONTRACT_VERSION, with_contract


MAPPINGS = [
    ({"app/api/*", "app/auth*", "app/jobs/*", "app/db/*", "scripts/upgrade/migration_runner.py"},
     "tests.vnext.test_api_export_security tests.vnext.test_jobs tests.vnext.test_migration_runner tests.database.test_phase0_baseline tests.security.test_api_auth"),
    ({"app/bootstrap.py", "app/config/*", "coverage_config.json", "config/*"},
     "tests.vnext.test_runtime_config tests.vnext.test_vnext_runtime tests.vnext.test_api_export_security"),
    ({"app/code_detail/*", "web/assets/js/coverage_enhance.js", "web/assets/css/*"},
     "tests.vnext.test_vnext_runtime tests.vnext.test_api_export_security tests.code_detail.test_phase6_sidecar"),
    ({"app/progress/*", "app/services/progress_service.py", "web/assets/js/coverage_progress.js", "web/assets/js/incremental_developer_tasks.js"},
     "tests.progress.test_phase4_progress tests.vnext.test_vnext_runtime"),
    ({"app/incremental/*", "app/inject/*", "coverage_check.py"},
     "tests.incremental.test_line_ownership_and_lcov_ranges tests.incremental.test_phase5_inject_path tests.vnext.test_incremental_canonical"),
    ({"app/compat/*", "app/legacy_runtime.py", "app/incremental/legacy.py"},
     "tests.vnext.test_legacy_telemetry tests.vnext.test_runtime_config tests.code_detail.test_phase2_core tests.incremental.test_phase5_inject_path"),
    ({"app/inheritance/*", "contracts/inheritance*", "tests/fixtures/inheritance_deterministic_corpus.json"},
     "tests.vnext.test_inheritance_engine tests.vnext.test_deterministic_inheritance_corpus tests.vnext.test_parser_toolchain tests.vnext.test_analysis_domain tests.vnext.test_scan_import_lifecycle"),
    ({"app/scan_import/*"},
     "tests.vnext.test_scan_import_lifecycle tests.vnext.test_jobs tests.vnext.test_vnext_runtime"),
    ({"app/services/analysis_service.py", "app/services/project_service.py", "app/reports/*"},
     "tests.vnext.test_vnext_runtime tests.vnext.test_registry_and_api_contract tests.vnext.test_api_export_security"),
    ({"app/services/inheritance_review_service.py"},
     "tests.vnext.test_api_export_security tests.vnext.test_analysis_domain"),
    ({"web/templates/*", "coverage_progress.js", "coverage_enhance.js", "incremental_developer_tasks.js", "package.json", "package-lock.json"},
     # Browser specs are JavaScript/Playwright and are executed by the
     # dedicated browser job; this selector is consumed by Python unittest.
     "tests.vnext.test_vnext_runtime"),
    ({"scripts/diagnostics/*", "scripts/release/*", "scripts/upgrade/*"},
     "tests.vnext.test_architecture_audits tests.release.test_dod_status tests.release.test_release_readiness tests.release.test_upgrade_manifest tests.release.test_rollback_rehearsal tests.release.test_verified_backup_rehearsal tests.release.test_evidence_authenticity"),
    ({"scripts/maintenance/mysql_backup.py"},
     "tests.database.test_phase0_baseline tests.release.test_verified_backup_rehearsal"),
    ({"docs/*", "schema/*", "evidence/*"},
     "tests.vnext.test_architecture_audits tests.release.test_release_governance_tools tests.release.test_evidence_authenticity"),
    ({"scripts/upgrade/*", "app/release_identity.py", ".github/workflows/ci.yml"},
     "tests.vnext.test_migration_runner tests.release.test_upgrade_manifest tests.release.test_evidence_authenticity"),
    ({"docs/gate_dod_manifest.json"},
     "tests.release.test_dod_status tests.release.test_release_readiness"),
]


class DiffResolutionError(RuntimeError):
    pass


def _usable_ref(value):
    value = str(value or "").strip()
    return value and value != "0" * 40


def _git_revision(repo_root, ref):
    try:
        subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "--verify", ref],
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DiffResolutionError("git revision is unavailable: {}".format(ref)) from exc


def _diff_spec(base=None, before=None, head=None):
    head = head or "HEAD"
    if _usable_ref(base):
        return "{}...{}".format(base, head)
    if _usable_ref(before):
        return "{}..{}".format(before, head)
    return "HEAD^..HEAD"


def changed_files(repo_root, base=None, before=None, head=None):
    """Return changed files or raise when the diff cannot be established."""
    spec = _diff_spec(base=base, before=before, head=head)
    _git_revision(repo_root, head or "HEAD")
    if spec == "HEAD^..HEAD":
        _git_revision(repo_root, "HEAD^")
    else:
        left = spec.split("...")[0] if "..." in spec else spec.split("..")[0]
        _git_revision(repo_root, left)
    try:
        output = subprocess.check_output(
            ["git", "-C", repo_root, "diff", "--name-only", "-z", spec],
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DiffResolutionError("unable to resolve changed files for {}".format(spec)) from exc
    # ``-z`` disables Git's C-style quoting, so Unicode, whitespace and odd
    # but valid names remain exact manifest values rather than becoming an
    # escaped approximation that cannot match specialist ownership patterns.
    return [item for item in output.decode("utf-8").split("\0") if item]


def select(files):
    suites = set()
    for path in files:
        normalized_path = str(path or "").replace("\\", "/")
        # A changed test is itself a specialist owner.  Without this direct
        # mapping, a commit that only edits a test could run an unrelated
        # fallback suite and leave the changed regression unexecuted.
        if normalized_path.startswith("tests/") and normalized_path.endswith(".py"):
            module_path = normalized_path[:-3].replace("/", ".")
            if not module_path.endswith(".__init__"):
                suites.add(module_path)
        for patterns, suite in MAPPINGS:
            if any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
                suites.update(suite.split())
    if not suites:
        suites.add("tests.vnext.test_vnext_runtime")
    return sorted(suites)


def _write_manifest(path, result):
    if not path:
        return
    target = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with open(target, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)


def _run_tests(repo_root, modules):
    command = [sys.executable, "-m", "unittest"] + list(modules) + ["-v"]
    return subprocess.call(command, cwd=repo_root)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--before")
    parser.add_argument("--head")
    parser.add_argument("--manifest")
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--repo-root", default=os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
    args = parser.parse_args(argv)

    base = args.base or os.environ.get("GITHUB_BASE_SHA") or os.environ.get("GITHUB_EVENT_BASE")
    before = args.before or os.environ.get("GITHUB_EVENT_BEFORE")
    head = args.head or os.environ.get("GITHUB_SHA") or "HEAD"
    result = with_contract({
        "evidence_class": "changed_test_selection",
        "selection_status": "FAILED",
        "repo_root": os.path.realpath(args.repo_root),
        "base": base or "",
        "before": before or "",
        "head": head,
        "changed_files": [],
        "test_modules": [],
    })
    try:
        files = changed_files(args.repo_root, base=base, before=before, head=head)
        modules = select(files)
        result.update({
            "selection_status": "PASSED",
            "diff_spec": _diff_spec(base=base, before=before, head=head),
            "changed_files": files,
            "test_modules": modules,
        })
        if args.run_tests:
            exit_code = _run_tests(args.repo_root, modules)
            result["selected_tests_exit_code"] = int(exit_code)
            if exit_code != 0:
                result["selection_status"] = "FAILED"
    except DiffResolutionError as exc:
        result["error"] = str(exc)
        result["selection_status"] = "FAILED"

    _write_manifest(args.manifest, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["selection_status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
