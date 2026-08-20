"""Canonical Git/LCOV/ownership orchestration for incremental reports."""

from app.incremental.blame import owner_by_line
from app.incremental.git_diff import added_lines
from app.incremental.lcov import load_info
from app.incremental.path_index import LCOVPathLookupIndex
from app.incremental.report import IncrementalReport


class IncrementalOrchestrator(object):
    def build(self, project_name, repo_path, oldgit, newgit, info_path,
              repository_name="default", scan_id=None, report_id=""):
        additions = added_lines(repo_path, oldgit, newgit)
        lcov = load_info(info_path)
        # Build one immutable resolver for the whole report.  LCOV often
        # carries the build machine's absolute source root while Git returns
        # repository-relative paths; the shared index handles exact,
        # normalized, unique-suffix and fail-closed ambiguous matches.
        path_index = LCOVPathLookupIndex({repository_name: list(lcov.keys())})
        files = []
        for relative_path, added in sorted(additions.items()):
            lcov_item = self._find_lcov(
                lcov, relative_path, repository_name=repository_name,
                path_index=path_index,
            )
            if not lcov_item:
                continue
            owners = owner_by_line(repo_path, newgit, relative_path)
            uncovered = {
                int(line): int(count) for line, count in lcov_item.get("lines", {}).items()
                if int(count) == 0
            }
            details = []
            for line_number in sorted(set(added) & set(uncovered)):
                owner = owners.get(line_number) or {}
                details.append({
                    "line_number": line_number,
                    "execution_count": uncovered[line_number],
                    "suggested_reviewer": self._owner_name(owner),
                    "blame_commit": owner.get("commit", ""),
                    "blame_boundary": bool(owner.get("boundary")),
                })
            if details:
                files.append({
                    "repository_name": repository_name,
                    "file_path": relative_path,
                    "added_lines": sorted(added),
                    "details": details,
                    "function_ranges": lcov_item.get("function_ranges") or [],
                    "function_range_fallback": bool(
                        lcov_item.get("function_range_fallback")
                    ),
                })
        return IncrementalReport(
            project_name, repository_name, oldgit, newgit, files,
            scan_id=scan_id, report_id=report_id,
        ).to_dict()

    @staticmethod
    def _find_lcov(lcov, relative_path, repo_root=None, repository_name="default",
                   path_index=None):
        """Return the LCOV record resolved through the shared path index.

        ``repo_root`` remains accepted for compatibility with callers of the
        former helper; path resolution is intentionally independent of the
        current worktree's physical root.
        """
        index = path_index or LCOVPathLookupIndex({
            repository_name: list(lcov.keys())
        })
        resolved, _ = index.resolve_path(repository_name, str(relative_path or ""))
        return lcov.get(resolved) if resolved is not None else None

    @staticmethod
    def _owner_name(owner):
        author = str(owner.get("author") or "").strip()
        email = str(owner.get("author_mail") or "").strip()
        if author and email:
            return "{} <{}>".format(author, email)
        return author or email
