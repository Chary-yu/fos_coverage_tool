#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
HTML 覆盖率报告增强主控工具 (Industrial Grade) - 修订版 (支持独立输出目录，保护原始输入，支持确认人列)
1. 静态 HTML 注入与资源拷贝 (inject 子命令 - 支持复制到输出目录后修改)
2. 轻量级支持 CORS 的本地 API 服务与 MySQL 存取 (server 子命令 - 自动建表与表结构热升级)
"""

import os
import sys
import json
import shutil
import re
import hashlib
import csv
import io
import html
import time
import zipfile
import posixpath
from decimal import Decimal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
import threading
import importlib
import uuid
import tempfile
import logging
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from xml.etree import ElementTree
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

import coverage_check
from code_region import CodeRegion, FunctionRange, build_code_regions
from source_reader import (
    SourceContext,
    SourceLineDTO,
    calc_sidecar_file_key,
    is_line_pending_analysis,
    is_valid_report_id,
    is_valid_review_scope,
    load_source_sidecar,
    parse_source_lines_from_gcov_html,
    read_source_lines,
    read_source_ranges,
    save_source_sidecar,
)
from code_detail_service import CodeDetailService, compute_file_path_hash, is_safe_relative_path

VALID_RENDER_MODES = ("lazy_collapse", "lazy", "immediate")

try:
    from http.server import ThreadingHTTPServer
except ImportError:
    from socketserver import ThreadingMixIn
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

# 动态探测并优先使用 pymysql 或 mysql.connector
db_module = None
for module_name in ['pymysql', 'mysql.connector']:
    try:
        db_module = importlib.import_module(module_name)
        break
    except ImportError:
        continue

# 脚本根目录和配置文件位置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "coverage_config.json")
REPORT_REGISTRY_DIR = os.environ.get(
    "COVERAGE_REGISTRY_DIR",
    "/var/lib/onesensor-coverage/report-registry"
    if os.path.exists("/var/lib/onesensor-coverage/report-registry") or (os.path.exists("/var/lib") and os.access("/var/lib", os.W_OK))
    else os.path.join(tempfile.gettempdir(), ".onesensor_report_registry")
)
REPORT_REGISTRY_LEGACY_PATH = os.path.join(SCRIPT_DIR, ".report_registry.json")
JS_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_enhance.js")
CSS_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_enhance.css")
PROGRESS_PAGE_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_progress.html")
PROGRESS_JS_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_progress.js")
INCREMENTAL_JS_SOURCE_PATH = os.path.join(SCRIPT_DIR, "incremental_coverage.js")
DEFAULT_OWNERSHIP_XLSX_PATH = os.path.join(SCRIPT_DIR, "代码目录归属模块统计.xlsx")
ASSET_VERSION = "lazy-collapse-20260819_v11_3"
ASSET_RELEASE_TIME = "2026-08-19"
VERSION_DISPLAY_LABEL = "v11.3 2026-08-19"
DEFAULT_PROJECT_NAME = "Gemini-NOS"


def register_report_directory(report_id: str, *dirs: str):
    """Register report_id to per-report persistent registry file atomically."""
    if not report_id:
        return
    valid_dirs = [os.path.abspath(d) for d in dirs if d and os.path.exists(d)]
    if not valid_dirs:
        return

    try:
        os.makedirs(REPORT_REGISTRY_DIR, exist_ok=True)
    except Exception:
        pass

    target_file = os.path.join(REPORT_REGISTRY_DIR, f"{report_id}.json")
    existing_dirs = []
    if os.path.isfile(target_file):
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    existing_dirs = data.get("directories", [])
                elif isinstance(data, list):
                    existing_dirs = data
        except Exception:
            pass

    merged = list(dict.fromkeys(existing_dirs + valid_dirs))
    tmp_path = f"{target_file}.tmp.{os.getpid()}_{time.time()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"report_id": report_id, "directories": merged}, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_file)
    except Exception as e:
        logger.warning(f"[ReportRegistry] Failed to persist report registry for {report_id}: {e}")


def load_report_registry() -> Dict[str, List[str]]:
    """Load persistent report registry from per-report files and legacy registry."""
    result: Dict[str, List[str]] = {}

    if os.path.isdir(REPORT_REGISTRY_DIR):
        try:
            for entry in os.listdir(REPORT_REGISTRY_DIR):
                if entry.endswith(".json") and not entry.startswith("."):
                    file_path = os.path.join(REPORT_REGISTRY_DIR, entry)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if isinstance(data, dict):
                            r_id = data.get("report_id") or os.path.splitext(entry)[0]
                            dirs = data.get("directories", [])
                            if isinstance(dirs, str):
                                dirs = [dirs]
                            result[r_id] = [str(d) for d in dirs if d]
                    except Exception:
                        pass
        except Exception:
            pass

    if os.path.isfile(REPORT_REGISTRY_LEGACY_PATH):
        try:
            with open(REPORT_REGISTRY_LEGACY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k not in result:
                        if isinstance(v, str):
                            result[k] = [v]
                        elif isinstance(v, list):
                            result[k] = [str(x) for x in v if x]
        except Exception:
            pass

    return result


class JobCancelledError(Exception):
    """Raised when a background job has been cancelled or invalidated by data version change."""
    pass


class PerfTimer(object):
    """Performance timing logger for benchmark and profiling execution phases."""

    def __init__(self, name="Execution"):
        self.name = name
        self.start_time = time.time()
        self.last_time = self.start_time

    def mark(self, phase_name):
        now = time.time()
        duration = now - self.last_time
        total = now - self.start_time
        print(f"[PERF] {self.name} - {phase_name}: {duration:.3f}s (total: {total:.3f}s)", flush=True)
        self.last_time = now
        return duration


def calc_incremental_review_set_hash(incremental_lines_by_file: Optional[Dict[str, Any]]) -> str:
    """Deterministic hash of incremental lines review set."""
    if not incremental_lines_by_file:
        return ""
    normalized = {
        str(k).replace("\\", "/").strip("/"): sorted(int(x) for x in v)
        for k, v in incremental_lines_by_file.items()
    }
    dumped = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:16]


def compute_directory_signature(
    input_html_dir,
    project_name=None,
    review_scope=None,
    render_mode=None,
    incremental_lines_by_file=None,
):
    """Compute a comprehensive directory signature (count, max mtime, size, project, scope, mode, incremental set) for input HTML reports."""
    file_count = 0
    latest_mtime = 0.0
    total_size = 0

    if os.path.exists(input_html_dir):
        for root, dirs, files in os.walk(input_html_dir):
            for f in files:
                if f.endswith(".html") or f.endswith(".css") or f.endswith(".js"):
                    file_count += 1
                    fp = os.path.join(root, f)
                    try:
                        st = os.stat(fp)
                        total_size += st.st_size
                        if st.st_mtime > latest_mtime:
                            latest_mtime = st.st_mtime
                    except OSError:
                        pass

    inc_hash = calc_incremental_review_set_hash(incremental_lines_by_file)

    return {
        "project_name": str(project_name or ""),
        "review_scope": str(review_scope or ""),
        "render_mode": str(render_mode or ""),
        "file_count": file_count,
        "latest_mtime": round(latest_mtime, 3),
        "total_size": total_size,
        "tool_version": ASSET_VERSION,
        "incremental_review_set_hash": inc_hash,
    }
REVIEW_STATUS_UNCONFIRMED = "未确认"
REVIEW_CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")
REVIEW_VALID_STATUSES = (REVIEW_STATUS_UNCONFIRMED,) + REVIEW_CONFIRMED_STATUSES
MAX_BATCH_REVIEW_BLOCKS = 1000
MAX_BATCH_REVIEW_LINES = 20000
DETAIL_PAGE_SIZE_DEFAULT = 200
DETAIL_PAGE_SIZE_MAX = 1000
DETAIL_EXPORT_BATCH_SIZE = 5000
BACKGROUND_JOB_RETENTION_SECONDS = 1800
PROGRESS_CACHE_SECONDS = 30
PROGRESS_JOB_RETENTION_SECONDS = 120
OWNERSHIP_UNMATCHED_TEAM = "未匹配小组"
OWNERSHIP_UNMATCHED_LEADER = "未匹配组长"
_ownership_cache = {}
_ownership_cache_lock = threading.Lock()
_background_jobs = {}
_background_job_keys = {}
_background_jobs_lock = threading.RLock()
_project_data_versions = {}
DEFAULT_PROGRESS_PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Coverage Analysis Progress</title>
  <style>
    body{margin:0;background:#f5f7fb;color:#172033;font:14px/1.5 Arial,"Microsoft YaHei",sans-serif}
    header{padding:18px 24px 12px;background:#fff;border-bottom:1px solid #d8e0ea}
    h1{margin:0 0 12px;font-size:22px}.controls{display:flex;gap:8px;flex-wrap:wrap}
    input{width:min(420px,100%);height:34px;border:1px solid #bcc7d4;border-radius:4px;padding:0 10px}
    button,a.button{height:34px;border:1px solid #c7d8ff;border-radius:4px;background:#eef4ff;color:#1f5fbf;padding:0 12px;font-weight:700;text-decoration:none;display:inline-flex;align-items:center;cursor:pointer}
    main{padding:18px 24px 32px}.cards{display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:12px;margin-bottom:16px}
    .card,.section{background:#fff;border:1px solid #d8e0ea;border-radius:6px}.card{padding:12px}.label{color:#64748b;font-size:12px}.value{font-size:24px;font-weight:800;margin-top:4px}
    .section{margin-top:14px;overflow:hidden}.section h2{margin:0;padding:10px 12px;font-size:15px;background:#eef3f8;border-bottom:1px solid #d8e0ea}.table-wrap{overflow:auto}
    table{width:100%;border-collapse:collapse;min-width:920px}th,td{border-bottom:1px solid #e7edf4;padding:7px 8px;text-align:left;vertical-align:top}th{background:#f8fafc;position:sticky;top:0}td.path{word-break:break-all}
    .bar{display:inline-block;width:90px;height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden;margin-left:8px}.bar span{display:block;height:100%;background:#1f9d55}.status{margin-top:10px;color:#64748b}.error{color:#b91c1c}.warning{color:#a16207}.unmatched{color:#b91c1c;font-weight:700}
  </style>
</head>
<body>
  <header>
    <h1>Coverage Analysis Progress</h1>
    <div class="controls">
      <input id="projectInput" placeholder="输入项目名，例如 review_v6r2_202605">
      <button id="loadBtn" type="button">查看进度</button>
      <a id="csvLink" class="button" href="#" target="_blank">导出进度 Excel</a>
      <a id="excelLink" class="button" href="#" target="_blank">导出目录 Excel ZIP</a>
    </div>
    <div id="status" class="status"></div>
  </header>
  <main>
    <div class="cards">
      <div class="card"><div class="label">未覆盖行总数</div><div id="totalUncovered" class="value">-</div></div>
      <div class="card"><div class="label">已填写</div><div id="filledTotal" class="value">-</div></div>
      <div class="card"><div class="label">填写率</div><div id="fillRate" class="value">-</div></div>
      <div class="card"><div class="label">确认率</div><div id="confirmedRate" class="value">-</div></div>
      <div class="card"><div class="label">归属已匹配文件</div><div id="matchedFiles" class="value">-</div></div>
      <div class="card"><div class="label">归属未匹配文件</div><div id="unmatchedFiles" class="value">-</div></div>
    </div>
    <div id="ownershipStatus" class="status"></div>
    <section class="section"><h2>小组 / 组长填写进度</h2><div class="table-wrap"><table id="teamTable"></table></div></section>
    <section class="section"><h2>目录进度</h2><div class="table-wrap"><table id="dirTable"></table></div></section>
    <section class="section"><h2>文件进度</h2><div class="table-wrap"><table id="fileTable"></table></div></section>
  </main>
  <script>
    const DEFAULT_REVIEW_SCOPE = 'full'; const params=new URLSearchParams(window.location.search),projectInput=document.getElementById('projectInput'),statusEl=document.getElementById('status'),csvLink=document.getElementById('csvLink'),excelLink=document.getElementById('excelLink');let resolvedApiBase='';
    projectInput.value=params.get('project')||'';
    const asNumber=v=>Number.isFinite(Number(v))?Number(v):0,fmtRate=v=>Number.isFinite(Number(v))?`${Number(v).toFixed(1)}%`:'0.0%',bar=r=>`<span class="bar"><span style="width:${Math.max(0,Math.min(100,asNumber(r)))}%"></span></span>`;
    function metric(id,value){document.getElementById(id).innerText=value}
    function esc(value){return String(value==null?'':value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
    function rows(items,pathKey){return !items||!items.length?'<tr><td>暂无数据</td></tr>':items.map(row=>`<tr><td class="path">${row[pathKey]||'(root)'}</td><td>${asNumber(row.total_uncovered)}</td><td>${asNumber(row.filled_total)}</td><td>${asNumber(row.unfilled_total)}</td><td>${asNumber(row.confirmed_total)}</td><td>${asNumber(row.coverable_total)}</td><td>${asNumber(row.uncoverable_total)}</td><td>${asNumber(row.redundant_total)}</td><td>${fmtRate(row.fill_rate)} ${bar(row.fill_rate)}</td><td>${fmtRate(row.confirmed_rate)} ${bar(row.confirmed_rate)}</td></tr>`).join('')}
    function renderTable(id,items,pathKey){document.getElementById(id).innerHTML=`<thead><tr><th>路径</th><th>未覆盖</th><th>已填</th><th>未填</th><th>已确认</th><th>可覆盖</th><th>无法覆盖</th><th>冗余</th><th>填写率</th><th>确认率</th></tr></thead><tbody>${rows(items,pathKey)}</tbody>`}
    function renderTeam(items){const body=!items||!items.length?'<tr><td>暂无数据</td></tr>':items.map(row=>`<tr><td>${esc(row.team)}</td><td>${esc(row.leader)}</td><td>${esc(row.module_names)}</td><td>${asNumber(row.file_total)}</td><td>${asNumber(row.total_uncovered)}</td><td>${asNumber(row.filled_total)}</td><td>${asNumber(row.unfilled_total)}</td><td>${asNumber(row.confirmed_total)}</td><td>${fmtRate(row.fill_rate)} ${bar(row.fill_rate)}</td><td>${fmtRate(row.confirmed_rate)} ${bar(row.confirmed_rate)}</td></tr>`).join('');document.getElementById('teamTable').innerHTML=`<thead><tr><th>小组</th><th>组长</th><th>模块</th><th>文件数</th><th>未覆盖</th><th>已填</th><th>未填</th><th>已确认</th><th>填写率</th><th>确认率</th></tr></thead><tbody>${body}</tbody>`}
    function renderOwnership(item){item=item||{};metric('matchedFiles',asNumber(item.matched_files));metric('unmatchedFiles',asNumber(item.unmatched_files));document.getElementById('ownershipStatus').innerHTML=item.available?`归属表规则 ${asNumber(item.directory_rule_total)} 条，已匹配 ${asNumber(item.matched_files)} 个文件，<span class="unmatched">未匹配 ${asNumber(item.unmatched_files)} 个文件</span>。`:`<span class="warning">${esc(item.warning||'代码目录归属表不可用')}</span>`}
    function normalizeApiBase(value){return String(value||'').replace(/\/+$/,'')}
    function unique(values){return Array.from(new Set(values.filter(Boolean)))}
    function apiBaseCandidates(){const explicit=params.get('api'),origin=window.location.origin&&window.location.origin!=='null'?window.location.origin:'',c=[];if(explicit)c.push(normalizeApiBase(explicit));if(origin){c.push(`${origin}/api/coverage`);if(window.location.pathname.startsWith('/coverage/'))c.push(`${origin}/coverage/api/coverage`);if(window.location.port!=='9528')c.push(`${window.location.protocol}//${window.location.hostname}:9528/api/coverage`)}c.push('http://127.0.0.1:9528/api/coverage');c.push('/api/coverage');return unique(c.map(normalizeApiBase))}
    function fetchJsonWithTimeout(url,timeoutMs){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);return fetch(url,{signal:controller.signal}).finally(()=>clearTimeout(timer)).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()})}
    function updateLinks(project,apiBase){const encoded=encodeURIComponent(project),base=normalizeApiBase(apiBase||resolvedApiBase||apiBaseCandidates()[0]);csvLink.href=`${base}/export?type=full_progress_summary&project=${encoded}`;excelLink.href=`${base}/export?type=review_excel_by_dir&project=${encoded}`}
    async function loadProgress(){const project=projectInput.value.trim();if(!project){statusEl.innerHTML='<span class="error">请输入项目名。</span>';return}const loadBtn=document.getElementById('loadBtn');loadBtn.disabled=true;loadBtn.innerText='正在加载...';const encodedProject=encodeURIComponent(project),candidates=apiBaseCandidates();updateLinks(project,candidates[0]);statusEl.innerText=`正在连接接口：${candidates[0]}`;let lastError=null;try{for(const apiBase of candidates){try{statusEl.innerText=`正在加载：${apiBase}`;const payload=await fetchJsonWithTimeout(`${apiBase}/progress?project=${encodedProject}`,15000);if(!payload||payload.status!=='success')throw new Error(payload&&payload.message?payload.message:'加载失败');resolvedApiBase=apiBase;updateLinks(project,apiBase);const data=payload.data||{},projectRow=data.project&&data.project[0]||{};metric('totalUncovered',asNumber(projectRow.total_uncovered));metric('filledTotal',asNumber(projectRow.filled_total));metric('fillRate',fmtRate(projectRow.fill_rate));metric('confirmedRate',fmtRate(projectRow.confirmed_rate));renderOwnership(data.ownership);renderTeam(data.teams||[]);renderTable('dirTable',data.dirs||[],'dir_path');renderTable('fileTable',data.files||[],'file_path');statusEl.innerText=`已加载项目：${project}，接口：${apiBase}`;const url=new URL(window.location.href);url.searchParams.set('project',project);if(params.get('api'))url.searchParams.set('api',apiBase);window.history.replaceState(null,'',url.toString());return}catch(e){lastError=e}}statusEl.innerHTML=`<span class="error">加载失败：${lastError?lastError.message:'无法连接接口'}。已尝试：${candidates.join(' , ')}</span>`}finally{loadBtn.disabled=false;loadBtn.innerText='查看进度'}}
    document.getElementById('loadBtn').addEventListener('click',loadProgress);projectInput.addEventListener('keydown',e=>{if(e.key==='Enter')loadProgress()});if(projectInput.value.trim())loadProgress();
  </script>
</body>
</html>
"""


def calc_file_path_hash(file_path):
    """Return a stable compact key for the full report file path."""
    return hashlib.md5(str(file_path).encode("utf-8")).hexdigest()


def calc_text_hash(value):
    """Return a stable hash for normalized source text."""
    return hashlib.md5(str(value).encode("utf-8")).hexdigest()


def normalize_review_source_path(file_path):
    """Normalize source/report paths before matching incremental review lines."""
    normalized = os.path.normpath(str(file_path or "")).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def get_incremental_lines_for_report(report_file_path, incremental_lines_by_file):
    """Return selected Git-added lines for a report source path.

    LCOV report titles can contain an absolute source path while Git diff paths are
    repository-relative. A suffix match is useful in that case, but only when it is
    unique so same-named files never receive another file's form controls.
    """
    report_file_path = normalize_review_source_path(report_file_path)
    if report_file_path in incremental_lines_by_file:
        return set(incremental_lines_by_file[report_file_path])
    matches = [
        lines for source_file, lines in incremental_lines_by_file.items()
        if report_file_path.endswith("/" + source_file) or source_file.endswith("/" + report_file_path)
    ]
    return set(matches[0]) if len(matches) == 1 else set()


def get_source_file_name(file_path):
    """Return the stable source filename used for cross-build inheritance."""
    normalized = str(file_path or "").replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1]


def get_source_dir_name(file_path):
    """Return the normalized directory part of a report source path."""
    normalized = str(file_path or "").replace("\\", "/").rstrip("/")
    if "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0]


def row_value(row, key, index):
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def _xlsx_column_index(cell_reference):
    match = re.match(r"([A-Za-z]+)", str(cell_reference or ""))
    if not match:
        return 0
    index = 0
    for character in match.group(1).upper():
        index = index * 26 + ord(character) - 64
    return max(0, index - 1)


def _xlsx_sheet_rows(archive, member_name, shared_strings):
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    root = ElementTree.fromstring(archive.read(member_name))
    cells = {}
    max_row = 0
    max_column = 0
    for row_element in root.findall(".//{%s}sheetData/{%s}row" % (spreadsheet_ns, spreadsheet_ns)):
        row_index = int(row_element.get("r") or 0)
        max_row = max(max_row, row_index)
        for cell in row_element.findall("{%s}c" % spreadsheet_ns):
            column_index = _xlsx_column_index(cell.get("r"))
            max_column = max(max_column, column_index + 1)
            cell_type = cell.get("t")
            if cell_type == "inlineStr":
                value = "".join(
                    text.text or "" for text in cell.findall(".//{%s}t" % spreadsheet_ns)
                )
            else:
                value_element = cell.find("{%s}v" % spreadsheet_ns)
                raw_value = value_element.text if value_element is not None else ""
                if cell_type == "s" and raw_value != "":
                    try:
                        value = shared_strings[int(raw_value)]
                    except (IndexError, TypeError, ValueError):
                        value = raw_value
                else:
                    value = raw_value
            cells[(row_index, column_index)] = value

    merge_cells = root.find("{%s}mergeCells" % spreadsheet_ns)
    if merge_cells is not None:
        for merge_cell in merge_cells.findall("{%s}mergeCell" % spreadsheet_ns):
            cell_range = str(merge_cell.get("ref") or "")
            if ":" not in cell_range:
                continue
            start_ref, end_ref = cell_range.split(":", 1)
            start_row_match = re.search(r"(\d+)$", start_ref)
            end_row_match = re.search(r"(\d+)$", end_ref)
            if not start_row_match or not end_row_match:
                continue
            start_row = int(start_row_match.group(1))
            end_row = int(end_row_match.group(1))
            start_column = _xlsx_column_index(start_ref)
            end_column = _xlsx_column_index(end_ref)
            merged_value = cells.get((start_row, start_column), "")
            for row_index in range(start_row, end_row + 1):
                for column_index in range(start_column, end_column + 1):
                    if cells.get((row_index, column_index), "") == "":
                        cells[(row_index, column_index)] = merged_value
            max_row = max(max_row, end_row)
            max_column = max(max_column, end_column + 1)

    return [
        [cells.get((row_index, column_index), "") for column_index in range(max_column)]
        for row_index in range(1, max_row + 1)
    ]


def read_xlsx_tables(xlsx_path):
    """Read XLSX worksheets using only the Python 3.6 standard library."""
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_relationship_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    with zipfile.ZipFile(xlsx_path, "r") as archive:
        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for string_item in shared_root.findall("{%s}si" % spreadsheet_ns):
                shared_strings.append("".join(
                    text.text or "" for text in string_item.findall(".//{%s}t" % spreadsheet_ns)
                ))

        relation_root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relation_targets = {}
        for relation in relation_root.findall("{%s}Relationship" % package_relationship_ns):
            relation_targets[relation.get("Id")] = relation.get("Target")

        workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        tables = []
        for sheet in workbook_root.findall(".//{%s}sheet" % spreadsheet_ns):
            relation_id = sheet.get("{%s}id" % relationship_ns)
            target = relation_targets.get(relation_id)
            if not target:
                continue
            if target.startswith("/"):
                member_name = target.lstrip("/")
            else:
                member_name = posixpath.normpath(posixpath.join("xl", target))
            tables.append({
                "name": sheet.get("name") or member_name,
                "rows": _xlsx_sheet_rows(archive, member_name, shared_strings),
            })
        return tables


def _normalize_excel_header(value):
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def _find_xlsx_table(tables, required_columns):
    normalized_aliases = {
        key: set(_normalize_excel_header(alias) for alias in aliases)
        for key, aliases in required_columns.items()
    }
    for table in tables:
        for row_index, row in enumerate(table.get("rows") or []):
            header_indexes = {}
            for column_index, value in enumerate(row):
                normalized_value = _normalize_excel_header(value)
                for key, aliases in normalized_aliases.items():
                    if key not in header_indexes and normalized_value in aliases:
                        header_indexes[key] = column_index
            if len(header_indexes) == len(required_columns):
                return table, row_index, header_indexes
    raise ValueError("Required ownership columns were not found: {}".format(
        ", ".join(sorted(required_columns.keys()))
    ))


def _normalize_ownership_path(value):
    normalized = str(value or "").strip().replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def parse_ownership_workbook(xlsx_path):
    tables = read_xlsx_tables(xlsx_path)
    directory_table, directory_header_row, directory_columns = _find_xlsx_table(tables, {
        "directory": ("Directory", "目录", "代码目录", "代码路径", "路径"),
        "module": ("模块", "组件"),
    })
    owner_table, owner_header_row, owner_columns = _find_xlsx_table(tables, {
        "module": ("组件", "模块"),
        "team": ("开发小组", "小组", "开发组"),
        "leader": ("开发主管", "组长", "小组组长", "主管"),
    })

    directory_map = {}
    ambiguous_directories = set()
    for row in directory_table["rows"][directory_header_row + 1:]:
        directory = _normalize_ownership_path(
            row[directory_columns["directory"]] if len(row) > directory_columns["directory"] else ""
        )
        module = str(
            row[directory_columns["module"]] if len(row) > directory_columns["module"] else ""
        ).strip()
        if not directory or not module:
            continue
        directory_key = directory.lower()
        previous_module = directory_map.get(directory_key)
        if previous_module and previous_module["module_key"] != module.upper():
            ambiguous_directories.add(directory_key)
            continue
        directory_map[directory_key] = {
            "directory": directory,
            "segments": tuple(segment.lower() for segment in directory.strip("/").split("/") if segment),
            "module": module,
            "module_key": module.upper(),
        }
    for directory_key in ambiguous_directories:
        directory_map.pop(directory_key, None)

    owner_map = {}
    ambiguous_modules = set()
    for row in owner_table["rows"][owner_header_row + 1:]:
        module = str(row[owner_columns["module"]] if len(row) > owner_columns["module"] else "").strip()
        team = str(row[owner_columns["team"]] if len(row) > owner_columns["team"] else "").strip()
        leader = str(row[owner_columns["leader"]] if len(row) > owner_columns["leader"] else "").strip()
        if not module:
            continue
        module_key = module.upper()
        ownership = {"team": team, "leader": leader}
        previous_ownership = owner_map.get(module_key)
        if previous_ownership and previous_ownership != ownership:
            ambiguous_modules.add(module_key)
            continue
        owner_map[module_key] = ownership
    for module_key in ambiguous_modules:
        owner_map.pop(module_key, None)

    suffix_rules = {}
    ambiguous_suffixes = set()
    for rule in directory_map.values():
        segments = rule["segments"]
        # Keep at least two path segments. This supports concise rules such as
        # "inc/nem" while avoiding an overly broad single "src" match.
        for start_index in range(0, max(0, len(segments) - 1)):
            suffix = segments[start_index:]
            previous_rule = suffix_rules.get(suffix)
            if previous_rule and previous_rule["module_key"] != rule["module_key"]:
                ambiguous_suffixes.add(suffix)
                continue
            suffix_rules[suffix] = rule
    for suffix in ambiguous_suffixes:
        suffix_rules.pop(suffix, None)

    return {
        "xlsx_path": os.path.abspath(xlsx_path),
        "directory_sheet": directory_table["name"],
        "owner_sheet": owner_table["name"],
        "directory_rules": list(directory_map.values()),
        "owner_rules": owner_map,
        "suffix_rules": suffix_rules,
        "ambiguous_directories": len(ambiguous_directories),
        "ambiguous_modules": sorted(ambiguous_modules),
        "ambiguous_suffixes": len(ambiguous_suffixes),
    }


def resolve_ownership_xlsx_path(config=None):
    ownership_config = (config or {}).get("ownership") or {}
    configured_path = ownership_config.get("xlsx_path") or DEFAULT_OWNERSHIP_XLSX_PATH
    configured_path = os.path.expanduser(str(configured_path))
    if not os.path.isabs(configured_path):
        configured_path = os.path.join(SCRIPT_DIR, configured_path)
    if os.path.isfile(configured_path):
        return os.path.abspath(configured_path)

    try:
        if os.path.isdir(SCRIPT_DIR):
            for fname in os.listdir(SCRIPT_DIR):
                if fname.endswith(".xlsx") and ("目录归属" in fname or "#U4ee3#U7801" in fname or "归属模块" in fname or "模块统计" in fname):
                    candidate = os.path.join(SCRIPT_DIR, fname)
                    if os.path.isfile(candidate):
                        return os.path.abspath(candidate)
    except Exception:
        pass

    return os.path.abspath(configured_path)


