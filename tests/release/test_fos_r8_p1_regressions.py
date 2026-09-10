import errno
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from scripts.release import fos_r8_conductor as conductor
from scripts.upgrade.run_upgrade import UpgradeOrchestrator


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def _write_json(path, payload):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


class FOSR8P1RegressionTest(unittest.TestCase):
    def test_oneclick_prefers_production_python_path_but_keeps_override_and_version_gate(self):
        with open(
                os.path.join(ROOT, "scripts", "release",
                             "build_oneclick_release.py"),
                encoding="utf-8") as stream:
            source = stream.read()
        self.assertIn(
            'PYTHON_BIN=\\"${FOS_R8_PYTHON:-/usr/bin/python3}\\"',
            source,
        )
        self.assertIn("sys.version_info[:2] == (3, 6)", source)

    def _read_until(self, master, process, output, marker, timeout=8):
        marker = marker if isinstance(marker, bytes) else marker.encode("utf-8")
        deadline = time.time() + timeout
        while marker not in output:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.fail(
                    "timed out waiting for {!r}; output={!r}".format(
                        marker, output.decode("utf-8", "replace")
                    )
                )
            ready, _, _ = select.select([master], [], [], min(0.25, remaining))
            if not ready:
                if process.poll() is not None:
                    self.fail(
                        "interactive child exited before {!r}; output={!r}".format(
                            marker, output.decode("utf-8", "replace")
                        )
                    )
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output += chunk
        self.assertIn(marker, output)
        return output

    def test_interactive_child_streams_url_and_reads_browser_ready_from_parent_tty(self):
        driver = textwrap.dedent(
            """
            import os
            import sys
            from unittest import mock

            sys.path.insert(0, {root!r})
            from scripts.release.fos_r8_conductor import _interactive_command
            from scripts.upgrade.run_upgrade import UpgradeOrchestrator

            orchestrator = UpgradeOrchestrator(repo_root={root!r})
            orchestrator._upgrade_mode = "production"
            orchestrator.manifest = mock.Mock()
            identity = {{"commit_sha": "a" * 40}}
            command = [
                sys.executable, "-c",
                "from scripts.upgrade.run_upgrade import UpgradeOrchestrator; "
                "from unittest import mock; "
                "o=UpgradeOrchestrator(repo_root={root!r}); "
                "o._upgrade_mode='production'; o.manifest=mock.Mock(); "
                "i={{'commit_sha':'a'*40}}; "
                "b=o._require_operator_browser_observation({{'operator_browser_pause':True, 'candidate_browser_url':'https://candidate.example/report.html'}}, i); "
                "print('BROWSER_RESULT='+str(b), flush=True); "
                "a=o._require_production_mutation_confirmation({{'require_apply_confirmation':True}}, i); "
                "print('APPLY_RESULT='+str(a), flush=True)"
            ]
            code, _, _ = _interactive_command(command)
            print("DRIVER_CODE={{}}".format(code), flush=True)
            """.format(root=ROOT)
        )
        master, slave = pty.openpty()
        process = None
        output = b""
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", driver],
                cwd=ROOT,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
            )
            os.close(slave)
            output = self._read_until(
                master, process, output, "Candidate browser URL:"
            )
            self.assertIsNone(process.poll())
            os.write(master, b"BROWSER READY\n")
            output = self._read_until(
                master, process, output, "PRE_CUTOVER_READY is complete"
            )
            self.assertNotIn(b"APPLY_RESULT=", output)
            os.write(master, b"APPLY R8\n")
            output = self._read_until(master, process, output, "APPLY_RESULT=True")
            self.assertIn(b"BROWSER_RESULT=True", output)
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def test_wrong_apply_confirmation_keeps_mutation_none_and_never_enters_phase_d(self):
        with tempfile.TemporaryDirectory(prefix="r8-p1-confirmation-") as root:
            orchestrator = UpgradeOrchestrator(repo_root=root)
            orchestrator._upgrade_mode = "production"
            orchestrator.manifest = mock.Mock()
            with mock.patch("builtins.input", return_value="not APPLY R8"):
                confirmed = orchestrator._require_production_mutation_confirmation(
                    {"require_apply_confirmation": True},
                    {"commit_sha": "a" * 40},
                )
            self.assertFalse(confirmed)
            self.assertFalse(orchestrator._phase_d_entered)
            evidence = orchestrator.manifest.record.call_args[0][1]
            self.assertEqual(evidence["production_mutation"], "NONE")
            self.assertFalse(evidence["phase_d_entered"])

    def test_authoritative_status_stays_before_phase_d_until_apply_evidence(self):
        with tempfile.TemporaryDirectory(prefix="r8-p1-status-") as root:
            evidence_root = os.path.join(root, "evidence")
            os.makedirs(evidence_root)
            _write_json(os.path.join(root, "upgrade-state.json"), {
                "state": "PRE_CUTOVER_READY",
            })
            _write_json(
                os.path.join(evidence_root, "production_evidence_manifest.json"),
                {
                    "production_mutation_boundary": {
                        "status": "FAILED",
                        "production_mutation": "NONE",
                    }
                },
            )
            before = conductor._authoritative_phase_d_state(root, evidence_root)
            self.assertFalse(before["phase_d_authorized"])
            self.assertFalse(conductor.status(root).get("phase") == "D")

            _write_json(os.path.join(root, "upgrade-state.json"), {
                "state": "CUTTING_OVER",
            })
            _write_json(
                os.path.join(evidence_root, "production_evidence_manifest.json"),
                {
                    "production_mutation_boundary": {
                        "status": "PASSED",
                        "production_mutation": "AUTHORIZED",
                        "confirmation": "APPLY R8",
                    }
                },
            )
            after = conductor._authoritative_phase_d_state(root, evidence_root)
            self.assertTrue(after["phase_d_authorized"])
            self.assertTrue(after["apply_authorized"])

    def test_phase_d_failure_uses_authoritative_evidence_for_rollback_classification(self):
        with tempfile.TemporaryDirectory(prefix="r8-p1-rollback-") as root:
            evidence_root = os.path.join(root, "evidence")
            os.makedirs(evidence_root)
            _write_json(os.path.join(root, "upgrade-state.json"), {
                "state": "FAILED_CUTOVER_FREEZE_DRAIN_STOP_FAILED",
            })
            _write_json(
                os.path.join(evidence_root, "production_evidence_manifest.json"),
                {
                    "production_mutation_boundary": {
                        "status": "PASSED",
                        "production_mutation": "AUTHORIZED",
                        "confirmation": "APPLY R8",
                    },
                    "file_cutover": {"status": "FAILED"},
                },
            )
            classification = conductor._authoritative_phase_d_state(
                root, evidence_root
            )
            self.assertTrue(classification["phase_d_authorized"])
            with open(
                    os.path.join(ROOT, "scripts", "release", "fos_r8_conductor.py"),
                    encoding="utf-8") as stream:
                self.assertNotIn('"Phase D" in output', stream.read())


if __name__ == "__main__":
    unittest.main()
