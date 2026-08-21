"""Fixed Candidate predecessor resolution; never searches historical Scans."""

from __future__ import absolute_import

from app.db.repositories.base import fetchall, fetchone


class PredecessorResolver(object):
    def resolve(self, connection, candidate_scan_id, repository_name=None):
        candidate = fetchone(connection, "SELECT * FROM coverage_scans WHERE id=?",
                             (int(candidate_scan_id),))
        if not candidate:
            raise KeyError("candidate scan not found")
        predecessor_id = candidate.get("predecessor_scan_id")
        if not predecessor_id:
            return {"status": "NO_PREDECESSOR", "candidate_scan_id": int(candidate_scan_id),
                    "predecessor_scan_id": None, "repositories": []}
        predecessor = fetchone(connection, "SELECT * FROM coverage_scans WHERE id=?",
                                (int(predecessor_id),))
        if not predecessor or int(predecessor.get("project_id")) != int(candidate.get("project_id")):
            return {"status": "NO_PREDECESSOR", "candidate_scan_id": int(candidate_scan_id),
                    "predecessor_scan_id": int(predecessor_id), "repositories": []}
        rows = fetchall(connection, """
            SELECT c.repository_name AS candidate_repository,
                   c.branch_name AS candidate_branch, c.commit_sha AS candidate_commit,
                   p.branch_name AS predecessor_branch, p.commit_sha AS predecessor_commit,
                   p.repository_id AS predecessor_repository_id
            FROM coverage_scan_repositories c
            LEFT JOIN coverage_scan_repositories p
              ON p.scan_id=? AND p.repository_name=c.repository_name
            WHERE c.scan_id=?
            ORDER BY c.repository_name
        """, (int(predecessor_id), int(candidate_scan_id)))
        if repository_name is not None:
            rows = [row for row in rows if str(row.get("candidate_repository") or "") == str(repository_name)]
        result = []
        for row in rows:
            if not row.get("predecessor_repository_id"):
                reason = "NO_PREDECESSOR"
            elif row.get("candidate_branch") != row.get("predecessor_branch"):
                reason = "BRANCH_MISMATCH"
            else:
                reason = "READY"
            result.append(dict(row, reason_code=reason))
        return {"status": "READY" if any(item["reason_code"] == "READY" for item in result)
                else (result[0]["reason_code"] if result else "NO_PREDECESSOR"),
                "candidate_scan_id": int(candidate_scan_id),
                "predecessor_scan_id": int(predecessor_id), "repositories": result}
