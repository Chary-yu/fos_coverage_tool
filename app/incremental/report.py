"""Serializable Incremental report model and identity metadata."""

import hashlib
import json
from datetime import datetime


class IncrementalReport(object):
    def __init__(self, project_name, repository_name, oldgit, newgit, files,
                 scan_id=None, report_id=""):
        self.project_name = project_name
        self.repository_name = repository_name
        self.oldgit = oldgit
        self.newgit = newgit
        self.files = files or []
        self.scan_id = scan_id
        self.report_id = report_id
        self.generated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def source_signature(self):
        payload = {
            "project_name": self.project_name,
            "repository_name": self.repository_name,
            "oldgit": self.oldgit,
            "newgit": self.newgit,
            "files": self.files,
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    def to_dict(self):
        return {
            "project_name": self.project_name,
            "repository_name": self.repository_name,
            "oldgit": self.oldgit,
            "newgit": self.newgit,
            "scan_id": self.scan_id,
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "source_signature": self.source_signature,
            "files": self.files,
        }