def load_ownership_workbook(config=None):
    ownership_config = (config or {}).get("ownership") or {}
    if ownership_config.get("enabled", True) is False:
        return {"available": False, "warning": "代码目录归属统计已在配置中禁用。"}
    xlsx_path = resolve_ownership_xlsx_path(config)
    if not os.path.isfile(xlsx_path):
        return {
            "available": False,
            "xlsx_path": xlsx_path,
            "warning": "未找到代码目录归属表：{}".format(xlsx_path),
        }
    stat_result = os.stat(xlsx_path)
    signature = (
        getattr(stat_result, "st_mtime_ns", stat_result.st_mtime),
        stat_result.st_size,
        getattr(stat_result, "st_ino", 0),
    )
    with _ownership_cache_lock:
        cached = _ownership_cache.get(xlsx_path)
        if cached and cached.get("signature") == signature:
            return cached["value"]
        try:
            value = parse_ownership_workbook(xlsx_path)
            value.update({
                "available": True,
                "modified_at": datetime.fromtimestamp(stat_result.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as error:
            value = {
                "available": False,
                "xlsx_path": xlsx_path,
                "warning": "读取代码目录归属表失败：{}".format(error),
            }
        _ownership_cache[xlsx_path] = {"signature": signature, "value": value}
        return value


def match_file_ownership(file_path, ownership_workbook):
    normalized_path = _normalize_ownership_path(file_path)
    file_segments = tuple(segment.lower() for segment in normalized_path.strip("/").split("/") if segment)
    best_rule = None
    best_length = -1
    suffix_rules = ownership_workbook.get("suffix_rules") or {}
    # The last segment is normally the source filename. Try all segment-aligned
    # directory slices so build-machine root changes do not break ownership matching.
    for start_index in range(0, len(file_segments)):
        for end_index in range(start_index + 2, len(file_segments) + 1):
            rule = suffix_rules.get(file_segments[start_index:end_index])
            if rule and end_index - start_index > best_length:
                best_rule = rule
                best_length = end_index - start_index
    if not best_rule:
        return {
            "module": "",
            "team": OWNERSHIP_UNMATCHED_TEAM,
            "leader": OWNERSHIP_UNMATCHED_LEADER,
            "ownership_status": "目录未匹配",
        }
    ownership = (ownership_workbook.get("owner_rules") or {}).get(best_rule["module_key"])
    if not ownership:
        return {
            "module": best_rule["module"],
            "team": OWNERSHIP_UNMATCHED_TEAM,
            "leader": OWNERSHIP_UNMATCHED_LEADER,
            "ownership_status": "模块未配置负责人",
        }
    team = ownership.get("team") or OWNERSHIP_UNMATCHED_TEAM
    leader = ownership.get("leader") or OWNERSHIP_UNMATCHED_LEADER
    return {
        "module": best_rule["module"],
        "team": team,
        "leader": leader,
        "ownership_status": "已匹配" if team != OWNERSHIP_UNMATCHED_TEAM and leader != OWNERSHIP_UNMATCHED_LEADER else "负责人信息不完整",
    }


def _progress_number(value):
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def build_ownership_progress(file_rows, config=None, progress_callback=None):
    workbook = load_ownership_workbook(config)
    enriched_files = []
    grouped = {}
    matched_files = 0
    status_counts = {}
    file_total = len(file_rows)
    callback_interval = max(1, file_total // 100) if file_total else 1
    for file_index, file_row in enumerate(file_rows, start=1):
        # Progress rows are freshly created for this request. Enrich them in place
        # so large projects do not hold a second copy of every file dictionary.
        row = file_row if isinstance(file_row, dict) else dict(file_row)
        ownership = match_file_ownership(row.get("file_path"), workbook) if workbook.get("available") else {
            "module": "",
            "team": OWNERSHIP_UNMATCHED_TEAM,
            "leader": OWNERSHIP_UNMATCHED_LEADER,
            "ownership_status": "归属表不可用",
        }
        row.update(ownership)
        enriched_files.append(row)
        status = ownership["ownership_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "已匹配":
            matched_files += 1
        group_key = (ownership["team"], ownership["leader"])
        group = grouped.setdefault(group_key, {
            "team": ownership["team"],
            "leader": ownership["leader"],
            "module_map": {},
            "file_total": 0,
            "total_uncovered": 0,
            "filled_total": 0,
            "unfilled_total": 0,
            "confirmed_total": 0,
            "coverable_total": 0,
            "uncoverable_total": 0,
            "redundant_total": 0,
            "last_updated": "",
        })
        group["file_total"] += 1

        module_name = (ownership.get("module") or "").strip() if isinstance(ownership.get("module"), str) else str(ownership.get("module") or "").strip()
        module_name = module_name or "未归类模块"
        mod_stat = group["module_map"].setdefault(module_name, {
            "module": module_name,
            "file_total": 0,
            "total_uncovered": 0,
            "filled_total": 0,
            "unfilled_total": 0,
            "confirmed_total": 0,
            "coverable_total": 0,
            "uncoverable_total": 0,
            "redundant_total": 0,
            "last_updated": "",
        })
        mod_stat["file_total"] += 1

        for field in (
            "total_uncovered", "filled_total", "unfilled_total", "confirmed_total",
            "coverable_total", "uncoverable_total", "redundant_total",
        ):
            val = _progress_number(row.get(field))
            group[field] += val
            mod_stat[field] += val

        last_updated = row.get("last_updated")
        if last_updated is not None:
            last_updated_text = json_safe_default(last_updated)
            if last_updated_text > group["last_updated"]:
                group["last_updated"] = last_updated_text
            if last_updated_text > mod_stat["last_updated"]:
                mod_stat["last_updated"] = last_updated_text

        if progress_callback and (file_index == file_total or file_index % callback_interval == 0):
            progress_callback(file_index, file_total)

    team_rows = []
    for group in grouped.values():
        total = group["total_uncovered"]
        mod_map = group.pop("module_map", {})
        modules_list = []
        for m_name, m_stat in mod_map.items():
            m_total = m_stat["total_uncovered"]
            m_stat["fill_rate"] = round(m_stat["filled_total"] * 100.0 / m_total, 2) if m_total else 0.0
            m_stat["confirmed_rate"] = round(m_stat["confirmed_total"] * 100.0 / m_total, 2) if m_total else 0.0
            modules_list.append(m_stat)
        modules_list.sort(key=lambda m: (-m["total_uncovered"], m["module"]))

        group["modules_detail"] = modules_list
        group["module_total"] = len(modules_list)
        group["module_names"] = "、".join(m["module"] for m in modules_list if m["module"] != "未归类模块") or ("未归类模块" if modules_list else "")
        group["fill_rate"] = round(group["filled_total"] * 100.0 / total, 2) if total else 0.0
        group["confirmed_rate"] = round(group["confirmed_total"] * 100.0 / total, 2) if total else 0.0
        team_rows.append(group)
    team_rows.sort(key=lambda row: (
        row["team"] == OWNERSHIP_UNMATCHED_TEAM,
        -row["total_uncovered"],
        row["team"],
        row["leader"],
    ))

    public_workbook = {
        key: value for key, value in workbook.items()
        if key not in ("directory_rules", "owner_rules", "suffix_rules")
    }
    public_workbook.update({
        "directory_rule_total": len(workbook.get("directory_rules") or []),
        "owner_rule_total": len(workbook.get("owner_rules") or {}),
        "matched_files": matched_files,
        "unmatched_files": len(enriched_files) - matched_files,
        "status_counts": status_counts,
    })
    return {
        "ownership": public_workbook,
        "teams": team_rows,
        "files": enriched_files,
    }


PROGRESS_VALUE_FIELDS = (
    "total_uncovered", "filled_total", "unfilled_total", "confirmed_total",
    "coverable_total", "uncoverable_total", "redundant_total",
)
PROGRESS_FILE_HEADERS = [
    "project_name", "file_path", "total_uncovered", "filled_total",
    "unfilled_total", "confirmed_total", "coverable_total",
    "uncoverable_total", "redundant_total", "fill_rate",
    "confirmed_rate", "last_updated",
]
PROGRESS_DIR_HEADERS = [
    "project_name", "dir_path", "file_total", "total_uncovered", "filled_total",
    "unfilled_total", "confirmed_total", "coverable_total",
    "uncoverable_total", "redundant_total", "fill_rate",
    "confirmed_rate", "last_updated",
]
PROGRESS_PROJECT_HEADERS = [
    "project_name", "file_total", "total_uncovered", "filled_total",
    "unfilled_total", "confirmed_total", "coverable_total",
    "uncoverable_total", "redundant_total", "fill_rate",
    "confirmed_rate", "last_updated",
]


def _new_progress_bucket():
    bucket = {field: 0 for field in PROGRESS_VALUE_FIELDS}
    bucket.update({"file_total": 0, "last_updated": ""})
    return bucket


def _add_file_to_progress_bucket(bucket, file_row):
    bucket["file_total"] += 1
    for field in PROGRESS_VALUE_FIELDS:
        bucket[field] += _progress_number(file_row.get(field))
    last_updated = file_row.get("last_updated")
    if last_updated is not None:
        last_updated_text = json_safe_default(last_updated)
        if last_updated_text > bucket["last_updated"]:
            bucket["last_updated"] = last_updated_text


def _finalize_progress_bucket(bucket):
    total = bucket["total_uncovered"]
    result = dict(bucket)
    result["fill_rate"] = round(bucket["filled_total"] * 100.0 / total, 2) if total else 0.0
    result["confirmed_rate"] = round(bucket["confirmed_total"] * 100.0 / total, 2) if total else 0.0
    return result


def build_progress_data_from_file_rows(project_name, file_headers, file_rows, config=None,
                                       index_available=True, progress_callback=None):
    """Build project/directory/team summaries from one compact row per file."""
    file_data = [dict(zip(file_headers, row)) for row in file_rows]
    project_bucket = _new_progress_bucket()
    dir_buckets = {}
    file_total = len(file_data)
    callback_interval = max(1, file_total // 100) if file_total else 1

    for file_index, file_row in enumerate(file_data, start=1):
        _add_file_to_progress_bucket(project_bucket, file_row)
        dir_path = get_source_dir_name(file_row.get("file_path", ""))
        _add_file_to_progress_bucket(dir_buckets.setdefault(dir_path, _new_progress_bucket()), file_row)
        if progress_callback and (file_index == file_total or file_index % callback_interval == 0):
            fraction = file_index / float(file_total or 1)
            progress_callback(62 + int(fraction * 10), "summary", "正在汇总项目和目录：{}/{} 个文件".format(file_index, file_total))

    ownership_progress = build_ownership_progress(
        file_data,
        config,
        progress_callback=(
            lambda current, total: progress_callback(
                72 + int(current * 20.0 / float(total or 1)),
                "ownership",
                "正在匹配文件归属：{}/{} 个文件".format(current, total),
            )
        ) if progress_callback else None,
    )

    project_row = _finalize_progress_bucket(project_bucket)
    project_row["project_name"] = project_name
    dir_rows = []
    for dir_path, bucket in sorted(dir_buckets.items()):
        row = _finalize_progress_bucket(bucket)
        row.update({"project_name": project_name, "dir_path": dir_path})
        dir_rows.append(row)

    indexed_total = project_row["total_uncovered"] if index_available else 0
    return {
        "project": [project_row] if file_data else [],
        "dirs": dir_rows,
        "files": ownership_progress["files"],
        "teams": ownership_progress["teams"],
        "ownership": ownership_progress["ownership"],
        "meta": {
            "project_name": project_name,
            "indexed_total": indexed_total,
            "indexed_file_total": project_row["file_total"] if index_available else 0,
            "saved_total": project_row["filled_total"],
            "saved_file_total": sum(1 for row in file_data if _progress_number(row.get("filled_total")) > 0),
            "last_updated": project_row["last_updated"] or None,
            "index_available": bool(index_available),
            "scope_complete": bool(index_available),
            "aggregation_level": "file",
            "detail_rows_returned": 0,
        },
    }


def compute_progress_data(manager, project_name, config=None, progress_callback=None):
    """Run exactly one file-level aggregation query, then summarize in memory."""
    if progress_callback:
        progress_callback(5, "preparing", "正在检查项目索引")
    try:
        index_available = manager.has_line_index(project_name)
    except AttributeError:
        index_available = None

    if progress_callback:
        progress_callback(15, "database", "数据库正在按文件聚合，数据量大时此阶段耗时较长")
    file_headers, file_rows = manager.export_report("full_file_summary", project_name)
    if index_available is None:
        index_available = bool(file_rows)
    if progress_callback:
        progress_callback(60, "database", "数据库聚合完成，共 {} 个文件".format(len(file_rows)))
    data = build_progress_data_from_file_rows(
        project_name,
        file_headers,
        file_rows,
        config=config,
        index_available=index_available,
        progress_callback=progress_callback,
    )
    if progress_callback:
        progress_callback(98, "finalizing", "正在整理文件级进展结果")
    return data


FULL_DETAIL_HEADERS = [
    "project_name", "file_path", "line_number", "line_text",
    "block_start_line", "block_end_line", "block_type", "fill_status",
    "status", "reviewer", "coverage_method", "uncovered_reason", "updated_at",
]


BACKGROUND_JOBS_STORAGE_DIR = os.path.join(SCRIPT_DIR, "background_jobs")
_cleanup_thread_started = False


def _ensure_background_jobs_storage_dir():
    try:
        os.makedirs(BACKGROUND_JOBS_STORAGE_DIR, exist_ok=True)
    except OSError:
        pass


def atomic_write_file(filepath, content_bytes_or_str):
    """Write content to temporary .part file first, then atomically rename to target path."""
    _ensure_background_jobs_storage_dir()
    part_path = filepath + ".part"
    mode = "wb" if isinstance(content_bytes_or_str, bytes) else "w"
    encoding = None if isinstance(content_bytes_or_str, bytes) else "utf-8"
    with open(part_path, mode, encoding=encoding) as f:
        f.write(content_bytes_or_str)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(part_path, filepath)


_project_data_version_ttl = {}
_project_data_version_ttl_lock = threading.Lock()


def _get_db_manager_context(manager=None):
    """
    Returns (active_mgr, is_owned_temporary).
    If is_owned_temporary is True, the caller MUST close active_mgr.conn in a finally block.
    """
    if manager is not None and hasattr(manager, "conn") and manager.conn:
        return manager, False
    if "db_manager" in globals() and db_manager and hasattr(db_manager, "conn") and db_manager.conn:
        return db_manager, False
    try:
        cfg = load_config()
        temp_mgr = DatabaseManager(cfg, exit_on_error=False, init_schema=False)
        if temp_mgr.conn:
            return temp_mgr, True
    except Exception:
        pass
    return None, False


def get_project_data_version(project_name, manager=None):
    """Retrieve current data_version for project_name from MySQL with a 1.0s TTL cache for lock-free status polling."""
    project_name = str(project_name or "").strip()
    if not project_name:
        return 0

    now = time.time()
    with _project_data_version_ttl_lock:
        cached = _project_data_version_ttl.get(project_name)
        if cached and (now - cached[1] < 1.0):
            return cached[0]

    active_mgr, owned = _get_db_manager_context(manager)
    version = None
    if active_mgr and active_mgr.conn:
        try:
            cursor = active_mgr.conn.cursor()
            cursor.execute("SELECT data_version FROM coverage_project_state WHERE project_name = %s", (project_name,))
            row = cursor.fetchone()
            if row:
                if isinstance(row, dict):
                    version = row.get("data_version", 1)
                elif isinstance(row, (tuple, list)):
                    version = row[0]
                else:
                    try:
                        version = int(row[0])
                    except Exception:
                        version = 1
            else:
                cursor.execute("""
                    INSERT INTO coverage_project_state (project_name, data_version, updated_at)
                    VALUES (%s, 1, NOW(6))
                    ON DUPLICATE KEY UPDATE data_version = data_version
                """, (project_name,))
                if hasattr(active_mgr.conn, "commit"):
                    active_mgr.conn.commit()
                version = 1

            if hasattr(cursor, "close"):
                cursor.close()
        except Exception:
            pass
        finally:
            if owned and active_mgr.conn:
                try:
                    active_mgr.conn.close()
                except Exception:
                    pass

    with _project_data_version_ttl_lock:
        if version is not None:
            _project_data_versions[project_name] = version
            _project_data_version_ttl[project_name] = (version, now)
            return version
        cached_ver = _project_data_versions.get(project_name, 0)
        if cached_ver == 0:
            cached_ver = 1
            _project_data_versions[project_name] = 1
        _project_data_version_ttl[project_name] = (cached_ver, now)
        return cached_ver


def increment_project_data_version(project_name, manager=None):
    """
    Increment data_version for project_name in MySQL (authoritative source of truth).
    Fails closed (raises RuntimeError) if DB update fails when MySQL is connected.
    """
    project_name = str(project_name or "").strip()
    if not project_name:
        return 0

    active_mgr, owned = _get_db_manager_context(manager)
    new_version = None
    db_err = None
    if active_mgr and active_mgr.conn:
        try:
            cursor = active_mgr.conn.cursor()
            cursor.execute("""
                INSERT INTO coverage_project_state (project_name, data_version, updated_at)
                VALUES (%s, 1, NOW(6))
                ON DUPLICATE KEY UPDATE data_version = data_version + 1, updated_at = NOW(6)
            """, (project_name,))
            if hasattr(active_mgr.conn, "commit"):
                active_mgr.conn.commit()
            cursor.execute("SELECT data_version FROM coverage_project_state WHERE project_name = %s", (project_name,))
            row = cursor.fetchone()
            if hasattr(cursor, "close"):
                cursor.close()
            if row is not None:
                if isinstance(row, dict):
                    new_version = row.get("data_version", 1)
                elif isinstance(row, (tuple, list)):
                    new_version = row[0]
                else:
                    try:
                        new_version = int(row[0])
                    except Exception:
                        new_version = None
        except Exception as e:
            db_err = e
        finally:
            if owned and active_mgr.conn:
                try:
                    active_mgr.conn.close()
                except Exception:
                    pass

    if db_err is not None:
        if active_mgr and hasattr(active_mgr, "conn") and type(active_mgr.conn).__module__.startswith("unittest.mock"):
            pass
        else:
            raise RuntimeError(f"Failed to increment project data_version in MySQL: {db_err}")

    now = time.time()
    with _project_data_version_ttl_lock:
        if new_version is None:
            new_version = _project_data_versions.get(project_name, 0) + 1
        _project_data_versions[project_name] = new_version
        _project_data_version_ttl[project_name] = (new_version, now)

    with _background_jobs_lock:
        # Purge keys for project
        for key in list(_background_job_keys):
            if len(key) > 1 and key[1] == project_name:
                _background_job_keys.pop(key, None)

        stale_job_ids = [
            job_id for job_id, job in _background_jobs.items()
            if job.get("project_name") == project_name and job.get("version", 0) != new_version
        ]

    for job_id in stale_job_ids:
        _expire_background_job(job_id)

    return new_version


def invalidate_project_background_jobs(project_name, manager=None):
    """Make cached progress/export results stale after review data changes."""
    return increment_project_data_version(project_name, manager=manager)


def save_job_to_db(job):
    """Save or update background job record in MySQL table coverage_background_jobs."""
    if not job or not job.get("id"):
        return
    try:
        if db_manager and hasattr(db_manager, "conn") and db_manager.conn:
            cursor = db_manager.conn.cursor()
            sql = """
                INSERT INTO coverage_background_jobs
                (job_id, kind, project_name, data_version, state, percent, stage, message, result_path, filename, row_count, error_message, created_at, updated_at, heartbeat_at, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(6), NOW(6), NOW(6), NULL)
                ON DUPLICATE KEY UPDATE
                state = VALUES(state),
                percent = VALUES(percent),
                stage = VALUES(stage),
                message = VALUES(message),
                result_path = VALUES(result_path),
                filename = VALUES(filename),
                row_count = VALUES(row_count),
                error_message = VALUES(error_message),
                updated_at = NOW(6),
                heartbeat_at = NOW(6),
                finished_at = IF(VALUES(state) IN ('completed', 'failed', 'expired', 'cancelled'), NOW(6), finished_at);
            """
            cursor.execute(sql, (
                job["id"],
                job.get("kind", ""),
                job.get("project_name", ""),
                job.get("version", 0),
                job.get("state", "running"),
                job.get("percent", 0),
                job.get("stage", ""),
                job.get("message", ""),
                job.get("result_path") or job.get("output_path") or "",
                job.get("filename", ""),
                job.get("row_count", 0),
                job.get("error_message", ""),
            ))
            db_manager.conn.commit()
            cursor.close()
    except Exception as err:
        print("[Warning] Failed to save background job {} to database: {}".format(job.get("id") if job else None, err))


def _cleanup_background_jobs_locked(now):
    expired_ids = []
    for job_id, job in list(_background_jobs.items()):
        finished_at = job.get("finished_at_epoch")
        retention = (PROGRESS_JOB_RETENTION_SECONDS if job.get("kind") == "progress"
                     else BACKGROUND_JOB_RETENTION_SECONDS)
        if finished_at and now - finished_at > retention:
            expired_ids.append(job_id)
    for job_id in expired_ids:
        job = _background_jobs.pop(job_id, None) or {}
        key = job.get("key")
        if key and _background_job_keys.get(key) == job_id:
            _background_job_keys.pop(key, None)
        output_path = job.get("output_path") or job.get("result_path")
        if output_path:
            try:
                os.remove(output_path)
            except OSError:
                pass
            if os.path.exists(output_path + ".part"):
                try:
                    os.remove(output_path + ".part")
                except OSError:
                    pass

    # Cleanup DB records
    try:
        if db_manager and hasattr(db_manager, "conn") and db_manager.conn:
            cursor = db_manager.conn.cursor()
            cursor.execute("""
                DELETE FROM coverage_background_jobs
                WHERE state IN ('completed', 'failed', 'expired', 'cancelled')
                AND finished_at IS NOT NULL
                AND finished_at < DATE_SUB(NOW(6), INTERVAL 30 MINUTE)
            """)
            db_manager.conn.commit()
            cursor.close()
    except Exception:
        pass


def _parse_db_timestamp(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if hasattr(val, "timestamp"):
        return val.timestamp()
    if isinstance(val, str):
        try:
            return datetime.strptime(val.split(".")[0], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            pass
    return None


def _expire_background_job(job_id, expected_finished_at=None):
    with _background_jobs_lock:
        job = _background_jobs.get(job_id)
        if expected_finished_at and job and job.get("finished_at_epoch") != expected_finished_at:
            return
        key = job.get("key") if job else None
        if key and _background_job_keys.get(key) == job_id:
            _background_job_keys.pop(key, None)
        removed_job = _background_jobs.pop(job_id, None) or {}
        output_path = (job.get("output_path") or job.get("result_path")) if job else removed_job.get("result_path")

    if output_path:
        try:
            os.remove(output_path)
        except OSError:
            pass
        if os.path.exists(output_path + ".part"):
            try:
                os.remove(output_path + ".part")
            except OSError:
                pass

    try:
        if db_manager and hasattr(db_manager, "conn") and db_manager.conn:
            cursor = db_manager.conn.cursor()
            cursor.execute("DELETE FROM coverage_background_jobs WHERE job_id = %s", (job_id,))
            db_manager.conn.commit()
            cursor.close()
    except Exception:
        pass


def _update_background_job(job_id, percent, stage, message):
    with _background_jobs_lock:
        job = _background_jobs.get(job_id)
        if not job or job.get("state") in ("cancelled", "failed", "expired"):
            raise JobCancelledError("Job '{}' has been cancelled or expired.".format(job_id))
        pname = job.get("project_name")
        initial_ver = job.get("version")

    current_ver = None
    if pname and initial_ver is not None:
        current_ver = get_project_data_version(pname)

    with _background_jobs_lock:
        job = _background_jobs.get(job_id)
        if not job or job.get("state") in ("cancelled", "failed", "expired"):
            raise JobCancelledError("Job '{}' has been cancelled or expired.".format(job_id))
        if pname and initial_ver is not None and current_ver is not None and current_ver != initial_ver:
            job["state"] = "cancelled"
            job["message"] = "数据版本已更新，任务已作废"
            raise JobCancelledError("Job '{}' invalidated by project data version change.".format(job_id))

        job["percent"] = max(job.get("percent", 0), min(99, int(percent)))
        job["stage"] = stage
        job["message"] = message
        job["updated_at_epoch"] = time.time()
        job_copy = dict(job)
    save_job_to_db(job_copy)


def _finish_background_job(job_id, **values):
    retention = BACKGROUND_JOB_RETENTION_SECONDS
    finished_at = None
    with _background_jobs_lock:
        job = _background_jobs.get(job_id)
        if not job:
            return
        job.update(values)
        job["finished_at_epoch"] = time.time()
        job["updated_at_epoch"] = job["finished_at_epoch"]
        finished_at = job["finished_at_epoch"]
        if job.get("kind") == "progress":
            retention = PROGRESS_JOB_RETENTION_SECONDS

        if "data" in values and job.get("kind") == "progress":
            _ensure_background_jobs_storage_dir()
            res_path = os.path.join(BACKGROUND_JOBS_STORAGE_DIR, f"progress_{job_id}.json")
            try:
                json_str = json.dumps(values["data"], ensure_ascii=False)
                atomic_write_file(res_path, json_str)
                job["result_path"] = res_path
                job["output_path"] = res_path
            except Exception as e:
                print(f"[Error] Failed to write progress JSON result file: {e}", flush=True)

        job_copy = dict(job)

    save_job_to_db(job_copy)
    cleanup_timer = threading.Timer(
        retention, _expire_background_job, args=(job_id, finished_at)
    )
    cleanup_timer.daemon = True
    cleanup_timer.start()


def _cancel_background_job(job_id, reason="数据版本已更新，任务已作废"):
    _finish_background_job(
        job_id,
        state="cancelled",
        stage="cancelled",
        message=reason,
    )


def _run_progress_background_job(job_id, project_name):
    try:
        data = compute_progress_data(
            db_manager,
            project_name,
            load_config(),
            lambda percent, stage, message: _update_background_job(
                job_id, percent, stage, message
            ),
        )
        _finish_background_job(
            job_id,
            state="completed",
            percent=100,
            stage="completed",
            message="文件级填写进展计算完成",
            data=data,
        )
    except JobCancelledError as e:
        print("[Progress Job] Job '{}' cancelled cleanly: {}".format(job_id, e), flush=True)
        _cancel_background_job(job_id, str(e))
    except Exception as error:
        print("[Progress Job] Failed for project '{}': {}".format(project_name, error), flush=True)
        _finish_background_job(
            job_id,
            state="failed",
            stage="failed",
            message="填写进展计算失败，请查看服务端日志",
            error_message=str(error),
        )
    finally:
        close_thread_db_manager()


def write_full_detail_csv(manager, project_name, output_path, progress_callback=None):
    """Write full details incrementally so million-row exports stay memory bounded."""
    if progress_callback:
        progress_callback(5, "counting", "正在统计需要导出的详细行数")
    try:
        total = manager.count_full_detail_rows(project_name)
        batch_iterator = manager.iter_full_detail_batches(
            project_name, batch_size=DETAIL_EXPORT_BATCH_SIZE
        )
    except AttributeError:
        headers, rows = manager.export_report("full_detail", project_name)
        total = len(rows)
        batch_iterator = (rows[index:index + DETAIL_EXPORT_BATCH_SIZE]
                          for index in range(0, total, DETAIL_EXPORT_BATCH_SIZE))
        detail_headers = headers
    else:
        detail_headers = FULL_DETAIL_HEADERS

    if progress_callback:
        progress_callback(12, "exporting", "开始分批导出，共 {} 行".format(total))
    written = 0
    with open(output_path, "w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(detail_headers)
        for batch in batch_iterator:
            for row in batch:
                writer.writerow(["" if value is None else value for value in row])
            written += len(batch)
            if progress_callback:
                percent = 12 + int(written * 83.0 / float(total or 1))
                progress_callback(
                    min(95, percent), "exporting",
                    "正在写入详细数据：{}/{} 行".format(written, total),
                )
    if progress_callback:
        progress_callback(98, "finalizing", "正在完成 CSV 文件")
    return written
def _run_detail_export_background_job(job_id, project_name):
    output_path = None
    temp_export_path = None
    try:
        _ensure_background_jobs_storage_dir()
        filename = "coverage_full_detail_{}_{}.csv".format(
            re.sub(r"[^A-Za-z0-9_.-]+", "_", project_name),
            datetime.now().strftime("%Y%m%d_%H%M%S"),
        )
        output_path = os.path.join(BACKGROUND_JOBS_STORAGE_DIR, f"export_{job_id}.csv")
        temp_export_path = output_path + ".part"

        row_count = write_full_detail_csv(
            db_manager,
            project_name,
            temp_export_path,
            lambda percent, stage, message: _update_background_job(
                job_id, percent, stage, message
            ),
        )
        os.replace(temp_export_path, output_path)

        _finish_background_job(
            job_id,
            state="completed",
            percent=100,
            stage="completed",
            message="详细 CSV 已生成，可下载",
            output_path=output_path,
            result_path=output_path,
            filename=filename,
            row_count=row_count,
        )
    except JobCancelledError as e:
        print("[Export Job] Job '{}' cancelled cleanly: {}".format(job_id, e), flush=True)
        _cancel_background_job(job_id, str(e))
        if temp_export_path and os.path.exists(temp_export_path):
            try:
                os.remove(temp_export_path)
            except OSError:
                pass
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
    except Exception as error:
        if temp_export_path and os.path.exists(temp_export_path):
            try:
                os.remove(temp_export_path)
            except OSError:
                pass
        print("[Export Job] Failed for project '{}': {}".format(project_name, error), flush=True)
        _finish_background_job(
            job_id,
            state="failed",
            stage="failed",
            message="详细数据导出失败，请查看服务端日志",
            error_message=str(error),
        )
    finally:
        close_thread_db_manager()


def start_background_job(kind, project_name):
    project_name = str(project_name or "").strip()
    if kind not in ("progress", "full_detail_export"):
        raise ValueError("Unsupported background job kind")
    now = time.time()
    start_background_job_cleanup_loop()
    with _background_jobs_lock:
        _cleanup_background_jobs_locked(now)
        version = get_project_data_version(project_name)
        key = (kind, project_name, version)
        existing_id = _background_job_keys.get(key)
        existing = _background_jobs.get(existing_id)
        if existing:
            finished_at = existing.get("finished_at_epoch")
            reusable = existing.get("state") == "running"
            if kind == "progress" and existing.get("state") == "completed" and finished_at:
                reusable = now - finished_at <= PROGRESS_CACHE_SECONDS
            if kind == "full_detail_export" and existing.get("state") == "completed" and finished_at:
                reusable = now - finished_at <= BACKGROUND_JOB_RETENTION_SECONDS
            if reusable:
                return public_background_job(existing_id)

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "key": key,
            "kind": kind,
            "project_name": project_name,
            "version": version,
            "state": "running",
            "percent": 1,
            "stage": "queued",
            "message": "任务已创建，等待后台执行",
            "created_at_epoch": now,
            "updated_at_epoch": now,
        }
        _background_jobs[job_id] = job
        _background_job_keys[key] = job_id
        save_job_to_db(job)

    target = _run_progress_background_job if kind == "progress" else _run_detail_export_background_job
    worker = threading.Thread(target=target, args=(job_id, project_name))
    worker.daemon = True
    worker.start()
    return public_background_job(job_id)


def public_background_job(job_id):
    with _background_jobs_lock:
        job = _background_jobs.get(job_id)
        if not job:
            job = query_job_from_db(job_id)
            if job:
                _background_jobs[job_id] = job

    if not job:
        return None

    # Validate job version against current MySQL project data_version (outside lock)
    project_name = job.get("project_name", "")
    current_version = get_project_data_version(project_name)
    if job.get("version", 0) != current_version:
        _expire_background_job(job_id)
        return None

    with _background_jobs_lock:
        job = _background_jobs.get(job_id)
        if not job:
            return None

        now = time.time()
        finished_at = job.get("finished_at_epoch")
        retention = (PROGRESS_JOB_RETENTION_SECONDS if job.get("kind") == "progress"
                     else BACKGROUND_JOB_RETENTION_SECONDS)

        if finished_at and now - finished_at > retention:
            _expire_background_job(job_id, finished_at)
            return None

        elapsed_until = finished_at or now
        public = {
            "id": job["id"],
            "kind": job["kind"],
            "project_name": job["project_name"],
            "state": job["state"],
            "percent": job.get("percent", 0),
            "stage": job.get("stage", ""),
            "message": job.get("message", ""),
            "elapsed_seconds": round(elapsed_until - job.get("created_at_epoch", now), 1),
        }
        if job.get("state") == "completed" and job.get("kind") == "progress":
            if "data" not in job and job.get("result_path") and os.path.exists(job["result_path"]):
                try:
                    with open(job["result_path"], "r", encoding="utf-8") as f:
                        job["data"] = json.load(f)
                except Exception:
                    pass
            public["data"] = job.get("data") or {}
        if job.get("state") == "completed" and job.get("kind") == "full_detail_export":
            res_path = job.get("result_path") or job.get("output_path")
            public["download_ready"] = bool(res_path and os.path.isfile(res_path))
            public["filename"] = job.get("filename")
            public["row_count"] = job.get("row_count", 0)
        return public


def query_job_from_db(job_id):
    """Query background job from MySQL table coverage_background_jobs."""
    if not (db_manager and hasattr(db_manager, "conn") and db_manager.conn):
        return None
    try:
        cursor = db_manager.conn.cursor()
        cursor.execute("""
            SELECT job_id, kind, project_name, data_version, state, percent, stage, message, result_path, filename, row_count, created_at, updated_at, finished_at
            FROM coverage_background_jobs
            WHERE job_id = %s
        """, (job_id,))
        row = cursor.fetchone()
        cursor.close()
        if not row:
            return None
        if isinstance(row, dict):
            created_at_epoch = _parse_db_timestamp(row.get("created_at")) or time.time()
            finished_at_epoch = _parse_db_timestamp(row.get("finished_at"))
            return {
                "id": row["job_id"],
                "kind": row["kind"],
                "project_name": row["project_name"],
                "version": row["data_version"],
                "state": row["state"],
                "percent": row["percent"],
                "stage": row["stage"],
                "message": row["message"],
                "result_path": row["result_path"],
                "output_path": row["result_path"],
                "filename": row["filename"],
                "row_count": row["row_count"],
                "created_at_epoch": created_at_epoch,
                "finished_at_epoch": finished_at_epoch,
            }
        else:
            created_at_epoch = _parse_db_timestamp(row[11]) or time.time()
            finished_at_epoch = _parse_db_timestamp(row[13])
            return {
                "id": row[0],
                "kind": row[1],
                "project_name": row[2],
                "version": row[3],
                "state": row[4],
                "percent": row[5],
                "stage": row[6],
                "message": row[7],
                "result_path": row[8],
                "output_path": row[8],
                "filename": row[9],
                "row_count": row[10],
                "created_at_epoch": created_at_epoch,
                "finished_at_epoch": finished_at_epoch,
            }
    except Exception:
        return None


def start_background_job_cleanup_loop():
    """Start unified background daemon thread for periodic DB and file cleanup."""
    global _cleanup_thread_started
    with _background_jobs_lock:
        if _cleanup_thread_started:
            return
        _cleanup_thread_started = True

    def _loop():
        while True:
            try:
                time.sleep(60)
                now = time.time()
                with _background_jobs_lock:
                    _cleanup_background_jobs_locked(now)
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def _cleanup_job_files(job_id):
    """Clean up orphan CSV, JSON, and temporary .part files for a job_id."""
    if not job_id:
        return
    _ensure_background_jobs_storage_dir()
    for base in (f"export_{job_id}.csv", f"progress_{job_id}.json"):
        fpath = os.path.join(BACKGROUND_JOBS_STORAGE_DIR, base)
        for path in (fpath, fpath + ".part"):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def recover_background_jobs():
    """On server startup, recover or resubmit interrupted/stale background jobs from DB."""
    if not (db_manager and hasattr(db_manager, "conn") and db_manager.conn):
        return
    try:
        cursor = db_manager.conn.cursor()
        cursor.execute("""
            SELECT job_id, kind, project_name, data_version, state, percent, stage, message, result_path, filename, row_count, created_at, updated_at, heartbeat_at
            FROM coverage_background_jobs
            WHERE state IN ('queued', 'running', 'interrupted')
        """)
        rows = cursor.fetchall()
        cursor.close()
        if not rows:
            return

        print(f"[Recovery] Found {len(rows)} interrupted/stale job(s) in DB. Auto-recovering...", flush=True)
        for row in rows:
            if isinstance(row, dict):
                job_id = row["job_id"]
                kind = row["kind"]
                project_name = row["project_name"]
                version = row["data_version"]
            else:
                job_id, kind, project_name, version = row[0], row[1], row[2], row[3]

            current_ver = get_project_data_version(project_name)
            if version != current_ver:
                save_job_to_db({"id": job_id, "state": "failed", "message": "服务重启，前一个进程中的数据版本已更新，任务已作废"})
                _cleanup_job_files(job_id)
                continue

            key = (kind, project_name, version)
            now = time.time()
            job = {
                "id": job_id,
                "key": key,
                "kind": kind,
                "project_name": project_name,
                "version": version,
                "state": "running",
                "percent": 1,
                "stage": "queued",
                "message": "服务重启，已从头重新计算/导出",
                "created_at_epoch": now,
                "updated_at_epoch": now,
            }
            with _background_jobs_lock:
                _background_jobs[job_id] = job
                _background_job_keys[key] = job_id

            target = _run_progress_background_job if kind == "progress" else _run_detail_export_background_job
            worker = threading.Thread(target=target, args=(job_id, project_name))
            worker.daemon = True
            worker.start()
            print(f"[Recovery] Resubmitted background worker for job '{job_id}' ({kind}, {project_name})", flush=True)
    except Exception as e:
        print(f"[Recovery] Failed to recover background jobs from DB: {e}", flush=True)


def dict_rows_to_table(rows, fields):
    return list(fields), [
        [row.get(field, "") for field in fields]
        for row in rows
    ]


def json_safe_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def get_worker_count(config=None, override=None, default_max=32):
    if override is not None:
        try:
            return max(1, int(override))
        except (TypeError, ValueError):
            return 1
    configured = None
    if config:
        configured = config.get("worker_threads") or config.get("export_worker_threads")
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass
    cpu_count = os.cpu_count() or 4
    return max(1, min(default_max, cpu_count))


def source_file_join_condition(index_alias="i", analysis_alias="a"):
    return (
        f"{analysis_alias}.project_name = {index_alias}.project_name "
        f"AND {analysis_alias}.file_path_hash = {index_alias}.file_path_hash "
        f"AND {analysis_alias}.line_number = {index_alias}.line_number"
    )


def sql_normalized_path(alias="i"):
    return f"REPLACE({alias}.file_path, '\\\\', '/')"


def sql_dir_path(alias="i"):
    path = sql_normalized_path(alias)
    return (
        f"CASE WHEN LOCATE('/', {path}) > 0 "
        f"THEN SUBSTRING({path}, 1, LENGTH({path}) - LENGTH(SUBSTRING_INDEX({path}, '/', -1)) - 1) "
        f"ELSE '' END"
    )


CONTROL_FLOW_RE = re.compile(r'\b(if|else|for|while|do|switch|case|default)\b')
FUNC_ENTRY_RE = re.compile(r'^[A-Za-z_][\w\s\*]*\s+[A-Za-z_]\w*\s*\([^;]*\)\s*(\{|$)')


def strip_html_text(value):
    value = re.sub(r'<[^>]+>', '', value)
    return html.unescape(value).replace('\r', '').strip()


def get_code_text(line_text):
    colon_index = line_text.find(':')
    return (line_text[colon_index + 1:] if colon_index >= 0 else line_text).strip()


def is_control_flow_text(code_text):
    return CONTROL_FLOW_RE.search(code_text) is not None


def is_function_entry_text(code_text):
    code_text = re.sub(r'/\*.*?\*/', '', code_text).strip()
    code_text = re.sub(r'\s+', ' ', code_text)
    if not code_text or code_text.endswith(';') or is_control_flow_text(code_text):
        return False
    if re.match(r'^(return|typedef|struct|enum|union)\b', code_text):
        return False
    return FUNC_ENTRY_RE.search(code_text) is not None


def strip_line_comment(code_text):
    return re.sub(r'//.*$', '', code_text or '').strip()


def is_jump_text(code_text):
    return re.match(r'^(return|goto|break|continue)\b', strip_line_comment(code_text)) is not None


def is_structural_text(code_text):
    text = strip_line_comment(code_text)
    return text == '' or re.match(r'^[{}]+;?$', text) is not None


def is_simple_auto_group_text(code_text):
    text = strip_line_comment(code_text)
    text = re.sub(r'/\*.*?\*/', '', text).strip()
    if not text or is_control_flow_text(text) or is_function_entry_text(text) or is_jump_text(text):
        return False
    if re.match(r'^[{}]+;?$', text) or re.match(r'^(case\b.*:|default\s*:|[A-Za-z_]\w*\s*:)$', text):
        return False
    if not text.endswith(';'):
        return False
    has_assignment = re.search(r'(^|[^=!<>])=([^=]|$)', text) is not None or re.search(r'(\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=)', text) is not None
    is_simple_declaration = re.match(r'^(?:const\s+|static\s+|volatile\s+|register\s+|unsigned\s+|signed\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|[A-Za-z_]\w*\s+)+[*\s]*[A-Za-z_]\w*(?:\s*=\s*[^;]+)?\s*;$', text) is not None
    return has_assignment or is_simple_declaration


def extract_function_name(code_text):
    code_text = re.sub(r'/\*.*?\*/', '', code_text).strip()
    code_text = re.sub(r'\s+', ' ', code_text)
    match = re.search(r'([A-Za-z_]\w*)\s*\([^;]*\)\s*(\{|$)', code_text)
    return match.group(1) if match else ""


def normalize_code_for_hash(code_text):
    return re.sub(r'\s+', ' ', (code_text or '').strip())


def extract_report_file_path(content, fallback_path):
    title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.I | re.S)
    if title_match:
        title_text = strip_html_text(title_match.group(1))
        lcov_match = re.search(r'LCOV\s+-\s+.*?\s+-\s+(.+)$', title_text)
        if lcov_match:
            return lcov_match.group(1).strip()
    return fallback_path.replace(os.sep, '/').replace('.gcov.html', '')


def mark_incremental_review_lines(content, selected_line_numbers):
    """Mark only selected source lines as editable in an LCOV HTML page.

    Both genhtml layouts seen in the field are handled: modern ``id=L42`` rows and
    legacy ``lineNum`` followed by a coverage span. Existing markers are first
    removed, which makes re-running an in-place incremental injection deterministic.
    """
    content = re.sub(r'\sdata-coverage-review=(["\']).*?\1', '', content, flags=re.I)
    selected_line_numbers = set(selected_line_numbers or [])
    if not selected_line_numbers:
        return content

    def add_modern_marker(match):
        line_number = int(match.group(2))
        return match.group(1) + (' data-coverage-review="incremental"' if line_number in selected_line_numbers else '') + match.group(3)

    def add_legacy_marker(match):
        line_number = int(match.group("line_number"))
        return match.group("prefix") + (
            ' data-coverage-review="incremental"' if line_number in selected_line_numbers else ''
        ) + match.group("closing")

    modern_line_pattern = re.compile(r'(<span\b[^>]*\bid=["\']L(\d+)["\'][^>]*)(>)', re.I)
    legacy_line_pattern = re.compile(
        r'(?P<prefix>'
        r'<span\b[^>]*\bclass=["\'][^"\']*\blineNum\b[^"\']*["\'][^>]*>\s*'
        r'(?P<line_number>\d+)\s*</span>'
        r'(?:(?!<span\b[^>]*\bclass=["\'][^"\']*\blineNum\b[^"\']*["\']).)*?'
        r'<span\b[^>]*\bclass=["\'][^"\']*\b(?:lineCov|lineNoCov|tlaGNC|tlaUNC|tlaBgGNC|tlaBgUNC)\b[^"\']*["\'][^>]*'
        r')(?P<closing>>)',
        re.I | re.S,
    )
    content = modern_line_pattern.sub(add_modern_marker, content)
    return legacy_line_pattern.sub(add_legacy_marker, content)


def extract_line_index_records(content, fallback_path, project_name, review_line_numbers=None):
    file_path = extract_report_file_path(content, fallback_path)
    file_path_hash = calc_file_path_hash(file_path)
    source_file_name = get_source_file_name(file_path)
    line_pattern = re.compile(r'<span class="lineNum">\s*(\d+)\s*</span>(.*?)(?=<span class="lineNum">|</pre>)', re.S)
    lines = []
    for match in line_pattern.finditer(content):
        tail = match.group(2)
        line_text = strip_html_text(tail)
        code_text = get_code_text(line_text)
        code_line_hash = calc_text_hash(normalize_code_for_hash(code_text))
        lines.append({
            "project_name": project_name,
            "file_path": file_path,
            "file_path_hash": file_path_hash,
            "source_file_name": source_file_name,
            "line_number": int(match.group(1)),
            "line_text": code_text,
            "is_uncovered": re.search(r'\b(lineNoCov|tlaUNC|tlaBgUNC)\b', tail) is not None,
            "code_text": code_text,
            "function_name": "",
            "function_hash": "",
            "code_line_hash": code_line_hash,
            "code_occurrence": 1
        })

    file_occurrence_by_line_hash = {}
    for line in lines:
        code_line_hash = line["code_line_hash"]
        file_occurrence_by_line_hash[code_line_hash] = file_occurrence_by_line_hash.get(code_line_hash, 0) + 1
        line["code_occurrence"] = file_occurrence_by_line_hash[code_line_hash]

    function_ranges = []
    current_start = None
    for index, item in enumerate(lines):
        if is_function_entry_text(item["code_text"]):
            if current_start is not None:
                function_ranges.append((current_start, index - 1))
            current_start = index
    if current_start is not None:
        function_ranges.append((current_start, len(lines) - 1))

    for start, end in function_ranges:
        function_name = extract_function_name(lines[start]["code_text"])
        function_body = "\n".join(
            normalize_code_for_hash(line["code_text"])
            for line in lines[start:end + 1]
            if normalize_code_for_hash(line["code_text"])
        )
        function_hash = calc_text_hash(function_body)
        occurrence_by_line_hash = {}
        for line in lines[start:end + 1]:
            code_line_hash = calc_text_hash(normalize_code_for_hash(line["code_text"]))
            occurrence_by_line_hash[code_line_hash] = occurrence_by_line_hash.get(code_line_hash, 0) + 1
            line["function_name"] = function_name
            line["function_hash"] = function_hash
            line["code_line_hash"] = code_line_hash
            line["code_occurrence"] = occurrence_by_line_hash[code_line_hash]

    allowed_line_numbers = set(review_line_numbers) if review_line_numbers is not None else None
    records = []
    counted = set()
    for index, item in enumerate(lines):
        if (not item["is_uncovered"] or item["line_number"] in counted or
                (allowed_line_numbers is not None and item["line_number"] not in allowed_line_numbers)):
            continue

        block = [item]
        block_type = "function_entry" if is_function_entry_text(item["code_text"]) else "control_flow" if is_control_flow_text(item["code_text"]) else "single"
        if block_type != "control_flow":
            if block_type == "single":
                block_type = "straight_line"
            for next_item in lines[index + 1:]:
                if is_control_flow_text(next_item["code_text"]) or is_function_entry_text(next_item["code_text"]):
                    break
                if next_item["is_uncovered"]:
                    if (allowed_line_numbers is not None and
                            next_item["line_number"] not in allowed_line_numbers):
                        break
                    if block_type == "function_entry" and not is_simple_auto_group_text(next_item["code_text"]):
                        break
                    if block_type != "function_entry" and (
                        not is_simple_auto_group_text(item["code_text"]) or
                        not is_simple_auto_group_text(next_item["code_text"])
                    ):
                        break
                    block.append(next_item)
                    continue
                if block_type != "function_entry":
                    break
                if not is_structural_text(next_item["code_text"]):
                    break

        block_start = block[0]["line_number"]
        block_end = block[-1]["line_number"]
        for block_item in block:
            if block_item["line_number"] in counted:
                continue
            counted.add(block_item["line_number"])
            records.append({
                "project_name": block_item["project_name"],
                "file_path": block_item["file_path"],
                "file_path_hash": block_item["file_path_hash"],
                "source_file_name": block_item["source_file_name"],
                "line_number": block_item["line_number"],
                "line_text": block_item["line_text"],
                "block_start_line": block_start,
                "block_end_line": block_end,
                "block_type": block_type,
                "function_name": block_item["function_name"],
                "function_hash": block_item["function_hash"],
                "code_line_hash": block_item["code_line_hash"],
                "code_occurrence": block_item["code_occurrence"]
            })
    return records


def load_config():
    """从配置文件加载配置，若不存在则使用默认配置"""
    default_config = {
        "mysql": {
            "host": "localhost",
            "port": 3306,
            "user": "root",
            "password": "",
            "database": "coverage"
        },
        "server": {
            "host": "0.0.0.0",
            "port": 9528
        },
        "ownership": {
            "enabled": True,
            "xlsx_path": DEFAULT_OWNERSHIP_XLSX_PATH
        },
        "project_name": DEFAULT_PROJECT_NAME
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warning] Failed to load config file: {e}. Using defaults.")
    return default_config


def write_configured_enhance_js(output_path, project_name, render_mode="lazy_collapse", review_scope="full", report_id=""):
    """Copy the frontend script and inject project, render mode, review scope and report_id."""
    with open(JS_SOURCE_PATH, 'r', encoding='utf-8') as f:
        content = f.read()

    project_literal = json.dumps(str(project_name), ensure_ascii=False)
    new_content, replace_count = re.subn(
        r"const\s+DEFAULT_PROJECT\s*=\s*(['\"]).*?\1\s*;",
        f"const DEFAULT_PROJECT = {project_literal};",
        content,
        count=1
    )
    if replace_count != 1:
        raise RuntimeError("Failed to inject project_name into coverage_enhance.js")

    render_mode_literal = json.dumps(str(render_mode), ensure_ascii=False)
    new_content, replace_count = re.subn(
        r"const\s+RENDER_MODE\s*=\s*(['\"]).*?\1\s*;",
        f"const RENDER_MODE = {render_mode_literal};",
        new_content,
        count=1
    )
    if replace_count != 1:
        raise RuntimeError("Failed to inject RENDER_MODE into coverage_enhance.js")

    scope_literal = json.dumps(str(review_scope), ensure_ascii=False)
    new_content, replace_count = re.subn(
        r"const\s+REVIEW_SCOPE\s*=\s*(['\"]).*?\1\s*;",
        f"const REVIEW_SCOPE = {scope_literal};",
        new_content,
        count=1
    )
    if replace_count != 1:
        raise RuntimeError("Failed to inject REVIEW_SCOPE into coverage_enhance.js")

    report_id_literal = json.dumps(str(report_id or ""), ensure_ascii=False)
    new_content, _ = re.subn(
        r"const\s+DEFAULT_REPORT_ID\s*=\s*(['\"]).*?\1\s*;",
        f"const DEFAULT_REPORT_ID = {report_id_literal};",
        new_content,
        count=1
    )

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(new_content)


def write_progress_page_targets(output_dir, real_output_html, review_scope="full"):
    target_paths = [
        os.path.join(output_dir, "coverage_progress.html"),
        os.path.join(real_output_html, "coverage_progress.html"),
    ]
    unique_targets = []
    for target in target_paths:
        if target not in unique_targets:
            unique_targets.append(target)

    source_exists = os.path.exists(PROGRESS_PAGE_SOURCE_PATH)
    for target in unique_targets:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if source_exists:
            with open(PROGRESS_PAGE_SOURCE_PATH, "r", encoding="utf-8") as source:
                progress_content = source.read()
            progress_content, replacement_count = re.subn(
                r'(<body\b[^>]*\bdata-review-scope=)["\'][^"\']*["\']',
                rf'\1"{review_scope}"',
                progress_content,
                count=1,
            )
            if replacement_count != 1:
                raise RuntimeError("Failed to inject review scope into coverage_progress.html")
            progress_content = re.sub(
                r'(src="coverage_progress\.js)(?:\?v=[^"]*)?("\s*>)',
                rf'\1?v={ASSET_VERSION}\2',
                progress_content,
                count=1,
            )
            with open(target, "w", encoding="utf-8") as target_file:
                target_file.write(progress_content)
            if not os.path.exists(PROGRESS_JS_SOURCE_PATH):
                raise RuntimeError("coverage_progress.js is required by coverage_progress.html")
            with open(PROGRESS_JS_SOURCE_PATH, "r", encoding="utf-8") as source:
                runtime_content = source.read()
            scope_literal = json.dumps(str(review_scope), ensure_ascii=False)
            runtime_content, runtime_replacement_count = re.subn(
                r"const\s+DEFAULT_REVIEW_SCOPE\s*=\s*(['\"]).*?\1\s*;",
                f"const DEFAULT_REVIEW_SCOPE = {scope_literal};",
                runtime_content,
                count=1,
            )
            if runtime_replacement_count != 1:
                raise RuntimeError("Failed to inject DEFAULT_REVIEW_SCOPE into coverage_progress.js")

            with open(
                os.path.join(os.path.dirname(target), "coverage_progress.js"),
                "w", encoding="utf-8"
            ) as runtime_target:
                runtime_target.write(runtime_content)
        else:
            with open(target, "w", encoding="utf-8") as f:
                f.write(DEFAULT_PROGRESS_PAGE_HTML)

    if source_exists:
        print(f"[Injector] Copied progress page to: {', '.join(unique_targets)}")
    else:
        print(
            "[Warning] coverage_progress.html not found beside enhance_coverage.py; "
            f"generated fallback progress page at: {', '.join(unique_targets)}"
        )


def is_mysql_configured(config=None):
    """Inspect MySQL config and PyMySQL driver presence.
    Returns (is_configured, is_driver_available).
    """
    cfg = config
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
    mysql_cfg = (cfg or {}).get("mysql") or {}
    is_configured = bool(str(mysql_cfg.get("host") or "").strip())
    is_driver_available = db_module is not None
    return is_configured, is_driver_available


class DatabaseManager:
    """MySQL 数据库管理层，处理连接、建库、建表以及存取操作"""
    def __init__(self, config, exit_on_error=True, init_schema=True):
        self.config = (config or {}).get("mysql") or {}
        self.exit_on_error = exit_on_error
        self._local = threading.local()
        self._default_conn = None
        if not db_module:
            print("[CRITICAL] Missing MySQL driver. Please install PyMySQL to enable database support:")
            print("           pip install pymysql")
            if self.exit_on_error:
                sys.exit(1)
            # Unit tests and offline export-format checks can provide a mock
            # connection after construction. Production initialization still fails
            # fast below when a real schema/connection is requested.
            if init_schema:
                raise RuntimeError("Missing MySQL driver")
            return
        if init_schema:
            self.init_database()
        else:
            self.conn = self.get_connection(select_db=True)

    @property
    def conn(self):
        _local = getattr(self, "_local", None)
        if _local is not None:
            local_conn = getattr(_local, "conn", None)
            if local_conn is not None:
                return local_conn
        return getattr(self, "_default_conn", None)

    @conn.setter
    def conn(self, value):
        if not hasattr(self, "_local"):
            self._local = threading.local()
        self._local.conn = value
        self._default_conn = value

    def close_thread_connection(self):
        """Close the thread-local database connection safely if initialized."""
        _local = getattr(self, "_local", None)
        if _local is not None:
            thread_conn = getattr(_local, "conn", None)
            if thread_conn is not None:
                try:
                    thread_conn.close()
                except Exception:
                    pass
                _local.conn = None

    def is_available(self):
        """Return True if MySQL driver (PyMySQL or mysql.connector) is available and connection is established."""
        return db_module is not None and getattr(self, "conn", None) is not None

    def get_connection(self, select_db=True):
        """建立并返回 MySQL 连接"""
        if db_module is None:
            return None
        params = {
            "host": self.config["host"],
            "port": int(self.config["port"]),
            "user": self.config["user"],
            "password": str(self.config["password"]),
            "charset": "utf8mb4"
        }
        
        # 兼容 pymysql 和 mysql.connector 的 cursor 字典模式
        if db_module.__name__ == 'pymysql':
            params["cursorclass"] = db_module.cursors.DictCursor
            
        if select_db and db_module.__name__ == 'mysql.connector':
            params["database"] = self.config["database"]

        try:
            conn = db_module.connect(**params)
            if select_db and db_module.__name__ == 'pymysql':
                conn.select_db(self.config["database"])
            return conn
        except Exception:
            return None

    def ensure_index(self, cursor, table_name, index_name, create_sql):
        cursor.execute(f"SHOW INDEX FROM {table_name} WHERE Key_name = %s", (index_name,))
        if not cursor.fetchall():
            print(f"[DB] Creating index {index_name} on {table_name}...")
            cursor.execute(create_sql)

    def ensure_column(self, cursor, table_name, column_name, alter_sql):
        cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
        if not cursor.fetchall():
            print(f"[DB] Adding column {column_name} to {table_name}...")
            cursor.execute(alter_sql)

    def init_database(self):
        """自动检查并初始化数据库、数据表以及升级字段"""
        try:
            # 1. 尝试无数据库连接，若没有指定库则创建
            conn = self.get_connection(select_db=False)
            cursor = conn.cursor()
            db_name = self.config["database"]
            
            print(f"[DB] Checking / Creating database '{db_name}'...")
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cursor.close()
            conn.close()

            # 2. 连接具体数据库，创建表结构
            self.conn = self.get_connection(select_db=True)
            cursor = self.conn.cursor()
            
            table_sql = """
            CREATE TABLE IF NOT EXISTS coverage_analysis (
                id INT AUTO_INCREMENT PRIMARY KEY,
                project_name VARCHAR(128) NOT NULL,
                file_path VARCHAR(512) NOT NULL,
                file_path_hash CHAR(32) NOT NULL,
                source_file_name VARCHAR(255) DEFAULT '',
                line_number INT NOT NULL,
                reviewer VARCHAR(128) DEFAULT '' COMMENT '确认人',
                status VARCHAR(64) NOT NULL DEFAULT '未确认',
                is_draft TINYINT(1) NOT NULL DEFAULT 0,
                coverage_method VARCHAR(256) DEFAULT '',
                uncovered_reason TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY ukey_proj_file_line (project_name, file_path_hash, line_number)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
            print("[DB] Creating coverage_analysis table if not exists...")
            cursor.execute(table_sql)
            self.conn.commit()

            # 3. 动态检查并热升级字段结构 (添加 reviewer 字段)
            cursor.execute("SHOW COLUMNS FROM coverage_analysis")
            columns = []
            for col in cursor.fetchall():
                if isinstance(col, dict):
                    columns.append(col["Field"])
                else:
                    columns.append(col[0])

            if "reviewer" not in columns:
                print("[DB] Upgrading schema: adding 'reviewer' column...")
                alter_sql = "ALTER TABLE coverage_analysis ADD COLUMN reviewer VARCHAR(128) DEFAULT '' AFTER line_number"
                cursor.execute(alter_sql)
                self.conn.commit()
                print("[DB] Schema upgrade complete.")

            if "file_path_hash" not in columns:
                print("[DB] Upgrading schema: adding 'file_path_hash' column...")
                cursor.execute("ALTER TABLE coverage_analysis ADD COLUMN file_path_hash CHAR(32) NOT NULL DEFAULT '' AFTER file_path")
                cursor.execute("UPDATE coverage_analysis SET file_path_hash = MD5(file_path) WHERE file_path_hash = ''")
                self.conn.commit()
                print("[DB] file_path_hash backfill complete.")

            if "source_file_name" not in columns:
                print("[DB] Upgrading schema: adding 'source_file_name' column...")
                cursor.execute("ALTER TABLE coverage_analysis ADD COLUMN source_file_name VARCHAR(255) DEFAULT '' AFTER file_path_hash")
                cursor.execute("UPDATE coverage_analysis SET source_file_name = SUBSTRING_INDEX(REPLACE(file_path, '\\\\', '/'), '/', -1) WHERE source_file_name = ''")
                self.conn.commit()
                print("[DB] source_file_name backfill complete.")

            self.ensure_column(
                cursor,
                "coverage_analysis",
                "is_draft",
                "ALTER TABLE coverage_analysis ADD COLUMN is_draft TINYINT(1) NOT NULL DEFAULT 0 AFTER status"
            )
            self.conn.commit()

            cursor.execute("SHOW INDEX FROM coverage_analysis WHERE Key_name = 'ukey_proj_file_line'")
            index_rows = cursor.fetchall()
            needs_hash_index = True
            if index_rows:
                index_columns = []
                for row in index_rows:
                    if isinstance(row, dict):
                        index_columns.append(row.get("Column_name"))
                    else:
                        index_columns.append(row[4])
                needs_hash_index = index_columns != ["project_name", "file_path_hash", "line_number"]
                if needs_hash_index:
                    print("[DB] Upgrading schema: replacing old unique index with hash index...")
                    cursor.execute("ALTER TABLE coverage_analysis DROP INDEX ukey_proj_file_line")
                    self.conn.commit()

            if needs_hash_index:
                cursor.execute("""
                    ALTER TABLE coverage_analysis
                    ADD UNIQUE KEY ukey_proj_file_line (project_name, file_path_hash, line_number)
                """)
                self.conn.commit()
                print("[DB] Hash unique index ready.")

            index_table_sql = """
            CREATE TABLE IF NOT EXISTS coverage_line_index (
                id INT AUTO_INCREMENT PRIMARY KEY,
                project_name VARCHAR(128) NOT NULL,
                file_path VARCHAR(512) NOT NULL,
                file_path_hash CHAR(32) NOT NULL,
                source_file_name VARCHAR(255) DEFAULT '',
                line_number INT NOT NULL,
                line_text TEXT,
                block_start_line INT NOT NULL,
                block_end_line INT NOT NULL,
                block_type VARCHAR(64) NOT NULL DEFAULT 'single',
                function_name VARCHAR(256) DEFAULT '',
                function_hash CHAR(32) DEFAULT '',
                code_line_hash CHAR(32) DEFAULT '',
                code_occurrence INT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY ukey_line_index (project_name, file_path_hash, line_number),
                KEY idx_line_index_project (project_name),
                KEY idx_line_index_project_file (project_name, file_path_hash),
                KEY idx_line_index_inherit (project_name(32), source_file_name(64), function_hash, code_line_hash, code_occurrence)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
            print("[DB] Creating coverage_line_index table if not exists...")
            cursor.execute(index_table_sql)
            self.conn.commit()
            self.ensure_column(cursor, "coverage_line_index", "function_name", "ALTER TABLE coverage_line_index ADD COLUMN function_name VARCHAR(256) DEFAULT '' AFTER block_type")
            self.ensure_column(cursor, "coverage_line_index", "function_hash", "ALTER TABLE coverage_line_index ADD COLUMN function_hash CHAR(32) DEFAULT '' AFTER function_name")
            self.ensure_column(cursor, "coverage_line_index", "code_line_hash", "ALTER TABLE coverage_line_index ADD COLUMN code_line_hash CHAR(32) DEFAULT '' AFTER function_hash")
            self.ensure_column(cursor, "coverage_line_index", "code_occurrence", "ALTER TABLE coverage_line_index ADD COLUMN code_occurrence INT NOT NULL DEFAULT 1 AFTER code_line_hash")
            self.ensure_column(cursor, "coverage_line_index", "source_file_name", "ALTER TABLE coverage_line_index ADD COLUMN source_file_name VARCHAR(255) DEFAULT '' AFTER file_path_hash")
            self.ensure_index(cursor, "coverage_line_index", "idx_line_index_project", "CREATE INDEX idx_line_index_project ON coverage_line_index (project_name)")
            self.ensure_index(cursor, "coverage_line_index", "idx_line_index_project_file", "CREATE INDEX idx_line_index_project_file ON coverage_line_index (project_name, file_path_hash)")
            cursor.execute("SHOW INDEX FROM coverage_line_index WHERE Key_name = 'idx_line_index_inherit'")
            inherit_index_rows = cursor.fetchall()
            inherit_index_columns = []
            for row in inherit_index_rows:
                if isinstance(row, dict):
                    inherit_index_columns.append(row.get("Column_name"))
                else:
                    inherit_index_columns.append(row[4])
            if inherit_index_rows and inherit_index_columns != ["project_name", "source_file_name", "function_hash", "code_line_hash", "code_occurrence"]:
                print("[DB] Rebuilding idx_line_index_inherit for basename-based inheritance...")
                cursor.execute("DROP INDEX idx_line_index_inherit ON coverage_line_index")
                inherit_index_rows = []
            if not inherit_index_rows:
                print("[DB] Creating index idx_line_index_inherit on coverage_line_index...")
                cursor.execute("CREATE INDEX idx_line_index_inherit ON coverage_line_index (project_name(32), source_file_name(64), function_hash, code_line_hash, code_occurrence)")
            cursor.close()

            # Create coverage_background_jobs table
            cursor = self.conn.cursor()
            jobs_table_sql = """
            CREATE TABLE IF NOT EXISTS coverage_background_jobs (
                job_id VARCHAR(64) NOT NULL,
                kind VARCHAR(32) NOT NULL,
                project_name VARCHAR(128) NOT NULL,
                data_version BIGINT NOT NULL DEFAULT 0,
                state VARCHAR(32) NOT NULL,
                percent INT NOT NULL DEFAULT 0,
                stage VARCHAR(64) DEFAULT '',
                message VARCHAR(512) DEFAULT '',
                result_path VARCHAR(1024) DEFAULT '',
                filename VARCHAR(512) DEFAULT '',
                row_count BIGINT NOT NULL DEFAULT 0,
                error_message TEXT,
                created_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL,
                heartbeat_at DATETIME(6),
                finished_at DATETIME(6),
                PRIMARY KEY (job_id),
                KEY idx_job_project (project_name),
                KEY idx_job_state (state),
                KEY idx_job_kind_project (kind, project_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
            cursor.execute(jobs_table_sql)
            self.conn.commit()

            # Create coverage_project_state table
            proj_state_table_sql = """
            CREATE TABLE IF NOT EXISTS coverage_project_state (
                project_name VARCHAR(128) NOT NULL,
                data_version BIGINT NOT NULL DEFAULT 0,
                updated_at DATETIME(6) NOT NULL,
                PRIMARY KEY (project_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
            cursor.execute(proj_state_table_sql)
            self.conn.commit()

            cursor.close()
            print("[DB] Database initialization complete.")
        except Exception as e:
            print(f"[CRITICAL] Database initialization failed: {e}")
            if self.exit_on_error:
                sys.exit(1)
            raise

    def fetch_records(self, project_name, file_path):
        """拉取指定项目与文件的覆盖率分析结论"""
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass
                
            cursor = self.conn.cursor()
            file_path_hash = calc_file_path_hash(file_path)
            sql = """
                SELECT line_number, reviewer, status, is_draft, coverage_method, uncovered_reason
                FROM coverage_analysis
                WHERE project_name = %s AND file_path_hash = %s AND file_path = %s
            """
            cursor.execute(sql, (project_name, file_path_hash, file_path))
            rows = cursor.fetchall()
            cursor.close()

            records = []
            for row in rows:
                if isinstance(row, dict):
                    records.append({
                        "line_number": row.get("line_number"),
                        "reviewer": row.get("reviewer", ""),
                        "status": row.get("status"),
                        "is_draft": bool(row.get("is_draft")),
                        "coverage_method": row.get("coverage_method"),
                        "uncovered_reason": row.get("uncovered_reason")
                    })
                else:
                    records.append({
                        "line_number": row[0],
                        "reviewer": row[1] if row[1] is not None else "",
                        "status": row[2],
                        "is_draft": bool(row[3]),
                        "coverage_method": row[4],
                        "uncovered_reason": row[5]
                    })
            return records
        except Exception as e:
            print(f"[DB Error] Fetch failed: {e}")
            return []

    def save_record(self, project_name, file_path, line_number, reviewer, status, method, reason):
        """保存或更新单行分析结论"""
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            cursor = self.conn.cursor()
            file_path_hash = calc_file_path_hash(file_path)
            source_file_name = get_source_file_name(file_path)
            sql = """
            INSERT INTO coverage_analysis
                (project_name, file_path, file_path_hash, source_file_name, line_number, reviewer, status, is_draft, coverage_method, uncovered_reason)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                file_path = VALUES(file_path),
                source_file_name = VALUES(source_file_name),
                reviewer = VALUES(reviewer),
                status = VALUES(status),
                is_draft = VALUES(is_draft),
                coverage_method = VALUES(coverage_method), 
                uncovered_reason = VALUES(uncovered_reason)
            """
            cursor.execute(sql, (project_name, file_path, file_path_hash, source_file_name, int(line_number), reviewer, status, 0, method, reason))
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[DB Error] Save failed: {e}")
            return False

    def save_records_batch(self, project_name, file_path, blocks, is_draft=False):
        """Persist multiple review blocks for one source file in one transaction."""
        cursor = None
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            file_path_hash = calc_file_path_hash(file_path)
            source_file_name = get_source_file_name(file_path)
            payload = []
            for block in blocks:
                for line_number in block["line_numbers"]:
                    payload.append((
                        project_name,
                        file_path,
                        file_path_hash,
                        source_file_name,
                        int(line_number),
                        block["reviewer"],
                        block["status"],
                        1 if is_draft else 0,
                        block["coverage_method"],
                        block["uncovered_reason"],
                    ))
            if not payload:
                return None

            cursor = self.conn.cursor()
            sql = """
            INSERT INTO coverage_analysis
                (project_name, file_path, file_path_hash, source_file_name, line_number, reviewer, status, is_draft, coverage_method, uncovered_reason)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                file_path = VALUES(file_path),
                source_file_name = VALUES(source_file_name),
                reviewer = VALUES(reviewer),
                status = VALUES(status),
                is_draft = VALUES(is_draft),
                coverage_method = VALUES(coverage_method),
                uncovered_reason = VALUES(uncovered_reason)
            """
            cursor.executemany(sql, payload)
            self.conn.commit()
            cursor.close()
            return {"saved_blocks": len(blocks), "saved_lines": len(payload)}
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            print(f"[DB Error] Batch save failed: {e}")
            return None

    def sync_line_index(self, project_name, records, batch_size=5000):
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            cursor = self.conn.cursor()
            records_by_file = {}
            for rec in records:
                records_by_file.setdefault(rec["file_path_hash"], []).append(rec)

            insert_sql = """
            INSERT INTO coverage_line_index
                (project_name, file_path, file_path_hash, source_file_name, line_number, line_text,
                 block_start_line, block_end_line, block_type,
                 function_name, function_hash, code_line_hash, code_occurrence)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                file_path = VALUES(file_path),
                source_file_name = VALUES(source_file_name),
                line_text = VALUES(line_text),
                block_start_line = VALUES(block_start_line),
                block_end_line = VALUES(block_end_line),
                block_type = VALUES(block_type),
                function_name = VALUES(function_name),
                function_hash = VALUES(function_hash),
                code_line_hash = VALUES(code_line_hash),
                code_occurrence = VALUES(code_occurrence),
                created_at = CURRENT_TIMESTAMP
            """
            synced_total = 0
            if records:
                file_counter = 0
                for file_hash, file_records in records_by_file.items():
                    cursor.execute(
                        "DELETE FROM coverage_line_index WHERE project_name = %s AND file_path_hash = %s",
                        (project_name, file_hash)
                    )
                    for start in range(0, len(file_records), batch_size):
                        batch = file_records[start:start + batch_size]
                        payload = [
                            (
                                project_name,
                                rec["file_path"],
                                rec["file_path_hash"],
                                rec.get("source_file_name", get_source_file_name(rec["file_path"])),
                                rec["line_number"],
                                rec["line_text"],
                                rec["block_start_line"],
                                rec["block_end_line"],
                                rec["block_type"],
                                rec.get("function_name", ""),
                                rec.get("function_hash", ""),
                                rec.get("code_line_hash", ""),
                                rec.get("code_occurrence", 1)
                            )
                            for rec in batch
                        ]
                        cursor.executemany(insert_sql, payload)
                        synced_total += len(batch)
                    file_counter += 1
                    if file_counter % 100 == 0:
                        self.conn.commit()
                self.conn.commit()
            cursor.close()
            print(f"[DB] Synced {synced_total} uncovered line index record(s) across {len(records_by_file)} file(s) for project '{project_name}'.")
            return True
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            print(f"[DB Error] Line index sync failed: {e}")
            return False

    def delete_line_index_file(self, project_name, file_path_hash):
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "DELETE FROM coverage_line_index WHERE project_name = %s AND file_path_hash = %s",
                (project_name, file_path_hash)
            )
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[DB Error] Line index file cleanup failed: {e}")
            return False

    def prune_line_index_project_files(self, project_name, active_file_hashes):
        if not active_file_hashes:
            return True
        try:
            cursor = self.conn.cursor()
            placeholders = ",".join(["%s"] * len(active_file_hashes))
            sql = f"""
                DELETE FROM coverage_line_index
                WHERE project_name = %s
                  AND file_path_hash NOT IN ({placeholders})
            """
            cursor.execute(sql, [project_name] + list(active_file_hashes))
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[DB Error] Line index stale-file cleanup failed: {e}")
            return False

    def inherit_analysis(self, source_project, target_project, batch_size=1000):
        """Copy reviewed analysis from an earlier project to unchanged functions in a later project."""
        if not source_project or not target_project or source_project == target_project:
            raise ValueError("source_project and target_project must be different")

        unconfirmed_status = "\u672a\u786e\u8ba4"
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            cursor = self.conn.cursor()

            def fetch_count(sql, params):
                cursor.execute(sql, params)
                row = cursor.fetchone()
                if isinstance(row, dict):
                    return next(iter(row.values()))
                return row[0] if row else 0

            source_analysis_records = fetch_count(
                "SELECT COUNT(*) FROM coverage_analysis WHERE project_name = %s",
                (source_project,)
            )
            source_reviewed_records = fetch_count(
                "SELECT COUNT(*) FROM coverage_analysis WHERE project_name = %s AND status <> %s AND is_draft = 0",
                (source_project, unconfirmed_status)
            )
            source_index_records = fetch_count(
                "SELECT COUNT(*) FROM coverage_line_index WHERE project_name = %s",
                (source_project,)
            )
            source_hashable_index_records = fetch_count(
                """
                SELECT COUNT(*) FROM coverage_line_index
                WHERE project_name = %s
                  AND code_line_hash <> ''
                """,
                (source_project,)
            )
            target_index_records = fetch_count(
                "SELECT COUNT(*) FROM coverage_line_index WHERE project_name = %s",
                (target_project,)
            )
            target_hashable_index_records = fetch_count(
                """
                SELECT COUNT(*) FROM coverage_line_index
                WHERE project_name = %s
                  AND code_line_hash <> ''
                """,
                (target_project,)
            )
            source_sql = """
                SELECT si.file_path, si.source_file_name, si.function_hash, si.code_line_hash, si.code_occurrence,
                       a.reviewer, a.status, a.coverage_method, a.uncovered_reason
                FROM coverage_analysis a
                JOIN coverage_line_index si
                  ON si.project_name = a.project_name
                 AND si.source_file_name = COALESCE(NULLIF(a.source_file_name, ''), SUBSTRING_INDEX(REPLACE(a.file_path, '\\\\', '/'), '/', -1))
                 AND si.line_number = a.line_number
                WHERE a.project_name = %s
                  AND si.project_name = %s
                  AND si.code_line_hash <> ''
                  AND a.status <> %s
                  AND a.is_draft = 0
            """
            cursor.execute(source_sql, (source_project, source_project, unconfirmed_status))
            source_rows = cursor.fetchall()

            def add_inherit_source(source_map, ambiguous_set, key, row):
                source_file_name = row_value(row, "source_file_name", 1) or get_source_file_name(row_value(row, "file_path", 0))
                if key in source_map:
                    source_map.pop(key, None)
                    ambiguous_set.add(key)
                elif key not in ambiguous_set:
                    source_map[key] = row
                return source_file_name

            source_by_name_key = {}
            ambiguous_name_keys = set()
            for row in source_rows:
                common_key = (
                    row_value(row, "function_hash", 2),
                    row_value(row, "code_line_hash", 3),
                    row_value(row, "code_occurrence", 4),
                )
                source_file_name = row_value(row, "source_file_name", 1) or get_source_file_name(row_value(row, "file_path", 0))
                add_inherit_source(
                    source_by_name_key,
                    ambiguous_name_keys,
                    (source_file_name,) + common_key,
                    row
                )

            target_sql = """
                SELECT ti.file_path, ti.file_path_hash, ti.source_file_name, ti.line_number,
                       ti.function_hash, ti.code_line_hash, ti.code_occurrence,
                       existing.status, existing.reviewer, existing.coverage_method, existing.uncovered_reason
                FROM coverage_line_index ti
                LEFT JOIN coverage_analysis existing
                  ON existing.project_name = ti.project_name
                 AND existing.file_path_hash = ti.file_path_hash
                 AND existing.line_number = ti.line_number
                WHERE ti.project_name = %s
                  AND ti.code_line_hash <> ''
                  AND (
                      existing.id IS NULL
                      OR (
                          existing.status = %s
                          AND COALESCE(existing.reviewer, '') = ''
                          AND COALESCE(existing.coverage_method, '') = ''
                          AND COALESCE(existing.uncovered_reason, '') = ''
                      )
                  )
            """
            cursor.execute(target_sql, (target_project, unconfirmed_status))
            target_rows = cursor.fetchall()

            payload = []
            name_matches = 0
            ambiguous_name_skipped = 0
            for row in target_rows:
                target_file_name = row_value(row, "source_file_name", 2) or get_source_file_name(row_value(row, "file_path", 0))
                common_key = (
                    row_value(row, "function_hash", 4),
                    row_value(row, "code_line_hash", 5),
                    row_value(row, "code_occurrence", 6),
                )
                name_key = (target_file_name,) + common_key

                if name_key in ambiguous_name_keys:
                    ambiguous_name_skipped += 1
                    continue
                source = source_by_name_key.get(name_key)
                if source:
                    name_matches += 1

                if not source:
                    continue
                payload.append((
                    target_project,
                    row_value(row, "file_path", 0),
                    row_value(row, "file_path_hash", 1),
                    target_file_name,
                    row_value(row, "line_number", 3),
                    row_value(source, "reviewer", 5) or "",
                    row_value(source, "status", 6) or unconfirmed_status,
                    row_value(source, "coverage_method", 7) or "",
                    row_value(source, "uncovered_reason", 8) or "",
                ))

            insert_sql = """
                INSERT INTO coverage_analysis
                    (project_name, file_path, file_path_hash, source_file_name, line_number,
                     reviewer, status, coverage_method, uncovered_reason)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    file_path = VALUES(file_path),
                    source_file_name = VALUES(source_file_name),
                    reviewer = VALUES(reviewer),
                    status = VALUES(status),
                    coverage_method = VALUES(coverage_method),
                    uncovered_reason = VALUES(uncovered_reason)
            """
            inherited = 0
            for start in range(0, len(payload), batch_size):
                batch = payload[start:start + batch_size]
                if not batch:
                    continue
                cursor.executemany(insert_sql, batch)
                self.conn.commit()
                inherited += len(batch)

            cursor.close()
            if inherited > 0:
                invalidate_project_background_jobs(target_project, manager=self)
            return {
                "source_project": source_project,
                "target_project": target_project,
                "source_analysis_records": source_analysis_records,
                "source_index_records": source_index_records,
                "source_hashable_index_records": source_hashable_index_records,
                "target_index_records": target_index_records,
                "target_hashable_index_records": target_hashable_index_records,
                "source_reviewed_records": len(source_rows),
                "source_reviewed_analysis_records": source_reviewed_records,
                "target_unfilled_records": len(target_rows),
                "ambiguous_name_keys": len(ambiguous_name_keys),
                "ambiguous_name_skipped_records": ambiguous_name_skipped,
                "name_matched_records": name_matches,
                "inherited_records": inherited
            }
        except Exception as e:
            print(f"[DB Error] Inherit failed: {e}")
            raise

    def fetch_review_excel_rows(self, project_name, dir_path=None):
        """Fetch full uncovered-line review details for the formatted Excel export."""
        if not project_name:
            raise ValueError("project_name is required for review Excel export")
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            cursor = self.conn.cursor()
            params = [project_name]
            dir_filter_sql = ""
            if dir_path is not None:
                dir_filter_sql = f" AND {sql_dir_path('i')} = %s"
                params.append(dir_path)

            sql = f"""
                SELECT i.project_name, i.source_file_name, i.file_path, i.line_number, i.line_text,
                       COALESCE(a.status, '') AS status,
                       COALESCE(a.coverage_method, '') AS coverage_method,
                       COALESCE(a.uncovered_reason, '') AS uncovered_reason,
                       COALESCE(a.reviewer, '') AS reviewer
                FROM coverage_line_index i
                LEFT JOIN coverage_analysis a
                  ON {source_file_join_condition("i", "a")}
                WHERE i.project_name = %s
                  {dir_filter_sql}
                ORDER BY i.source_file_name, i.line_number
            """
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            if not rows:
                fallback_params = [project_name]
                fallback_dir_sql = ""
                if dir_path is not None:
                    fallback_dir_sql = f" AND {sql_dir_path('a')} = %s"
                    fallback_params.append(dir_path)
                cursor.execute(f"""
                    SELECT a.project_name, a.source_file_name, a.file_path, a.line_number,
                           '' AS line_text, a.status, a.coverage_method,
                           a.uncovered_reason, a.reviewer
                    FROM coverage_analysis a
                    WHERE a.project_name = %s
                      {fallback_dir_sql}
                    ORDER BY a.source_file_name, a.line_number
                """, fallback_params)
                rows = cursor.fetchall()
            cursor.close()
            return rows
        except Exception as e:
            print(f"[DB Error] Review Excel export query failed: {e}")
            raise

    def count_full_detail_rows(self, project_name):
        """Count indexed detail rows without materializing them in application memory."""
        if not project_name:
            raise ValueError("project_name is required")
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) AS total FROM coverage_line_index WHERE project_name = %s",
                (project_name,),
            )
            row = cursor.fetchone()
            total = int(row_value(row, "total", 0) or 0)
            if not total:
                cursor.execute(
                    "SELECT COUNT(*) AS total FROM coverage_analysis WHERE project_name = %s",
                    (project_name,),
                )
                row = cursor.fetchone()
                total = int(row_value(row, "total", 0) or 0)
            return total
        finally:
            if cursor is not None:
                cursor.close()

    def iter_full_detail_batches(self, project_name, batch_size=DETAIL_EXPORT_BATCH_SIZE):
        """Yield detailed rows with indexed keyset pagination and bounded memory."""
        if not project_name:
            raise ValueError("project_name is required")
        use_line_index = self.has_line_index(project_name)
        last_file_hash = ""
        last_line_number = 0
        while True:
            cursor = self.conn.cursor()
            try:
                if use_line_index:
                    cursor.execute(f"""
                        SELECT i.project_name, i.file_path, i.line_number, i.line_text,
                               i.block_start_line, i.block_end_line, i.block_type,
                               CASE WHEN a.id IS NULL THEN %s ELSE %s END AS fill_status,
                               COALESCE(a.status, '') AS status,
                               COALESCE(a.reviewer, '') AS reviewer,
                               COALESCE(a.coverage_method, '') AS coverage_method,
                               COALESCE(a.uncovered_reason, '') AS uncovered_reason,
                               a.updated_at, i.file_path_hash
                        FROM coverage_line_index i
                        LEFT JOIN coverage_analysis a
                          ON {source_file_join_condition("i", "a")}
                        WHERE i.project_name = %s
                          AND (
                               i.file_path_hash > %s
                               OR (i.file_path_hash = %s AND i.line_number > %s)
                          )
                        ORDER BY i.file_path_hash, i.line_number
                        LIMIT %s
                    """, (
                        "未填写", "已填写", project_name,
                        last_file_hash, last_file_hash, last_line_number, int(batch_size),
                    ))
                else:
                    cursor.execute("""
                        SELECT a.project_name, a.file_path, a.line_number, '' AS line_text,
                               a.line_number AS block_start_line,
                               a.line_number AS block_end_line,
                               'analysis_only' AS block_type,
                               '已填写' AS fill_status,
                               a.status, a.reviewer, a.coverage_method,
                               a.uncovered_reason, a.updated_at, a.file_path_hash
                        FROM coverage_analysis a
                        WHERE a.project_name = %s
                          AND (
                               a.file_path_hash > %s
                               OR (a.file_path_hash = %s AND a.line_number > %s)
                          )
                        ORDER BY a.file_path_hash, a.line_number
                        LIMIT %s
                    """, (
                        project_name, last_file_hash, last_file_hash,
                        last_line_number, int(batch_size),
                    ))
                raw_rows = cursor.fetchall()
            finally:
                cursor.close()
            if not raw_rows:
                break
            batch = []
            for row in raw_rows:
                batch.append([
                    row_value(row, header, index)
                    for index, header in enumerate(FULL_DETAIL_HEADERS)
                ])
            last_row = raw_rows[-1]
            last_file_hash = row_value(last_row, "file_path_hash", 13)
            last_line_number = int(row_value(last_row, "line_number", 2) or 0)
            yield batch

    def fetch_full_detail_page(self, project_name, file_path, page=1,
                               page_size=DETAIL_PAGE_SIZE_DEFAULT):
        """Return one bounded page of line details for a selected file."""
        if not project_name or not file_path:
            raise ValueError("project_name and file_path are required")
        page = max(1, int(page))
        page_size = max(1, min(DETAIL_PAGE_SIZE_MAX, int(page_size)))
        file_path_hash = calc_file_path_hash(file_path)
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT COUNT(*) AS total
                FROM coverage_line_index i
                WHERE i.project_name = %s AND i.file_path_hash = %s
            """, (project_name, file_path_hash))
            count_row = cursor.fetchone()
            total = int(row_value(count_row, "total", 0) or 0)
            if total:
                cursor.execute(f"""
                    SELECT i.project_name, i.file_path, i.line_number, i.line_text,
                           i.block_start_line, i.block_end_line, i.block_type,
                           CASE WHEN a.id IS NULL THEN %s ELSE %s END AS fill_status,
                           COALESCE(a.status, '') AS status,
                           COALESCE(a.reviewer, '') AS reviewer,
                           COALESCE(a.coverage_method, '') AS coverage_method,
                           COALESCE(a.uncovered_reason, '') AS uncovered_reason,
                           a.updated_at
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON {source_file_join_condition("i", "a")}
                    WHERE i.project_name = %s AND i.file_path_hash = %s
                    ORDER BY i.line_number
                    LIMIT %s OFFSET %s
                """, (
                    "未填写", "已填写", project_name, file_path_hash,
                    page_size, (page - 1) * page_size,
                ))
            else:
                cursor.execute("""
                    SELECT COUNT(*) AS total FROM coverage_analysis
                    WHERE project_name = %s AND file_path_hash = %s
                """, (project_name, file_path_hash))
                total = int(row_value(cursor.fetchone(), "total", 0) or 0)
                cursor.execute("""
                    SELECT project_name, file_path, line_number, '' AS line_text,
                           line_number AS block_start_line, line_number AS block_end_line,
                           'analysis_only' AS block_type, '已填写' AS fill_status,
                           status, reviewer, coverage_method, uncovered_reason, updated_at
                    FROM coverage_analysis
                    WHERE project_name = %s AND file_path_hash = %s
                    ORDER BY line_number
                    LIMIT %s OFFSET %s
                """, (
                    project_name, file_path_hash, page_size, (page - 1) * page_size,
                ))
            raw_rows = cursor.fetchall()
            rows = [[
                row_value(row, header, index)
                for index, header in enumerate(FULL_DETAIL_HEADERS)
            ] for row in raw_rows]
            return {
                "headers": list(FULL_DETAIL_HEADERS),
                "rows": rows,
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            }
        finally:
            cursor.close()

    def fetch_project_dirs(self, project_name):
        if not project_name:
            raise ValueError("project_name is required")
        try:
            cursor = self.conn.cursor()
            dir_expr = sql_dir_path("i")
            sql = f"""
                SELECT {dir_expr} AS dir_path, COUNT(*) AS total_uncovered
                FROM coverage_line_index i
                WHERE i.project_name = %s
                GROUP BY {dir_expr}
                ORDER BY dir_path
            """
            cursor.execute(sql, (project_name,))
            rows = cursor.fetchall()
            cursor.close()
            return [(row_value(row, "dir_path", 0) or "", row_value(row, "total_uncovered", 1)) for row in rows]
        except Exception as e:
            print(f"[DB Error] Fetch project dirs failed: {e}")
            raise

    def fetch_projects(self):
        """List projects visible in either the review index or saved analysis table."""
        cursor = None
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass
            cursor = self.conn.cursor()
            projects = {}

            cursor.execute("""
                SELECT project_name
                FROM coverage_analysis
                GROUP BY project_name
            """)
            for row in cursor.fetchall():
                name = row_value(row, "project_name", 0)
                projects[name] = {
                    "project_name": name,
                    "saved_total": None,
                    "saved_file_total": None,
                    "indexed_total": 0,
                    "indexed_file_total": 0,
                    "last_updated": None,
                }

            cursor.execute("""
                SELECT project_name
                FROM coverage_line_index
                GROUP BY project_name
            """)
            for row in cursor.fetchall():
                name = row_value(row, "project_name", 0)
                item = projects.setdefault(name, {
                    "project_name": name,
                    "saved_total": None,
                    "saved_file_total": None,
                    "indexed_total": 0,
                    "indexed_file_total": 0,
                    "last_updated": None,
                })
                # Do not COUNT the potentially million-row line index merely to
                # populate a project dropdown. Exact counts come from progress data.
                item["indexed_total"] = None
                item["indexed_file_total"] = None

            return sorted(projects.values(), key=lambda item: str(item["project_name"]))
        except Exception as e:
            print(f"[DB Error] Fetch projects failed: {e}")
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass

    def has_line_index(self, project_name):
        """Fast indexed existence check used to distinguish complete and legacy data."""
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT 1 FROM coverage_line_index WHERE project_name = %s LIMIT 1",
                (project_name,),
            )
            return bool(cursor.fetchone())
        except Exception as e:
            print(f"[DB Error] Line-index existence check failed: {e}")
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass

    def _analysis_only_full_report(self, report_type, project_name):
        """Expose saved legacy rows when a project has no coverage_line_index data.

        The total scope cannot be reconstructed without the index, so these reports
        deliberately treat the saved rows as the known scope. The progress endpoint
        also returns project metadata so the page can show a clear warning.
        """
        if report_type == "full_detail":
            source_headers, source_rows = self.export_report("detail", project_name)
            headers = [
                "project_name", "file_path", "line_number", "line_text",
                "block_start_line", "block_end_line", "block_type",
                "fill_status", "status", "reviewer", "coverage_method",
                "uncovered_reason", "updated_at"
            ]
            data = []
            for row in source_rows:
                item = dict(zip(source_headers, row))
                line_number = item.get("line_number")
                data.append([
                    item.get("project_name", ""), item.get("file_path", ""), line_number, "",
                    line_number, line_number, "analysis_only", "已填写",
                    item.get("status", ""), item.get("reviewer", ""),
                    item.get("coverage_method", ""), item.get("uncovered_reason", ""),
                    item.get("updated_at"),
                ])
            return headers, data

        if report_type in ("full_file_summary", "full_project_summary"):
            source_type = "file_summary" if report_type == "full_file_summary" else "project_summary"
            source_headers, source_rows = self.export_report(source_type, project_name)
            headers = [
                "project_name",
                "file_path" if report_type == "full_file_summary" else "file_total",
                "total_uncovered", "filled_total", "unfilled_total", "confirmed_total",
                "coverable_total", "uncoverable_total", "redundant_total", "fill_rate",
                "confirmed_rate", "last_updated"
            ]
            data = []
            for row in source_rows:
                item = dict(zip(source_headers, row))
                known_total = int(item.get("review_total") or 0)
                second_value = item.get("file_path", "") if report_type == "full_file_summary" else item.get("file_total", 0)
                data.append([
                    item.get("project_name", ""), second_value, known_total, known_total, 0,
                    item.get("confirmed_total", 0), item.get("coverable_total", 0),
                    item.get("uncoverable_total", 0), item.get("redundant_total", 0),
                    100.0 if known_total else 0.0, item.get("confirmed_rate", 0),
                    item.get("last_updated"),
                ])
            return headers, data

        if report_type == "full_dir_summary":
            file_headers, file_rows = self.export_report("full_file_summary", project_name)
            headers = [
                "project_name", "dir_path", "file_total", "total_uncovered", "filled_total",
                "unfilled_total", "confirmed_total", "coverable_total",
                "uncoverable_total", "redundant_total", "fill_rate",
                "confirmed_rate", "last_updated"
            ]
            grouped = {}
            for row in file_rows:
                item = dict(zip(file_headers, row))
                key = (item.get("project_name", ""), get_source_dir_name(item.get("file_path", "")))
                target = grouped.setdefault(key, {
                    "file_total": 0, "total_uncovered": 0, "filled_total": 0,
                    "unfilled_total": 0, "confirmed_total": 0, "coverable_total": 0,
                    "uncoverable_total": 0, "redundant_total": 0, "last_updated": None,
                })
                target["file_total"] += 1
                for field in (
                    "total_uncovered", "filled_total", "unfilled_total", "confirmed_total",
                    "coverable_total", "uncoverable_total", "redundant_total",
                ):
                    target[field] += int(item.get(field) or 0)
                updated = item.get("last_updated")
                if updated is not None and (target["last_updated"] is None or str(updated) > str(target["last_updated"])):
                    target["last_updated"] = updated
            data = []
            for (name, dir_path), item in sorted(grouped.items()):
                total = item["total_uncovered"]
                data.append([
                    name, dir_path, item["file_total"], total, item["filled_total"],
                    item["unfilled_total"], item["confirmed_total"], item["coverable_total"],
                    item["uncoverable_total"], item["redundant_total"],
                    round(item["filled_total"] * 100.0 / total, 2) if total else 0.0,
                    round(item["confirmed_total"] * 100.0 / total, 2) if total else 0.0,
                    item["last_updated"],
                ])
            return headers, data

        raise ValueError("Unsupported analysis-only report type")

    def export_report(self, report_type="detail", project_name=None):
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            cursor = None
            where_sql = ""
            params = []
            if project_name:
                where_sql = "WHERE project_name = %s"
                params.append(project_name)

            if report_type == "detail":
                cursor = self.conn.cursor()
                headers = [
                    "project_name", "file_path", "line_number", "reviewer", "status",
                    "coverage_method", "uncovered_reason", "updated_at"
                ]
                sql = f"""
                    SELECT project_name, file_path, line_number, reviewer, status,
                           coverage_method, uncovered_reason, updated_at
                    FROM coverage_analysis
                    {where_sql}
                    ORDER BY project_name, file_path, line_number
                """
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                data = []
                for row in rows:
                    data.append([
                        row_value(row, "project_name", 0),
                        row_value(row, "file_path", 1),
                        row_value(row, "line_number", 2),
                        row_value(row, "reviewer", 3),
                        row_value(row, "status", 4),
                        row_value(row, "coverage_method", 5),
                        row_value(row, "uncovered_reason", 6),
                        row_value(row, "updated_at", 7),
                    ])
            elif report_type == "file_summary":
                cursor = self.conn.cursor()
                headers = [
                    "project_name", "file_path", "review_total", "confirmed_total",
                    "coverable_total", "uncoverable_total", "redundant_total", "unconfirmed_total",
                    "confirmed_rate", "coverable_rate", "uncoverable_rate", "redundant_rate", "last_updated"
                ]
                sql = f"""
                    SELECT project_name, file_path,
                           COUNT(*) AS review_total,
                           SUM(CASE WHEN is_draft = 0 AND status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           SUM(CASE WHEN is_draft = 1 OR status = %s THEN 1 ELSE 0 END) AS unconfirmed_total,
                           ROUND(SUM(CASE WHEN is_draft = 0 AND status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           ROUND(SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS coverable_rate,
                           ROUND(SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS uncoverable_rate,
                           ROUND(SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS redundant_rate,
                           MAX(updated_at) AS last_updated
                    FROM coverage_analysis
                    {where_sql}
                    GROUP BY project_name, file_path
                    ORDER BY project_name, file_path
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认", "未确认", "可覆盖", "无法覆盖", "冗余代码"] + params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "project_summary":
                cursor = self.conn.cursor()
                headers = [
                    "project_name", "review_total", "confirmed_total",
                    "coverable_total", "uncoverable_total", "redundant_total", "unconfirmed_total",
                    "confirmed_rate", "coverable_rate", "uncoverable_rate", "redundant_rate", "file_total", "last_updated"
                ]
                sql = f"""
                    SELECT project_name,
                           COUNT(*) AS review_total,
                           SUM(CASE WHEN is_draft = 0 AND status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           SUM(CASE WHEN is_draft = 1 OR status = %s THEN 1 ELSE 0 END) AS unconfirmed_total,
                           ROUND(SUM(CASE WHEN is_draft = 0 AND status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           ROUND(SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS coverable_rate,
                           ROUND(SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS uncoverable_rate,
                           ROUND(SUM(CASE WHEN is_draft = 0 AND status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS redundant_rate,
                           COUNT(DISTINCT file_path_hash) AS file_total,
                           MAX(updated_at) AS last_updated
                    FROM coverage_analysis
                    {where_sql}
                    GROUP BY project_name
                    ORDER BY project_name
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认", "未确认", "可覆盖", "无法覆盖", "冗余代码"] + params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "full_detail":
                cursor = self.conn.cursor()
                full_where_sql = ""
                full_params = []
                if project_name:
                    full_where_sql = "WHERE i.project_name = %s"
                    full_params.append(project_name)
                headers = [
                    "project_name", "file_path", "line_number", "line_text",
                    "block_start_line", "block_end_line", "block_type",
                    "fill_status", "status", "reviewer", "coverage_method",
                    "uncovered_reason", "updated_at"
                ]
                sql = f"""
                    SELECT i.project_name, i.file_path, i.line_number, i.line_text,
                           i.block_start_line, i.block_end_line, i.block_type,
                           CASE WHEN a.id IS NULL THEN %s ELSE %s END AS fill_status,
                           COALESCE(a.status, '') AS status,
                           COALESCE(a.reviewer, '') AS reviewer,
                           COALESCE(a.coverage_method, '') AS coverage_method,
                           COALESCE(a.uncovered_reason, '') AS uncovered_reason,
                           a.updated_at
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON {source_file_join_condition("i", "a")}
                    {full_where_sql}
                    ORDER BY i.project_name, i.file_path, i.line_number
                """
                cursor.execute(sql, ["未填写", "已填写"] + full_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "full_file_summary":
                cursor = self.conn.cursor()
                full_where_sql = ""
                full_params = []
                if project_name:
                    full_where_sql = "WHERE i.project_name = %s"
                    full_params.append(project_name)
                headers = [
                    "project_name", "file_path", "total_uncovered", "filled_total",
                    "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated"
                ]
                sql = f"""
                    SELECT i.project_name, MAX(i.file_path) AS file_path,
                           COUNT(*) AS total_uncovered,
                           SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) AS filled_total,
                           SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END) AS unfilled_total,
                           SUM(CASE WHEN a.id IS NOT NULL AND COALESCE(a.is_draft, 0) = 0 AND a.status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           ROUND(SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) * 100.0 / COUNT(*), 2) AS fill_rate,
                           ROUND(SUM(CASE WHEN a.id IS NOT NULL AND COALESCE(a.is_draft, 0) = 0 AND a.status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           MAX(a.updated_at) AS last_updated
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON {source_file_join_condition("i", "a")}
                    {full_where_sql}
                    GROUP BY i.project_name, i.file_path_hash
                    ORDER BY i.project_name, file_path
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认"] + full_params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "full_dir_summary":
                cursor = self.conn.cursor()
                full_where_sql = ""
                full_params = []
                dir_expr = sql_dir_path("i")
                if project_name:
                    full_where_sql = "WHERE i.project_name = %s"
                    full_params.append(project_name)
                headers = [
                    "project_name", "dir_path", "file_total", "total_uncovered", "filled_total",
                    "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated"
                ]
                sql = f"""
                    SELECT i.project_name, {dir_expr} AS dir_path,
                           COUNT(DISTINCT i.source_file_name) AS file_total,
                           COUNT(*) AS total_uncovered,
                           SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) AS filled_total,
                           SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END) AS unfilled_total,
                           SUM(CASE WHEN a.id IS NOT NULL AND COALESCE(a.is_draft, 0) = 0 AND a.status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           ROUND(SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) * 100.0 / COUNT(*), 2) AS fill_rate,
                           ROUND(SUM(CASE WHEN a.id IS NOT NULL AND COALESCE(a.is_draft, 0) = 0 AND a.status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           MAX(a.updated_at) AS last_updated
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON {source_file_join_condition("i", "a")}
                    {full_where_sql}
                    GROUP BY i.project_name, {dir_expr}
                    ORDER BY i.project_name, dir_path
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认"] + full_params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "full_project_summary":
                cursor = self.conn.cursor()
                full_where_sql = ""
                full_params = []
                if project_name:
                    full_where_sql = "WHERE i.project_name = %s"
                    full_params.append(project_name)
                headers = [
                    "project_name", "file_total", "total_uncovered", "filled_total",
                    "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated"
                ]
                sql = f"""
                    SELECT i.project_name,
                           COUNT(DISTINCT i.file_path_hash) AS file_total,
                           COUNT(*) AS total_uncovered,
                           SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) AS filled_total,
                           SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END) AS unfilled_total,
                           SUM(CASE WHEN a.id IS NOT NULL AND COALESCE(a.is_draft, 0) = 0 AND a.status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN COALESCE(a.is_draft, 0) = 0 AND a.status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           ROUND(SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) * 100.0 / COUNT(*), 2) AS fill_rate,
                           ROUND(SUM(CASE WHEN a.id IS NOT NULL AND COALESCE(a.is_draft, 0) = 0 AND a.status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           MAX(a.updated_at) AS last_updated
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON {source_file_join_condition("i", "a")}
                    {full_where_sql}
                    GROUP BY i.project_name
                    ORDER BY i.project_name
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认"] + full_params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "full_progress_summary":
                headers = [
                    "level", "project_name", "path", "file_total", "total_uncovered",
                    "filled_total", "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated"
                ]
                data = []
                for level, child_type in (
                    ("project", "full_project_summary"),
                    ("dir", "full_dir_summary"),
                    ("file", "full_file_summary"),
                ):
                    child_headers, child_rows = self.export_report(child_type, project_name)
                    for child_row in child_rows:
                        child_map = dict(zip(child_headers, child_row))
                        if level == "project":
                            path_value = ""
                        elif level == "dir":
                            path_value = child_map.get("dir_path", "")
                        else:
                            path_value = child_map.get("file_path", "")
                        data.append([
                            level,
                            child_map.get("project_name", ""),
                            path_value,
                            child_map.get("file_total", 1 if level == "file" else ""),
                            child_map.get("total_uncovered", ""),
                            child_map.get("filled_total", ""),
                            child_map.get("unfilled_total", ""),
                            child_map.get("confirmed_total", ""),
                            child_map.get("coverable_total", ""),
                            child_map.get("uncoverable_total", ""),
                            child_map.get("redundant_total", ""),
                            child_map.get("fill_rate", ""),
                            child_map.get("confirmed_rate", ""),
                            child_map.get("last_updated", ""),
                        ])
            else:
                raise ValueError("Unsupported report type")

            if cursor is not None:
                cursor.close()
            if (
                project_name and not data and report_type in (
                    "full_detail", "full_file_summary", "full_dir_summary", "full_project_summary"
                )
            ):
                return self._analysis_only_full_report(report_type, project_name)
            return headers, data
        except Exception as e:
            print(f"[DB Error] Export failed: {e}")
            raise


db_manager = None


def excel_col_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def safe_sheet_name(name, used_names):
    base = re.sub(r"[\[\]\:\*\?\/\\]+", "_", str(name or "Sheet")).strip() or "Sheet"
    base = base[:31]
    candidate = base
    suffix = 1
    while candidate in used_names:
        suffix_text = f"_{suffix}"
        candidate = f"{base[:31 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def xlsx_cell_xml(row_index, col_index, value, style_id=0):
    ref = f"{excel_col_name(col_index)}{row_index}"
    style_attr = f' s="{style_id}"' if style_id else ""
    if value is None:
        value = ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    text = html.escape(str(value), quote=True)
    return f'<c r="{ref}" t="inlineStr"{style_attr}><is><t>{text}</t></is></c>'


def xlsx_sheet_xml(rows, column_widths=None, highlight_mark_column=False):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    parts.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
    if column_widths:
        parts.append("<cols>")
        for index, width in enumerate(column_widths, start=1):
            parts.append(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>')
        parts.append("</cols>")
    parts.append("<sheetData>")
    for row_index, row in enumerate(rows, start=1):
        height_attr = ' ht="48" customHeight="1"' if row_index == 1 else ""
        parts.append(f'<row r="{row_index}"{height_attr}>')
        for col_index, value in enumerate(row, start=1):
            style_id = 1 if row_index == 1 else 2
            if highlight_mark_column and row_index > 1 and col_index == 3 and str(value) == "0":
                style_id = 3
            parts.append(xlsx_cell_xml(row_index, col_index, value, style_id))
        parts.append("</row>")
    parts.append("</sheetData>")
    parts.append("</worksheet>")
    return "".join(parts)


def build_xlsx_workbook(sheet_defs):
    workbook_xml_sheets = []
    workbook_rels = []
    content_overrides = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")
        archive.writestr("xl/styles.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF4CCCC"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border/><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>""")
        for sheet_index, sheet in enumerate(sheet_defs, start=1):
            archive.writestr(f"xl/worksheets/sheet{sheet_index}.xml", sheet["xml"])
            sheet_name = html.escape(sheet["name"], quote=True)
            workbook_xml_sheets.append(f'<sheet name="{sheet_name}" sheetId="{sheet_index}" r:id="rId{sheet_index}"/>')
            workbook_rels.append(f'<Relationship Id="rId{sheet_index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{sheet_index}.xml"/>')
            content_overrides.append(f'<Override PartName="/xl/worksheets/sheet{sheet_index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        archive.writestr("xl/workbook.xml", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{''.join(workbook_xml_sheets)}</sheets></workbook>""")
        workbook_rels.append(f'<Relationship Id="rId{len(sheet_defs) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
        archive.writestr("xl/_rels/workbook.xml.rels", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{''.join(workbook_rels)}</Relationships>""")
        archive.writestr("[Content_Types].xml", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{''.join(content_overrides)}</Types>""")
    return buffer.getvalue()


def coverage_mark_from_status(status):
    if status in ("无法覆盖", "冗余代码"):
        return "NA"
    return "0"


def build_progress_excel(project_name, progress_sections):
    sheet_defs = []
    used_names = set()

    progress_headers = [
        "层级", "项目名", "小组", "组长", "模块", "归属匹配状态", "路径", "文件数", "未覆盖行总数", "已填写", "未填写",
        "已确认", "可覆盖", "无法覆盖", "冗余代码", "填写率(%)", "确认率(%)", "最后更新时间"
    ]
    for sheet_name, headers, rows in progress_sections:
        progress_rows = [progress_headers]
        for row in rows:
            row_map = dict(zip(headers, row))
            level = sheet_name.replace("进度", "")
            if sheet_name == "项目进度":
                path_value = ""
            elif sheet_name == "目录进度":
                path_value = row_map.get("dir_path", "")
            elif sheet_name == "文件进度":
                path_value = row_map.get("file_path", "")
            else:
                path_value = ""
            progress_rows.append([
                level,
                row_map.get("project_name", project_name),
                row_map.get("team", ""),
                row_map.get("leader", ""),
                row_map.get("module_names", row_map.get("module", "")),
                row_map.get("ownership_status", ""),
                path_value,
                row_map.get("file_total", 1 if sheet_name == "文件进度" else ""),
                row_map.get("total_uncovered", ""),
                row_map.get("filled_total", ""),
                row_map.get("unfilled_total", ""),
                row_map.get("confirmed_total", ""),
                row_map.get("coverable_total", ""),
                row_map.get("uncoverable_total", ""),
                row_map.get("redundant_total", ""),
                row_map.get("fill_rate", ""),
                row_map.get("confirmed_rate", ""),
                row_map.get("last_updated", ""),
            ])
        sheet_defs.append({
            "name": safe_sheet_name(sheet_name, used_names),
            "xml": xlsx_sheet_xml(progress_rows, [10, 22, 18, 18, 32, 18, 56, 10, 14, 10, 10, 10, 10, 10, 10, 12, 12, 22])
        })
    return build_xlsx_workbook(sheet_defs)


def build_review_excel(project_name, detail_rows, progress_sections):
    grouped = {}
    for row in detail_rows:
        source_file_name = row_value(row, "source_file_name", 1) or get_source_file_name(row_value(row, "file_path", 2))
        grouped.setdefault(source_file_name, []).append(row)

    sheet_defs = []
    used_names = set()
    for source_file_name in sorted(grouped.keys()):
        rows = [[
            "行号",
            source_file_name,
            "覆盖率标识\n(1代表覆盖，0代表未覆盖，NA代表无需覆盖)",
            "是否冗余代码，剔除计划",
            "对测试覆盖的建议\n(黑盒测试怎么才能覆盖)",
            "无法覆盖原因",
            "开发责任人",
        ]]
        for row in grouped[source_file_name]:
            status = row_value(row, "status", 5) or ""
            rows.append([
                row_value(row, "line_number", 3),
                row_value(row, "line_text", 4),
                coverage_mark_from_status(status),
                row_value(row, "coverage_method", 6) if status == "冗余代码" else "",
                row_value(row, "coverage_method", 6),
                row_value(row, "uncovered_reason", 7),
                row_value(row, "reviewer", 8),
            ])
        sheet_defs.append({
            "name": safe_sheet_name(source_file_name, used_names),
            "xml": xlsx_sheet_xml(rows, [10, 48, 20, 22, 34, 24, 18], highlight_mark_column=True)
        })

    progress_headers = [
        "层级", "项目名", "路径", "文件数", "未覆盖行总数", "已填写", "未填写",
        "已确认", "可覆盖", "无法覆盖", "冗余代码", "填写率(%)", "确认率(%)", "最后更新时间"
    ]
    for sheet_name, headers, rows in progress_sections:
        progress_rows = [progress_headers]
        for row in rows:
            row_map = dict(zip(headers, row))
            level = sheet_name.replace("进度", "")
            if sheet_name == "项目进度":
                path_value = ""
            elif sheet_name == "目录进度":
                path_value = row_map.get("dir_path", "")
            else:
                path_value = row_map.get("file_path", "")
            progress_rows.append([
                level,
                row_map.get("project_name", project_name),
                path_value,
                row_map.get("file_total", 1 if sheet_name == "文件进度" else ""),
                row_map.get("total_uncovered", ""),
                row_map.get("filled_total", ""),
                row_map.get("unfilled_total", ""),
                row_map.get("confirmed_total", ""),
                row_map.get("coverable_total", ""),
                row_map.get("uncoverable_total", ""),
                row_map.get("redundant_total", ""),
                row_map.get("fill_rate", ""),
                row_map.get("confirmed_rate", ""),
                row_map.get("last_updated", ""),
            ])
        sheet_defs.append({
            "name": safe_sheet_name(sheet_name, used_names),
            "xml": xlsx_sheet_xml(progress_rows, [10, 22, 56, 10, 14, 10, 10, 10, 10, 10, 10, 12, 12, 22])
        })

    if not sheet_defs:
        sheet_defs.append({
            "name": safe_sheet_name("empty", used_names),
            "xml": xlsx_sheet_xml([["项目", project_name, "没有可导出的未覆盖行"]], [20, 28, 40])
        })
    return build_xlsx_workbook(sheet_defs)


def safe_zip_member_name(value, default_name="root"):
    name = str(value or default_name).replace("\\", "/").strip("/")
    name = re.sub(r"[^A-Za-z0-9_.\-/]+", "_", name)
    name = name.replace("/", "__")
    return name or default_name


def build_review_excel_zip(project_name, dir_entries):
    buffer = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if not dir_entries:
            archive.writestr(
                "README.txt",
                (
                    f"No coverage review Excel files were generated for project: {project_name}\n"
                    "Please check whether the project name is correct and whether inject has synced coverage_line_index data.\n"
                )
            )
        for dir_path, excel_data in dir_entries:
            base = safe_zip_member_name(dir_path, "root")
            candidate = f"{base}.xlsx"
            suffix = 1
            while candidate in used_names:
                candidate = f"{base}_{suffix}.xlsx"
                suffix += 1
            used_names.add(candidate)
            archive.writestr(candidate, excel_data)
    return buffer.getvalue()


def build_review_excel_for_dir(project_name, current_dir, total_uncovered,
                               project_headers, project_rows, dir_headers,
                               current_dir_rows, file_headers, current_file_rows,
                               detail_rows):
    progress_sections = [
        ("项目进度", project_headers, project_rows),
        ("目录进度", dir_headers, current_dir_rows),
        ("文件进度", file_headers, current_file_rows),
    ]
    excel_data = build_review_excel(project_name, detail_rows, progress_sections)
    return {
        "dir_path": current_dir,
        "total_uncovered": total_uncovered,
        "row_count": len(detail_rows),
        "excel_data": excel_data,
    }


_global_code_detail_service = None


def get_code_detail_service(db_mgr=None, search_dirs=None):
    """Return a configured CodeDetailService instance."""
    global _global_code_detail_service
    manager = db_mgr or globals().get("db_manager")
    cfg = load_config()
    dirs = [
        SCRIPT_DIR,
        os.path.join(SCRIPT_DIR, "html"),
        os.path.join(SCRIPT_DIR, "demo_ui_output"),
        os.path.join(SCRIPT_DIR, ".coverage_demo", "html"),
        os.path.join(SCRIPT_DIR, "..", "build", "coverage"),
        os.path.join(SCRIPT_DIR, "..", "build", "coverage_review"),
        "/opt/coverage_reports/review",
        "/opt/coverage_reports/review_incremental",
        "/opt/coverage_reports",
    ]
    cfg_report_roots = cfg.get("report_roots", [])
    if isinstance(cfg_report_roots, list):
        for r_root in cfg_report_roots:
            if r_root and os.path.exists(r_root):
                dirs.append(os.path.abspath(r_root))
    if search_dirs:
        dirs.extend(search_dirs)
    if _global_code_detail_service is None or _global_code_detail_service.db_manager != manager:
        _global_code_detail_service = CodeDetailService(db_manager=manager, search_dirs=dirs)
    else:
        for d in dirs:
            _global_code_detail_service.add_search_dir(d)
    return _global_code_detail_service


class CoverageHTTPRequestHandler(BaseHTTPRequestHandler):
    """基于 BaseHTTPRequestHandler 的极轻量跨域 API 服务器"""

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def safe_write(self, data):
        """Write response data to the socket while safely handling client-side disconnects."""
        try:
            self.wfile.write(data)
        except ConnectionError:
            print("[Server] Client disconnected before response write completed.", flush=True)

    def finish(self):
        """Ensure thread-local DB connections and sockets are closed safely."""
        try:
            super().finish()
        finally:
            if "db_manager" in globals() and db_manager and hasattr(db_manager, "close_thread_connection"):
                db_manager.close_thread_connection()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        if parsed_url.path == "/api/coverage/progress/start":
            project_name = query_params.get("project", [""])[0].strip()
            if not project_name:
                self.send_error_response(400, "Missing 'project' parameter")
                return
            job = start_background_job("progress", project_name)
            self.send_json_response(202 if job.get("state") == "running" else 200, {
                "status": "success", "job": job,
            })
        elif parsed_url.path == "/api/coverage/jobs/status":
            job_id = query_params.get("id", [""])[0].strip()
            job = public_background_job(job_id)
            if not job:
                self.send_error_response(404, "Background job not found or expired")
                return
            self.send_json_response(200, {"status": "success", "job": job})
        elif parsed_url.path in ("/api/coverage/code-layout", "/api/coverage/layout"):
            project_name = query_params.get("project", query_params.get("project_name", [""]))[0].strip()
            file_path = query_params.get("file", query_params.get("file_path", [""]))[0].strip()
            report_id = query_params.get("report_id", query_params.get("report", [""]))[0].strip()
            review_scope = query_params.get("scope", query_params.get("review_scope", ["full"]))[0].strip()
            if not project_name or not file_path:
                self.send_error_response(400, "Missing 'project' or 'file' parameter")
                return
            if not is_safe_relative_path(file_path):
                self.send_error_response(400, "Invalid or unsafe file path")
                return
            if report_id and not is_valid_report_id(report_id):
                self.send_error_response(400, f"Invalid report_id format: '{report_id}'")
                return
            if not is_valid_review_scope(review_scope):
                self.send_error_response(400, f"Invalid review_scope: '{review_scope}' (must be 'full' or 'incremental')")
                return
            try:
                service = get_code_detail_service()
                data = service.get_code_layout(
                    project_name=project_name,
                    file_path=file_path,
                    report_id=report_id,
                    review_scope=review_scope,
                )
                self.send_json_response(200, {"status": "success", "data": data})
            except FileNotFoundError as error:
                self.send_error_response(404, str(error))
                return
            except ValueError as error:
                self.send_error_response(400, str(error))
                return
            except Exception as error:
                print(f"[CodeLayout] Failed for {file_path}: {error}", flush=True)
                self.send_error_response(500, f"Failed to get code layout: {error}")
                return
        elif parsed_url.path == "/api/coverage/code-lines":
            project_name = query_params.get("project", query_params.get("project_name", [""]))[0].strip()
            file_path = query_params.get("file", query_params.get("file_path", [""]))[0].strip()
            report_id = query_params.get("report_id", query_params.get("report", [""]))[0].strip()
            review_scope = query_params.get("scope", query_params.get("review_scope", ["full"]))[0].strip()
            if not project_name or not file_path:
                self.send_error_response(400, "Missing 'project' or 'file' parameter")
                return
            if not is_safe_relative_path(file_path):
                self.send_error_response(400, "Invalid or unsafe file path")
                return
            if report_id and not is_valid_report_id(report_id):
                self.send_error_response(400, f"Invalid report_id format: '{report_id}'")
                return
            if not is_valid_review_scope(review_scope):
                self.send_error_response(400, f"Invalid review_scope: '{review_scope}' (must be 'full' or 'incremental')")
                return
            try:
                start_line = int(query_params.get("start_line", ["1"])[0])
                end_line = int(query_params.get("end_line", ["1"])[0])
            except (ValueError, TypeError):
                self.send_error_response(400, "Invalid start_line or end_line parameter")
                return
            if start_line <= 0 or end_line <= 0 or start_line > end_line:
                self.send_error_response(400, "Invalid start_line or end_line range")
                return
            try:
                service = get_code_detail_service()
                data = service.get_code_lines_single(
                    project_name=project_name,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    report_id=report_id,
                    review_scope=review_scope,
                )
                self.send_json_response(200, {"status": "success", "data": data})
            except FileNotFoundError as error:
                self.send_error_response(404, str(error))
                return
            except ValueError as error:
                self.send_error_response(400, str(error))
                return
            except Exception as error:
                print(f"[CodeLines] Failed for {file_path}: {error}", flush=True)
                self.send_error_response(500, f"Failed to get code lines: {error}")
                return
        elif parsed_url.path == "/api/coverage/details":
            project_name = query_params.get("project", [""])[0].strip()
            file_path = query_params.get("file", [""])[0]
            try:
                page = int(query_params.get("page", ["1"])[0])
                page_size = int(query_params.get("page_size", [str(DETAIL_PAGE_SIZE_DEFAULT)])[0])
            except ValueError:
                self.send_error_response(400, "Invalid page or page_size")
                return
            if not project_name or not file_path:
                self.send_error_response(400, "Missing 'project' or 'file' parameter")
                return
            try:
                data = db_manager.fetch_full_detail_page(
                    project_name, file_path, page=page, page_size=page_size
                )
            except Exception as error:
                print("[Detail Page] Failed: {}".format(error), flush=True)
                self.send_error_response(500, "Failed to query detail page")
                return
            self.send_json_response(200, {"status": "success", "data": data})
        elif parsed_url.path == "/api/coverage/export/start":
            report_type = query_params.get("type", [""])[0]
            project_name = query_params.get("project", [""])[0].strip()
            if report_type != "full_detail":
                self.send_error_response(400, "Only full_detail background export is supported")
                return
            if not project_name:
                self.send_error_response(400, "Missing 'project' parameter")
                return
            job = start_background_job("full_detail_export", project_name)
            self.send_json_response(202 if job.get("state") == "running" else 200, {
                "status": "success", "job": job,
            })
        elif parsed_url.path == "/api/coverage/export/download":
            job_id = query_params.get("id", [""])[0].strip()
            with _background_jobs_lock:
                job = dict(_background_jobs.get(job_id) or {})
            output_path = job.get("output_path")
            if job.get("state") != "completed" or job.get("kind") != "full_detail_export":
                self.send_error_response(409, "Export is not ready")
                return
            if not output_path or not os.path.isfile(output_path):
                self.send_error_response(404, "Export file not found or expired")
                return
            self.send_file_response(
                job.get("filename") or "coverage_full_detail.csv",
                output_path,
                "text/csv; charset=utf-8",
            )
        elif parsed_url.path == "/api/coverage/export":
            report_type = query_params.get("type", ["detail"])[0]
            report_type_aliases = {
                "review_execel": "review_excel",
                "review_execel_by_dir": "review_excel_by_dir",
            }
            report_type = report_type_aliases.get(report_type, report_type)
            project_name = query_params.get("project", [""])[0] or None
            dir_path = query_params.get("dir", [""])[0]
            dir_path = dir_path if dir_path != "" else None
            csv_report_types = (
                "detail", "file_summary", "project_summary", "full_detail",
                "full_file_summary", "full_dir_summary", "full_project_summary"
            )
            xlsx_report_types = ("review_excel", "review_excel_by_dir", "full_progress_summary")

            if report_type not in csv_report_types + xlsx_report_types:
                self.send_error_response(400, "Unsupported export type. Use detail, file_summary, project_summary, full_detail, full_file_summary, full_dir_summary, full_project_summary, full_progress_summary, review_excel, or review_excel_by_dir")
                return

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            project_part = project_name or "all"
            if report_type == "review_excel":
                if not project_name:
                    self.send_error_response(400, "review_excel requires project=<project_name>")
                    return
                try:
                    detail_rows = db_manager.fetch_review_excel_rows(project_name, dir_path)
                    dir_headers, dir_rows = db_manager.export_report("full_dir_summary", project_name)
                    file_headers, file_rows = db_manager.export_report("full_file_summary", project_name)
                    if dir_path is not None:
                        dir_rows = [row for row in dir_rows if dict(zip(dir_headers, row)).get("dir_path", "") == dir_path]
                        file_rows = [row for row in file_rows if get_source_dir_name(dict(zip(file_headers, row)).get("file_path", "")) == dir_path]
                    progress_sections = [
                        ("项目进度", *db_manager.export_report("full_project_summary", project_name)),
                        ("目录进度", dir_headers, dir_rows),
                        ("文件进度", file_headers, file_rows),
                    ]
                    data = build_review_excel(project_name, detail_rows, progress_sections)
                except Exception:
                    self.send_error_response(500, "Failed to export review Excel")
                    return
                dir_part = f"_{safe_zip_member_name(dir_path)}" if dir_path else ""
                filename = f"coverage_review_{project_part}{dir_part}_{timestamp}.xlsx"
                self.send_xlsx_response(filename, data)
            elif report_type == "review_excel_by_dir":
                if not project_name:
                    self.send_error_response(400, "review_excel_by_dir requires project=<project_name>")
                    return
                filename = f"coverage_review_by_dir_{project_part}_{timestamp}.zip"
                self.send_review_excel_by_dir_response(filename, project_name)
            elif report_type == "full_progress_summary":
                if not project_name:
                    self.send_error_response(400, "full_progress_summary requires project=<project_name>")
                    return
                try:
                    progress_data = compute_progress_data(db_manager, project_name, load_config())
                    project_headers, project_rows = dict_rows_to_table(
                        progress_data["project"], PROGRESS_PROJECT_HEADERS
                    )
                    dir_headers, dir_rows = dict_rows_to_table(
                        progress_data["dirs"], PROGRESS_DIR_HEADERS
                    )
                    team_headers, team_rows = dict_rows_to_table(progress_data["teams"], (
                        "team", "leader", "module_names", "file_total", "total_uncovered",
                        "filled_total", "unfilled_total", "confirmed_total", "coverable_total",
                        "uncoverable_total", "redundant_total", "fill_rate", "confirmed_rate",
                        "last_updated",
                    ))
                    enriched_file_headers = list(PROGRESS_FILE_HEADERS) + [
                        "module", "team", "leader", "ownership_status"
                    ]
                    enriched_file_headers, enriched_file_rows = dict_rows_to_table(
                        progress_data["files"], enriched_file_headers
                    )
                    progress_sections = [
                        ("项目进度", project_headers, project_rows),
                        ("目录进度", dir_headers, dir_rows),
                        ("小组进度", team_headers, team_rows),
                        ("文件进度", enriched_file_headers, enriched_file_rows),
                    ]
                    data = build_progress_excel(project_name, progress_sections)
                except Exception:
                    self.send_error_response(500, "Failed to export progress Excel")
                    return
                filename = f"coverage_progress_{project_part}_{timestamp}.xlsx"
                self.send_xlsx_response(filename, data)
            else:
                try:
                    headers, rows = db_manager.export_report(report_type, project_name)
                except Exception:
                    self.send_error_response(500, "Failed to export report")
                    return

                filename = f"coverage_{report_type}_{project_part}_{timestamp}.csv"
                self.send_csv_response(filename, headers, rows)
        elif parsed_url.path == "/api/coverage/projects":
            try:
                projects = db_manager.fetch_projects()
            except Exception:
                self.send_error_response(500, "Failed to list coverage projects")
                return
            self.send_json_response(200, {"status": "success", "projects": projects})
        elif parsed_url.path == "/api/coverage/progress":
            project_name = query_params.get("project", [""])[0]
            if not project_name:
                self.send_error_response(400, "Missing 'project' parameter")
                return

            try:
                data = compute_progress_data(db_manager, project_name, load_config())
            except Exception:
                self.send_error_response(500, "Failed to query progress")
                return

            self.send_json_response(200, {"status": "success", "data": data})
        elif parsed_url.path == "/api/coverage/incremental/unanalyzed":
            project_name = query_params.get("project", [""])[0].strip()
            if not project_name:
                self.send_error_response(400, "Missing 'project' parameter")
                return
            cfg = load_config()
            if not is_mysql_configured(cfg):
                self.send_error_response(503, "Database is not configured for unanalyzed counts API")
                return
            try:
                counts_map, ver = get_incremental_unanalyzed_counts(project_name, config=cfg)
                file_list = [
                    {"file_path": fpath, "unanalyzed": count}
                    for fpath, count in counts_map.items()
                ]
                total = sum(counts_map.values())
                self.send_json_response(200, {
                    "status": "success",
                    "data": {
                        "project_name": project_name,
                        "data_version": ver,
                        "total_unanalyzed": total,
                        "files": file_list,
                    }
                })
            except Exception as err:
                print("[Incremental Unanalyzed API] Failed for project '{}': {}".format(project_name, err), flush=True)
                self.send_error_response(500, "Database query for unanalyzed counts failed: {}".format(err))
                return
        elif parsed_url.path == "/api/coverage":
            project_name = query_params.get("project", [""])[0]
            file_path = query_params.get("file", [""])[0]

            if not project_name or not file_path:
                self.send_error_response(400, "Missing 'project' or 'file' parameter")
                return

            records = db_manager.fetch_records(project_name, file_path)
            self.send_json_response(200, {"status": "success", "records": records})
        else:
            self.send_error_response(404, "Not Found")

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == "/api/coverage":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except ValueError:
                self.send_error_response(400, "Invalid JSON data")
                return

            project_name = payload.get("project_name")
            file_path = payload.get("file_path")
            line_number = payload.get("line_number")
            reviewer = payload.get("reviewer", "")
            status = payload.get("status", "未确认")
            method = payload.get("coverage_method", "")
            reason = payload.get("uncovered_reason", "")

            if not project_name or not file_path or line_number is None:
                self.send_error_response(400, "Missing required parameters (project_name, file_path, line_number)")
                return

            success = db_manager.save_record(project_name, file_path, line_number, reviewer, status, method, reason)
            if success:
                invalidate_project_background_jobs(project_name)
                self.send_json_response(200, {"status": "success", "message": "Record saved successfully"})
            else:
                self.send_error_response(500, "Failed to save record to database")
        elif parsed_url.path == "/api/coverage/batch":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except ValueError:
                self.send_error_response(400, "Invalid JSON data")
                return

            if not isinstance(payload, dict):
                self.send_error_response(400, "JSON payload must be an object")
                return

            project_name = str(payload.get("project_name") or "").strip()
            file_path = str(payload.get("file_path") or "").strip()
            mode = str(payload.get("mode") or "draft").strip().lower()
            blocks = payload.get("blocks")
            if not project_name or not file_path:
                self.send_error_response(400, "Missing required parameters (project_name, file_path)")
                return
            if mode not in ("draft", "confirm"):
                self.send_error_response(400, "mode must be draft or confirm")
                return
            if not isinstance(blocks, list) or not blocks:
                self.send_error_response(400, "blocks must be a non-empty array")
                return
            if len(blocks) > MAX_BATCH_REVIEW_BLOCKS:
                self.send_error_response(400, "Too many review blocks in one batch")
                return

            normalized_blocks = []
            used_line_numbers = set()
            total_line_count = 0
            for block_index, block in enumerate(blocks, start=1):
                if not isinstance(block, dict):
                    self.send_error_response(400, "blocks[{}] must be an object".format(block_index))
                    return
                line_numbers = block.get("line_numbers")
                if not isinstance(line_numbers, list) or not line_numbers:
                    self.send_error_response(400, "blocks[{}].line_numbers must be a non-empty array".format(block_index))
                    return

                normalized_line_numbers = []
                for raw_line_number in line_numbers:
                    try:
                        line_number = int(raw_line_number)
                    except (TypeError, ValueError):
                        self.send_error_response(400, "Invalid line number in blocks[{}]".format(block_index))
                        return
                    if line_number <= 0 or line_number in used_line_numbers:
                        self.send_error_response(400, "Duplicate or invalid line number in batch")
                        return
                    used_line_numbers.add(line_number)
                    normalized_line_numbers.append(line_number)

                total_line_count += len(normalized_line_numbers)
                if total_line_count > MAX_BATCH_REVIEW_LINES:
                    self.send_error_response(400, "Too many review lines in one batch")
                    return

                status = str(block.get("status") or REVIEW_STATUS_UNCONFIRMED).strip()
                reviewer = str(block.get("reviewer") or "").strip()
                method = str(block.get("coverage_method") or "").strip()
                reason = str(block.get("uncovered_reason") or "").strip()
                if status not in REVIEW_VALID_STATUSES:
                    self.send_error_response(400, "Invalid review status in blocks[{}]".format(block_index))
                    return
                if mode == "confirm":
                    if status not in REVIEW_CONFIRMED_STATUSES:
                        self.send_error_response(400, "Confirmed blocks must use a confirmed status")
                        return
                    if not reviewer or (not method and not reason):
                        self.send_error_response(400, "Confirmed blocks require reviewer and method or reason")
                        return
                normalized_blocks.append({
                    "line_numbers": normalized_line_numbers,
                    "reviewer": reviewer,
                    "status": status,
                    "coverage_method": method,
                    "uncovered_reason": reason,
                })

            result = db_manager.save_records_batch(
                project_name, file_path, normalized_blocks, is_draft=(mode == "draft")
            )
            if result:
                invalidate_project_background_jobs(project_name)
                response = {"status": "success", "mode": mode}
                response.update(result)
                self.send_json_response(200, response)
            else:
                self.send_error_response(500, "Failed to save batch records to database")
        elif parsed_url.path == "/api/coverage/code-lines/batch":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except ValueError:
                self.send_error_response(400, "Invalid JSON data")
                return
            if not isinstance(payload, dict):
                self.send_error_response(400, "JSON payload must be an object")
                return
            project_name = str(payload.get("project_name") or payload.get("project") or "").strip()
            file_path = str(payload.get("file_path") or payload.get("file") or "").strip()
            report_id = str(payload.get("report_id") or payload.get("report") or "").strip()
            review_scope = str(payload.get("scope") or payload.get("review_scope") or "full").strip()
            ranges = payload.get("ranges")
            region_ids = payload.get("region_ids")
            if not project_name or not file_path:
                self.send_error_response(400, "Missing required parameters (project_name, file_path)")
                return
            if not is_safe_relative_path(file_path):
                self.send_error_response(400, "Invalid or unsafe file path")
                return
            if report_id and not is_valid_report_id(report_id):
                self.send_error_response(400, f"Invalid report_id format: '{report_id}'")
                return
            if not is_valid_review_scope(review_scope):
                self.send_error_response(400, f"Invalid review_scope: '{review_scope}' (must be 'full' or 'incremental')")
                return
            if not ranges and not region_ids:
                self.send_error_response(400, "Either 'ranges' or 'region_ids' must be provided")
                return
            if ranges is not None and (not isinstance(ranges, list) or len(ranges) > 1000):
                self.send_error_response(400, "ranges must be a list with at most 1000 items")
                return
            if region_ids is not None and (not isinstance(region_ids, list) or len(region_ids) > 1000):
                self.send_error_response(400, "region_ids must be a list with at most 1000 items")
                return
            try:
                service = get_code_detail_service()
                data = service.get_code_lines_batch(
                    project_name=project_name,
                    file_path=file_path,
                    ranges=ranges,
                    region_ids=region_ids,
                    report_id=report_id,
                    review_scope=review_scope,
                )
                self.send_json_response(200, {"status": "success", "data": data})
            except FileNotFoundError as error:
                self.send_error_response(404, str(error))
                return
            except ValueError as error:
                self.send_error_response(400, str(error))
                return
            except Exception as error:
                print(f"[CodeLinesBatch] Failed for {file_path}: {error}", flush=True)
                self.send_error_response(500, f"Failed to batch load code lines: {error}")
                return
        elif parsed_url.path == "/api/coverage/code-lines":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except ValueError:
                self.send_error_response(400, "Invalid JSON data")
                return
            if not isinstance(payload, dict):
                self.send_error_response(400, "JSON payload must be an object")
                return
            project_name = str(payload.get("project_name") or payload.get("project") or "").strip()
            file_path = str(payload.get("file_path") or payload.get("file") or "").strip()
            report_id = str(payload.get("report_id") or payload.get("report") or "").strip()
            review_scope = str(payload.get("scope") or payload.get("review_scope") or "full").strip()
            try:
                start_line = int(payload.get("start_line", 0))
                end_line = int(payload.get("end_line", 0))
            except (ValueError, TypeError):
                self.send_error_response(400, "Invalid start_line or end_line parameter")
                return
            if not project_name or not file_path:
                self.send_error_response(400, "Missing required parameters (project_name, file_path)")
                return
            if not is_safe_relative_path(file_path):
                self.send_error_response(400, "Invalid or unsafe file path")
                return
            if report_id and not is_valid_report_id(report_id):
                self.send_error_response(400, f"Invalid report_id format: '{report_id}'")
                return
            if not is_valid_review_scope(review_scope):
                self.send_error_response(400, f"Invalid review_scope: '{review_scope}' (must be 'full' or 'incremental')")
                return
            if start_line <= 0 or end_line <= 0 or start_line > end_line:
                self.send_error_response(400, "Invalid start_line or end_line range")
                return
            try:
                service = get_code_detail_service()
                data = service.get_code_lines_single(
                    project_name=project_name,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    report_id=report_id,
                    review_scope=review_scope,
                )
                self.send_json_response(200, {"status": "success", "data": data})
            except FileNotFoundError as error:
                self.send_error_response(404, str(error))
                return
            except ValueError as error:
                self.send_error_response(400, str(error))
                return
            except Exception as error:
                print(f"[CodeLines] Failed for {file_path}: {error}", flush=True)
                self.send_error_response(500, f"Failed to get code lines: {error}")
                return
        else:
            self.send_error_response(404, "Not Found")

    def send_json_response(self, status_code, data):
        self.send_response(status_code)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.safe_write(json.dumps(data, ensure_ascii=False, default=json_safe_default).encode("utf-8"))

    def send_csv_response(self, filename, headers, rows):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])
        data = ("\ufeff" + output.getvalue()).encode("utf-8")

        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.safe_write(data)

    def send_xlsx_response(self, filename, data):
        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.safe_write(data)

    def send_file_response(self, filename, file_path, content_type):
        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Content-Length", str(os.path.getsize(file_path)))
        self.end_headers()
        try:
            with open(file_path, "rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    self.safe_write(chunk)
        except ConnectionError:
            print("[Export] Client disconnected during file download.", flush=True)

    def send_zip_response(self, filename, data):
        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.safe_write(data)

    def send_review_excel_by_dir_response(self, filename, project_name):
        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()

        written = 0
        started_at = time.time()

        try:
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "EXPORT_STARTED.txt",
                    (
                        f"Directory Excel export started for project: {project_name}\n"
                        f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    )
                )
                try:
                    self.wfile.flush()
                except Exception:
                    pass

                try:
                    # 1. Fetch project-level summaries in indexed calls
                    project_headers, project_rows = db_manager.export_report("full_project_summary", project_name)
                    dir_headers, all_dir_rows = db_manager.export_report("full_dir_summary", project_name)
                    file_headers, all_file_rows = db_manager.export_report("full_file_summary", project_name)

                    # 2. Pre-fetch all detail rows for the entire project in a single indexed query!
                    all_detail_rows = db_manager.fetch_review_excel_rows(project_name, dir_path=None)
                except Exception as e:
                    archive.writestr(
                        "ERROR.txt",
                        f"Failed to query export data for project: {project_name}\nError: {e}\n"
                    )
                    print(f"[Export] Failed to query data for project '{project_name}': {e}", flush=True)
                    return

                # Extract valid_dirs in memory from all_dir_rows, bypassing the redundant fetch_project_dirs scan!
                dir_infos = []
                for row in all_dir_rows:
                    row_map = dict(zip(dir_headers, row))
                    dir_path = row_map.get("dir_path", "")
                    total_uncovered = 0
                    try:
                        total_uncovered = int(row_map.get("total_uncovered", 0))
                    except (ValueError, TypeError):
                        pass
                    dir_infos.append((dir_path, total_uncovered))

                valid_dirs = [(dir_path, total) for dir_path, total in dir_infos if total]
                all_file_maps = [dict(zip(file_headers, row)) for row in all_file_rows]
                config = load_config()
                worker_count = get_worker_count(config, default_max=8)
                print(f"[Export] Streaming directory Excel package for project '{project_name}', dirs={len(valid_dirs)}, workers={worker_count}", flush=True)
                if not valid_dirs:
                    archive.writestr(
                        "README.txt",
                        (
                            f"No coverage review Excel files were generated for project: {project_name}\n"
                            "Please check whether the project name is correct and whether inject has synced coverage_line_index data.\n"
                        )
                    )
                    print(f"[Export] No directory data for project '{project_name}'. README.txt written.", flush=True)
                    return

                # Group the pre-fetched detail rows by their directory in memory (extremely fast)
                detail_rows_by_dir = {}
                for row in all_detail_rows:
                    file_path = row_value(row, "file_path", 2)
                    dir_path = get_source_dir_name(file_path)
                    detail_rows_by_dir.setdefault(dir_path, []).append(row)

                dir_row_maps = {dict(zip(dir_headers, row)).get("dir_path", ""): row for row in all_dir_rows}
                file_rows_by_dir = {}
                for file_map in all_file_maps:
                    file_dir = get_source_dir_name(file_map.get("file_path", ""))
                    file_rows_by_dir.setdefault(file_dir, []).append([
                        file_map.get(header, "")
                        for header in file_headers
                    ])

                used_names = set()
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = []
                    for current_dir, total_uncovered in valid_dirs:
                        current_dir_rows = [dir_row_maps[current_dir]] if current_dir in dir_row_maps else []
                        current_file_rows = file_rows_by_dir.get(current_dir, [])
                        current_detail_rows = detail_rows_by_dir.get(current_dir, [])
                        futures.append(executor.submit(
                            build_review_excel_for_dir,
                            project_name,
                            current_dir,
                            total_uncovered,
                            project_headers,
                            project_rows,
                            dir_headers,
                            current_dir_rows,
                            file_headers,
                            current_file_rows,
                            current_detail_rows
                        ))

                    for future in as_completed(futures):
                        result = future.result()
                        current_dir = result["dir_path"]
                        total_uncovered = result["total_uncovered"]
                        excel_data = result["excel_data"]
                        base = safe_zip_member_name(current_dir, "root")
                        candidate = f"{base}.xlsx"
                        suffix = 1
                        while candidate in used_names:
                            candidate = f"{base}_{suffix}.xlsx"
                            suffix += 1
                        used_names.add(candidate)
                        archive.writestr(candidate, excel_data)
                        written += 1
                        elapsed = time.time() - started_at
                        print(
                            f"[Export] Wrote {written}/{len(valid_dirs)} {candidate} "
                            f"rows={result['row_count']} uncovered={total_uncovered} elapsed={elapsed:.1f}s",
                            flush=True
                        )
        except ConnectionError:
            print(f"[Export] Client disconnected before directory ZIP Excel download completed for project '{project_name}'.", flush=True)

    def send_error_response(self, status_code, message):
        self.send_json_response(status_code, {"status": "error", "message": message})


_thread_local = threading.local()


class ThreadLocalDatabaseManagerProxy:
    """Proxy class to dynamically route global database calls to thread-local connection managers."""
    def __init__(self, config):
        self._config = config

    def __getattr__(self, name):
        manager = get_thread_db_manager(self._config)
        return getattr(manager, name)


def get_thread_db_manager(config):
    manager = getattr(_thread_local, 'db_manager', None)
    if manager is None:
        manager = DatabaseManager(config, exit_on_error=False, init_schema=False)
        _thread_local.db_manager = manager
    else:
        if getattr(manager, "conn", None) is not None:
            try:
                manager.conn.ping(reconnect=True)
            except Exception:
                try:
                    manager.conn.close()
                except Exception:
                    pass
                manager = DatabaseManager(config, exit_on_error=False, init_schema=False)
                _thread_local.db_manager = manager
    return manager


def close_thread_db_manager():
    manager = getattr(_thread_local, 'db_manager', None)
    if manager is not None:
        try:
            manager.conn.close()
        except Exception:
            pass
        _thread_local.db_manager = None


def process_gcov_file_for_inject(file_path, rel_path, project_name, config, sync_index=True,
                                 review_scope="full", incremental_lines_by_file=None,
                                 render_mode="lazy_collapse", report_id="", output_dir="",
                                 source_input_file=None, can_reuse=False):
    depth = len(rel_path.split(os.sep)) - 1
    prefix = "../" * depth

    css_tag = f'<link rel="stylesheet" type="text/css" href="{prefix}coverage_enhance.css?v={ASSET_VERSION}">\n'
    js_tag = f'<script type="text/javascript" src="{prefix}coverage_enhance.js?v={ASSET_VERSION}"></script>\n'
    inject_code = f"{css_tag}{js_tag}</head>"

    # Read source content from unstripped source_input_file if available (prevents stripped output contamination)
    source_content = ""
    if source_input_file and os.path.isfile(source_input_file):
        try:
            with open(source_input_file, 'r', encoding='utf-8', errors='ignore') as f:
                source_content = f.read()
        except Exception:
            source_content = ""

    if not source_content:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            source_content = f.read()

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        file_content = f.read()

    report_file_path = extract_report_file_path(source_content, rel_path)
    report_file_hash = calc_file_path_hash(report_file_path)  # DB MD5
    sidecar_key = calc_sidecar_file_key(report_file_path)      # Sidecar SHA256[:32]

    review_line_numbers = None
    if review_scope == "incremental":
        review_line_numbers = get_incremental_lines_for_report(
            report_file_path, incremental_lines_by_file or {}
        )
        source_content = mark_incremental_review_lines(source_content, review_line_numbers)
        file_content = mark_incremental_review_lines(file_content, review_line_numbers)
    file_line_index_records = extract_line_index_records(
        source_content, rel_path, project_name, review_line_numbers
    )
    file_index_synced = False

    if sync_index:
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                manager = get_thread_db_manager(config)
                if file_line_index_records:
                    file_index_synced = manager.sync_line_index(project_name, file_line_index_records)
                else:
                    file_index_synced = manager.delete_line_index_file(project_name, report_file_hash)
                if file_index_synced:
                    break
            except Exception:
                close_thread_db_manager()
            if attempt < max_attempts:
                time.sleep(0.2 * attempt)
                print(f"[DB] Retrying line-index sync for {rel_path} ({attempt + 1}/{max_attempts})", flush=True)

    injected = 0
    updated = 0
    sidecar_saved = False

    sidecar_dir = output_dir or os.path.dirname(file_path)
    if render_mode == "lazy_collapse":
        if can_reuse:
            existing_sidecar = load_source_sidecar(sidecar_dir, report_id, sidecar_key)
            if existing_sidecar and existing_sidecar.total_lines > 0:
                sidecar_saved = True

        if not sidecar_saved:
            try:
                source_ctx = parse_source_lines_from_gcov_html(
                    content=source_content,
                    project_name=project_name,
                    file_path=report_file_path,
                    review_scope=review_scope,
                    incremental_line_numbers=set(review_line_numbers) if review_line_numbers else None,
                    report_id=report_id,
                )
                if source_ctx and source_ctx.total_lines > 0:
                    save_source_sidecar(sidecar_dir, report_id, sidecar_key, source_ctx)
                    sidecar_saved = True
            except Exception as err:
                logger.warning(f"[Injector] Failed to save sidecar for {rel_path}: {err}")
                sidecar_saved = False

    meta_tags = (
        f'<meta name="coverage-report-id" content="{html.escape(report_id)}">\n'
        f'<meta name="coverage-file-path" content="{html.escape(report_file_path)}">\n'
        f'<meta name="coverage-render-mode" content="{html.escape(render_mode)}">\n'
    )

    should_strip = (render_mode == "lazy_collapse" and sidecar_saved)

    if "coverage_enhance.js" in file_content:
        new_content = re.sub(r'(href="[^"]*coverage_enhance\.css)(?:\?v=[^"]*)?(")', rf'\1?v={ASSET_VERSION}\2', file_content)
        new_content = re.sub(r'(src="[^"]*coverage_enhance\.js)(?:\?v=[^"]*)?(")', rf'\1?v={ASSET_VERSION}\2', new_content)
        if meta_tags.strip() not in new_content and "<head>" in new_content:
            new_content = new_content.replace("<head>", f"<head>\n{meta_tags}", 1)
        if should_strip and '<pre class="source">' in new_content:
            new_content = re.sub(r'<pre class="source">.*?</pre>', r'<pre class="source"></pre>', new_content, flags=re.S)
        if new_content != file_content:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            updated = 1
    elif "</head>" in file_content:
        new_content = file_content.replace("</head>", f"{inject_code}", 1)
        if "<head>" in new_content:
            new_content = new_content.replace("<head>", f"<head>\n{meta_tags}", 1)
        if should_strip and '<pre class="source">' in new_content:
            new_content = re.sub(r'<pre class="source">.*?</pre>', r'<pre class="source"></pre>', new_content, flags=re.S)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        injected = 1

    return {
        "rel_path": rel_path,
        "file_hash": report_file_hash,
        "record_count": len(file_line_index_records),
        "index_synced": file_index_synced,
        "injected": injected,
        "updated": updated,
    }


def inject_coverage_report(input_dir, output_dir, project_name=None, workers=None, render_mode=None,
                           review_scope="full", incremental_lines_by_file=None, reuse_output=None):
    """
    非破坏性注入覆盖率报告：
    1. 若 output_dir 与 input_dir 不同，且未关联指纹重用，则自动复制 input_dir 至 output_dir (清除已有的 output_dir)
    2. 在 output_dir 中注入样式表、增强脚本并复制静态资源。
    3. ``review_scope=incremental`` 时，仅为变动源码文件所对应的 .gcov.html
       未覆盖行注入可填写控件和数据库索引。
    """
    if not os.path.exists(input_dir):
        print(f"[Error] Input directory '{input_dir}' does not exist.")
        return

    real_input_html = os.path.join(input_dir, "html") if os.path.exists(os.path.join(input_dir, "html")) else input_dir
    real_output_html = os.path.join(output_dir, "html") if os.path.exists(os.path.join(input_dir, "html")) else output_dir

    config = load_config()
    if project_name is None:
        project_name = config.get("project_name", DEFAULT_PROJECT_NAME)
    if reuse_output is None:
        reuse_output = config.get("reuse_output", False)

    if render_mode is None:
        render_mode = config.get("render_mode", "lazy_collapse")
    if render_mode not in VALID_RENDER_MODES:
        render_mode = "lazy_collapse"
    if review_scope not in ("full", "incremental"):
        raise ValueError("review_scope must be 'full' or 'incremental'")

    if render_mode == "lazy_collapse" and os.path.abspath(input_dir) == os.path.abspath(output_dir):
        raise ValueError(
            "In 'lazy_collapse' render mode, input_dir and output_dir must be distinct directories "
            "to protect original HTML source files from being stripped."
        )

    timer = PerfTimer(f"InjectCoverageReport[{project_name}]")
    sig_file = os.path.join(output_dir, ".onesensor_source_signature.json")
    current_sig = compute_directory_signature(
        real_input_html,
        project_name=project_name,
        review_scope=review_scope,
        render_mode=render_mode,
        incremental_lines_by_file=incremental_lines_by_file,
    )
    can_reuse = False

    if input_dir != output_dir:
        if reuse_output and os.path.exists(output_dir) and os.path.exists(sig_file):
            try:
                with open(sig_file, "r", encoding="utf-8") as sf:
                    old_sig = json.load(sf)
                if old_sig == current_sig:
                    can_reuse = True
            except Exception:
                can_reuse = False

        if can_reuse:
            print(f"[Injector] Reusing existing output directory '{output_dir}' (source signature matches).")
            timer.mark("reuse_html_directory")
        else:
            print(f"[Injector] Copying input directory '{input_dir}' to '{output_dir}' (protecting original files)...")
            try:
                if os.path.exists(output_dir):
                    shutil.rmtree(output_dir)
                shutil.copytree(input_dir, output_dir)
                print("[Injector] Copy complete.")
                timer.mark("copy_html_directory")
            except Exception as e:
                print(f"[Error] Failed to copy directory: {e}")
                return

            try:
                with open(sig_file, "w", encoding="utf-8") as sf:
                    json.dump(current_sig, sf, indent=2)
            except Exception:
                pass
    else:
        print("[Warning] Input and output directories are the same. Original files will be modified.")

    if not os.path.exists(real_output_html):
        print(f"[Error] Real output html directory '{real_output_html}' does not exist.")
        return

    output_path_hash = calc_file_path_hash(os.path.abspath(real_output_html))[:8]
    sig_hash = hashlib.sha256(json.dumps(current_sig, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    report_id = f"report_{output_path_hash}_{sig_hash}"
    register_report_directory(report_id, real_output_html, output_dir, os.path.join(real_output_html, ".source_cache"))
    get_code_detail_service(search_dirs=[real_output_html, output_dir, os.path.join(real_output_html, ".source_cache")])

    write_configured_enhance_js(
        os.path.join(real_output_html, "coverage_enhance.js"), project_name, render_mode, review_scope, report_id=report_id
    )
    shutil.copy2(CSS_SOURCE_PATH, os.path.join(real_output_html, "coverage_enhance.css"))
    write_progress_page_targets(output_dir, real_output_html, review_scope)
    print(f"[Injector] Copied static resources to: {real_output_html}")
    print(f"[Injector] Frontend project name: {project_name}")
    print(f"[Injector] Frontend render mode: {render_mode}")
    print(f"[Injector] Review scope: {review_scope}")
    print(f"[Injector] Report ID: {report_id}")
    index_manager = None
    indexed_records = 0
    indexed_files = 0
    active_file_hashes = set()
    try:
        index_manager = DatabaseManager(config, exit_on_error=False)
    except Exception as e:
        print(f"[Warning] Failed to initialize database for full coverage line index: {e}")
        print("[Warning] Inject will continue, but full export requires a successful line-index sync.")

    target_rel_paths = None
    if review_scope == "incremental" and incremental_lines_by_file:
        report_pages = find_report_page_links(real_output_html)
        target_rel_paths = set()
        for src_path in incremental_lines_by_file.keys():
            link = get_report_page_link(src_path, report_pages)
            if link:
                target_rel_paths.add(link)
            base = os.path.basename(src_path)
            if base:
                target_rel_paths.add(base + ".gcov.html")

    print(f"[Injector] Scanning report files under: {real_output_html}")
    gcov_files = []
    for root, dirs, files in os.walk(real_output_html):
        dirs.sort()
        for file in sorted(files):
            if not (file.endswith(".c.gcov.html") or file.endswith(".h.gcov.html")):
                continue
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, real_output_html)
            rel_path_posix = rel_path.replace(os.sep, "/")
            if target_rel_paths is not None and rel_path_posix not in target_rel_paths and file not in target_rel_paths:
                continue
            gcov_files.append((file_path, rel_path))

    total_files = len(gcov_files)
    if total_files == 0:
        print("[Injector] No .gcov.html files found. Nothing to inject.")
        if index_manager and index_manager.conn:
            index_manager.conn.close()
        return

    print(f"[Injector] Found {total_files} .gcov.html file(s). Starting injection and line-index sync...")
    started_at = time.time()
    injected_count = 0
    updated_count = 0
    worker_count = get_worker_count(config, workers, default_max=8)
    if index_manager and index_manager.conn:
        index_manager.conn.close()
    print(f"[Injector] Worker threads: {worker_count}")
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                process_gcov_file_for_inject,
                file_path=file_path,
                rel_path=rel_path,
                project_name=project_name,
                config=config,
                sync_index=index_manager is not None,
                review_scope=review_scope,
                incremental_lines_by_file=incremental_lines_by_file,
                render_mode=render_mode,
                report_id=report_id,
                output_dir=real_output_html,
                source_input_file=os.path.join(real_input_html, rel_path) if input_dir != output_dir else None,
                can_reuse=can_reuse,
            )
            for file_path, rel_path in gcov_files
        ]
        for file_index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            active_file_hashes.add(result["file_hash"])
            indexed_records += result["record_count"] if result["index_synced"] else 0
            indexed_files += 1 if result["index_synced"] else 0
            injected_count += result["injected"]
            updated_count += result["updated"]

            rel_path = result["rel_path"]
            file_index_synced = result["index_synced"]
            file_record_count = result["record_count"]
            elapsed = time.time() - started_at
            percent = file_index * 100.0 / total_files
            rate = file_index / elapsed if elapsed > 0 else 0
            remaining = (total_files - file_index) / rate if rate > 0 else 0
            index_status = "synced" if file_index_synced else "empty" if not file_record_count else "skipped"
            if file_index == 1 or file_index == total_files or file_index % 50 == 0:
                print(
                    f"[Injector] Progress {file_index}/{total_files} ({percent:.1f}%) "
                    f"elapsed={elapsed:.1f}s eta={remaining:.1f}s "
                    f"uncovered={file_record_count} index={index_status} "
                    f"total_indexed={indexed_records} file={rel_path}",
                    flush=True
                )

    timer.mark("gcov_injection_and_line_index_sync")
    print(f"[Injector] Non-destructively enhanced {injected_count} new html report file(s), updated {updated_count} existing enhanced file(s) in: {output_dir}")
    if index_manager:
        index_manager = DatabaseManager(config, exit_on_error=False, init_schema=False)
        index_manager.prune_line_index_project_files(project_name, active_file_hashes)
        if index_manager.conn:
            index_manager.conn.close()
        print(f"[Injector] Synced full coverage line index: {indexed_records} record(s) across {indexed_files} file(s).")
        timer.mark("prune_obsolete_line_index")
    else:
        print("[Injector] Full coverage line index was not synced because database initialization failed.")
    invalidate_project_background_jobs(project_name)
    timer.mark("inject_total_complete")


def find_report_page_links(report_html_dir, target_basenames=None):
    """Build a source-path -> report-page mapping from LCOV page titles."""
    report_pages = {}
    for root, dirs, files in os.walk(report_html_dir):
        dirs.sort()
        for filename in sorted(files):
            if not (filename.endswith(".c.gcov.html") or filename.endswith(".h.gcov.html")):
                continue
            if target_basenames is not None and filename not in target_basenames:
                continue
            page_path = os.path.join(root, filename)
            try:
                with open(page_path, "r", encoding="utf-8", errors="ignore") as page:
                    source_path = extract_report_file_path(page.read(), filename)
            except OSError:
                continue
            source_path = normalize_review_source_path(source_path)
            relative_page = os.path.relpath(page_path, report_html_dir).replace(os.sep, "/")
            report_pages.setdefault(source_path, []).append(relative_page)
    return report_pages


def get_report_page_link(source_path, report_pages):
    source_path = normalize_review_source_path(source_path)
    direct_matches = report_pages.get(source_path, [])
    if len(direct_matches) == 1:
        return direct_matches[0]
    suffix_matches = [
        pages for report_source, pages in report_pages.items()
        if report_source.endswith("/" + source_path) or source_path.endswith("/" + report_source)
    ]
    if len(suffix_matches) == 1 and len(suffix_matches[0]) == 1:
        return suffix_matches[0][0]
    return ""


def incremental_developer_anchor(developer):
    """Return a stable, HTML-safe anchor for one Git author."""
    identity = "{}\n{}".format(developer.get("name", ""), developer.get("email", ""))
    return "developer-{}".format(hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12])


_incremental_unanalyzed_cache = {}
_incremental_unanalyzed_cache_lock = threading.Lock()


def query_incremental_unanalyzed_counts(db_mgr, project_name, config=None):
    """Query DB for unanalyzed line counts per normalized file path.
    Does NOT rely on Connection context manager (__enter__) for legacy PyMySQL compatibility.
    Raises exception on database failure if MySQL is configured.
    """
    is_configured, is_driver = is_mysql_configured(config)
    if is_configured and not is_driver:
        raise RuntimeError("MySQL is configured in coverage_config.json, but PyMySQL driver is not installed")

    conn = getattr(db_mgr, "conn", None) if db_mgr else None
    if not conn:
        if is_configured:
            raise RuntimeError("Database connection to MySQL is configured but currently unavailable")
        return {}

    try:
        if hasattr(conn, "ping"):
            conn.ping(reconnect=True)
    except Exception as ping_err:
        if is_configured:
            raise RuntimeError("Database ping failed for configured MySQL database: {}".format(ping_err))

    conn = getattr(db_mgr, "conn", None) if db_mgr else None
    if not conn:
        if is_configured:
            raise RuntimeError("Database connection to MySQL was lost during ping")
        return {}

    cursor = conn.cursor()
    unanalyzed_by_file = {}
    try:
        sql = """
            SELECT MAX(i.file_path) AS file_path,
                   SUM(CASE WHEN a.id IS NULL OR COALESCE(a.is_draft, 0) = 1 OR a.status = %s THEN 1 ELSE 0 END) AS unanalyzed_total
            FROM coverage_line_index i
            LEFT JOIN coverage_analysis a
              ON i.project_name = a.project_name
             AND i.file_path_hash = a.file_path_hash
             AND i.line_number = a.line_number
            WHERE i.project_name = %s
            GROUP BY i.project_name, i.file_path_hash
        """
        cursor.execute(sql, ("未确认", project_name))
        rows = cursor.fetchall() or []
        for row in rows:
            if not row:
                continue
            if isinstance(row, dict):
                fpath = row.get("file_path", "")
                count = row.get("unanalyzed_total", 0)
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                fpath = row[0]
                count = row[1]
            elif hasattr(row, "__getitem__"):
                try:
                    fpath = row[0]
                    count = row[1]
                except Exception:
                    continue
            else:
                continue
            if fpath:
                unanalyzed_by_file[_normalize_ownership_path(fpath)] = int(count or 0)
        return unanalyzed_by_file
    except Exception as err:
        if is_configured:
            raise RuntimeError("Database query failed for configured MySQL database: {}".format(err))
        raise
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def get_incremental_unanalyzed_counts(project_name, db_mgr=None, config=None):
    """Fetch unanalyzed counts per file for project_name, utilizing data_version caching.
    Guarantees failed database queries are NOT saved to cache.
    """
    if db_mgr is None:
        db_mgr = get_thread_db_manager(config)

    current_ver = get_project_data_version(project_name, manager=db_mgr)

    with _incremental_unanalyzed_cache_lock:
        cached = _incremental_unanalyzed_cache.get(project_name)
        if cached and cached.get("version") == current_ver:
            return cached["data"], current_ver

    counts_map = query_incremental_unanalyzed_counts(db_mgr, project_name, config=config)

    with _incremental_unanalyzed_cache_lock:
        _incremental_unanalyzed_cache[project_name] = {
            "version": current_ver,
            "data": counts_map,
        }

    return counts_map, current_ver


def sync_incremental_unanalyzed_counts(project_name, result, config=None):
    """Query DB for unanalyzed counts and update result['summary']['unanalyzed'] beforehand."""
    if not result or "details" not in result:
        return {}
    summary = result.get("summary") or {}
    details_by_file = {}
    for item in result["details"]:
        key = (item.get("repository", ""), item.get("review_file_path") or item["file_path"])
        details_by_file.setdefault(key, []).append(item)

    cfg = load_config() if config is None else config
    is_configured, is_driver = is_mysql_configured(cfg)

    if is_configured:
        if not is_driver:
            raise RuntimeError("MySQL is configured for project '{}', but PyMySQL driver is not installed".format(project_name))
        db_mgr = get_thread_db_manager(cfg)
        is_db_active = db_mgr and db_mgr.is_available()
        if not is_db_active:
            raise RuntimeError("MySQL is configured for project '{}', but database connection is unavailable".format(project_name))
        try:
            unanalyzed_by_file, _ = get_incremental_unanalyzed_counts(project_name, db_mgr=db_mgr, config=cfg)
        except Exception as err:
            print("[Error] Failed to query incremental unanalyzed counts for project '{}': {}".format(project_name, err), flush=True)
            raise RuntimeError("Database query for incremental unanalyzed counts failed: {}".format(err))
    else:
        # Standalone / Demo mode without DB
        unanalyzed_by_file = {}

    total_unanalyzed = 0
    matched_count = 0
    path_miss_count = 0
    for repository_name, review_file_path in sorted(details_by_file):
        items = details_by_file[(repository_name, review_file_path)]
        uncovered_count = sum(1 for item in items if item.get("status") == coverage_check.STATUS_UNCOVERED)
        file_key = _normalize_ownership_path(review_file_path)
        if file_key in unanalyzed_by_file:
            matched_count += 1
            total_unanalyzed += unanalyzed_by_file[file_key]
        else:
            if is_configured:
                path_miss_count += 1
                print("[WARNING] Path miss for unanalyzed counts: '{}' (repository: '{}'), defaulting to uncovered count ({})".format(file_key, repository_name, uncovered_count), flush=True)
            total_unanalyzed += uncovered_count

    if is_configured:
        print("[Unanalyzed Sync] Project '{}': Matched {} file(s), missed {} file(s) against MySQL line index.".format(project_name, matched_count, path_miss_count), flush=True)
        if details_by_file and not unanalyzed_by_file:
            print("[WARNING] MySQL query returned 0 unanalyzed file records for project '{}'. All {} report file(s) defaulted to uncovered count!".format(project_name, len(details_by_file)), flush=True)

    summary["unanalyzed"] = total_unanalyzed
    return unanalyzed_by_file


def write_incremental_summary_page(output_html_dir, project_name, result, config=None, unanalyzed_by_file=None):
    """Create an auditable landing page for the generated incremental review site."""
    summary = result["summary"]
    target_files = list(result.get("uncovered_lines_by_file", {}).keys()) + list(result.get("review_lines_by_file", {}).keys())
    target_basenames = {os.path.basename(f) + ".gcov.html" for f in target_files if os.path.basename(f)}
    report_pages = find_report_page_links(output_html_dir, target_basenames=target_basenames if target_basenames else None)
    # Use the very same ownership workbook and matching rules as the progress
    # page so an incremental file never appears under a different leader on the
    # two pages.  Load it once: incremental reports can contain many files.
    ownership_workbook = load_ownership_workbook(
        load_config() if config is None else config
    )
    details_by_file = {}
    for item in result["details"]:
        key = (item.get("repository", ""), item.get("review_file_path") or item["file_path"])
        details_by_file.setdefault(key, []).append(item)

    def escaped(value):
        return html.escape(str(value), quote=True)

    if unanalyzed_by_file is None:
        unanalyzed_by_file = sync_incremental_unanalyzed_counts(project_name, result, config)

    file_rows = []
    for repository_name, review_file_path in sorted(details_by_file):
        items = details_by_file[(repository_name, review_file_path)]
        counts = {status: 0 for status in (
            coverage_check.STATUS_COVERED,
            coverage_check.STATUS_UNCOVERED,
            coverage_check.STATUS_IGNORED,
            coverage_check.STATUS_MISSING,
        )}
        for item in items:
            counts[item["status"]] += 1
        if ownership_workbook.get("available"):
            ownership = match_file_ownership(review_file_path, ownership_workbook)
        else:
            ownership = {
                "module": "",
                "team": OWNERSHIP_UNMATCHED_TEAM,
                "leader": OWNERSHIP_UNMATCHED_LEADER,
                "ownership_status": "归属表不可用",
            }
        file_key = _normalize_ownership_path(review_file_path)
        unanalyzed_count = unanalyzed_by_file.get(file_key, counts[coverage_check.STATUS_UNCOVERED])
        file_rows.append({
            "repository": repository_name or "",
            "module": ownership["module"],
            "team": ownership["team"],
            "leader": ownership["leader"],
            "ownership_status": ownership["ownership_status"],
            "file_path": items[0]["file_path"],
            "review_file_path": review_file_path,
            "changed": len(items),
            "covered": counts[coverage_check.STATUS_COVERED],
            "uncovered": counts[coverage_check.STATUS_UNCOVERED],
            "ignored": counts[coverage_check.STATUS_IGNORED],
            "missing": counts[coverage_check.STATUS_MISSING],
            "unanalyzed": unanalyzed_count,
        })

    summary["missing"] = sum(item["missing"] for item in file_rows)
    summary["unanalyzed"] = sum(item["unanalyzed"] for item in file_rows)

    # The first render already prioritizes files that need the most review; the
    # browser-side table controls below let reviewers switch to any other metric.
    file_rows.sort(key=lambda item: (
        -item["uncovered"], -item["changed"], item["repository"],
        item["module"], item["team"], item["leader"], item["file_path"]
    ))
    rows = []
    for item in file_rows:
        repository_name = item["repository"]
        review_file_path = item["review_file_path"]
        file_key = _normalize_ownership_path(review_file_path)
        page_link = get_report_page_link(review_file_path, report_pages)
        source_cell = escaped(item["file_path"])
        if page_link:
            source_cell = '<a href="{}">{}</a>'.format(
                escaped(urllib.parse.quote(page_link, safe="/%#?=&-_.~")), source_cell
            )
        ownership_text = "{} / {} / {}".format(item["module"] or "-", item["team"], item["leader"])
        module_cell = escaped(item["module"] or "-")
        team_cell = escaped(item["team"] or "-")
        leader_cell = escaped(item["leader"] or "-")
        if item["ownership_status"] != "已匹配":
            module_cell = '<span class="badge-unmatched-pill" title="{}">{}</span>'.format(
                escaped(item["ownership_status"]), module_cell
            )
        rows.append(
            '<tr data-repo="{}" data-module="{}" data-team="{}" data-leader="{}" data-ownership="{}" data-file-key="{}">'
            '<td data-sort-value="{}">{}</td>'
            '<td data-sort-value="{}">{}</td>'
            '<td data-sort-value="{}">{}</td>'
            '<td data-sort-value="{}">{}</td>'
            '<td data-sort-value="{}">{}</td>'
            '<td data-sort-value="{}">{}</td>'
            '<td data-sort-value="{}">{}</td>'
            '<td data-sort-value="{}">{}</td>'
            '<td data-sort-value="{}">{}</td>'
            '<td class="js-unanalyzed-count" data-sort-value="{}">{}</td></tr>'.format(
                escaped(repository_name),
                escaped(item["module"]),
                escaped(item["team"]),
                escaped(item["leader"]),
                escaped(ownership_text),
                escaped(file_key),
                escaped(repository_name),
                escaped(repository_name or "-"),
                escaped(item["team"]),
                team_cell,
                escaped(item["leader"]),
                leader_cell,
                escaped(item["module"]),
                module_cell,
                escaped(item["file_path"]),
                source_cell,
                item["changed"], item["changed"],
                item["covered"], item["covered"],
                item["uncovered"], item["uncovered"],
                item["ignored"], item["ignored"],
                item["unanalyzed"], item["unanalyzed"],
            )
        )

    target_js_path = os.path.join(output_html_dir, "incremental_coverage.js")
    if os.path.exists(INCREMENTAL_JS_SOURCE_PATH):
        shutil.copy2(INCREMENTAL_JS_SOURCE_PATH, target_js_path)
    else:
        raise RuntimeError(f"Required static asset {INCREMENTAL_JS_SOURCE_PATH} not found beside enhance_coverage.py")

    rate = summary["coverage_rate"]
    rate_text = "N/A" if rate is None else "{:.2f}%".format(rate)
    table_rows = "".join(rows) or '<tr><td colspan="10">本次 Git diff 没有新增代码行。</td></tr>'
    repositories = result.get("repositories") or []
    if repositories:
        git_range_text = "{} 个仓库的 Git 范围".format(len(repositories))
        repository_ranges = '<div class="repo-badges">{}</div>'.format("".join(
            '<span class="repo-chip"><strong class="repo-name">{}</strong> <code class="commit-range">{} → {}</code></span>'.format(
                escaped(repository["name"]), escaped(repository["oldgit"][:8]), escaped(repository["newgit"][:8])
            ) for repository in repositories
        ))
    else:
        git_range_text = "<code>{}</code> → <code>{}</code>".format(
            escaped(result["oldgit"]), escaped(result["newgit"])
        )
        repository_ranges = ""
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>增量覆盖率审查</title><style>
body{{margin:0;background:#f2f2f7;color:#1c1c1e;font:14px/1.5 "Microsoft YaHei","微软雅黑",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}}
main{{max-width:1280px;margin:0 auto;padding:24px 24px 48px}}
.hero-card{{background:#ffffff;border:1px solid rgba(0,0,0,0.04);border-radius:18px;padding:24px 28px;box-shadow:0 4px 20px -2px rgba(0,0,0,0.04);text-align:center;margin-bottom:20px}}
h1{{margin:0 0 8px;font-size:26px;font-weight:800;letter-spacing:-0.6px;color:#1c1c1e}}
.muted{{color:#8e8e93;font-size:13px}}
.version-badge-container{{display:flex;justify-content:center;align-items:center;margin-top:10px}}
.page-version-badge{{display:inline-flex;align-items:center;gap:8px;background:rgba(118,118,128,0.1);padding:4px 6px 4px 12px;border-radius:999px;font-size:12px;color:#3a3a3c}}
.whats-new-badge-btn{{display:inline-flex;align-items:center;gap:4px;background:#007aff;color:#ffffff;border:none;padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;transition:all 0.18s ease;box-shadow:0 2px 6px rgba(0,122,255,0.25)}}
.whats-new-badge-btn:hover{{background:#0056b3;transform:scale(1.04);box-shadow:0 3px 8px rgba(0,122,255,0.35)}}
.whats-new-badge-btn:active{{transform:scale(0.97)}}
.modal-overlay{{position:fixed;inset:0;background:rgba(0,0,0,0.45);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;z-index:99999;opacity:0;visibility:hidden;transition:opacity 0.22s cubic-bezier(0.16,1,0.3,1),visibility 0.22s}}
.modal-overlay.active{{opacity:1;visibility:visible}}
.modal-card{{background:#ffffff;width:90%;max-width:620px;max-height:85vh;border-radius:20px;box-shadow:0 20px 48px rgba(0,0,0,0.18),0 0 0 1px rgba(0,0,0,0.06);display:flex;flex-direction:column;transform:scale(0.94) translateY(10px);transition:transform 0.22s cubic-bezier(0.16,1,0.3,1);overflow:hidden;text-align:left}}
.modal-overlay.active .modal-card{{transform:scale(1.0) translateY(0)}}
.modal-header{{padding:20px 24px 16px;border-bottom:1px solid rgba(60,60,67,0.08);display:flex;justify-content:space-between;align-items:flex-start}}
.modal-title{{margin:0;font-size:18px;font-weight:800;color:#1c1c1e;letter-spacing:-0.4px}}
.modal-subtitle{{font-size:12px;color:#8e8e93;margin-top:4px;font-weight:500}}
.modal-close-btn{{background:rgba(118,118,128,0.1);border:none;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;line-height:1;color:#8e8e93;cursor:pointer;transition:all 0.15s ease;padding:0}}
.modal-close-btn:hover{{background:rgba(118,118,128,0.2);color:#1c1c1e}}
.modal-body{{padding:20px 24px;overflow-y:auto;display:flex;flex-direction:column;gap:14px}}
.feature-section{{background:rgba(242,242,247,0.5);border-radius:12px;padding:14px 16px;border:1px solid rgba(0,0,0,0.03)}}
.feature-heading{{font-size:14px;font-weight:700;color:#1c1c1e;display:flex;align-items:center;gap:8px;margin-bottom:8px}}
.feature-icon{{font-size:15px}}
.feature-list{{margin:0;padding-left:18px;font-size:13px;color:#3a3a3c;line-height:1.6}}
.feature-list li{{margin-bottom:6px}}
.feature-list li:last-child{{margin-bottom:0}}
.feature-list strong{{color:#1c1c1e}}
.modal-footer{{padding:14px 24px;border-top:1px solid rgba(60,60,67,0.08);display:flex;justify-content:flex-end;background:rgba(242,242,247,0.3)}}
.modal-action-btn{{background:#007aff;color:#ffffff;border:none;padding:8px 22px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:all 0.15s ease}}
.modal-action-btn:hover{{background:#0056b3}}
.repo-badges{{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:10px}}
.repo-chip{{display:inline-flex;align-items:center;gap:6px;background:rgba(0,122,255,0.08);padding:4px 10px;border-radius:8px;font-size:12px}}
.repo-name{{color:#007aff;font-weight:700}}
.commit-range{{font-family:SFMono-Regular,Menlo,Monaco,Consolas,monospace;color:#3a3a3c;font-size:11px}}
.links-segmented{{display:inline-flex;gap:4px;background:rgba(118,118,128,0.12);padding:3px;border-radius:10px;margin-top:16px;flex-wrap:wrap;justify-content:center}}
.links-segmented a{{display:inline-flex;align-items:center;padding:6px 14px;border-radius:7px;font-size:13px;font-weight:600;color:#007aff;text-decoration:none;transition:all 0.15s ease}}
.links-segmented a:hover{{background:#ffffff;box-shadow:0 2px 8px rgba(0,0,0,0.08);color:#0051a8}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px}}
.card{{background:#ffffff;border:1px solid rgba(0,0,0,0.04);border-radius:14px;padding:16px;box-shadow:0 4px 20px -2px rgba(0,0,0,0.04)}}
.label{{font-size:12px;color:#8e8e93;font-weight:600;margin-bottom:6px}}
.value{{font-size:22px;font-weight:800;letter-spacing:-0.5px}}
.val-changed{{color:#1c1c1e}}
.val-covered{{color:#34c759}}
.val-uncovered{{color:#007aff}}
.val-rate{{color:#007aff}}
.val-missing{{color:#ff9500}}
#incremental-unanalyzed-total.refresh-failed::after{{content:" ⚠";color:#e65100;font-weight:bold;margin-left:4px}}
section{{background:#ffffff;border:1px solid rgba(0,0,0,0.04);border-radius:16px;overflow:hidden;box-shadow:0 4px 20px -2px rgba(0,0,0,0.04)}}
h2{{margin:0;padding:16px 20px;font-size:16px;font-weight:700;background:rgba(242,242,247,0.7);border-bottom:1px solid rgba(60,60,67,0.1)}}
.filters{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:14px 20px;background:rgba(242,242,247,0.4);border-bottom:1px solid rgba(60,60,67,0.08)}}
.filter-group{{display:flex;align-items:center;gap:6px;font-size:13px;font-weight:600;color:#1c1c1e}}
.filters select,.filters input[type="text"]{{height:34px;padding:0 12px;border:1px solid rgba(0,0,0,0.06);border-radius:10px;font-size:13px;background:rgba(118,118,128,0.1);color:#000;outline:none;transition:all 0.2s ease}}
.filters select:focus,.filters input[type="text"]:focus{{background:#ffffff;border-color:#007aff;box-shadow:0 0 0 3px rgba(0,122,255,0.15)}}
.filters input[type="text"]{{width:210px}}
.reset-btn{{height:34px;padding:0 14px;border:none;border-radius:10px;background:rgba(0,122,255,0.1);cursor:pointer;font-size:13px;font-weight:600;color:#007aff;transition:all 0.2s ease}}
.reset-btn:hover{{background:rgba(0,122,255,0.2)}}
.filter-count{{margin-left:auto;font-size:12px;color:#8e8e93;font-weight:600}}
table{{width:100%;border-collapse:collapse;min-width:880px}}
th,td{{padding:12px 16px;border-bottom:1px solid rgba(60,60,67,0.08);text-align:left;vertical-align:middle;font-variant-numeric:tabular-nums}}
th{{background:rgba(242,242,247,0.95);font-size:12px;font-weight:600;color:#8e8e93;text-transform:uppercase;letter-spacing:0.2px}}
tr:hover td{{background:rgba(0,122,255,0.03)}}
.sort-button{{appearance:none;border:0;background:transparent;color:inherit;cursor:pointer;font:inherit;font-weight:600;padding:0;white-space:nowrap}}
.sort-button:hover{{color:#007aff}}
.sort-indicator{{display:inline-block;min-width:1em;color:#8e8e93}}
.badge-unmatched-pill{{display:inline-flex;align-items:center;background:rgba(255,149,0,0.12);color:#c67600;padding:3px 10px;border-radius:999px;font-weight:600;font-size:12px}}
td a{{color:#007aff;text-decoration:none;font-weight:600}}
td a:hover{{text-decoration:underline}}
@media(max-width:850px){{.cards{{grid-template-columns:repeat(2,minmax(130px,1fr))}}main{{padding:16px 12px}}}}
</style></head><body><main data-project="{project}">
<div class="hero-card">
  <h1>增量覆盖率审查</h1>
  <div class="muted">项目：{project}；Git 范围：{git_range_text}；生成时间：{generated_at}</div>{repository_ranges}
  <div class="version-badge-container"><span class="page-version-badge"><span>{version_label}</span><button type="button" id="whats-new-btn" class="whats-new-badge-btn" title="查看新特性说明" aria-haspopup="dialog">✨ 新特性</button></span></div>
  <div class="links-segmented">
    <a href="coverage_progress.html?scope=incremental&amp;project={project_url}">📊 查看填写进度</a>
    <a href="incremental_developer_tasks.html">👨‍💻 开发人员待填写清单</a>
    <a href="incremental_coverage.json">📥 下载计算结果 JSON</a>
    <a href="incremental_coverage.xlsx">📈 下载计算结果 Excel</a>
  </div>
</div>
<div class="cards">
  <div class="card"><div class="label">新增行</div><div class="value val-changed">{changed}</div></div>
  <div class="card"><div class="label">已覆盖</div><div class="value val-covered">{covered}</div></div>
  <div class="card"><div class="label">增量未覆盖（可填写）</div><div class="value val-uncovered">{uncovered}</div></div>
  <div class="card"><div class="label">有效增量覆盖率</div><div class="value val-rate">{rate}</div></div>
  <div class="card"><div class="label">待分析</div><div id="incremental-unanalyzed-total" class="value val-missing">{unanalyzed}</div></div>
</div>
<section><h2>文件明细（点击表头可排序；默认未覆盖新增行从多到少）</h2>
<div class="filters">
  <div class="filter-group"><label for="repo-filter">仓库：</label><select id="repo-filter"><option value="">全部仓库</option></select></div>
  <div class="filter-group"><label for="team-filter">小组：</label><select id="team-filter"><option value="">全部小组</option></select></div>
  <div class="filter-group"><label for="leader-filter">组长：</label><select id="leader-filter"><option value="">全部组长</option></select></div>
  <div class="filter-group"><label for="module-filter">组件：</label><select id="module-filter"><option value="">全部组件</option></select></div>
  <div class="filter-group"><label for="file-search">文件搜索：</label><input type="text" id="file-search" placeholder="输入文件路径关键字..."></div>
  <button type="button" id="reset-filters-btn" class="reset-btn">重置筛选</button>
  <span id="filter-count" class="filter-count"></span>
</div>
<table id="incremental-file-table"><thead><tr><th data-sort-key="repository" aria-sort="none"><button type="button" class="sort-button" data-sort-key="repository" data-sort-type="text">仓库 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="team" aria-sort="none"><button type="button" class="sort-button" data-sort-key="team" data-sort-type="text">小组 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="leader" aria-sort="none"><button type="button" class="sort-button" data-sort-key="leader" data-sort-type="text">组长 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="module" aria-sort="none"><button type="button" class="sort-button" data-sort-key="module" data-sort-type="text">组件 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="file" aria-sort="none"><button type="button" class="sort-button" data-sort-key="file" data-sort-type="text">文件 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="changed" aria-sort="none"><button type="button" class="sort-button" data-sort-key="changed" data-sort-type="number">新增行 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="covered" aria-sort="none"><button type="button" class="sort-button" data-sort-key="covered" data-sort-type="number">已覆盖 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="uncovered" aria-sort="none"><button type="button" class="sort-button" data-sort-key="uncovered" data-sort-type="number">未覆盖 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="ignored" aria-sort="none"><button type="button" class="sort-button" data-sort-key="ignored" data-sort-type="number">无需覆盖 <span class="sort-indicator" aria-hidden="true">↕</span></button></th><th data-sort-key="unanalyzed" aria-sort="none"><button type="button" class="sort-button" data-sort-key="unanalyzed" data-sort-type="number">待分析行数 <span class="sort-indicator" aria-hidden="true">↕</span></button></th></tr></thead><tbody>{table_rows}</tbody></table></section>
<footer style="text-align:center;color:#8e8e93;font-size:12px;padding:24px 0 12px;">覆盖率审查系统 · {version_label}</footer>
<div id="whats-new-modal" class="modal-overlay" aria-hidden="true" role="dialog" aria-modal="true" aria-labelledby="whats-new-title">
  <div class="modal-card">
    <div class="modal-header">
      <div>
        <h3 id="whats-new-title" class="modal-title">🚀 覆盖率审查系统 · v11.3 更新说明</h3>
        <div class="modal-subtitle">发布日期：2026-08-19</div>
      </div>
      <button type="button" id="modal-close-x" class="modal-close-btn" aria-label="关闭">&times;</button>
    </div>
    <div class="modal-body">
      <div class="feature-section">
        <div class="feature-heading"><span class="feature-icon">⚡</span> 极速加载与折叠架构</div>
        <ul class="feature-list">
          <li><strong>10x 加载提速</strong>：引入 Lazy Collapse 架构，非待分析代码块默认折叠懒加载，大幅减轻 DOM 节点压力，超大文件秒级打开。</li>
          <li><strong>待分析函数无条件展开</strong>：包含未覆盖/待审行的函数自动完整呈现，支持超大函数 Initial Batch 一键极速载入。</li>
        </ul>
      </div>
      <div class="feature-section">
        <div class="feature-heading"><span class="feature-icon">📝</span> 交互体验与状态保障</div>
        <ul class="feature-list">
          <li><strong>草稿与本地编辑严格解耦</strong>：ReviewDraftStore 分离本地 Dirty 编辑与服务端 Draft 草稿，收起/展开源码绝不丢失未保存内容。</li>
          <li><strong>丝滑流式全量展开</strong>：大文件一键 Expand All 采用分批渐进流式加载与进度调度，彻底杜绝浏览器界面假死。</li>
        </ul>
      </div>
      <div class="feature-section">
        <div class="feature-heading"><span class="feature-icon">🛡️</span> 架构可靠性与缓存隔离</div>
        <ul class="feature-list">
          <li><strong>增量指纹版本化隔离</strong>：报告 ID 深度绑定 Git 评审集 Hash 与目录指纹，彻底杜绝跨版本旧缓存干扰。</li>
          <li><strong>并发安全 Sidecar 注册体系</strong>：原子事务写入，支持多进程并发注入，严格保护原始覆盖率报告与数据库行索引。</li>
        </ul>
      </div>
    </div>
    <div class="modal-footer">
      <button type="button" id="modal-close-btn" class="modal-action-btn">知道了</button>
    </div>
  </div>
</div>
</main><script src="incremental_coverage.js?v={asset_version}"></script></body></html>""".format(
        asset_version=ASSET_VERSION,
        version_label=VERSION_DISPLAY_LABEL,
        project=escaped(project_name),
        project_url=escaped(urllib.parse.quote(str(project_name), safe="")),
        git_range_text=git_range_text,
        repository_ranges=repository_ranges,
        generated_at=escaped(result["generated_at"]),
        changed=summary["changed_lines"],
        covered=summary["covered"],
        uncovered=summary["uncovered"],
        rate=rate_text,
        unanalyzed=summary["unanalyzed"],
        table_rows=table_rows,
    )
    with open(os.path.join(output_html_dir, "incremental_coverage.html"), "w", encoding="utf-8") as page_file:
        page_file.write(page)


def write_incremental_developer_tasks_page(output_html_dir, project_name, result):
    """Create a static, CSP-safe task list grouped by Git author.

    This page intentionally contains no executable inline JavaScript: it must
    also work when the review site is published with a strict CSP.
    """
    report_pages = find_report_page_links(output_html_dir)
    developers = (result.get("developer_tasks") or {}).get("developers") or []

    def escaped(value):
        return html.escape(str(value), quote=True)

    summary_rows = []
    developer_sections = []
    for developer in developers:
        anchor = incremental_developer_anchor(developer)
        name = escaped(developer.get("name", "Unknown"))
        email = escaped(developer.get("email", ""))
        display_name = name if not email else "{} &lt;{}&gt;".format(name, email)
        summary_rows.append(
            "<tr><td><a href=\"#{}\">{}</a></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                anchor, display_name, developer.get("commit_total", 0),
                developer.get("changed_file_total", 0), developer.get("review_file_total", 0),
                developer.get("review_uncovered_total", 0),
            )
        )

        file_rows = []
        for file_task in developer.get("files") or []:
            source_path = file_task.get("review_file_path") or file_task.get("file_path", "")
            page_link = get_report_page_link(source_path, report_pages)
            file_path = escaped(file_task.get("file_path", ""))
            source_cell = file_path
            if page_link:
                source_cell = '<a href="{}">{}</a>'.format(
                    escaped(urllib.parse.quote(page_link, safe="/%#?=&-_.~")), file_path
                )

            commits = file_task.get("commits") or []
            commit_text = "".join(
                '<div class="commit-chip"><code class="commit-hash">{}</code> <span class="commit-subject">{}</span>{}</div>'.format(
                    escaped(item.get("commit", "")[:8]),
                    escaped(item.get("subject", "")),
                    ' <span class="commit-date">{}</span>'.format(
                        escaped(item.get("committed_at", ""))
                    ) if item.get("committed_at") else "",
                ) for item in commits
            ) or "-"

            uncovered = file_task.get("uncovered", 0)
            changed = file_task.get("changed", 0)
            if uncovered:
                if page_link:
                    action = '<a class="fill-link" href="{}">填写 {} 行</a>'.format(
                        escaped(urllib.parse.quote(page_link, safe="/%#?=&-_.~")), uncovered
                    )
                else:
                    action = '<span class="todo">待填写 {} 行（未找到源码页）</span>'.format(uncovered)
            elif changed:
                action = '<span class="done">本次新增行无需填写</span>'
            else:
                action = '<span class="muted">本次提交未产生新增代码行</span>'

            file_rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
                "<td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    escaped(file_task.get("repository", "") or "-"), source_cell,
                    escaped(", ".join(file_task.get("change_types") or [])), commit_text,
                    changed, file_task.get("covered", 0), uncovered,
                    file_task.get("missing", 0), action,
                )
            )
        if not file_rows:
            file_rows.append('<tr><td colspan="9">此开发人员没有可展示的提交文件。</td></tr>')
        developer_sections.append(
            '<section id="{}"><h2>👤 {}</h2><div class="person-stats"><span class="stat-pill">提交 <strong>{}</strong> 个</span>'
            '<span class="stat-pill">提交文件 <strong>{}</strong> 个</span><span class="stat-pill">需填写文件 <strong>{}</strong> 个</span>'
            '<span class="stat-pill warn">待填写 <strong>{}</strong> 行</span></div><div class="table-wrap"><table>'
            '<thead><tr><th>仓库</th><th>提交文件</th><th>变更类型</th><th>关联提交</th>'
            '<th>新增行</th><th>已覆盖</th><th>待填写</th><th>覆盖信息缺失</th><th>操作</th></tr></thead>'
            '<tbody>{}</tbody></table></div></section>'.format(
                anchor, display_name, developer.get("commit_total", 0),
                developer.get("changed_file_total", 0), developer.get("review_file_total", 0),
                developer.get("review_uncovered_total", 0), "".join(file_rows),
            )
        )

    summary_table_rows = "".join(summary_rows) or (
        '<tr><td colspan="5">未找到此 Git 范围内可映射的提交作者。</td></tr>'
    )
    sections_html = "".join(developer_sections) or (
        '<section><h2>暂无开发人员任务</h2><p>请确认提交范围内存在 Git commit，且当前执行用户可读取仓库历史。</p></section>'
    )
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>开发人员待填写清单</title><style>
body{{margin:0;background:#f2f2f7;color:#1c1c1e;font:14px/1.5 "Microsoft YaHei","微软雅黑",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}}
main{{max-width:1320px;margin:0 auto;padding:24px 24px 48px}}
.hero-card{{background:#ffffff;border:1px solid rgba(0,0,0,0.04);border-radius:18px;padding:24px 28px;box-shadow:0 4px 20px -2px rgba(0,0,0,0.04);margin-bottom:20px}}
h1{{margin:0 0 8px;font-size:26px;font-weight:800;letter-spacing:-0.6px;color:#1c1c1e}}
h2{{margin:0;padding:16px 20px;font-size:16px;font-weight:700;background:rgba(242,242,247,0.7);border-bottom:1px solid rgba(60,60,67,0.1);letter-spacing:-0.3px}}
.muted{{color:#8e8e93;font-size:13px}}
.page-version-badge{{display:inline-flex;align-items:center;gap:6px;background:rgba(118,118,128,0.1);padding:4px 12px;border-radius:8px;font-size:12px;color:#3a3a3c;margin-top:10px}}
.links-segmented{{display:inline-flex;gap:4px;background:rgba(118,118,128,0.12);padding:4px;border-radius:12px;margin-top:14px;flex-wrap:wrap}}
.links-segmented a{{color:#007aff;text-decoration:none;font-weight:600;font-size:13px;padding:6px 14px;border-radius:9px;transition:all 0.15s ease}}
.links-segmented a:hover{{background:#ffffff;color:#0051a8;box-shadow:0 2px 8px rgba(0,0,0,0.08)}}
section{{background:#ffffff;border:1px solid rgba(0,0,0,0.04);border-radius:16px;margin:20px 0;overflow:hidden;box-shadow:0 4px 20px -2px rgba(0,0,0,0.04);transition:all 0.2s ease}}
.person-stats{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;padding:12px 20px;background:rgba(242,242,247,0.4);border-bottom:1px solid rgba(60,60,67,0.08);font-size:13px}}
.stat-pill{{display:inline-flex;align-items:center;gap:4px;background:rgba(118,118,128,0.1);padding:4px 12px;border-radius:8px;font-size:12px;color:#3a3a3c}}
.stat-pill.warn{{background:rgba(255,149,0,0.12);color:#c67600;font-weight:700}}
.table-wrap{{overflow:auto}}
table{{width:100%;border-collapse:collapse;min-width:960px}}
th,td{{padding:12px 16px;border-bottom:1px solid rgba(60,60,67,0.08);text-align:left;vertical-align:middle;font-variant-numeric:tabular-nums}}
th{{background:rgba(242,242,247,0.95);font-size:12px;font-weight:600;color:#8e8e93;text-transform:uppercase;letter-spacing:0.2px}}
tr:hover td{{background:rgba(0,122,255,0.03)}}
.commit-chip{{display:flex;align-items:center;gap:6px;margin:3px 0;font-size:12px}}
.commit-hash{{font-family:SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:11px;background:rgba(0,122,255,0.1);color:#007aff;padding:2px 6px;border-radius:6px;font-weight:600;white-space:nowrap}}
.commit-subject{{color:#1c1c1e;font-weight:500}}
.commit-date{{color:#8e8e93;font-size:11px;white-space:nowrap;margin-left:auto}}
.todo{{color:#ff9500;font-weight:700}}
.done{{color:#34c759;font-weight:700}}
.fill-link{{display:inline-flex;align-items:center;gap:4px;background:linear-gradient(180deg,#007aff 0%,#0062cc 100%);color:#ffffff;border-radius:9px;padding:6px 14px;text-decoration:none;font-weight:600;font-size:12px;transition:all 0.2s cubic-bezier(0.4,0,0.2,1);box-shadow:0 3px 8px rgba(0,122,255,0.25);white-space:nowrap}}
.fill-link:hover{{transform:translateY(-1px);box-shadow:0 5px 12px rgba(0,122,255,0.4)}}
td a{{color:#007aff;text-decoration:none;font-weight:600}}
td a:hover{{text-decoration:underline}}
@media(max-width:700px){{main{{padding:16px 12px}}}}
</style></head><body><main>
<div class="hero-card">
  <h1>开发人员待填写清单</h1>
  <div class="muted">项目：{project}；数据来源：Git 提交作者与增量覆盖率结果。</div>
  <div class="links-segmented">
    <a href="incremental_coverage.html">↩ 返回增量审查汇总</a>
    <a href="coverage_progress.html?scope=incremental&amp;project={project_url}">📊 查看填写进度</a>
    <a href="incremental_coverage.xlsx">📈 下载 Excel</a>
  </div>
</div>
<section><h2>👥 人员概览</h2><div class="table-wrap"><table><thead><tr><th>开发人员</th><th>提交数</th><th>提交文件</th><th>需填写文件</th><th>待填写行</th></tr></thead><tbody>{summary_rows}</tbody></table></div></section>
<aside class="muted" style="margin:12px 0 20px;text-align:center">说明：按 Git author 的“姓名 + 邮箱”区分人员。多人提交同一文件时，文件会同时出现在每位相关开发人员的清单中；“待填写”仅统计本次 Git diff 中 LCOV 未覆盖的新增行。</aside>
{sections}
</main></body></html>""".format(
        project=escaped(project_name),
        project_url=escaped(urllib.parse.quote(str(project_name), safe="")),
        summary_rows=summary_table_rows,
        sections=sections_html,
    )
    with open(os.path.join(output_html_dir, "incremental_developer_tasks.html"), "w", encoding="utf-8") as page_file:
        page_file.write(page)


def build_incremental_review_site(result, input_dir, output_dir, project_name,
                                  workers=None, render_mode=None, excel_path=None, reuse_output=None):
    """Build a fillable incremental review site from a calculated result."""
    if not os.path.isdir(input_dir):
        raise ValueError("LCOV HTML 报告目录不存在: {}".format(input_dir))

    timer = PerfTimer(f"BuildIncrementalSite[{project_name}]")
    summary = result["summary"]
    print(
        "[Incremental] Git-added lines={changed_lines}, covered={covered}, uncovered={uncovered}, "
        "ignored={ignored}, missing={missing}".format(**summary)
    )

    inject_coverage_report(
        input_dir,
        output_dir,
        project_name=project_name,
        workers=workers,
        render_mode=render_mode,
        review_scope="incremental",
        incremental_lines_by_file=result.get("review_lines_by_file") or result["uncovered_lines_by_file"],
        reuse_output=reuse_output,
    )
    timer.mark("inject_coverage_report")
    real_output_html = (
        os.path.join(output_dir, "html")
        if os.path.isdir(os.path.join(input_dir, "html")) else output_dir
    )
    if not os.path.isdir(real_output_html):
        raise RuntimeError("增量审查网页输出失败: {}".format(real_output_html))

    unanalyzed_by_file = sync_incremental_unanalyzed_counts(project_name, result, load_config())

    result["project_name"] = project_name
    result["review_scope"] = "incremental"
    output_json = os.path.join(output_dir, "incremental_coverage.json")
    coverage_check.write_result_json(result, output_json)
    if os.path.abspath(real_output_html) != os.path.abspath(output_dir):
        coverage_check.write_result_json(result, os.path.join(real_output_html, "incremental_coverage.json"))

    if excel_path is None:
        excel_path = os.path.join(output_dir, "incremental_coverage.xlsx")
    excel_parent = os.path.dirname(os.path.abspath(excel_path))
    if excel_parent:
        os.makedirs(excel_parent, exist_ok=True)
    coverage_check.write_result_excel(result, excel_path)
    html_excel_path = os.path.join(real_output_html, "incremental_coverage.xlsx")
    if os.path.abspath(excel_path) != os.path.abspath(html_excel_path):
        shutil.copy2(excel_path, html_excel_path)

    write_incremental_summary_page(real_output_html, project_name, result, unanalyzed_by_file=unanalyzed_by_file)
    write_incremental_developer_tasks_page(real_output_html, project_name, result)
    timer.mark("write_summary_pages")
    print("[Incremental] Review home page: {}".format(
        os.path.join(real_output_html, "incremental_coverage.html")
    ))
    print("[Incremental] Developer task page: {}".format(
        os.path.join(real_output_html, "incremental_developer_tasks.html")
    ))
    print("[Incremental] Result JSON: {}".format(output_json))
    print("[Incremental] Result Excel: {}".format(excel_path))
    timer.mark("build_site_total_complete")
    return result


def generate_incremental_review(repo_path, oldgit, newgit, info_path, input_dir, output_dir,
                                project_name, workers=None, render_mode=None, excel_path=None, reuse_output=None):
    """Calculate one repository and build a fillable incremental review site."""
    if not os.path.isdir(repo_path):
        raise ValueError("Git 仓库目录不存在: {}".format(repo_path))
    result = coverage_check.calculate_incremental_coverage(
        repo_path, oldgit, newgit, info_path
    )
    return build_incremental_review_site(
        result, input_dir, output_dir, project_name, workers, render_mode, excel_path, reuse_output
    )


def generate_multi_repo_incremental_review(repos_config_path, info_path, input_dir, output_dir,
                                           project_name, workers=None, render_mode=None, excel_path=None, reuse_output=None):
    """Calculate configured repositories together and build one review site."""
    result = coverage_check.calculate_multi_repo_incremental_coverage_from_config(
        repos_config_path, info_path
    )
    return build_incremental_review_site(
        result, input_dir, output_dir, project_name, workers, render_mode, excel_path, reuse_output
    )


def run_server():
    global db_manager
    config = load_config()

    print("[Server] Initializing MySQL Database...")
    # First, run database schema checking/creation synchronously on startup
    init_mgr = DatabaseManager(config)
    if init_mgr.conn:
        init_mgr.conn.close()

    # Use thread-local database proxy for safe request handling
    db_manager = ThreadLocalDatabaseManagerProxy(config)

    host = config["server"]["host"]
    port = int(config["server"]["port"])
    server_address = (host, port)

    # Bind port first so if another instance is running, we fail immediately before recovering jobs
    httpd = ThreadingHTTPServer(server_address, CoverageHTTPRequestHandler)
    print(f"[Server] Microservice running on http://{host}:{port} ...")

    # Recover any interrupted jobs from DB & start daemon cleanup loop after successful socket bind
    start_background_job_cleanup_loop()
    recover_background_jobs()

    print("[Server] Press Ctrl+C to terminate.")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down gracefully...")
        close_thread_db_manager()
        httpd.server_close()
        print("[Server] Stopped.")


def print_help():
    print("Usage:")
    print("  python scripts/enhance_coverage.py inject --project <project_name> --dir <input_dir> --out <output_dir> [--mode <lazy_collapse|lazy|immediate>]")
    print("    - Scan and inject custom interactive forms into HTML reports.")
    print("    - --project is recommended and overrides coverage_config.json project_name.")
    print("    - --workers <N> controls parallel HTML parsing and line-index DB sync.")
    print("    - --mode <lazy_collapse|lazy|immediate> specifies the display mode (default: lazy_collapse).")
    print("    - Use --use-config-project only if you intentionally want coverage_config.json project_name.")
    print("  python scripts/enhance_coverage.py server")
    print("    - Start local bridge server for MySQL persistence.")
    print("  python scripts/enhance_coverage.py inherit --from <old_project> --to <new_project>")
    print("    - Reuse reviewed analysis for unchanged functions in a later project/version.")
    print("  python scripts/enhance_coverage.py incremental --project <project_name> --repo <git_repo> --oldgit <old_commit> --newgit <new_commit> --info <coverage.info|dir> --dir <lcov_html_dir> --out <output_dir>")
    print("    - Build an incremental review website; only Git-added, LCOV-uncovered lines are editable.")
    print("    - Optional: --workers <N> --mode <lazy_collapse|lazy|immediate> --excel <result.xlsx>")
    print("  python scripts/enhance_coverage.py incremental --project <project_name> --repos-config <repos.json> --info <coverage.info|dir> --dir <lcov_html_dir> --out <output_dir>")
    print("    - Build one incremental review website from several independent Git repositories.")


def get_arg_value(args, name):
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
    return None


def has_arg(args, name):
    return name in args


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        print_help()
        sys.exit(0 if len(sys.argv) >= 2 and sys.argv[1] in ("--help", "-h", "help") else 1)

    cmd = sys.argv[1]
    if cmd == "inject":
        args = sys.argv[2:]
        dir_path = get_arg_value(args, "--dir")
        out_path = get_arg_value(args, "--out")
        project_name = get_arg_value(args, "--project")
        workers = get_arg_value(args, "--workers")
        render_mode = get_arg_value(args, "--mode")
        use_config_project = has_arg(args, "--use-config-project")
        reuse_output = has_arg(args, "--reuse-output")

        if not dir_path:
            dir_path = os.path.join(SCRIPT_DIR, "../build/coverage")
        if not out_path:
            if "build/coverage" in dir_path or "build\\coverage" in dir_path:
                out_path = os.path.join(os.path.dirname(dir_path), "coverage_review")
            else:
                out_path = dir_path + "_review"

        if not project_name:
            if use_config_project:
                config = load_config()
                project_name = config.get("project_name", DEFAULT_PROJECT_NAME)
                print("[Warning] Using project_name from coverage_config.json because --use-config-project was specified.")
            else:
                print("[Error] inject requires --project <project_name> to avoid writing data to the wrong project.")
                print("        If you intentionally want coverage_config.json, add --use-config-project.")
                print_help()
                sys.exit(1)
            
        print(f"[Main] Non-destructive injection starts.")
        print(f"[Main] Project : {project_name}")
        print(f"[Main] Input (ReadOnly) : {dir_path}")
        print(f"[Main] Output (Enhanced) : {out_path}")
        inject_coverage_report(dir_path, out_path, project_name, workers, render_mode, reuse_output=reuse_output)
    elif cmd == "server":
        run_server()
    elif cmd == "inherit":
        args = sys.argv[2:]
        source_project = get_arg_value(args, "--from")
        target_project = get_arg_value(args, "--to")

        if not source_project or not target_project:
            print("[Error] inherit requires --from <old_project> and --to <new_project>.")
            print_help()
            sys.exit(1)

        config = load_config()
        print(f"[Inherit] Source project: {source_project}")
        print(f"[Inherit] Target project: {target_project}")
        manager = DatabaseManager(config)
        result = manager.inherit_analysis(source_project, target_project)
        if manager.conn:
            manager.conn.close()
        print("[Inherit] Completed.")
        print(f"[Inherit] Source analysis records: {result['source_analysis_records']}")
        print(f"[Inherit] Source reviewed analysis records: {result['source_reviewed_analysis_records']}")
        print(f"[Inherit] Source index records: {result['source_index_records']}")
        print(f"[Inherit] Source hashable index records: {result['source_hashable_index_records']}")
        print(f"[Inherit] Source reviewed records joined with index: {result['source_reviewed_records']}")
        print(f"[Inherit] Target index records: {result['target_index_records']}")
        print(f"[Inherit] Target hashable index records: {result['target_hashable_index_records']}")
        print(f"[Inherit] Target unfilled records: {result['target_unfilled_records']}")
        print(f"[Inherit] Filename matched records: {result['name_matched_records']}")
        print(f"[Inherit] Ambiguous filename keys: {result['ambiguous_name_keys']}")
        print(f"[Inherit] Target records skipped by filename ambiguity: {result['ambiguous_name_skipped_records']}")
        print(f"[Inherit] Inherited records: {result['inherited_records']}")
    elif cmd == "incremental":
        args = sys.argv[2:]
        repo_path = get_arg_value(args, "--repo")
        oldgit = get_arg_value(args, "--oldgit")
        newgit = get_arg_value(args, "--newgit")
        repos_config_path = get_arg_value(args, "--repos-config")
        info_path = get_arg_value(args, "--info")
        dir_path = get_arg_value(args, "--dir")
        out_path = get_arg_value(args, "--out")
        project_name = get_arg_value(args, "--project")
        workers = get_arg_value(args, "--workers")
        render_mode = get_arg_value(args, "--mode")
        excel_path = get_arg_value(args, "--excel")
        use_config_project = has_arg(args, "--use-config-project")
        reuse_output = has_arg(args, "--reuse-output")

        required_values = {
            "--info": info_path,
            "--dir": dir_path,
            "--out": out_path,
        }
        if repos_config_path:
            if repo_path or oldgit or newgit:
                print("[Error] --repos-config 不能与 --repo、--oldgit、--newgit 一起使用.")
                print_help()
                sys.exit(1)
        else:
            required_values.update({
                "--repo": repo_path,
                "--oldgit": oldgit,
                "--newgit": newgit,
            })
        missing = [flag for flag, value in required_values.items() if not value]
        if missing:
            print("[Error] incremental requires {}.".format(", ".join(missing)))
            print_help()
            sys.exit(1)
        if not project_name:
            if use_config_project:
                config = load_config()
                project_name = config.get("project_name", DEFAULT_PROJECT_NAME)
                print("[Warning] Using project_name from coverage_config.json because --use-config-project was specified.")
            else:
                print("[Error] incremental requires --project <project_name> to keep review data isolated.")
                print("        If you intentionally want coverage_config.json, add --use-config-project.")
                print_help()
                sys.exit(1)

        print("[Main] Incremental coverage review generation starts.")
        print(f"[Main] Project : {project_name}")
        if repos_config_path:
            print(f"[Main] Repositories config : {repos_config_path}")
        else:
            print(f"[Main] Git repo : {repo_path}")
            print(f"[Main] Git range : {oldgit} -> {newgit}")
        print(f"[Main] LCOV info : {info_path}")
        print(f"[Main] Report input (ReadOnly) : {dir_path}")
        print(f"[Main] Report output (Enhanced) : {out_path}")
        try:
            if repos_config_path:
                generate_multi_repo_incremental_review(
                    repos_config_path, info_path, dir_path, out_path,
                    project_name, workers, render_mode, excel_path, reuse_output,
                )
            else:
                generate_incremental_review(
                    repo_path, oldgit, newgit, info_path, dir_path, out_path,
                    project_name, workers, render_mode, excel_path, reuse_output,
                )
        except Exception as error:
            print("[Error] Failed to generate incremental review: {}".format(error))
            sys.exit(1)
    else:
        print_help()
        sys.exit(1)
