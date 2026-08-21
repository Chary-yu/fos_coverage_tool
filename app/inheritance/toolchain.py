"""Parser adapter registry and production dependency preflight.

The inheritance engine consumes one small, deterministic parser contract.  The
repository ships a conservative dependency-free implementation for local
tests, while production may select an external helper through the
``coverage-cpp-parser-v1`` JSON protocol.  Merely finding an executable is not
enough: preflight also runs the adapter's protocol smoke test.
"""

from __future__ import absolute_import

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess


PARSER_PROTOCOL_VERSION = "coverage-cpp-parser-v1"
PARSER_OUTPUT_LIMIT = 8 * 1024 * 1024


class ParserAdapterError(RuntimeError):
    """The selected parser did not satisfy the canonical adapter contract."""


class CppParserAdapter(object):
    """Minimal parser interface consumed by :class:`InheritanceEngine`."""

    adapter_name = ""
    external = False

    def analyze(self, text, path=""):
        raise NotImplementedError

    def function_for_line(self, analysis, line_number):
        # The external adapter returns the same FunctionRange objects as the
        # builtin analyzer, so line lookup remains one canonical operation.
        from app.inheritance.cpp_parser import CppSourceAnalyzer
        return CppSourceAnalyzer().function_for_line(analysis, line_number)


class ExternalJsonCppParserAdapter(CppParserAdapter):
    """Run a parser helper using the versioned JSON request contract.

    The helper is invoked as ``<command> --analyze-json`` and receives one JSON
    object on stdin:

    ``{"protocol":"coverage-cpp-parser-v1","path":"...","source":"..."}``

    It must write one JSON object to stdout.  The object may be the analysis
    itself or ``{"protocol": ..., "analysis": ...}``.  Function entries use
    ``scope``, ``name``, ``parameters``, ``qualifiers``, ``trailing_return``,
    ``start_line`` and ``end_line``.  Context maps are keyed by physical line.
    """

    adapter_name = "json-cli-v1"
    external = True

    def __init__(self, command, timeout=30):
        self.command = [str(item) for item in (command or []) if str(item)]
        self.timeout = max(1, int(timeout))
        if not self.command:
            raise ParserAdapterError("external parser command is empty")

    def analyze(self, text, path=""):
        request = {
            "protocol": PARSER_PROTOCOL_VERSION,
            "path": str(path or ""),
            "source": str(text or ""),
        }
        try:
            completed = subprocess.run(
                self.command + ["--analyze-json"],
                input=json.dumps(request, ensure_ascii=False),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ParserAdapterError("parser helper execution failed: {}".format(exc))
        stdout = completed.stdout or ""
        stderr = (completed.stderr or "")[:1024]
        if completed.returncode != 0:
            raise ParserAdapterError(
                "parser helper exited {}: {}".format(completed.returncode, stderr)
            )
        if len(stdout.encode("utf-8")) > PARSER_OUTPUT_LIMIT:
            raise ParserAdapterError("parser helper output exceeds configured limit")
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError) as exc:
            raise ParserAdapterError("parser helper returned invalid JSON: {}".format(exc))
        if not isinstance(payload, dict):
            raise ParserAdapterError("parser helper response must be an object")
        if payload.get("protocol") and payload.get("protocol") != PARSER_PROTOCOL_VERSION:
            raise ParserAdapterError("parser helper protocol version mismatch")
        analysis = payload.get("analysis") if "analysis" in payload else payload
        return _canonicalize_analysis(analysis, path)


def _as_tuple(value, field_name, allow_string=False):
    if value is None:
        return ()
    if allow_string and isinstance(value, str):
        return tuple(value.split())
    if not isinstance(value, (list, tuple)):
        raise ParserAdapterError("{} must be an array".format(field_name))
    return tuple(str(item) for item in value)


