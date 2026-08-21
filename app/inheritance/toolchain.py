"""Parser/toolchain provenance and production preflight helpers."""

from __future__ import absolute_import

import hashlib
import os
import platform
import shlex
import shutil
import subprocess


class ParserToolchainPreflight(object):
    """Describe the parser selected for an inheritance run.

    The dependency-free parser remains useful for deterministic local tests,
    but it is deliberately not labelled production-ready.  A release host
    must provide an explicitly selected external parser command and a verified
    adapter before Gate F can advance.
    """

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
            "production_ready": False,
            "command": parts,
            "version": "",
            "binary_path": "",
            "binary_sha256": "",
            "python": platform.python_version(),
            "platform": platform.platform(),
            "violations": [],
        }
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
        if self.adapter_name == "builtin-conservative":
            result["violations"].append(
                "external parser is present but no verified adapter is wired into the engine"
            )
            if require_external:
                result["status"] = "FAILED"
            return result
        # Merely naming an adapter cannot attest that its AST/function/context
        # contract is actually connected to InheritanceEngine.  Keep the
        # require a future code change to register the adapter integration
        # before this preflight can ever become PASS.
        result["violations"].append(
            "parser adapter '{}' is not registered as an engine integration".format(
                self.adapter_name
            )
        )
        if require_external:
            result["status"] = "FAILED"
        return result


def parser_toolchain_preflight(command=None, adapter_name="builtin-conservative",
                               require_external=False):
    return ParserToolchainPreflight(command, adapter_name).run(
        require_external=require_external
    )
