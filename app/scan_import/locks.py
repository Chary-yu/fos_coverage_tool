"""Physical repository locks with monotonic fencing tokens."""

from __future__ import absolute_import

from datetime import datetime, timedelta

from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone
from app.time_utils import utc_sql, utc_now_naive


class RepositoryBusyError(Exception):
    code = "REPOSITORY_BUSY"


def _expired(value, now=None):
    if not value:
        return False
    now = now or utc_now_naive()
    if isinstance(value, datetime):
        return value < now
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S") < now
    except ValueError:
        return False


class RepositoryResourceLockService(object):
    def _next_token(self, connection, resource_id):
        cursor = execute(connection, """
            UPDATE coverage_repository_resources
            SET next_fencing_token=next_fencing_token+1
            WHERE id=?
        """, (int(resource_id),))
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            cursor.close()
            raise KeyError("physical repository resource not found")
        cursor.close()
        row = fetchone(connection, """
            SELECT next_fencing_token FROM coverage_repository_resources WHERE id=?
        """, (int(resource_id),))
        return int(row["next_fencing_token"])

    def acquire(self, connection, resource_ids, job_id, owner_token,
                lease_seconds=300):
        acquired = []
        now = utc_sql()
        expires = (utc_now_naive() + timedelta(seconds=float(lease_seconds))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            for resource_id in sorted(set(int(item) for item in (resource_ids or []))):
                lock = fetchone(connection, """
                    SELECT * FROM coverage_repository_resource_locks
                    WHERE physical_resource_id=?
                """, (resource_id,))
                if lock and not _expired(lock.get("expires_at")) and str(lock.get("job_id")) != str(job_id):
                    raise RepositoryBusyError(
                        "resource {} is owned by another active import".format(resource_id)
                    )
                token = self._next_token(connection, resource_id)
                if lock:
                    cursor = execute(connection, """
                        UPDATE coverage_repository_resource_locks
                        SET job_id=?, owner_token=?, fencing_token=?, heartbeat_at=?,
                            acquired_at=?, expires_at=?
                        WHERE physical_resource_id=?
                    """, (str(job_id), str(owner_token), token, now, now, expires,
                          resource_id))
                    cursor.close()
                else:
                    cursor = execute(connection, """
                        INSERT INTO coverage_repository_resource_locks(
                            physical_resource_id, job_id, owner_token, fencing_token,
                            heartbeat_at, acquired_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (resource_id, str(job_id), str(owner_token), token, now, now,
                          expires))
                    cursor.close()
                acquired.append({"physical_resource_id": resource_id,
                                 "fencing_token": token})
        except Exception:
            self.release(connection, job_id, owner_token)
            raise
        return acquired

    def heartbeat(self, connection, job_id, owner_token, fencing_token,
                  lease_seconds=300):
        expires = (utc_now_naive() + timedelta(seconds=float(lease_seconds))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = execute(connection, """
            UPDATE coverage_repository_resource_locks
            SET heartbeat_at=?, expires_at=?
            WHERE job_id=? AND owner_token=? AND fencing_token=?
        """, (utc_sql(), expires, str(job_id), str(owner_token), int(fencing_token)))
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        if count == 0:
            raise ValueError("LOCK_FENCING_FAILED")
        return count

    def assert_fence(self, connection, resource_id, job_id, owner_token,
                     fencing_token):
        row = fetchone(connection, """
            SELECT * FROM coverage_repository_resource_locks
            WHERE physical_resource_id=? AND job_id=? AND owner_token=?
              AND fencing_token=?
        """, (int(resource_id), str(job_id), str(owner_token), int(fencing_token)))
        if not row or _expired(row.get("expires_at")):
            raise ValueError("LOCK_FENCING_FAILED")
        return row

    def release(self, connection, job_id, owner_token, fencing_token=None):
        clauses = ["job_id=?", "owner_token=?"]
        params = [str(job_id), str(owner_token)]
        if fencing_token is not None:
            clauses.append("fencing_token=?")
            params.append(int(fencing_token))
        cursor = execute(connection, "DELETE FROM coverage_repository_resource_locks WHERE " +
                         " AND ".join(clauses), params)
        count = int(getattr(cursor, "rowcount", 0) or 0)
        cursor.close()
        return count

    def list_locks(self, connection):
        return fetchall(connection, """
            SELECT * FROM coverage_repository_resource_locks ORDER BY physical_resource_id
        """)
