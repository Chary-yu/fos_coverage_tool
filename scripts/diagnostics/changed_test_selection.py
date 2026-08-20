"""Map changed ownership boundaries to specialist regression suites."""

import argparse
import fnmatch
import json
import os
import subprocess


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
    ({"app/services/analysis_service.py", "app/services/project_service.py", "app/reports/*"},
     "tests.vnext.test_vnext_runtime tests.vnext.test_registry_and_api_contract tests.vnext.test_api_export_security"),
    ({"web/templates/*", "coverage_progress.js", "coverage_enhance.js", "incremental_developer_tasks.js", "package.json", "package-lock.json"},
     "tests.browser.coverage_real_browser tests.vnext.test_vnext_runtime"),
    ({"scripts/diagnostics/*", "scripts/release/*", "scripts/upgrade/*"},
     "tests.vnext.test_architecture_audits tests.release.test_upgrade_manifest tests.release.test_evidence_authenticity"),
    ({"scripts/upgrade/*", "app/release_identity.py", ".github/workflows/ci.yml"},
     "tests.vnext.test_migration_runner tests.release.test_upgrade_manifest tests.release.test_evidence_authenticity"),
]


def changed_files(repo_root, base=None):
    command = ["git", "-C", repo_root, "diff", "--name-only"]
    if base:
        command.append("{}...HEAD".format(base))
    else:
        command.extend(["HEAD^", "HEAD"])
    try:
        output = subprocess.check_output(command, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return []
    return [item for item in output.decode("utf-8").splitlines() if item]


def select(files):
    suites = set()
    for path in files:
        for patterns, suite in MAPPINGS:
            if any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
                suites.update(suite.split())
    if not suites:
        suites.add("tests.vnext.test_vnext_runtime")
    return sorted(suites)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--repo-root", default=os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
    args = parser.parse_args()
    files = changed_files(args.repo_root, args.base)
    result = {"changed_files": files, "test_modules": select(files)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
