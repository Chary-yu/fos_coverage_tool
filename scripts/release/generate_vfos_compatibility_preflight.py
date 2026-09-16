#!/usr/bin/env python3
"""Generate the one-shot, read-only VFOS compatibility preflight.

The generated TXT is intentionally independent of GitHub and the network. It
is a human-carried target-runtime probe: it verifies the pinned source
checkout, gathers host/runtime facts, and performs the real Python 3.6
syntax/import gate before allowing isolated Candidate staging. It has no
publication, cutover, service, database, or configuration mutation path.
"""

from __future__ import print_function

import argparse
import hashlib
import os
import re
import shlex
import stat
import sys
import textwrap


SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
DEFAULT_CANDIDATE_PARENT = "/home/zcyu/coverage_candidate"


_PREFLIGHT_TEMPLATE = r'''#!/usr/bin/env bash
set -u
umask 077

# FOS VFOS compatibility preflight: read-only facts and isolated checks only.
# This file never contacts GitHub/the Internet, opens a database connection,
# publishes a Candidate, changes CURRENT/Nginx/systemd, replaces files, or
# starts/stops/restarts a service.
PREFLIGHT_SCHEMA_VERSION=1
PRECHECK_MODE=READ_ONLY
EXPECTED_SOURCE_SHA=@@SOURCE_SHA@@
EXPECTED_SOURCE_TREE=@@SOURCE_TREE@@
EXPECTED_HOST=@@EXPECTED_HOST@@
EXPECTED_CANDIDATE_PARENT=@@CANDIDATE_PARENT@@
PYTHON36_BIN="${FOS_VFOS_PYTHON36_BIN:-python3.6}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)
SOURCE_ROOT="${FOS_VFOS_SOURCE_ROOT:-$SCRIPT_DIR}"
CANDIDATE_ROOT="${FOS_VFOS_CANDIDATE_ROOT:-$EXPECTED_CANDIDATE_PARENT}"
APPLICATION_ROOT="${FOS_VFOS_APPLICATION_ROOT:-$SOURCE_ROOT}"
FAILURES=0

usage() {
  printf '%s\n' "usage: bash $0 [--source-root PATH] [--candidate-root PATH] [--application-root PATH] [--python36-bin PATH]"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-root)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      SOURCE_ROOT="$2"
      shift 2
      ;;
    --candidate-root)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      CANDIDATE_ROOT="$2"
      shift 2
      ;;
    --application-root)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      APPLICATION_ROOT="$2"
      shift 2
      ;;
    --python36-bin)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      PYTHON36_BIN="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

record() {
  printf '%s=%s\n' "$1" "$2"
}

one_line() {
  tr '\n' ' ' | sed 's/[[:space:]][[:space:]]*/ /g'
}

pass_check() {
  record "CHECK_$1" "PASS"
}

fail_check() {
  FAILURES=$((FAILURES + 1))
  record "CHECK_$1" "FAIL"
  record "BLOCKER_$1" "$2"
}

not_present() {
  record "CHECK_$1" "NOT_PRESENT"
  record "DETAIL_$1" "$2"
}

finish_python_fail() {
  record "PYTHON36_COMPAT" "FAIL"
  record "READY_FOR_ISOLATED_CANDIDATE_STAGING" "NO"
  record "READY_FOR_PUBLICATION" "NO"
  record "READY_FOR_CUTOVER" "NO"
  record "PRODUCTION_READY" "NO"
  record "PRECHECK_STATUS" "FAIL"
  record "PRODUCTION_MUTATION" "NONE"
  exit 1
}

record "PRODUCTION_MUTATION" "NONE"
record "GITHUB_ACCESS" "NONE"
record "NETWORK_ACCESS" "NONE"
record "DATABASE_CONNECTIONS" "NONE"
record "PUBLICATION" "NOT_PERFORMED"
record "CUTOVER" "NOT_PERFORMED"
record "CURRENT_CHANGE" "NOT_PERFORMED"
record "SERVICE_CONTROL" "NOT_PERFORMED"
record "SOURCE_ROOT" "$SOURCE_ROOT"
record "CANDIDATE_ROOT" "$CANDIDATE_ROOT"
record "APPLICATION_ROOT" "$APPLICATION_ROOT"
record "HOST_EXPECTED" "$EXPECTED_HOST"
record "SOURCE_SHA_EXPECTED" "$EXPECTED_SOURCE_SHA"
record "SOURCE_TREE_EXPECTED" "$EXPECTED_SOURCE_TREE"

record "PYTHON36_GATE" "BEGIN"
if ! command -v "$PYTHON36_BIN" >/dev/null 2>&1; then
  record "PYTHON36_EXECUTABLE" "MISSING"
  finish_python_fail
fi
record "PYTHON36_EXECUTABLE" "$PYTHON36_BIN"
PYTHON_VERSION=$("$PYTHON36_BIN" -c 'import sys; print("%s.%s" % sys.version_info[:2])' 2>/dev/null)
if [ "$PYTHON_VERSION" != "3.6" ]; then
  record "PYTHON36_VERSION" "${PYTHON_VERSION:-UNAVAILABLE}"
  finish_python_fail
fi
record "PYTHON36_VERSION" "$PYTHON_VERSION"

PYTHON36_CHECK_OUTPUT=$(PYTHONPATH="$SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON36_BIN" - "$SOURCE_ROOT" <<'PYTHON36_PREFLIGHT'
import importlib
import os
import sys

root = sys.argv[1]
python_files = (
    "app/release_trust_policy.py",
    "app/candidate_artifact.py",
    "app/release_publication.py",
    "scripts/release/release_input_manifest.py",
    "scripts/release/compatibility_surface.py",
    "scripts/release/flat_candidate_operator_runtime.py",
    "scripts/release/build_production_candidate_artifact.py",
    "scripts/release/build_flat_production_candidate_artifact.py",
    "scripts/release/publish_release.py",
    "scripts/upgrade/run_upgrade.py",
)
python_modules = (
    "app.release_trust_policy",
    "app.candidate_artifact",
    "app.release_publication",
    "scripts.release.release_input_manifest",
    "scripts.release.compatibility_surface",
    "scripts.release.flat_candidate_operator_runtime",
    "scripts.release.build_production_candidate_artifact",
    "scripts.release.build_flat_production_candidate_artifact",
    "scripts.release.publish_release",
    "scripts.upgrade.run_upgrade",
)

for relative_path in python_files:
    path = os.path.join(root, relative_path)
    with open(path, "rb") as source:
        compile(source.read(), path, "exec")

import pymysql
print("PYMYSQL_VERSION=%s" % getattr(pymysql, "__version__", "UNKNOWN"))
print("PYTHON36_DEPENDENCY_IMPORT=PASS")
for module_name in python_modules:
    importlib.import_module(module_name)
    print("PYTHON36_IMPORT=%s:PASS" % module_name)
print("PYTHON36_ENTRYPOINT_IMPORT=PASS")
PYTHON36_PREFLIGHT
)
PYTHON36_EXIT=$?
if [ "$PYTHON36_EXIT" -ne 0 ]; then
  printf '%s\n' "$PYTHON36_CHECK_OUTPUT"
  finish_python_fail
fi
printf '%s\n' "$PYTHON36_CHECK_OUTPUT"
record "PYTHON36_COMPAT" "PASS"
record "PYTHON36_GATE" "PASS"

record "HOST_OBSERVED" "$(hostname 2>/dev/null | one_line)"
if [ "$(hostname 2>/dev/null | one_line)" = "$EXPECTED_HOST" ]; then
  pass_check "HOST_IDENTITY"
else
  fail_check "HOST_IDENTITY" "hostname does not match the pinned production host"
fi

if command -v uname >/dev/null 2>&1; then
  record "OS_ARCHITECTURE" "$(uname -srm 2>/dev/null | one_line)"
  pass_check "OS_ARCHITECTURE"
else
  fail_check "OS_ARCHITECTURE" "uname is unavailable"
fi

check_directory() {
  LABEL="$1"
  PATH_VALUE="$2"
  REQUIRED="$3"
  if [ -L "$PATH_VALUE" ]; then
    fail_check "$LABEL" "path is a symlink; preflight requires a direct directory"
    return
  fi
  if [ -d "$PATH_VALUE" ]; then
    RESOLVED=$(CDPATH= cd -- "$PATH_VALUE" 2>/dev/null && pwd -P)
    record "${LABEL}_RESOLVED" "$RESOLVED"
    pass_check "$LABEL"
  elif [ "$REQUIRED" = "YES" ]; then
    fail_check "$LABEL" "required directory is absent"
  else
    not_present "$LABEL" "optional directory is not present before Candidate staging"
  fi
}

check_directory "SOURCE_ROOT" "$SOURCE_ROOT" "YES"
check_directory "CANDIDATE_ROOT_PARENT" "$(dirname -- "$CANDIDATE_ROOT")" "YES"
check_directory "CANDIDATE_ROOT" "$CANDIDATE_ROOT" "NO"
check_directory "APPLICATION_ROOT" "$APPLICATION_ROOT" "YES"

if command -v git >/dev/null 2>&1 && [ -d "$SOURCE_ROOT" ]; then
  SOURCE_HEAD=$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null | tr '[:upper:]' '[:lower:]')
  SOURCE_TREE=$(git -C "$SOURCE_ROOT" rev-parse HEAD^{tree} 2>/dev/null | tr '[:upper:]' '[:lower:]')
  record "SOURCE_SHA_OBSERVED" "$SOURCE_HEAD"
  record "SOURCE_TREE_OBSERVED" "$SOURCE_TREE"
  if [ "$SOURCE_HEAD" = "$EXPECTED_SOURCE_SHA" ] && \
     [ "$SOURCE_TREE" = "$EXPECTED_SOURCE_TREE" ]; then
    pass_check "SOURCE_IDENTITY"
  else
    fail_check "SOURCE_IDENTITY" "checkout does not match the immutable source identity"
  fi
  SOURCE_STATUS=$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=all 2>/dev/null)
  if [ -z "$SOURCE_STATUS" ]; then
    pass_check "SOURCE_CLEAN"
  else
    record "SOURCE_STATUS" "$(printf '%s' "$SOURCE_STATUS" | one_line)"
    fail_check "SOURCE_CLEAN" "source checkout is dirty"
  fi
else
  fail_check "SOURCE_IDENTITY" "git or source checkout is unavailable"
  fail_check "SOURCE_CLEAN" "source checkout is unavailable"
fi

if command -v git >/dev/null 2>&1; then
  record "GIT_VERSION" "$(git --version 2>&1 | one_line)"
  pass_check "GIT_VERSION"
else
  fail_check "GIT_VERSION" "git is unavailable"
fi

if command -v mariadb >/dev/null 2>&1; then
  DATABASE_CLIENT=mariadb
elif command -v mysql >/dev/null 2>&1; then
  DATABASE_CLIENT=mysql
else
  DATABASE_CLIENT=""
fi
if [ -n "$DATABASE_CLIENT" ]; then
  record "MARIADB_MYSQL_VERSION" "$($DATABASE_CLIENT --version 2>&1 | one_line)"
  pass_check "MARIADB_MYSQL_VERSION"
else
  record "MARIADB_MYSQL_VERSION" "NOT_VERIFIED"
  fail_check "MARIADB_MYSQL_VERSION" "mariadb/mysql client is unavailable; no database connection was attempted"
fi

if command -v nginx >/dev/null 2>&1; then
  record "NGINX_VERSION" "$(nginx -v 2>&1 | one_line)"
  pass_check "NGINX_INFO"
else
  record "NGINX_VERSION" "NOT_VERIFIED"
  fail_check "NGINX_INFO" "nginx executable is unavailable"
fi

if command -v systemctl >/dev/null 2>&1; then
  record "SYSTEMD_VERSION" "$(systemctl --version 2>&1 | one_line)"
  pass_check "SYSTEMD_INFO"
else
  record "SYSTEMD_VERSION" "NOT_VERIFIED"
  fail_check "SYSTEMD_INFO" "systemctl is unavailable"
fi

if command -v locale >/dev/null 2>&1; then
  LOCALE_CHARMAP=$(locale charmap 2>/dev/null | one_line)
  record "LOCALE_CHARMAP" "$LOCALE_CHARMAP"
  case "$LOCALE_CHARMAP" in
    *UTF-8*|*utf-8*) pass_check "LOCALE_UTF8" ;;
    *) fail_check "LOCALE_UTF8" "locale charmap is not UTF-8" ;;
  esac
else
  fail_check "LOCALE_UTF8" "locale command is unavailable"
fi

if command -v df >/dev/null 2>&1 && [ -d "$SOURCE_ROOT" ]; then
  DISK_FACTS=$(df -Pk "$SOURCE_ROOT" 2>&1 | one_line)
  record "DISK_FACTS" "$DISK_FACTS"
  if [ -n "$DISK_FACTS" ]; then
    pass_check "DISK_FACTS"
  else
    fail_check "DISK_FACTS" "disk facts are unavailable"
  fi
else
  fail_check "DISK_FACTS" "df or source path is unavailable"
fi

if command -v free >/dev/null 2>&1; then
  MEMORY_FACTS=$(free -b 2>&1 | one_line)
  record "MEMORY_FACTS" "$MEMORY_FACTS"
  pass_check "MEMORY_FACTS"
elif [ -r /proc/meminfo ]; then
  MEMORY_FACTS=$(sed -n '1,3p' /proc/meminfo | one_line)
  record "MEMORY_FACTS" "$MEMORY_FACTS"
  pass_check "MEMORY_FACTS"
else
  fail_check "MEMORY_FACTS" "memory facts are unavailable"
fi

record "PRODUCTION_MUTATION" "NONE"
record "GITHUB_ACCESS" "NONE"
record "NETWORK_ACCESS" "NONE"
record "DATABASE_CONNECTIONS" "NONE"
record "PUBLICATION" "NOT_PERFORMED"
record "CUTOVER" "NOT_PERFORMED"
record "CURRENT_CHANGE" "NOT_PERFORMED"
record "SERVICE_CONTROL" "NOT_PERFORMED"
if [ "$FAILURES" -eq 0 ]; then
  record "PRECHECK_STATUS" "PASS"
  record "READY_FOR_ISOLATED_CANDIDATE_STAGING" "YES"
else
  record "PRECHECK_STATUS" "FAIL"
  record "READY_FOR_ISOLATED_CANDIDATE_STAGING" "NO"
fi
record "READY_FOR_PUBLICATION" "NO"
record "READY_FOR_CUTOVER" "NO"
record "PRODUCTION_READY" "NO"
if [ "$FAILURES" -eq 0 ]; then
  exit 0
fi
exit 1
'''


