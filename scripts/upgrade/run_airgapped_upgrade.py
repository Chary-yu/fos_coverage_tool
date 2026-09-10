#!/usr/bin/env python3
"""Air-gapped vfoswind production conductor.

This thin wrapper preserves the canonical UpgradeOrchestrator. It pauses at
the first log boundary after CANDIDATE_READY, waits for the authenticated
operator browser observation written by the isolated Candidate API, binds two
pre-staged exact-revision performance source artifacts to the current
publication attempt, joins the evidence, and then returns control to the
canonical runner. No public network, browser runtime, or Node.js runtime is
required on vfoswind.
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse

from scripts.upgrade import run_upgrade as core
from scripts.upgrade.airgapped_evidence_join import build_join
from scripts.diagnostics.release_performance_ab import build_release_performance_ab
from scripts.release.current_adoption import current_flat_source_binding


PAUSE_LOG = "[Step 5/10] Executing Targeted Unit Test Suites (Phases 0-6)..."
OPERATOR_OBSERVATION_NAME = "operator_browser_observation.json"
OPERATOR_WORKLOAD_JOIN_NAME = "candidate_browser_join_workload.json"


def _load_json(path, label):
    if not path or os.path.islink(path) or not os.path.isfile(path):
        raise RuntimeError("{} is missing: {}".format(label, path))
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError("{} must be a JSON object".format(label))
    return value


def _resolve_revision_source(repo_root, raw, revision, label):
    value = str(raw or "").strip()
    if not value:
        raise RuntimeError("{} is required".format(label))
    value = value.replace("{commit_sha}", revision)
    if not os.path.isabs(value):
        value = os.path.join(repo_root, value)
    path = os.path.realpath(os.path.abspath(value))
    if os.path.islink(path) or not os.path.isfile(path):
        raise RuntimeError("{} must be a regular pre-staged file: {}".format(label, path))
    return path


def _external_origin(value):
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("Candidate Gateway origin is not an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Candidate Gateway origin may not contain credentials/query/fragment")
    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname or hostname in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError("Candidate Gateway origin must be externally reachable")
    path = str(parsed.path or "").rstrip("/")
    if path:
        raise RuntimeError("Candidate Gateway origin must not include a path")
    return "{}://{}".format(parsed.scheme, parsed.netloc)


def _operator_page_url(candidate_gateway_origin):
    return _external_origin(candidate_gateway_origin) + \
        "/api/coverage/release-validation/page.html"


def _same_origin(url, expected_origin):
    try:
        return _external_origin("{}://{}".format(
            urlparse(str(url or "")).scheme,
            urlparse(str(url or "")).netloc,
        )) == _external_origin(expected_origin)
    except Exception:
        return False


_FLAT_SOURCE_BINDING_FIELDS = (
    "previous_release_commit_sha",
    "legacy_source_root_realpath",
    "legacy_source_tree_sha256",
    "legacy_source_file_count",
    "legacy_source_total_size",
    "legacy_release_identity_sha256",
)


def verify_production_candidate_flat_source_binding(candidate_provenance,
                                                    flat_binding):
    """Bind a production Candidate to deterministic legacy Flat bytes.

    The immutable bootstrap release manifest contains a generated timestamp,
    so its complete tree hash differs across independent safe bootstraps.  The
    Air-Gapped Flat transition instead verifies the stable source tree and
    baseline identity fields carried by the Candidate.  Once CURRENT already
    exists, the canonical full Served Root binding remains unchanged.
    """
    if not isinstance(candidate_provenance, dict) or not isinstance(
            flat_binding, dict):
        raise RuntimeError(
            "production Candidate Flat source binding is unavailable"
        )
    normalized = {}
    errors = []
    for field in _FLAT_SOURCE_BINDING_FIELDS:
        candidate_value = candidate_provenance.get(field)
        actual_value = flat_binding.get(field)
        if field in ("legacy_source_file_count", "legacy_source_total_size"):
            try:
                candidate_value = int(candidate_value)
                actual_value = int(actual_value)
            except (TypeError, ValueError):
                errors.append(field)
                continue
        else:
            candidate_value = str(candidate_value or "").strip()
            actual_value = str(actual_value or "").strip()
            if field != "legacy_source_root_realpath":
                candidate_value = candidate_value.lower()
                actual_value = actual_value.lower()
        if candidate_value in (None, "") or candidate_value != actual_value:
            errors.append(field)
        else:
            normalized[field] = candidate_value
    if errors:
        raise RuntimeError(
            "production Candidate Flat source binding {} does not match the "
            "authoritative Flat deployment selected for this upgrade".format(
                ", ".join(errors)
            )
        )
    return normalized


class AirGappedUpgradeOrchestrator(core.UpgradeOrchestrator):
    def __init__(self, *args, **kwargs):
        super(AirGappedUpgradeOrchestrator, self).__init__(*args, **kwargs)
        self._airgapped_runtime_config = {}
        self._airgapped_mode = ""
        self._airgapped_join_done = False
        self._airgapped_wait_active = False

    def execute_upgrade(self, *args, **kwargs):
        self._airgapped_runtime_config = kwargs.get("runtime_config") or {}
        self._airgapped_mode = str(kwargs.get("mode") or "")
        return super(AirGappedUpgradeOrchestrator, self).execute_upgrade(*args, **kwargs)

    def _configure_release_controls(self, upgrade_config, identity,
                                    previous_release, runtime_config):
        result = super(AirGappedUpgradeOrchestrator, self)._configure_release_controls(
            upgrade_config, identity, previous_release, runtime_config
        )
        if self._deployment_layout == core.FLAT:
            deployment = self._current_adoption_plan or {}
            flat_binding = deployment.get("flat_source_binding") or {}
            candidate_binding = verify_production_candidate_flat_source_binding(
                self.candidate_preflight.get("source_provenance") or {},
                flat_binding,
            )
            self.candidate_preflight["candidate_flat_source_binding"] = \
                candidate_binding
            self.candidate_preflight["current_flat_source_binding"] = \
                flat_binding
        return result

    def _ensure_flat_current_baseline(self, upgrade_config, previous_release):
        """Adopt Flat CURRENT without comparing volatile bootstrap metadata."""
        if self._deployment_layout != core.FLAT or not self._current_adoption_plan:
            return None
        if not upgrade_config.get("flat_current_adoption_on_cutover"):
            raise RuntimeError(
                "Flat deployment requires explicit flat_current_adoption_on_cutover"
            )
        identity_path = upgrade_config.get("flat_release_identity_path") or \
            upgrade_config.get("legacy_flat_release_identity_path", "")
        flat_root = upgrade_config.get("flat_served_root") or \
            upgrade_config.get("legacy_flat_served_root", "")
        if identity_path and not os.path.isabs(str(identity_path)):
            identity_path = os.path.join(self.repo_root, str(identity_path))
        if flat_root and not os.path.isabs(str(flat_root)):
            flat_root = os.path.join(self.repo_root, str(flat_root))
        baseline_session = str(
            upgrade_config.get("flat_baseline_session_id") or ""
        ).strip()
        if not baseline_session:
            raise RuntimeError(
                "flat_baseline_session_id is required for explicit adoption"
            )
        application_root = ""
        if self._production_lifecycle_adapter is not None:
            application_root = str(
                self._production_lifecycle_adapter.config.get(
                    "legacy_application_root"
                ) or ""
            ).strip()
            if not application_root:
                raise RuntimeError(
                    "vfoswind Flat adoption requires legacy_application_root"
                )
        result = core.bootstrap_flat_current(
            self.publish_root, flat_root, identity_path,
            previous_release.get("commit_sha", ""), baseline_session,
            switch=True,
            api_contract_version=upgrade_config.get("api_contract_version", ""),
            application_root=application_root,
        )
        if result.get("status") != "PASSED":
            raise RuntimeError("Flat baseline adoption did not pass")
        current = self.publisher.validate_current()
        if current.get("status") != "PASSED":
            raise RuntimeError("adopted baseline CURRENT failed validation")
        current_binding = core.current_served_root_binding(self.publish_root)
        if current_binding.get("previous_release_commit_sha") != \
                previous_release.get("commit_sha"):
            raise RuntimeError(
                "adopted baseline CURRENT commit does not match rollback identity"
            )
        flat_binding = current_flat_source_binding(
            flat_root, identity_path, previous_release.get("commit_sha", "")
        )
        candidate_binding = verify_production_candidate_flat_source_binding(
            self.candidate_preflight.get("source_provenance") or {},
            flat_binding,
        )
        self.candidate_preflight["candidate_flat_source_binding"] = \
            candidate_binding
        self.candidate_preflight["current_flat_source_binding"] = flat_binding
        self.candidate_preflight["current_served_root_binding"] = current_binding
        self.previous_published_session_id = self.publisher.current_session_id()
        self._deployment_layout = core.IMMUTABLE_CURRENT
        result["deployment_layout_after_adoption"] = core.IMMUTABLE_CURRENT
        self.manifest.record("flat_current_adoption", result)
        return result

    def log(self, message):
        if message == PAUSE_LOG and not self._airgapped_join_done:
            self._wait_and_join_airgapped_evidence()
        return super(AirGappedUpgradeOrchestrator, self).log(message)

    def _wait_and_join_airgapped_evidence(self):
        if self._airgapped_wait_active:
            raise RuntimeError("recursive air-gapped evidence wait")
        upgrade = dict((self._airgapped_runtime_config or {}).get("upgrade") or {})
        profile = dict(upgrade.get("airgapped_operator_browser") or {})
        if self._airgapped_mode != "production" or not profile.get("enabled"):
            return
        if str(profile.get("mode") or "operator_browser").strip() != "operator_browser":
            raise RuntimeError("unsupported airgapped_operator_browser.mode")
        if profile.get("public_network_required") not in (None, False):
            raise RuntimeError("air-gapped release may not require public network")

        self._airgapped_wait_active = True
        try:
            candidate_revision = str(self._target_identity.get("commit_sha") or "").strip()
            baseline_revision = str(self._previous_release_identity.get("commit_sha") or "").strip()
            if not candidate_revision or not baseline_revision or candidate_revision == baseline_revision:
                raise RuntimeError("air-gapped release revision identities are incomplete")
            if not self.release_validation_session_id or not self._candidate_artifact_sha256 or \
                    not self._served_root_sha256:
                raise RuntimeError("Candidate publication identity is incomplete at CANDIDATE_READY")

            evidence_dir = os.path.dirname(self.candidate_browser_evidence_path)
            observation_path = os.path.join(evidence_dir, OPERATOR_OBSERVATION_NAME)
            workload_output = os.path.join(evidence_dir, OPERATOR_WORKLOAD_JOIN_NAME)
            expected_auth = os.path.join(evidence_dir, "candidate_auth_probe_evidence.json")
            if os.path.realpath(expected_auth) != os.path.realpath(self.candidate_auth_probe_evidence_path):
                raise RuntimeError(
                    "candidate_auth_probe_evidence_path must share the air-gapped session evidence directory"
                )

            baseline_source = _resolve_revision_source(
                self.repo_root, profile.get("baseline_performance_revision_path"),
                baseline_revision, "airgapped baseline performance revision"
            )
            candidate_source = _resolve_revision_source(
                self.repo_root, profile.get("candidate_performance_revision_path"),
                candidate_revision, "airgapped candidate performance revision"
            )
            baseline_payload = _load_json(baseline_source, "baseline performance revision")
            candidate_payload = _load_json(candidate_source, "candidate performance revision")
            workload_hash = str(baseline_payload.get("workload_hash") or "")
            if not workload_hash or candidate_payload.get("workload_hash") != workload_hash:
                raise RuntimeError("air-gapped performance source workload hashes do not match")
            if baseline_payload.get("revision") != baseline_revision or \
                    candidate_payload.get("revision") != candidate_revision:
                raise RuntimeError("air-gapped performance source revisions do not match release")

            gateway_origin = _external_origin(
                profile.get("candidate_gateway_origin") or
                upgrade.get("candidate_gateway_origin") or ""
            )
            operator_url = _operator_page_url(gateway_origin)
            timeout_sec = int(profile.get("operator_timeout_sec") or 1800)
            poll_sec = float(profile.get("poll_interval_sec") or 2.0)
            if timeout_sec < 60 or timeout_sec > 7200:
                raise RuntimeError("operator_timeout_sec must be between 60 and 7200")
            if poll_sec < 0.5 or poll_sec > 30:
                raise RuntimeError("poll_interval_sec must be between 0.5 and 30")

            super(AirGappedUpgradeOrchestrator, self).log(
                "[Air-Gapped] Candidate is isolated and ready. Open this intranet URL in the normal operator Chrome/Edge:"
            )
            super(AirGappedUpgradeOrchestrator, self).log("[Air-Gapped] {}".format(operator_url))
            super(AirGappedUpgradeOrchestrator, self).log(
                "[Air-Gapped] Waiting up to {} seconds; Phase D remains forbidden.".format(timeout_sec)
            )

            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if os.path.isfile(observation_path) and os.path.isfile(self.candidate_auth_probe_evidence_path):
                    break
                time.sleep(poll_sec)
            else:
                raise RuntimeError(
                    "operator browser observation timed out; production cutover remains blocked"
                )

            observation = _load_json(observation_path, "operator browser observation")
            auth = _load_json(
                self.candidate_auth_probe_evidence_path,
                "candidate authenticated mutation evidence",
            )
            for label, payload in (("operator", observation), ("auth", auth)):
                if payload.get("candidate_revision") != candidate_revision or \
                        payload.get("release_validation_session_id") != self.release_validation_session_id or \
                        payload.get("candidate_artifact_sha256") != self._candidate_artifact_sha256 or \
                        payload.get("served_root_sha256") != self._served_root_sha256:
                    raise RuntimeError("{} air-gapped evidence identity mismatch".format(label))
            observed_candidate_url = str(observation.get("candidate_url") or "").strip()
            if not observed_candidate_url or auth.get("candidate_url") != observed_candidate_url:
                raise RuntimeError("air-gapped browser/auth Candidate URL mismatch")
            if not _same_origin(observed_candidate_url, gateway_origin):
                raise RuntimeError("manifest-derived Candidate report URL is outside configured gateway origin")

            performance = build_release_performance_ab(
                baseline_source,
                candidate_source,
                baseline_revision,
                candidate_revision,
                workload_hash,
                self.performance_evidence_path,
                release_validation_session_id=self.release_validation_session_id,
                candidate_artifact_sha256=self._candidate_artifact_sha256,
                served_root_sha256=self._served_root_sha256,
                max_regression_percent=profile.get("max_regression_percent", 20.0),
            )
            if performance.get("status") != "PASSED":
                raise RuntimeError(
                    "release performance A/B bind failed: {}".format(
                        "; ".join(performance.get("violations") or [])
                    )
                )

            joined = build_join(
                observation_path, self.candidate_auth_probe_evidence_path,
                self.performance_evidence_path, workload_output,
                self.candidate_browser_evidence_path,
                {
                    "revision": candidate_revision,
                    "session_id": self.release_validation_session_id,
                    "candidate_artifact_sha256": self._candidate_artifact_sha256,
                    "served_root_sha256": self._served_root_sha256,
                    "candidate_url": observed_candidate_url,
                },
            )
            if joined.get("status") != "PASSED":
                raise RuntimeError("air-gapped evidence join did not pass")
            self._airgapped_join_done = True
            super(AirGappedUpgradeOrchestrator, self).log(
                "[Air-Gapped] Operator browser, auth, and exact-revision performance evidence joined: PASSED"
            )
        finally:
            self._airgapped_wait_active = False


def main():
    parser = argparse.ArgumentParser(
        description="Air-gapped manifest-driven safe coverage upgrade"
    )
    parser.add_argument("--mode", choices=("staging", "production"), required=True)
    parser.add_argument("--manifest", required=True, dest="deployment_manifest")
    parser.add_argument("--target-release", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with open(args.target_release, "r", encoding="utf-8") as stream:
        target = json.load(stream)
    with open(args.config, "r", encoding="utf-8") as stream:
        config = json.load(stream)

    upgrade_config = config.get("upgrade") or {}
    if args.mode == "production":
        profile = upgrade_config.get("airgapped_operator_browser") or {}
        if not isinstance(profile, dict) or profile.get("enabled") is not True:
            print("Production air-gapped conductor requires upgrade.airgapped_operator_browser.enabled=true", file=sys.stderr)
            return 1
        try:
            _external_origin(
                profile.get("candidate_gateway_origin") or
                upgrade_config.get("candidate_gateway_origin") or ""
            )
        except Exception as exc:
            print("Production air-gapped conductor requires a valid Candidate Gateway origin: {}".format(exc), file=sys.stderr)
            return 1

    orchestrator = AirGappedUpgradeOrchestrator(
        backup_root=upgrade_config.get("backup_root")
    )
    mysql_config = config.get("mysql", config)
    target_mysql = upgrade_config.get("target_mysql") or config.get("target_mysql")
    if not isinstance(target_mysql, dict) or not target_mysql.get("database"):
        print("Live upgrade requires an explicit disposable upgrade.target_mysql database", file=sys.stderr)
        return 1
    target_preparation_mode = str(upgrade_config.get("target_preparation_mode") or "").strip()
    if target_preparation_mode not in core.DISPOSABLE_TARGET_MODES:
        print("Live upgrade requires an explicit disposable target_preparation_mode", file=sys.stderr)
        return 1

    connection = None
    target_connection = None
    target_connection_holder = {}
    target_connection_factory = None
    try:
        connection = core.connect_live_database(mysql_config)
        if target_preparation_mode in (
                core.RESTORE_FROM_VERIFIED_BACKUP, core.EMPTY_NEW_TARGET):
            def _prepare_target(backup_manifest, source_generation):
                if source_generation == core.VNEXT and \
                        target_preparation_mode != core.RESTORE_FROM_VERIFIED_BACKUP:
                    raise RuntimeError("Existing-VNext target preparation mode is invalid")
                if source_generation == core.LEGACY and \
                        target_preparation_mode != core.EMPTY_NEW_TARGET:
                    raise RuntimeError("Legacy target preparation mode is invalid")
                if target_preparation_mode == core.RESTORE_FROM_VERIFIED_BACKUP:
                    evidence = core.create_disposable_target_from_backup(
                        backup_manifest, mysql_config, target_mysql
                    )
                else:
                    evidence = core.create_empty_disposable_target(
                        mysql_config, target_mysql
                    )
                prepared_connection = core.connect_live_database(target_mysql)
                target_connection_holder["connection"] = prepared_connection
                return prepared_connection, evidence
            target_connection_factory = _prepare_target
        else:
            target_connection = core.connect_live_database(target_mysql)

        success, status = orchestrator.execute_upgrade(
            dry_run=False, mode=args.mode, connection=connection,
            db_config=dict(
                mysql_config,
                auth_mode=(config.get("auth") or {}).get("mode", "reverse_proxy"),
            ),
            deployment_manifest=args.deployment_manifest,
            target_release=target, runtime_config=config,
            target_connection=target_connection,
            target_db_config=dict(target_mysql),
            target_connection_factory=target_connection_factory,
        )
        return 0 if success else 1
    except Exception as exc:
        print("Air-gapped upgrade connection/orchestration failed: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        target_to_close = target_connection or target_connection_holder.get("connection")
        if target_to_close is not None:
            try:
                target_to_close.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
