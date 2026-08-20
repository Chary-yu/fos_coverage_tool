"""Small process controller used by the isolated local staging rehearsal.

It starts the real ``enhance_coverage.py server`` with the supplied staging
configuration.  Production deployments must provide their own process
supervisor commands through the same lifecycle interface.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def _read_pid(path):
    with open(path, "r", encoding="utf-8") as stream:
        return int(stream.read().strip())


def start(config_path, pid_path, endpoint):
    if os.path.isfile(pid_path):
        try:
            os.kill(_read_pid(pid_path), 0)
            return
        except (OSError, ValueError):
            pass
    env = dict(os.environ)
    env["COVERAGE_CONFIG_PATH"] = os.path.abspath(config_path)
    log_path = pid_path + ".log"
    os.makedirs(os.path.dirname(os.path.abspath(pid_path)), exist_ok=True)
    log_stream = open(log_path, "ab")
    process = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "enhance_coverage.py"), "server"],
        cwd=ROOT, env=env, stdout=log_stream, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with open(pid_path, "w", encoding="utf-8") as stream:
        stream.write(str(process.pid))
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("staging API did not become ready; see {}".format(log_path))


def stop(pid_path):
    if not os.path.isfile(pid_path):
        return
    pid = _read_pid(pid_path)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "freeze", "drain", "open"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:19528/api/coverage/release")
    args = parser.parse_args()
    if args.action == "start":
        start(args.config, args.pid_file, args.endpoint)
    elif args.action == "stop":
        stop(args.pid_file)
    # freeze/open are enforced by the shared marker in UpgradeLifecycle;
    # these commands are explicit lifecycle acknowledgements.
    elif args.action in ("freeze", "drain", "open"):
        return


if __name__ == "__main__":
    main()
