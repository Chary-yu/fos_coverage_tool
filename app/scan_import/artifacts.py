"""Immutable staged import artifacts."""

from __future__ import absolute_import

import hashlib
import os
import shutil
import tempfile

from app.db.repositories.base import adapt_sql, fetchone, insert_id
from app.time_utils import utc_sql


class ImmutableArtifactStager(object):
    def __init__(self, staging_root):
        self.staging_root = os.path.realpath(str(staging_root))
        if not os.path.isdir(self.staging_root):
            os.makedirs(self.staging_root)

    def stage(self, connection, job_id, source_path, kind="LCOV_INFO"):
        source_path = os.path.realpath(str(source_path or ""))
        if not os.path.isfile(source_path):
            raise FileNotFoundError(source_path)
        artifact_id = "{}-{}".format(str(job_id), str(kind).lower())
        destination = os.path.join(self.staging_root, artifact_id + ".staged")
        temporary = destination + ".{}.tmp".format(os.getpid())
        digest = hashlib.sha256()
        size = 0
        with open(source_path, "rb") as source, open(temporary, "wb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
        # Directory fsync is best effort on platforms/filesystems where it is
        # unavailable; the file itself is always flushed before the rename.
        try:
            directory_fd = os.open(self.staging_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        sha256 = digest.hexdigest()
        existing = fetchone(connection, """
            SELECT * FROM coverage_import_artifacts WHERE artifact_id=?
        """, (artifact_id,))
        if existing:
            if existing.get("sha256") != sha256 or existing.get("staged_path") != destination:
                raise ValueError("immutable staged artifact identity changed")
            return existing
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            INSERT INTO coverage_import_artifacts(
                artifact_id, job_id, kind, staged_path, sha256, size_bytes,
                immutable, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """), (artifact_id, str(job_id), str(kind), destination, sha256, size, utc_sql()))
        cursor.close()
        return fetchone(connection, """
            SELECT * FROM coverage_import_artifacts WHERE artifact_id=?
        """, (artifact_id,))

    def verify_staged(self, connection, artifact_id, expected_sha256=""):
        """Verify the staged file without touching the user's original path."""
        artifact = self.get_descriptor(connection, artifact_id)
        staged_path = os.path.realpath(str(artifact.get("staged_path") or ""))
        digest = hashlib.sha256()
        size = 0
        with open(staged_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        observed = digest.hexdigest()
        expected = str(expected_sha256 or artifact.get("sha256") or "").lower()
        if observed.lower() != expected or int(artifact.get("size_bytes") or 0) != size:
            raise ValueError("STAGED_ARTIFACT_CHANGED")
        return artifact

    def get_descriptor(self, connection, artifact_id):
        """Return the durable descriptor without rereading the staged bytes."""
        artifact = fetchone(connection, """
            SELECT * FROM coverage_import_artifacts WHERE artifact_id=?
        """, (str(artifact_id),))
        if not artifact:
            raise KeyError("staged artifact not found")
        staged_path = os.path.realpath(str(artifact.get("staged_path") or ""))
        if not os.path.isfile(staged_path):
            raise FileNotFoundError(staged_path)
        if not int(artifact.get("immutable") or 0):
            raise ValueError("STAGED_ARTIFACT_NOT_IMMUTABLE")
        return artifact
