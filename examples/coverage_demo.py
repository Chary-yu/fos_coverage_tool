#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Self-contained browser demo for the coverage review workflow.

The demo intentionally uses SQLite instead of MySQL so it can be launched in a
fresh WSL checkout. It exercises the same HTTP handler, frontend save calls,
progress page, and Excel/ZIP builders used by production.
"""

import argparse
import mimetypes
import os
import shutil
import sqlite3
import sys
import threading
import urllib.parse
from datetime import datetime

# Keep the demo runnable from any working directory after it is moved out of
# the repository root.  The production compatibility shims and app package
# still live at the repository root.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import enhance_coverage
from enhance_coverage import CoverageHTTPRequestHandler, ThreadingHTTPServer


DEMO_PROJECT = "coverage_demo"
DEMO_FILE = "demo/src/calculator.c"


class DemoDatabaseManager:
    """Small SQLite-backed implementation of the production manager contract."""

    FULL_HEADERS = {
        "full_detail": [
            "project_name", "file_path", "line_number", "line_text",
            "block_start_line", "block_end_line", "block_type", "fill_status",
            "status", "reviewer", "coverage_method", "uncovered_reason", "updated_at",
        ],
        "full_file_summary": [
            "project_name", "file_path", "total_uncovered", "filled_total",
            "unfilled_total", "confirmed_total", "coverable_total",
            "uncoverable_total", "redundant_total", "fill_rate", "confirmed_rate",
            "last_updated",
        ],
        "full_dir_summary": [
            "project_name", "dir_path", "file_total", "total_uncovered",
            "filled_total", "unfilled_total", "confirmed_total", "coverable_total",
            "uncoverable_total", "redundant_total", "fill_rate", "confirmed_rate",
            "last_updated",
        ],
        "full_project_summary": [
            "project_name", "file_total", "total_uncovered", "filled_total",
            "unfilled_total", "confirmed_total", "coverable_total",
            "uncoverable_total", "redundant_total", "fill_rate", "confirmed_rate",
            "last_updated",
        ],
    }

    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self._seed_line_index()

    def _init_schema(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS coverage_analysis (
                    project_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    reviewer TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '未确认',
                    is_draft INTEGER NOT NULL DEFAULT 0,
                    coverage_method TEXT NOT NULL DEFAULT '',
                    uncovered_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_name, file_path, line_number)
                );
                CREATE TABLE IF NOT EXISTS coverage_line_index (
                    project_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source_file_name TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    line_text TEXT NOT NULL,
                    block_start_line INTEGER NOT NULL,
                    block_end_line INTEGER NOT NULL,
                    block_type TEXT NOT NULL,
                    PRIMARY KEY (project_name, file_path, line_number)
                );
            """)
            self.conn.commit()

    def _seed_line_index(self):
        lines = [
            (3, "    int result = 0;", 3, 3, "single"),
            (5, "        result = left + right;", 5, 5, "control"),
            (7, "        result = left - right;", 7, 7, "control"),
            (9, "        result = left * right;", 9, 9, "control"),
            (11, "        result = left / right;", 11, 11, "control"),
            (13, "        result = -1;", 13, 13, "control"),
        ]
        with self.lock:
            self.conn.executemany("""
                INSERT OR IGNORE INTO coverage_line_index
                    (project_name, file_path, source_file_name, line_number, line_text,
                     block_start_line, block_end_line, block_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (DEMO_PROJECT, DEMO_FILE, "calculator.c", line, text, start, end, block_type)
                for line, text, start, end, block_type in lines
            ])
            self.conn.commit()

    def _fetchall(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def fetch_records(self, project_name, file_path):
        return self._fetchall("""
            SELECT line_number, reviewer, status, is_draft, coverage_method, uncovered_reason
            FROM coverage_analysis
            WHERE project_name = ? AND file_path = ?
            ORDER BY line_number
        """, (project_name, file_path))

    def save_record(self, project_name, file_path, line_number, reviewer, status, method, reason):
        result = self.save_records_batch(project_name, file_path, [{
            "line_numbers": [line_number], "reviewer": reviewer, "status": status,
            "coverage_method": method, "uncovered_reason": reason,
        }], is_draft=False)
        return bool(result)

    def save_records_batch(self, project_name, file_path, blocks, is_draft=False):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for block in blocks:
            for line_number in block["line_numbers"]:
                rows.append((
                    project_name, file_path, int(line_number), block["reviewer"],
                    block["status"], 1 if is_draft else 0, block["coverage_method"],
                    block["uncovered_reason"], now,
                ))
        if not rows:
            return None
        with self.lock:
            self.conn.executemany("""
                INSERT INTO coverage_analysis
                    (project_name, file_path, line_number, reviewer, status, is_draft,
                     coverage_method, uncovered_reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, file_path, line_number) DO UPDATE SET
                    reviewer = excluded.reviewer,
                    status = excluded.status,
                    is_draft = excluded.is_draft,
                    coverage_method = excluded.coverage_method,
                    uncovered_reason = excluded.uncovered_reason,
                    updated_at = excluded.updated_at
            """, rows)
            self.conn.commit()
        print("[Demo DB] saved blocks={} lines={} draft={} sqlite={}".format(
            len(blocks), len(rows), bool(is_draft), self.db_path
        ), flush=True)
        return {"saved_blocks": len(blocks), "saved_lines": len(rows)}

    def fetch_projects(self):
        indexed = self._fetchall("""
            SELECT project_name, COUNT(*) AS indexed_total,
                   COUNT(DISTINCT file_path) AS indexed_file_total
            FROM coverage_line_index GROUP BY project_name
        """)
        saved = {
            row["project_name"]: row for row in self._fetchall("""
                SELECT project_name, COUNT(*) AS saved_total,
                       COUNT(DISTINCT file_path) AS saved_file_total,
                       MAX(updated_at) AS last_updated
                FROM coverage_analysis GROUP BY project_name
            """)
        }
        result = []
        for item in indexed:
            review = saved.get(item["project_name"], {})
            result.append({
                "project_name": item["project_name"],
                "indexed_total": item["indexed_total"],
                "indexed_file_total": item["indexed_file_total"],
                "saved_total": review.get("saved_total", 0),
                "saved_file_total": review.get("saved_file_total", 0),
                "last_updated": review.get("last_updated"),
            })
        return result

    def has_line_index(self, project_name):
        rows = self._fetchall(
            "SELECT 1 AS found FROM coverage_line_index WHERE project_name = ? LIMIT 1",
            (project_name,),
        )
        return bool(rows)

    def _joined_rows(self, project_name=None):
        params = () if project_name is None else (project_name,)
        where_sql = "" if project_name is None else "WHERE i.project_name = ?"
        return self._fetchall("""
            SELECT i.project_name, i.file_path, i.source_file_name, i.line_number,
                   i.line_text, i.block_start_line, i.block_end_line, i.block_type,
                   a.reviewer, a.status, a.is_draft, a.coverage_method,
                   a.uncovered_reason, a.updated_at,
                   CASE WHEN a.line_number IS NULL THEN 0 ELSE 1 END AS is_filled
            FROM coverage_line_index i
            LEFT JOIN coverage_analysis a
              ON a.project_name = i.project_name
             AND a.file_path = i.file_path
             AND a.line_number = i.line_number
            {}
            ORDER BY i.project_name, i.file_path, i.line_number
        """.format(where_sql), params)

    @staticmethod
    def _summary(project_name, path_value, rows, include_file_total=False):
        total = len(rows)
        filled = sum(int(row["is_filled"]) for row in rows)
        confirmed = sum(
            1 for row in rows
            if row["is_filled"] and not row["is_draft"] and row["status"] != "未确认"
        )
        values = [
            project_name, path_value,
            total, filled, total - filled, confirmed,
            sum(1 for row in rows if row["is_filled"] and not row["is_draft"] and row["status"] == "可覆盖"),
            sum(1 for row in rows if row["is_filled"] and not row["is_draft"] and row["status"] == "无法覆盖"),
            sum(1 for row in rows if row["is_filled"] and not row["is_draft"] and row["status"] == "冗余代码"),
            round(filled * 100.0 / total, 2) if total else 0.0,
            round(confirmed * 100.0 / total, 2) if total else 0.0,
            max((row["updated_at"] for row in rows if row["updated_at"]), default=None),
        ]
        if include_file_total:
            values[1] = len(set(row["file_path"] for row in rows))
        return values

    def export_report(self, report_type="detail", project_name=None):
        joined = self._joined_rows(project_name)
        if report_type == "full_detail":
            headers = self.FULL_HEADERS[report_type]
            data = [[
                row["project_name"], row["file_path"], row["line_number"], row["line_text"],
                row["block_start_line"], row["block_end_line"], row["block_type"],
                "已填写" if row["is_filled"] else "未填写", row["status"] or "",
                row["reviewer"] or "", row["coverage_method"] or "",
                row["uncovered_reason"] or "", row["updated_at"],
            ] for row in joined]
            return headers, data

        if report_type in ("full_file_summary", "full_dir_summary", "full_project_summary"):
            grouped = {}
            for row in joined:
                if report_type == "full_file_summary":
                    key = (row["project_name"], row["file_path"])
                elif report_type == "full_dir_summary":
                    key = (row["project_name"], enhance_coverage.get_source_dir_name(row["file_path"]))
                else:
                    key = (row["project_name"], "")
                grouped.setdefault(key, []).append(row)
            data = []
            for (name, path_value), rows in sorted(grouped.items()):
                if report_type == "full_dir_summary":
                    summary = self._summary(name, path_value, rows)
                    summary.insert(2, len(set(row["file_path"] for row in rows)))
                else:
                    summary = self._summary(
                        name, path_value, rows,
                        include_file_total=(report_type == "full_project_summary"),
                    )
                data.append(summary)
            return self.FULL_HEADERS[report_type], data

        if report_type == "full_progress_summary":
            headers = [
                "level", "project_name", "path", "file_total", "total_uncovered",
                "filled_total", "unfilled_total", "confirmed_total", "coverable_total",
                "uncoverable_total", "redundant_total", "fill_rate", "confirmed_rate",
                "last_updated",
            ]
            data = []
            for level, child_type in (
                ("project", "full_project_summary"),
                ("dir", "full_dir_summary"),
                ("file", "full_file_summary"),
            ):
                child_headers, child_rows = self.export_report(child_type, project_name)
                for child_row in child_rows:
                    item = dict(zip(child_headers, child_row))
                    path = item.get("dir_path", "") if level == "dir" else item.get("file_path", "") if level == "file" else ""
                    data.append([
                        level, item.get("project_name", ""), path,
                        item.get("file_total", 1 if level == "file" else ""),
                        item.get("total_uncovered", 0), item.get("filled_total", 0),
                        item.get("unfilled_total", 0), item.get("confirmed_total", 0),
                        item.get("coverable_total", 0), item.get("uncoverable_total", 0),
                        item.get("redundant_total", 0), item.get("fill_rate", 0),
                        item.get("confirmed_rate", 0), item.get("last_updated"),
                    ])
            return headers, data

        analysis = self._fetchall("""
            SELECT project_name, file_path, line_number, reviewer, status, is_draft,
                   coverage_method, uncovered_reason, updated_at
            FROM coverage_analysis
            {} ORDER BY project_name, file_path, line_number
        """.format("" if project_name is None else "WHERE project_name = ?"),
            () if project_name is None else (project_name,))
        if report_type == "detail":
            headers = [
                "project_name", "file_path", "line_number", "reviewer", "status",
                "coverage_method", "uncovered_reason", "updated_at",
            ]
            return headers, [[row.get(header) for header in headers] for row in analysis]

        if report_type in ("file_summary", "project_summary"):
            groups = {}
            for row in analysis:
                key = (row["project_name"], row["file_path"] if report_type == "file_summary" else "")
                groups.setdefault(key, []).append(row)
            headers = [
                "project_name", "file_path" if report_type == "file_summary" else "review_total",
                "review_total" if report_type == "file_summary" else "confirmed_total",
                "confirmed_total" if report_type == "file_summary" else "coverable_total",
                "coverable_total" if report_type == "file_summary" else "uncoverable_total",
                "uncoverable_total" if report_type == "file_summary" else "redundant_total",
                "redundant_total" if report_type == "file_summary" else "unconfirmed_total",
                "unconfirmed_total" if report_type == "file_summary" else "confirmed_rate",
                "confirmed_rate" if report_type == "file_summary" else "coverable_rate",
                "coverable_rate" if report_type == "file_summary" else "uncoverable_rate",
                "uncoverable_rate" if report_type == "file_summary" else "redundant_rate",
                "redundant_rate" if report_type == "file_summary" else "file_total",
                "last_updated",
            ]
            data = []
            for (name, path), rows in sorted(groups.items()):
                total = len(rows)
                confirmed = sum(1 for row in rows if not row["is_draft"] and row["status"] != "未确认")
                coverable = sum(1 for row in rows if not row["is_draft"] and row["status"] == "可覆盖")
                uncoverable = sum(1 for row in rows if not row["is_draft"] and row["status"] == "无法覆盖")
                redundant = sum(1 for row in rows if not row["is_draft"] and row["status"] == "冗余代码")
                values = [
                    name, path if report_type == "file_summary" else total,
                    total if report_type == "file_summary" else confirmed,
                    confirmed if report_type == "file_summary" else coverable,
                    coverable if report_type == "file_summary" else uncoverable,
                    uncoverable if report_type == "file_summary" else redundant,
                    redundant if report_type == "file_summary" else total - confirmed,
                    total - confirmed if report_type == "file_summary" else round(confirmed * 100.0 / total, 2),
                    round(confirmed * 100.0 / total, 2) if report_type == "file_summary" else round(coverable * 100.0 / total, 2),
                    round(coverable * 100.0 / total, 2) if report_type == "file_summary" else round(uncoverable * 100.0 / total, 2),
                    round(uncoverable * 100.0 / total, 2) if report_type == "file_summary" else round(redundant * 100.0 / total, 2),
                    round(redundant * 100.0 / total, 2) if report_type == "file_summary" else len(set(row["file_path"] for row in rows)),
                    max(row["updated_at"] for row in rows),
                ]
                data.append(values)
            return headers, data
        raise ValueError("Unsupported demo report type: {}".format(report_type))

    def fetch_review_excel_rows(self, project_name, dir_path=None):
        rows = self._joined_rows(project_name)
        if dir_path is not None:
            rows = [row for row in rows if enhance_coverage.get_source_dir_name(row["file_path"]) == dir_path]
        return [[
            row["project_name"], row["source_file_name"], row["file_path"],
            row["line_number"], row["line_text"], row["status"] or "",
            row["coverage_method"] or "", row["uncovered_reason"] or "",
            row["reviewer"] or "",
        ] for row in rows]

    def count_full_detail_rows(self, project_name):
        rows = self._fetchall(
            "SELECT COUNT(*) AS total FROM coverage_line_index WHERE project_name = ?",
            (project_name,),
        )
        return int(rows[0]["total"] if rows else 0)

    def iter_full_detail_batches(self, project_name, batch_size=5000):
        headers, rows = self.export_report("full_detail", project_name)
        del headers
        for index in range(0, len(rows), batch_size):
            yield rows[index:index + batch_size]

    def fetch_full_detail_page(self, project_name, file_path, page=1, page_size=200):
        page = max(1, int(page))
        page_size = max(1, min(enhance_coverage.DETAIL_PAGE_SIZE_MAX, int(page_size)))
        rows = [
            row for row in self._joined_rows(project_name)
            if row["file_path"] == file_path
        ]
        total = len(rows)
        selected = rows[(page - 1) * page_size:page * page_size]
        detail_rows = [[
            row["project_name"], row["file_path"], row["line_number"], row["line_text"],
            row["block_start_line"], row["block_end_line"], row["block_type"],
            "已填写" if row["is_filled"] else "未填写", row["status"] or "",
            row["reviewer"] or "", row["coverage_method"] or "",
            row["uncovered_reason"] or "", row["updated_at"],
        ] for row in selected]
        return {
            "headers": list(enhance_coverage.FULL_DETAIL_HEADERS),
            "rows": detail_rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        }


class DemoHTTPRequestHandler(CoverageHTTPRequestHandler):
    static_root = ""

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path.startswith("/api/coverage"):
            return super().do_GET()
        self.send_static_file()

    def send_static_file(self):
        request_path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        relative = request_path.lstrip("/") or "index.html"
        candidate = os.path.realpath(os.path.join(self.static_root, relative))
        root = os.path.realpath(self.static_root)
        if candidate != root and not candidate.startswith(root + os.sep):
            self.send_error(403)
            return
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        filename = os.path.basename(candidate)
        if filename == "coverage_progress.html":
            src = os.path.join(enhance_coverage.SCRIPT_DIR, "coverage_progress.html")
            if os.path.isfile(src):
                shutil.copy2(src, candidate)
        elif filename == "coverage_enhance.css":
            src = os.path.join(enhance_coverage.SCRIPT_DIR, "coverage_enhance.css")
            if os.path.isfile(src):
                shutil.copy2(src, candidate)
        elif filename == "coverage_progress.js":
            src = os.path.join(enhance_coverage.SCRIPT_DIR, "coverage_progress.js")
            if os.path.isfile(src):
                shutil.copy2(src, candidate)
        if not os.path.isfile(candidate):
            self.send_error(404)
            return
        with open(candidate, "rb") as source:
            data = source.read()
        content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'",
        )
        self.end_headers()
        self.safe_write(data)


def build_demo_site(output_dir):
    html_dir = os.path.join(output_dir, "html")
    os.makedirs(html_dir, exist_ok=True)
    enhance_coverage.write_configured_enhance_js(
        os.path.join(html_dir, "coverage_enhance.js"), DEMO_PROJECT, "immediate", "full"
    )
    shutil.copy2(enhance_coverage.CSS_SOURCE_PATH, os.path.join(html_dir, "coverage_enhance.css"))
    enhance_coverage.write_progress_page_targets(output_dir, html_dir, "full")

    source_lines = [
        (1, "lineCov", "int calculate(int left, int right, char op) {"),
        (2, "lineCov", "    // Demo: edit the controls on uncovered lines"),
        (3, "lineNoCov", "    int result = 0;"),
        (4, "lineCov", "    if (op == '+') {"),
        (5, "lineNoCov", "        result = left + right;"),
        (6, "lineCov", "    } else if (op == '-') {"),
        (7, "lineNoCov", "        result = left - right;"),
        (8, "lineCov", "    } else if (op == '*') {"),
        (9, "lineNoCov", "        result = left * right;"),
        (10, "lineCov", "    } else if (right != 0) {"),
        (11, "lineNoCov", "        result = left / right;"),
        (12, "lineCov", "    } else {"),
        (13, "lineNoCov", "        result = -1;"),
        (14, "lineCov", "    }"),
        (15, "lineCov", "    return result;"),
        (16, "lineCov", "}"),
    ]
    source_html = "\n".join(
        '<span id="L{}" class="{}">{:4d}: {}</span>'.format(
            number, css_class, number, enhance_coverage.html.escape(text)
        )
        for number, css_class, text in source_lines
    )
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>LCOV - demo - {file_path}</title>
<link rel="stylesheet" href="coverage_enhance.css?v={version}">
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:20px;background:#f8fafc;color:#172033}}
.demo-tip{{padding:12px 14px;margin-bottom:14px;background:#eef4ff;border:1px solid #bfd3ff;border-radius:6px}}
.header{{background:white;border-collapse:collapse;margin-bottom:12px}}.header td{{padding:5px 8px;border:1px solid #d8e0ea}}
pre.source{{position:relative;display:block;min-width:1200px;background:white;padding:12px;border:1px solid #d8e0ea;line-height:24px;font:14px/24px monospace}}
pre.source > span{{display:block;position:relative;white-space:pre}}.lineCov{{background:#e8f7ed}}.lineNoCov{{background:#ffe7e7;color:#8b1e1e}}
</style><script src="coverage_enhance.js?v={version}"></script></head>
<body><div class="demo-tip"><strong>WSL Coverage Demo</strong>：先填写并保存一条来源记录，再在下方某个控件点击“批量继承”，来源之后至当前控件会一起进入待暂存状态；最后点击“暂存草稿”或“确认提交”。数据会写入本地 SQLite，刷新页面仍会回显。</div>
<table class="header"><tr><td class="headerItem">Lines:</td><td class="headerValue">Demo</td><td></td><td class="headerItem">Coverage:</td><td>62.5 %</td><td>16</td><td>10</td></tr></table>
<pre class="source">{source_html}</pre></body></html>""".format(
        file_path=DEMO_FILE,
        version=enhance_coverage.ASSET_VERSION,
        source_html=source_html,
    )
    with open(os.path.join(html_dir, "calculator.c.gcov.html"), "w", encoding="utf-8") as target:
        target.write(page)

    # Also expose the new incremental author-to-file workflow in the browser
    # demo.  The two developers deliberately share one source file so the
    # collaboration rule is visible on the generated task page.
    demo_details = [
        {"repository": "demo_repo", "file_path": DEMO_FILE, "coverage_file": DEMO_FILE,
         "review_file_path": DEMO_FILE, "line_number": 3, "execution_count": 0,
         "status": enhance_coverage.coverage_check.STATUS_UNCOVERED,
         "author_name": "Alice Chen", "author_email": "alice@example.com",
         "suggested_reviewer": "Alice Chen", "commit": "demoa1234567",
         "subject": "add calculator branches"},
        {"repository": "demo_repo", "file_path": DEMO_FILE, "coverage_file": DEMO_FILE,
         "review_file_path": DEMO_FILE, "line_number": 5, "execution_count": 0,
         "status": enhance_coverage.coverage_check.STATUS_UNCOVERED,
         "author_name": "Bob Li", "author_email": "bob@example.com",
         "suggested_reviewer": "Bob Li", "commit": "demob7654321",
         "subject": "complete boundary branch"},
        {"repository": "demo_repo", "file_path": DEMO_FILE, "coverage_file": DEMO_FILE,
         "review_file_path": DEMO_FILE, "line_number": 7, "execution_count": 1,
         "status": enhance_coverage.coverage_check.STATUS_COVERED,
         "author_name": "Bob Li", "author_email": "bob@example.com",
         "suggested_reviewer": "Bob Li", "commit": "demob7654321",
         "subject": "complete boundary branch"},
    ]
    developer_changes = [
        {"repository": "demo_repo", "commit": "demoa1234567", "author_name": "Alice Chen",
         "author_email": "alice@example.com", "committed_at": "2026-08-13T09:30:00+08:00",
         "subject": "add calculator branches", "file_path": DEMO_FILE, "change_type": "M"},
        {"repository": "demo_repo", "commit": "demob7654321", "author_name": "Bob Li",
         "author_email": "bob@example.com", "committed_at": "2026-08-13T10:15:00+08:00",
         "subject": "complete boundary branch", "file_path": DEMO_FILE, "change_type": "M"},
    ]
    demo_result = {
        "schema_version": 3,
        "generated_at": "2026-08-13 10:30:00",
        "oldgit": "demo-old", "newgit": "demo-new",
        "summary": {"changed_lines": 3, "covered": 1, "uncovered": 2, "ignored": 0,
                    "missing": 0, "coverable_total": 3, "coverage_rate": 33.3333333333},
        "details": demo_details,
        "reviewers_by_file": {
            DEMO_FILE: {"3": "Alice Chen", "5": "Bob Li"}
        },
        "function_ranges_by_file": {},
        "developer_file_changes": developer_changes,
        "developer_tasks": enhance_coverage.coverage_check.build_developer_tasks(
            demo_details, developer_changes
        ),
    }
    enhance_coverage.write_incremental_summary_page(html_dir, DEMO_PROJECT, demo_result, unanalyzed_by_file={})
    enhance_coverage.write_incremental_developer_tasks_page(html_dir, DEMO_PROJECT, demo_result)
    enhance_coverage.coverage_check.write_result_json(
        demo_result, os.path.join(html_dir, "incremental_coverage.json")
    )
    enhance_coverage.coverage_check.write_result_excel(
        demo_result, os.path.join(html_dir, "incremental_coverage.xlsx")
    )

    index = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Coverage Tool Demo</title>
<body><h1>Coverage Tool 浏览器 Demo</h1><div class="card"><p>项目：<code>coverage_demo</code>。示例使用 SQLite 持久化，不需要安装 MySQL。</p><p>建议先打开增量覆盖率审查汇总页（可查看完整版本号、生成时间与新特性说明），或直接打开源码填写页进行在线分析暂存与提交。</p><a href="html/incremental_coverage.html">打开增量覆盖率审查汇总页</a><a href="html/calculator.c.gcov.html?mode=lazy_collapse">打开源码填写页 (Lazy Collapse)</a><a class="secondary" href="html/coverage_progress.html?project=coverage_demo">查看进展与导出</a><a href="html/incremental_developer_tasks.html">查看开发人员增量清单</a></div></body></html>"""
    with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as target:
        target.write(index)


def main():
    parser = argparse.ArgumentParser(description="Launch a self-contained Coverage Tool browser demo")
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
    parser.add_argument("--output", default=os.path.join(enhance_coverage.SCRIPT_DIR, ".coverage_demo"))
    args = parser.parse_args()

    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)
    build_demo_site(output_dir)
    enhance_coverage.db_manager = DemoDatabaseManager(os.path.join(output_dir, "coverage_demo.sqlite3"))
    DemoHTTPRequestHandler.static_root = output_dir
    server = ThreadingHTTPServer((args.host, args.port), DemoHTTPRequestHandler)
    print("[Demo] Coverage browser demo is ready.", flush=True)
    print("[Demo] Open: http://localhost:{}/".format(args.port), flush=True)
    print("[Demo] SQLite: {}".format(os.path.join(output_dir, "coverage_demo.sqlite3")), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
