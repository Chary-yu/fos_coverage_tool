"""Single fail-closed registry for Report ID to report roots."""

import json
import os
import tempfile

from app.reports.identity import validate_report_id


class ReportRegistry(object):
    def __init__(self, registry_dir, legacy_path=None):
        self.registry_dir = os.path.realpath(registry_dir)
        self.legacy_path = legacy_path

    def _path(self, report_id):
        return os.path.join(self.registry_dir, validate_report_id(report_id) + ".json")

    def register(self, report_id, directories, **metadata):
        report_id = validate_report_id(report_id)
        directories = [self._canonical_report_root(report_id, path) for path in directories or []]
        directories = list(dict.fromkeys(
            path for path in directories
            if path and os.path.isdir(path) and not os.path.islink(path)
        ))
        if not directories:
            return None
        os.makedirs(self.registry_dir, exist_ok=True)
        # A report output can be removed independently of the registry file
        # (for example by retention cleanup).  Prune those dead registrations
        # at the next registration boundary so the registry does not grow
        # stale roots that make the sidecar audit fail closed forever.  The
        # explicit audit remains observation-only; this is lifecycle cleanup
        # performed only while a valid report is being registered.
        self.prune()
        path = self._path(report_id)
        current = self.load_exact(report_id) or {}
        existing_directories = list(dict.fromkeys(current.get("directories") or []))
        if existing_directories and set(existing_directories) != set(directories):
            # A second root for the same Report ID is not a harmless merge:
            # it makes physical Sidecar resolution ambiguous.  Relocation is
            # an explicit operation and must replace the old registration.
            if not metadata.get("replace"):
                raise ValueError("report_id is already registered to another root")
            existing_directories = []
        merged = list(dict.fromkeys(existing_directories + directories))
        payload = {
            "report_id": report_id,
            "directories": merged,
            "sidecar_required": bool(metadata.get(
                "sidecar_required", current.get("sidecar_required", False)
            )),
            "report_root": metadata.get("report_root", current.get("report_root", "")),
            "scan_id": metadata.get("scan_id", current.get("scan_id")),
            "source_signature": metadata.get(
                "source_signature", current.get("source_signature", "")
            ),
        }
        fd, temp_path = tempfile.mkstemp(prefix=".registry-", dir=self.registry_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return payload

    @staticmethod
    def _canonical_report_root(report_id, path):
        """Normalize accidental Sidecar paths back to the report root."""
        if not path:
            return ""
        candidate = os.path.realpath(str(path))
        if os.path.basename(candidate) == report_id and \
                os.path.basename(os.path.dirname(candidate)) == ".source_cache":
            return os.path.realpath(os.path.dirname(os.path.dirname(candidate)))
        if os.path.basename(candidate) == ".source_cache":
            return os.path.realpath(os.path.dirname(candidate))
        return candidate

    def load_exact(self, report_id):
        report_id = validate_report_id(report_id)
        path = self._path(report_id)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            if not isinstance(value, dict) or value.get("report_id") not in (None, report_id):
                return None
            directories = [os.path.realpath(item) for item in value.get("directories") or []
                           if item]
            value["report_id"] = report_id
            value["directories"] = directories
            return value
        except (OSError, ValueError, TypeError):
            return None

    def load_all(self):
        result = {}
        if os.path.isdir(self.registry_dir):
            for name in sorted(os.listdir(self.registry_dir)):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                report_id = name[:-5]
                try:
                    value = self.load_exact(report_id)
                except ValueError:
                    value = None
                if value:
                    result[report_id] = value
        if self.legacy_path and os.path.isfile(self.legacy_path):
            try:
                with open(self.legacy_path, "r", encoding="utf-8") as stream:
                    legacy = json.load(stream)
                if isinstance(legacy, dict):
                    for report_id, directories in legacy.items():
                        if report_id in result:
                            continue
                        try:
                            report_id = validate_report_id(report_id)
                        except ValueError:
                            continue
                        if isinstance(directories, str):
                            directories = [directories]
                        result[report_id] = {
                            "report_id": report_id,
                            "directories": [os.path.realpath(item) for item in directories or []],
                            "sidecar_required": False,
                        }
            except (OSError, ValueError, TypeError):
                pass
        return result

    @staticmethod
    def _requires_sidecar(value, directory, report_id):
        """Honor explicit VNext metadata while preserving old registry files.

        VNext registrations always write ``sidecar_required``.  Historical
        per-report registry files predate that field and expected a root that
        contains ``.source_cache`` to contain that report's own cache.  Keep
        that compatibility rule only for the unversioned shape; an explicit
        ``false`` remains a valid static/legacy root and is not contaminated by
        a sibling report's cache.
        """
        if value.get("sidecar_required"):
            return True
        return (
            "sidecar_required" not in value
            and os.path.isdir(os.path.join(directory, ".source_cache"))
        )

    def prune(self):
        removed = []
        if not os.path.isdir(self.registry_dir):
            return removed
        for report_id, value in self.load_all().items():
            directories = []
            for directory in value.get("directories") or []:
                if not os.path.isdir(directory):
                    continue
                # A sibling report may have created ``.source_cache`` under
                # this root. That directory alone says nothing about this
                # report's format; only this report's own registration flag
                # can require a report-specific Sidecar directory.
                if self._requires_sidecar(value, directory, report_id):
                    sidecar = os.path.join(directory, ".source_cache", report_id)
                    if not os.path.isdir(sidecar):
                        continue
                directories.append(directory)
            path = self._path(report_id)
            if directories:
                if directories != value.get("directories"):
                    value["directories"] = directories
                    with open(path, "w", encoding="utf-8") as stream:
                        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            else:
                try:
                    os.remove(path)
                    removed.append(report_id)
                except OSError:
                    pass
        return removed

    def resolve_exact_root(self, report_id):
        value = self.load_exact(report_id)
        if not value:
            return None
        roots = []
        for path in value.get("directories") or []:
            if not os.path.isdir(path):
                continue
            if self._requires_sidecar(value, path, report_id) and not os.path.isdir(
                    os.path.join(path, ".source_cache", report_id)):
                continue
            roots.append(path)
        if len(roots) != 1:
            return roots[0] if len(roots) == 1 else None
        return roots[0]
