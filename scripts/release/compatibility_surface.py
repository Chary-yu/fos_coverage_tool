#!/usr/bin/env python3
"""Classify release changes that require legacy target-runtime checks.

The classifier compares two exact Git commits.  It deliberately classifies
the changed production surface rather than guessing from the host runtime:
production Python changes require the Python 3.6 gate, while MariaDB 5.5 is
only relevant for an explicitly DB-sensitive path or changed DB operation.
"""

from __future__ import print_function

import os
import re
import subprocess


SCHEMA_VERSION = 1
SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")

# These are the Python files that can be carried into a production Candidate
# or executed by the production application/runtime.  Local-only tests and
# diagnostics are intentionally outside this set.
_PRODUCTION_PYTHON_ROOTS = (
    "app/",
    "scripts/maintenance/",
    "scripts/release/",
    "scripts/upgrade/",
)
_PRODUCTION_PYTHON_FILES = (
    "code_detail_service.py",
    "code_region.py",
    "coverage_check.py",
    "enhance_coverage.py",
    "source_reader.py",
)
_LOCAL_ONLY_PYTHON_PATHS = (
    # These programs run only in the repository-local release gate.  They are
    # deliberately not confused with Python carried into a VFOS Candidate.
    "scripts/release/compatibility_surface.py",
    "scripts/release/local_compatibility_gate.py",
    "scripts/release/local_release_gate.py",
    "scripts/release/generate_flat_production_candidate_operator.py",
    "scripts/release/generate_r8_flat_production_candidate_operator.py",
    "scripts/release/generate_vfos_compatibility_preflight.py",
)

# A path in these areas is DB-sensitive even if a particular change is only
# a small implementation detail.  Content matching below catches SQL/driver
# changes in other production files without treating every word "database"
# as a MariaDB compatibility change.
_DB_SENSITIVE_PATH_PREFIXES = (
    "app/db/",
    "scripts/maintenance/mysql_",
    "scripts/diagnostics/mariadb",
    "scripts/diagnostics/mysql",
    "scripts/upgrade/database_",
    "scripts/upgrade/domain_migration.py",
    "scripts/upgrade/migration_",
    "scripts/upgrade/schema_",
)
_DB_SENSITIVE_FILENAMES = (
    "schema.sql",
    "migration.sql",
    "migrations.sql",
)
_DB_SENSITIVE_CONTENT = re.compile(
    r"(?ix)"
    r"(?:\bpymysql\b|\bmysql(?:db)?\b|\bmariadb\b|"
    r"\b(?:grant|revoke)\b|"
    r"\b(?:create|alter|drop)\s+(?:user|database|table|index)\b|"
    r"\b(?:select|insert|update|delete)\s+.+\bfrom\b|"
    r"\bcursor\s*\(|\bconnect\s*\()"
)
_DB_SENSITIVE_CONTENT_EXCLUDED_PATHS = (
    # These files describe or execute disposable compatibility checks.  SQL
    # tokens in them are test inputs, not production database operations.
    "scripts/release/compatibility_surface.py",
    "scripts/release/local_compatibility_gate.py",
    "scripts/release/local_release_gate.py",
)


def _exact_sha(value, label):
    normalized = str(value or "").strip().lower()
    if not SHA40.fullmatch(normalized) or normalized == "0" * 40:
        raise ValueError("{} must be a concrete exact commit SHA".format(label))
    return normalized


def _git(repo_root, *args):
    try:
        output = subprocess.check_output(
            ["git", "-C", repo_root] + list(args),
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "output", b"")
        if not isinstance(detail, str):
            detail = detail.decode("utf-8", "replace")
        raise ValueError("git {} failed: {}".format(
            " ".join(args), detail.strip()[-2000:]
        ))
    return output.decode("utf-8", "replace")


def _verify_commit(repo_root, value, label):
    value = _exact_sha(value, label)
    observed = _git(repo_root, "rev-parse", "--verify", value + "^{commit}").strip().lower()
    if observed != value:
        raise ValueError("{} is not available as the exact requested commit".format(label))
    return value


def _changed_paths(repo_root, baseline_sha, source_sha):
    output = _git(
        repo_root, "diff", "--name-only", "--diff-filter=ACMRTUXB",
        baseline_sha, source_sha, "--",
    )
    return sorted(set(
        line.strip().replace(os.sep, "/")
        for line in output.splitlines() if line.strip()
    ))


def _is_production_python(path):
    path = str(path or "").replace(os.sep, "/")
    return path.endswith(".py") and path not in _LOCAL_ONLY_PYTHON_PATHS and (
        path in _PRODUCTION_PYTHON_FILES or
        path.startswith(_PRODUCTION_PYTHON_ROOTS)
    )


def _is_db_sensitive_path(path):
    path = str(path or "").replace(os.sep, "/")
    basename = path.rsplit("/", 1)[-1].lower()
    return path.startswith(_DB_SENSITIVE_PATH_PREFIXES) or \
        basename in _DB_SENSITIVE_FILENAMES


def _is_db_sensitive_content_path(path):
    path = str(path or "").replace(os.sep, "/")
    return _is_production_python(path) and \
        path not in _DB_SENSITIVE_CONTENT_EXCLUDED_PATHS


def _path_diff(repo_root, baseline_sha, source_sha, path):
    return _git(
        repo_root, "diff", "--unified=0", baseline_sha, source_sha,
        "--", path,
    )


def classify_compatibility_surface(repo_root, source_sha, baseline_sha):
    """Return deterministic Python/MariaDB surface classifications.

    ``source_sha`` and ``baseline_sha`` must both exist in ``repo_root``.
    The result is JSON-safe and contains no runtime verdict; the compatibility
    gate applies the host/target-runtime facts to these surface facts.
    """
    repo_root = os.path.realpath(os.path.abspath(str(repo_root)))
    if not os.path.isdir(repo_root):
        raise ValueError("repository root is not a directory")
    source_sha = _verify_commit(repo_root, source_sha, "source_sha")
    baseline_sha = _verify_commit(repo_root, baseline_sha, "baseline_sha")
    changed_paths = _changed_paths(repo_root, baseline_sha, source_sha)
    python_paths = [path for path in changed_paths if _is_production_python(path)]
    db_paths = []
    for path in changed_paths:
        if _is_db_sensitive_path(path) or (
                _is_db_sensitive_content_path(path) and
                _DB_SENSITIVE_CONTENT.search(
                _path_diff(repo_root, baseline_sha, source_sha, path)
                )):
            db_paths.append(path)

    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit_sha": source_sha,
        "baseline_commit_sha": baseline_sha,
        "changed_paths": changed_paths,
        "python": {
            "status": "PRODUCTION_PYTHON_CHANGED" if python_paths else
            "NOT_PRODUCTION_PYTHON",
            "changed_paths": python_paths,
            "production_execution": bool(python_paths),
        },
        "mariadb": {
            "status": "DB_SENSITIVE_CHANGED" if db_paths else
            "NOT_DB_SENSITIVE",
            "changed_paths": sorted(db_paths),
            "production_database_execution": bool(db_paths),
        },
    }