def _line_map(value, field_name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ParserAdapterError("{} must be an object keyed by line number".format(field_name))
    result = {}
    for key, item in value.items():
        try:
            line_number = int(key)
        except (TypeError, ValueError):
            raise ParserAdapterError("{} contains a non-numeric line".format(field_name))
        if line_number < 1:
            raise ParserAdapterError("{} contains an invalid line".format(field_name))
        if isinstance(item, (list, tuple)):
            result[line_number] = tuple(str(value) for value in item)
        elif item is None:
            result[line_number] = ()
        else:
            result[line_number] = (str(item),)
    return result


def _canonicalize_analysis(payload, path):
    """Convert helper JSON into the exact objects used by InheritanceEngine."""
    if not isinstance(payload, dict):
        raise ParserAdapterError("parser analysis must be an object")
    from app.inheritance.cpp_parser import FunctionIdentity, FunctionRange

    functions = []
    for item in payload.get("functions") or []:
        if not isinstance(item, dict):
            raise ParserAdapterError("function entry must be an object")
        identity_payload = item.get("identity") if isinstance(item.get("identity"), dict) else item
        name = identity_payload.get("name")
        if not name:
            raise ParserAdapterError("function identity name is required")
        try:
            start_line = int(item.get("start_line"))
            end_line = int(item.get("end_line"))
        except (TypeError, ValueError):
            raise ParserAdapterError("function range must have integer start/end lines")
        if start_line < 1 or end_line < start_line:
            raise ParserAdapterError("function range is invalid")
        identity = FunctionIdentity(
            identity_payload.get("path") or path,
            _as_tuple(identity_payload.get("scope"), "function scope", allow_string=True),
            name,
            _as_tuple(identity_payload.get("parameters"), "function parameters", allow_string=True),
            _as_tuple(identity_payload.get("qualifiers"), "function qualifiers", allow_string=True),
            _as_tuple(identity_payload.get("trailing_return"), "function trailing_return", allow_string=True),
        )
        body_tokens = _as_tuple(item.get("body_tokens"), "function body_tokens", allow_string=True)
        functions.append(FunctionRange(
            identity, start_line, end_line, body_tokens=body_tokens,
            uncertain=bool(item.get("uncertain", False)),
        ))
    return {
        "supported": bool(payload.get("supported", True)),
        "functions": functions,
        "controls": _line_map(payload.get("controls"), "controls"),
        "preprocessor": _line_map(payload.get("preprocessor"), "preprocessor"),
        "macros": _line_map(payload.get("macros"), "macros"),
        "constants": _line_map(payload.get("constants"), "constants"),
        "calls": _line_map(payload.get("calls"), "calls"),
        "lines": list(payload.get("lines") or []),
        "tokens": list(payload.get("tokens") or []),
        "path": str(payload.get("path") or path or ""),
        "uncertain": bool(payload.get("uncertain", False)),
    }


PARSER_ADAPTERS = {
    ExternalJsonCppParserAdapter.adapter_name: ExternalJsonCppParserAdapter,
}


def create_parser_adapter(command=None, adapter_name="builtin-conservative",
                          require_external=False):
    """Create the parser selected by configuration.

    The builtin parser is intentionally returned only for the compatibility
    default.  An explicitly selected external adapter is never silently
    replaced by the builtin implementation.
    """
    name = str(adapter_name or "builtin-conservative")
    if name == "builtin-conservative":
        if require_external:
            raise ParserAdapterError("builtin-conservative cannot satisfy external preflight")
        from app.inheritance.cpp_parser import CppSourceAnalyzer
        return CppSourceAnalyzer()
    adapter_class = PARSER_ADAPTERS.get(name)
    if adapter_class is None:
        raise ParserAdapterError("parser adapter '{}' is not registered".format(name))
    parts = ParserToolchainPreflight._command_parts(command)
    if not parts:
        raise ParserAdapterError("external parser command is not configured")
    return adapter_class(parts)


def parser_from_config(config=None):
    """Build the parser selected by the VNext runtime configuration.

    No parser stanza means the local conservative parser.  Once an external
    command/adapter is declared, startup fails closed unless the same command
    passes both executable and protocol smoke checks.
    """
    config = config or {}
    parser_config = config.get("inheritance_parser") or \
        (config.get("inheritance") or {}).get("parser") or {}
    if not isinstance(parser_config, dict):
        raise ParserAdapterError("inheritance parser configuration must be an object")
    adapter_name = str(
        parser_config.get("adapter") or
        os.environ.get("COVERAGE_CPP_PARSER_ADAPTER") or
        "builtin-conservative"
    )
    command = parser_config.get("command") or os.environ.get("COVERAGE_CPP_PARSER", "")
    require_external = bool(parser_config.get("require_external"))
    if adapter_name == "builtin-conservative" and not command and not require_external:
        return create_parser_adapter(adapter_name=adapter_name)
    if adapter_name == "builtin-conservative" and command:
        raise ParserAdapterError(
            "an external parser command requires a registered adapter name"
        )
    result = parser_toolchain_preflight(
        command=command, adapter_name=adapter_name, require_external=True
    )
    if result.get("status") != "PASSED":
        raise ParserAdapterError(
            "configured parser failed preflight: {}".format(
                "; ".join(result.get("violations") or [])
            )
        )
    return create_parser_adapter(
        command=command, adapter_name=adapter_name, require_external=True
    )


class ParserToolchainPreflight(object):
    """Describe and smoke-test the parser selected for an inheritance run."""

    def __init__(self, command=None, adapter_name="builtin-conservative"):
        self.command = command
        self.adapter_name = str(adapter_name or "builtin-conservative")

    @staticmethod
    def _command_parts(command):
        if command is None:
            command = os.environ.get("COVERAGE_CPP_PARSER", "")
        if isinstance(command, (list, tuple)):
            return [str(item) for item in command if str(item)]
        return shlex.split(str(command or ""))

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def run(self, require_external=False):
        parts = self._command_parts(self.command)
        result = {
            "status": "INCOMPLETE",
            "backend": self.adapter_name,
            "protocol": PARSER_PROTOCOL_VERSION if self.adapter_name in PARSER_ADAPTERS else "",
            "production_ready": False,
            "command": parts,
            "version": "",
            "binary_path": "",
            "binary_sha256": "",
            "smoke_test": "NOT_RUN",
            "python": platform.python_version(),
            "platform": platform.platform(),
            "violations": [],
        }
        if self.adapter_name == "builtin-conservative":
            if parts:
                binary = shutil.which(parts[0])
                if not binary:
                    result["status"] = "FAILED"
                    result["violations"].append("parser executable was not found")
                    return result
                result["binary_path"] = os.path.realpath(binary)
                if not os.access(result["binary_path"], os.X_OK):
                    result["status"] = "FAILED"
                    result["violations"].append("parser executable is not executable")
                    return result
                try:
                    result["binary_sha256"] = self._sha256(result["binary_path"])
                    completed = subprocess.check_output(
                        parts + ["--version"], stderr=subprocess.STDOUT,
                        universal_newlines=True, timeout=15,
                    )
                    result["version"] = (completed or "").splitlines()[0][:512]
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    result["status"] = "FAILED"
                    result["violations"].append("parser --version failed: {}".format(exc))
                    return result
                result["violations"].append(
                    "builtin-conservative is not a production external adapter"
                )
            else:
                result["violations"].append("external parser command is not configured")
            if require_external:
                result["status"] = "FAILED"
            return result
        if self.adapter_name not in PARSER_ADAPTERS:
            result["violations"].append(
                "parser adapter '{}' is not registered".format(self.adapter_name)
            )
            if require_external:
                result["status"] = "FAILED"
            return result
        if not parts:
            result["violations"].append("external parser command is not configured")
            if require_external:
                result["status"] = "FAILED"
            return result
        binary = shutil.which(parts[0])
        if not binary:
            result["status"] = "FAILED"
            result["violations"].append("parser executable was not found")
            return result
        result["binary_path"] = os.path.realpath(binary)
        if not os.access(result["binary_path"], os.X_OK):
            result["status"] = "FAILED"
            result["violations"].append("parser executable is not executable")
            return result
        try:
            result["binary_sha256"] = self._sha256(result["binary_path"])
            completed = subprocess.check_output(
                parts + ["--version"], stderr=subprocess.STDOUT,
                universal_newlines=True, timeout=15,
            )
            result["version"] = (completed or "").splitlines()[0][:512]
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            result["status"] = "FAILED"
            result["violations"].append("parser --version failed: {}".format(exc))
            return result
        try:
            adapter = create_parser_adapter(parts, self.adapter_name, require_external=True)
            analysis = adapter.analyze(
                "int preflight_function() { return 0; }\n", "preflight.c"
            )
            if not analysis.get("supported") or not analysis.get("functions"):
                raise ParserAdapterError("smoke analysis did not return a function range")
            if not adapter.function_for_line(analysis, 1):
                raise ParserAdapterError("smoke analysis function range is not queryable")
            result["smoke_test"] = "PASSED"
        except (OSError, ParserAdapterError, TypeError, ValueError) as exc:
            result["status"] = "FAILED" if require_external else "INCOMPLETE"
            result["violations"].append("parser adapter smoke test failed: {}".format(exc))
            return result
        result["status"] = "PASSED"
        result["production_ready"] = True
        return result


def parser_toolchain_preflight(command=None, adapter_name="builtin-conservative",
                               require_external=False):
    return ParserToolchainPreflight(command, adapter_name).run(
        require_external=require_external
    )
