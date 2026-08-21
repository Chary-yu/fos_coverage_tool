"""CAS-protected Scan Import checkpoint state machine."""

from __future__ import absolute_import

import json

from app.db.repositories.base import adapt_sql, fetchone
from app.time_utils import utc_sql


PHASES = (
    "LOCKED", "SCAN_CREATED", "INFO_STAGED", "COVERAGE_IMPORTED",
    "GIT_VERIFIED", "SOURCE_PREPARED", "LINE_MAP_BUILT",
    "INHERITANCE_COMPUTED", "STATS_REBUILT", "CONSISTENCY_VERIFIED",
    "SEALED", "PUBLISHED", "DONE",
)


class ImportCheckpointRepository(object):
    def get(self, connection, job_id):
        return fetchone(connection, "SELECT * FROM coverage_import_checkpoints WHERE job_id=?",
                        (str(job_id),))

    def create(self, connection, job_id, scan_id, expected_current_scan_id=None,
               input_sha256="", fencing_token=0, payload=None):
        existing = self.get(connection, job_id)
        if existing:
            return existing
        now = utc_sql()
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            INSERT INTO coverage_import_checkpoints(
                job_id, scan_id, phase, phase_version, checkpoint_seq, payload,
                input_sha256, fencing_token, expected_current_scan_id,
                created_at, updated_at
            ) VALUES (?, ?, 'LOCKED', 1, 0, ?, ?, ?, ?, ?, ?)
        """), (str(job_id), scan_id, json.dumps(payload or {}, sort_keys=True),
                input_sha256 or "", int(fencing_token or 0), expected_current_scan_id,
                now, now))
        cursor.close()
        return self.get(connection, job_id)

    def advance(self, connection, job_id, expected_seq, expected_fencing_token,
                phase, payload=None, input_sha256=None):
        phase = str(phase or "")
        if phase not in PHASES:
            raise ValueError("unknown import phase")
        current = self.get(connection, job_id)
        if not current:
            raise KeyError("import checkpoint not found")
        current_phase = str(current.get("phase") or "")
        if PHASES.index(phase) < PHASES.index(current_phase):
            raise ValueError("IMPORT_PHASE_REGRESSION")
        if PHASES.index(phase) > PHASES.index(current_phase) + 1:
            raise ValueError("IMPORT_PHASE_SKIP")
        next_seq = int(expected_seq) + 1
        now = utc_sql()
        columns = [phase, next_seq, json.dumps(payload or {}, sort_keys=True), now,
                   str(job_id), int(expected_seq), int(expected_fencing_token)]
        sql = """
            UPDATE coverage_import_checkpoints
            SET phase=?, checkpoint_seq=?, payload=?, updated_at=?
            {input_set}
            WHERE job_id=? AND checkpoint_seq=? AND fencing_token=?
        """
        if input_sha256 is None:
            input_set = ""
        else:
            input_set = ", input_sha256=?"
            columns.insert(3, input_sha256)
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, sql.format(input_set=input_set)), tuple(columns))
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        if count != 1:
            raise ValueError("STALE_IMPORT_CHECKPOINT")
        return self.get(connection, job_id)

    def claim_fencing(self, connection, job_id, fencing_token):
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_import_checkpoints SET fencing_token=?, updated_at=?
            WHERE job_id=? AND fencing_token < ?
        """), (int(fencing_token), utc_sql(), str(job_id), int(fencing_token)))
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        if count != 1:
            raise ValueError("STALE_IMPORT_FENCING")
        return self.get(connection, job_id)
