"""Run the repository-local deterministic inheritance corpus.

The corpus is a correctness smoke test, not production evidence.  It uses the
same line mapper, parser, dependency index and comparison engine as VNext,
then emits stable JSON so two runs can be compared byte-for-byte.  The output
is deliberately marked synthetic and release-ineligible; target-host parser,
Git and database evidence must still be supplied by the release environment.
"""

from __future__ import absolute_import

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.inheritance.toolchain import (
    PARSER_PROTOCOL_VERSION, ParserAdapterError, create_parser_adapter,
    parser_toolchain_preflight,
)
from app.inheritance.dependencies import SourceAnalysisIndex
from app.inheritance.engine import InheritanceEngine
from app.inheritance.line_map import GitLineMapEngine


DEFAULT_FIXTURE = os.path.join(
    ROOT, "tests", "fixtures", "inheritance_deterministic_corpus.json"
)


def _canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha256_bytes(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_fixture(path):
    with open(path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("deterministic corpus schema_version must be 1")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("deterministic corpus cases are required")
    ids = [str(item.get("case_id") or "") for item in cases]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("deterministic corpus case_id values must be unique")
    return payload


def _files(case, side):
    key = "{}_files".format(side)
    value = case.get(key)
    if value is not None:
        if not isinstance(value, dict) or not value:
            raise ValueError("{} must be a non-empty object".format(key))
        return {str(path): str(text) for path, text in value.items()}
    source = case.get("{}_source".format(side))
    path = str(case.get("path") or "")
    if source is None or not path:
        raise ValueError("{} requires path and source".format(case.get("case_id")))
    return {path: str(source)}


def _line(text, line_number):
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    number = int(line_number)
    if number < 1 or number > len(lines):
        raise ValueError("line number {} is outside source".format(number))
    return lines[number - 1]


def _analysis_bundle(files, analyzer):
    return {path: analyzer.analyze(text, path) for path, text in sorted(files.items())}


def _compare_case(case, analyzer, engine):
    old_files = _files(case, "old")
    new_files = _files(case, "new")
    path = str(case.get("path") or "")
    old_analysis = _analysis_bundle(old_files, analyzer)
    new_analysis = _analysis_bundle(new_files, analyzer)
    if path not in old_files or path not in new_files:
        raise ValueError("compare case path is absent from old/new files")
    old_line_number = int(case.get("old_line"))
    new_line_number = int(case.get("new_line"))
    old_index = SourceAnalysisIndex(old_analysis)
    new_index = SourceAnalysisIndex(new_analysis)
    result = engine.compare_line(
        _line(old_files[path], old_line_number),
        _line(new_files[path], new_line_number),
        old_analysis[path], new_analysis[path],
        old_line_number, new_line_number,
        old_index=old_index, new_index=new_index,
    )
    parser_uncertain = bool(
        old_analysis[path].get("uncertain") or new_analysis[path].get("uncertain")
    )
    expected_parser_uncertain = bool(case.get("parser_uncertain"))
    return {
        "case_id": str(case["case_id"]),
        "kind": "compare",
        "expected_ok": bool(case.get("expected_ok")),
        "expected_reason_code": str(case.get("expected_reason_code") or ""),
        "observed_ok": bool(result.ok),
        "observed_reason_code": str(result.reason_code or ""),
        "parser_uncertain": parser_uncertain,
        "parser_uncertainty_expectation_met": (
            parser_uncertain == expected_parser_uncertain
        ),
        "passed": bool(
            bool(result.ok) == bool(case.get("expected_ok")) and
            str(result.reason_code or "") == str(case.get("expected_reason_code") or "") and
            parser_uncertain == expected_parser_uncertain
        ),
    }


def _line_map_case(case, mapper):
    mapping = mapper.map_text(case.get("old_source", ""), case.get("new_source", ""))
    old_line = int(case.get("old_line"))
    if old_line in mapping.ambiguous:
        reason = "LINE_AMBIGUOUS"
        new_line = None
    elif old_line in mapping.deleted:
        reason = "LINE_DELETED"
        new_line = None
    else:
        new_line = mapping.get(old_line)
        reason = "LINE_MAPPED" if new_line is not None else "LINE_DELETED"
    return {
        "case_id": str(case["case_id"]),
        "kind": "line_map",
        "expected_ok": bool(case.get("expected_ok")),
        "expected_reason_code": str(case.get("expected_reason_code") or ""),
        "observed_ok": new_line is not None,
        "observed_reason_code": reason,
        "observed_new_line": new_line,
        "expected_new_line": case.get("expected_new_line"),
        "mapping_fingerprint": mapping.fingerprint,
        "passed": bool(
            (new_line is not None) == bool(case.get("expected_ok")) and
            reason == str(case.get("expected_reason_code") or "") and
            (case.get("expected_new_line") is None or
             int(case.get("expected_new_line")) == int(new_line or 0))
        ),
    }


def _select_parser(command=None, adapter_name="builtin-conservative",
                   require_external=False):
    """Select the corpus parser and run the production preflight when needed.

    The repository-local parser remains the compatibility/default backend.  A
    requested external backend must pass the same executable, version, binary
    hash and protocol smoke checks used by the runtime; it is never silently
    replaced by the builtin parser.
    """
    adapter_name = str(adapter_name or "builtin-conservative")
    external_requested = bool(require_external or
                              adapter_name != "builtin-conservative")
    preflight = parser_toolchain_preflight(
        command=command,
        adapter_name=adapter_name,
        require_external=external_requested,
    )
    if external_requested and preflight.get("status") != "PASSED":
        raise ParserAdapterError(
            "parser toolchain preflight failed: {}".format(
                "; ".join(preflight.get("violations") or [])
            )
        )
    parser = create_parser_adapter(
        command=command,
        adapter_name=adapter_name,
        require_external=external_requested,
    )
    return parser, preflight, external_requested


def _error_result(fixture, fixture_path, parser_toolchain, error,
                  parser_backend=""):
    case_count = len(fixture.get("cases") or [])
    return {
        "schema_version": 1,
        "corpus_name": fixture.get("name"),
        "fixture_sha256": _sha256_file(fixture_path),
        "status": "FAILED",
        "synthetic": True,
        "release_eligible": False,
        "parser_backend": parser_backend,
        "parser_external": False,
        "parser_protocol": PARSER_PROTOCOL_VERSION,
        "parser_toolchain": parser_toolchain,
        "cases_total": case_count,
        "passed_cases": 0,
        "failed_cases": case_count,
        "errors": [{"case_id": None, "error": str(error)}],
        "reason_counts": {"PARSER_TOOLCHAIN_FAILURE": 1},
        "parser_uncertainty_cases": [],
        "dependency_cases": [],
        "decisions": [],
    }


def run(fixture_path=DEFAULT_FIXTURE, command=None,
        adapter_name="builtin-conservative", require_external=False):
    fixture = _load_fixture(fixture_path)
    try:
        analyzer, parser_toolchain, parser_external = _select_parser(
            command=command,
            adapter_name=adapter_name,
            require_external=require_external,
        )
    except (OSError, ParserAdapterError, TypeError, ValueError) as exc:
        return _error_result(
            fixture, fixture_path,
            parser_toolchain={
                "status": "FAILED",
                "backend": str(adapter_name or "builtin-conservative"),
                "protocol": PARSER_PROTOCOL_VERSION,
                "production_ready": False,
                "violations": [str(exc)],
            },
            error=exc,
            parser_backend=str(adapter_name or "builtin-conservative"),
        )
    engine = InheritanceEngine(parser=analyzer)
    mapper = GitLineMapEngine()
    decisions = []
    errors = []
    for case in fixture["cases"]:
        try:
            kind = str(case.get("kind") or "compare")
            if kind == "compare":
                decisions.append(_compare_case(case, analyzer, engine))
            elif kind == "line_map":
                decisions.append(_line_map_case(case, mapper))
            else:
                raise ValueError("unsupported corpus case kind: {}".format(kind))
        except Exception as exc:
            errors.append({"case_id": case.get("case_id"), "error": str(exc)})
            decisions.append({
                "case_id": case.get("case_id"), "kind": case.get("kind"),
                "passed": False, "error": str(exc),
            })
    decisions.sort(key=lambda item: str(item.get("case_id") or ""))
    failed = [item for item in decisions if not item.get("passed")]
    reason_counts = {}
    for item in decisions:
        reason = str(item.get("observed_reason_code") or "ERROR")
        reason_counts[reason] = int(reason_counts.get(reason) or 0) + 1
    parser_cases = [item for item in decisions if item.get("parser_uncertain")]
    dependency_cases = [item for item in decisions if item.get("case_id", "").startswith(
        "D-CORP-06-callee") or item.get("case_id", "").startswith(
        "D-CORP-07-callee") or item.get("case_id", "").startswith(
        "D-CORP-15-unrelated-callee")]
    return {
        "schema_version": 1,
        "corpus_name": fixture.get("name"),
        "fixture_sha256": _sha256_file(fixture_path),
        "status": "PASSED" if not errors and not failed else "FAILED",
        "synthetic": not parser_external,
        "release_eligible": False,
        "parser_backend": str(getattr(analyzer, "adapter_name", "") or
                               ("external" if parser_external else
                                "builtin-conservative")),
        "parser_external": bool(parser_external),
        "parser_protocol": PARSER_PROTOCOL_VERSION,
        "parser_toolchain": parser_toolchain,
        "cases_total": len(decisions),
        "passed_cases": len(decisions) - len(failed),
        "failed_cases": len(failed),
        "errors": errors,
        "reason_counts": dict(sorted(reason_counts.items())),
        "parser_uncertainty_cases": [item["case_id"] for item in parser_cases],
        "dependency_cases": [item["case_id"] for item in dependency_cases],
        "decisions": decisions,
    }


def derived_reports(result):
    decisions = result.get("decisions") or []
    false_positives = [item for item in decisions
                       if item.get("expected_ok") is False and item.get("observed_ok")]
    parser_cases = [item for item in decisions if item.get("parser_uncertain")]
    dependency_cases = [item for item in decisions if item.get("case_id", "").startswith(
        "D-CORP-06-callee") or item.get("case_id", "").startswith(
        "D-CORP-07-callee") or item.get("case_id", "").startswith(
        "D-CORP-15-unrelated-callee")]
    return {
        "false_positive_check": {
            "status": "PASSED" if not false_positives else "FAILED",
            "synthetic": bool(result.get("synthetic", True)),
            "release_eligible": False,
            "known_false_positive_count": len(false_positives),
            "cases": false_positives,
        },
        "parser_uncertainty_report": {
            "status": "PASSED" if all(
                not item.get("observed_ok") for item in parser_cases
            ) else "FAILED",
            "synthetic": bool(result.get("synthetic", True)),
            "release_eligible": False,
            "parser_backend": result.get("parser_backend", ""),
            "parser_external": bool(result.get("parser_external", False)),
            "parser_toolchain": result.get("parser_toolchain") or {},
            "uncertain_case_count": len(parser_cases),
            "cases": parser_cases,
        },
        "dependency_resolution_report": {
            "status": "PASSED" if all(item.get("passed") for item in dependency_cases)
            else "FAILED",
            "synthetic": bool(result.get("synthetic", True)),
            "release_eligible": False,
            "case_count": len(dependency_cases),
            "cases": dependency_cases,
        },
    }


def write_result(result, output_path):
    directory = os.path.dirname(os.path.abspath(output_path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with open(output_path, "w", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, indent=2, ensure_ascii=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--command", default="",
        help="external parser command; required with a non-builtin adapter",
    )
    parser.add_argument("--adapter", default="builtin-conservative")
    parser.add_argument(
        "--require-external", action="store_true",
        help="fail unless an external parser passes toolchain preflight",
    )
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = run(
        os.path.abspath(args.fixture),
        command=args.command or None,
        adapter_name=args.adapter,
        require_external=args.require_external,
    )
    if args.output:
        write_result(result, args.output)
    print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