def _exact_sha(value, label):
    value = str(value or "").strip().lower()
    if not SHA40.fullmatch(value) or value == "0" * 40:
        raise ValueError("{} must be a concrete 40-character SHA".format(label))
    return value


def _safe_id(value, label):
    value = str(value or "").strip()
    if not SAFE_ID.fullmatch(value):
        raise ValueError("{} contains unsafe characters".format(label))
    return value


def _quote(value):
    return shlex.quote(str(value))


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render(source_sha, source_tree, expected_host, candidate_parent):
    """Return a deterministic standalone preflight script."""
    source_sha = _exact_sha(source_sha, "source SHA")
    source_tree = _exact_sha(source_tree, "source tree")
    expected_host = _safe_id(expected_host, "expected host")
    candidate_parent = str(candidate_parent or "").strip()
    if not os.path.isabs(candidate_parent) or "\x00" in candidate_parent:
        raise ValueError("candidate parent must be an absolute safe path")
    if ".." in candidate_parent.replace("\\", "/").split("/"):
        raise ValueError("candidate parent contains path traversal")
    return textwrap.dedent(_PREFLIGHT_TEMPLATE).replace(
        "@@SOURCE_SHA@@", _quote(source_sha)
    ).replace(
        "@@SOURCE_TREE@@", _quote(source_tree)
    ).replace(
        "@@EXPECTED_HOST@@", _quote(expected_host)
    ).replace(
        "@@CANDIDATE_PARENT@@", _quote(candidate_parent)
    )


def generate(output_path, source_sha, source_tree, expected_host="vfoswind",
             candidate_parent=DEFAULT_CANDIDATE_PARENT):
    """Write one new preflight file and return its immutable file facts."""
    output_path = os.path.abspath(str(output_path))
    if os.path.lexists(output_path):
        raise ValueError("output already exists: {}".format(output_path))
    parent = os.path.dirname(output_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    content = render(source_sha, source_tree, expected_host, candidate_parent)
    with open(output_path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.chmod(output_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return {
        "path": output_path,
        "sha256": _sha256(output_path),
        "size": os.path.getsize(output_path),
        "source_sha": _exact_sha(source_sha, "source SHA"),
        "source_tree": _exact_sha(source_tree, "source tree"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a standalone read-only VFOS compatibility preflight."
    )
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--expected-host", default="vfoswind")
    parser.add_argument(
        "--candidate-parent", default=DEFAULT_CANDIDATE_PARENT,
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = generate(
            args.output,
            args.source_sha,
            args.source_tree,
            expected_host=args.expected_host,
            candidate_parent=args.candidate_parent,
        )
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print("VFOS_PREFLIGHT_GENERATION=PASS")
    for key in sorted(result):
        print("{}={}".format(key.upper(), result[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
