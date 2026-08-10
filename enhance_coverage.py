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
from decimal import Decimal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import coverage_check

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
        db_module = __import__(module_name)
        break
    except ImportError:
        continue

# 脚本根目录和配置文件位置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "coverage_config.json")
JS_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_enhance.js")
CSS_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_enhance.css")
PROGRESS_PAGE_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_progress.html")
ASSET_VERSION = "incremental-review-20260810"
DEFAULT_PROJECT_NAME = "Gemini-NOS"
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
    main{padding:18px 24px 32px}.cards{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin-bottom:16px}
    .card,.section{background:#fff;border:1px solid #d8e0ea;border-radius:6px}.card{padding:12px}.label{color:#64748b;font-size:12px}.value{font-size:24px;font-weight:800;margin-top:4px}
    .section{margin-top:14px;overflow:hidden}.section h2{margin:0;padding:10px 12px;font-size:15px;background:#eef3f8;border-bottom:1px solid #d8e0ea}.table-wrap{overflow:auto}
    table{width:100%;border-collapse:collapse;min-width:920px}th,td{border-bottom:1px solid #e7edf4;padding:7px 8px;text-align:left;vertical-align:top}th{background:#f8fafc;position:sticky;top:0}td.path{word-break:break-all}
    .bar{display:inline-block;width:90px;height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden;margin-left:8px}.bar span{display:block;height:100%;background:#1f9d55}.status{margin-top:10px;color:#64748b}.error{color:#b91c1c}
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
    </div>
    <section class="section"><h2>目录进度</h2><div class="table-wrap"><table id="dirTable"></table></div></section>
    <section class="section"><h2>文件进度</h2><div class="table-wrap"><table id="fileTable"></table></div></section>
  </main>
  <script>
    const DEFAULT_REVIEW_SCOPE = 'full'; const params=new URLSearchParams(window.location.search),projectInput=document.getElementById('projectInput'),statusEl=document.getElementById('status'),csvLink=document.getElementById('csvLink'),excelLink=document.getElementById('excelLink');let resolvedApiBase='';
    projectInput.value=params.get('project')||'';
    const asNumber=v=>Number.isFinite(Number(v))?Number(v):0,fmtRate=v=>Number.isFinite(Number(v))?`${Number(v).toFixed(1)}%`:'0.0%',bar=r=>`<span class="bar"><span style="width:${Math.max(0,Math.min(100,asNumber(r)))}%"></span></span>`;
    function metric(id,value){document.getElementById(id).innerText=value}
    function rows(items,pathKey){return !items||!items.length?'<tr><td>暂无数据</td></tr>':items.map(row=>`<tr><td class="path">${row[pathKey]||'(root)'}</td><td>${asNumber(row.total_uncovered)}</td><td>${asNumber(row.filled_total)}</td><td>${asNumber(row.unfilled_total)}</td><td>${asNumber(row.confirmed_total)}</td><td>${asNumber(row.coverable_total)}</td><td>${asNumber(row.uncoverable_total)}</td><td>${asNumber(row.redundant_total)}</td><td>${fmtRate(row.fill_rate)} ${bar(row.fill_rate)}</td><td>${fmtRate(row.confirmed_rate)} ${bar(row.confirmed_rate)}</td></tr>`).join('')}
    function renderTable(id,items,pathKey){document.getElementById(id).innerHTML=`<thead><tr><th>路径</th><th>未覆盖</th><th>已填</th><th>未填</th><th>已确认</th><th>可覆盖</th><th>无法覆盖</th><th>冗余</th><th>填写率</th><th>确认率</th></tr></thead><tbody>${rows(items,pathKey)}</tbody>`}
    function normalizeApiBase(value){return String(value||'').replace(/\/+$/,'')}
    function unique(values){return Array.from(new Set(values.filter(Boolean)))}
    function apiBaseCandidates(){const explicit=params.get('api'),origin=window.location.origin&&window.location.origin!=='null'?window.location.origin:'',c=[];if(explicit)c.push(normalizeApiBase(explicit));if(origin){c.push(`${origin}/api/coverage`);if(window.location.pathname.startsWith('/coverage/'))c.push(`${origin}/coverage/api/coverage`);if(window.location.port!=='9528')c.push(`${window.location.protocol}//${window.location.hostname}:9528/api/coverage`)}c.push('http://127.0.0.1:9528/api/coverage');c.push('/api/coverage');return unique(c.map(normalizeApiBase))}
    function fetchJsonWithTimeout(url,timeoutMs){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);return fetch(url,{signal:controller.signal}).finally(()=>clearTimeout(timer)).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()})}
    function updateLinks(project,apiBase){const encoded=encodeURIComponent(project),base=normalizeApiBase(apiBase||resolvedApiBase||apiBaseCandidates()[0]);csvLink.href=`${base}/export?type=full_progress_summary&project=${encoded}`;excelLink.href=`${base}/export?type=review_excel_by_dir&project=${encoded}`}
    async function loadProgress(){const project=projectInput.value.trim();if(!project){statusEl.innerHTML='<span class="error">请输入项目名。</span>';return}const loadBtn=document.getElementById('loadBtn');loadBtn.disabled=true;loadBtn.innerText='正在加载...';const encodedProject=encodeURIComponent(project),candidates=apiBaseCandidates();updateLinks(project,candidates[0]);statusEl.innerText=`正在连接接口：${candidates[0]}`;let lastError=null;try{for(const apiBase of candidates){try{statusEl.innerText=`正在加载：${apiBase}`;const payload=await fetchJsonWithTimeout(`${apiBase}/progress?project=${encodedProject}`,15000);if(!payload||payload.status!=='success')throw new Error(payload&&payload.message?payload.message:'加载失败');resolvedApiBase=apiBase;updateLinks(project,apiBase);const data=payload.data||{},projectRow=data.project&&data.project[0]||{};metric('totalUncovered',asNumber(projectRow.total_uncovered));metric('filledTotal',asNumber(projectRow.filled_total));metric('fillRate',fmtRate(projectRow.fill_rate));metric('confirmedRate',fmtRate(projectRow.confirmed_rate));renderTable('dirTable',data.dirs||[],'dir_path');renderTable('fileTable',data.files||[],'file_path');statusEl.innerText=`已加载项目：${project}，接口：${apiBase}`;const url=new URL(window.location.href);url.searchParams.set('project',project);if(params.get('api'))url.searchParams.set('api',apiBase);window.history.replaceState(null,'',url.toString());return}catch(e){lastError=e}}statusEl.innerHTML=`<span class="error">加载失败：${lastError?lastError.message:'无法连接接口'}。已尝试：${candidates.join(' , ')}</span>`}finally{loadBtn.disabled=false;loadBtn.innerText='查看进度'}}
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

    def add_marker(match):
        line_number = int(match.group(2))
        return match.group(1) + (' data-coverage-review="incremental"' if line_number in selected_line_numbers else '') + match.group(3)

    modern_line_pattern = re.compile(r'(<span\b[^>]*\bid=["\']L(\d+)["\'][^>]*)(>)', re.I)
    legacy_line_pattern = re.compile(
        r'(<span\b[^>]*\bclass=["\'][^"\']*\blineNum\b[^"\']*["\'][^>]*>\s*(\d+)\s*</span>\s*<span\b[^>]*)(>)',
        re.I,
    )
    content = modern_line_pattern.sub(add_marker, content)
    return legacy_line_pattern.sub(add_marker, content)


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
        "project_name": DEFAULT_PROJECT_NAME
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warning] Failed to load config file: {e}. Using defaults.")
    return default_config


def write_configured_enhance_js(output_path, project_name, render_mode, review_scope="full"):
    """Copy the frontend script and inject project, render mode and review scope."""
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
            scope_literal = json.dumps(str(review_scope), ensure_ascii=False)
            progress_content, replacement_count = re.subn(
                r"const\s+DEFAULT_REVIEW_SCOPE\s*=\s*(['\"]).*?\1\s*;",
                f"const DEFAULT_REVIEW_SCOPE = {scope_literal};",
                progress_content,
                count=1,
            )
            if replacement_count != 1:
                raise RuntimeError("Failed to inject DEFAULT_REVIEW_SCOPE into coverage_progress.html")
            with open(target, "w", encoding="utf-8") as target_file:
                target_file.write(progress_content)
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


class DatabaseManager:
    """MySQL 数据库管理层，处理连接、建库、建表以及存取操作"""
    def __init__(self, config, exit_on_error=True, init_schema=True):
        self.config = config["mysql"]
        self.exit_on_error = exit_on_error
        self.conn = None
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

    def get_connection(self, select_db=True):
        """建立并返回 MySQL 连接"""
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
            
        conn = db_module.connect(**params)
        if select_db:
            conn.select_db(self.config["database"])
        return conn

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
                SELECT line_number, reviewer, status, coverage_method, uncovered_reason
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
                        "coverage_method": row.get("coverage_method"),
                        "uncovered_reason": row.get("uncovered_reason")
                    })
                else:
                    records.append({
                        "line_number": row[0],
                        "reviewer": row[1] if row[1] is not None else "",
                        "status": row[2],
                        "coverage_method": row[3],
                        "uncovered_reason": row[4]
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
                (project_name, file_path, file_path_hash, source_file_name, line_number, reviewer, status, coverage_method, uncovered_reason)
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
            cursor.execute(sql, (project_name, file_path, file_path_hash, source_file_name, int(line_number), reviewer, status, method, reason))
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[DB Error] Save failed: {e}")
            return False

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
                "SELECT COUNT(*) FROM coverage_analysis WHERE project_name = %s AND status <> %s",
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
            cursor.close()
            return rows
        except Exception as e:
            print(f"[DB Error] Review Excel export query failed: {e}")
            raise

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
                           SUM(CASE WHEN status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS unconfirmed_total,
                           ROUND(SUM(CASE WHEN status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS coverable_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS uncoverable_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS redundant_rate,
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
                           SUM(CASE WHEN status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS unconfirmed_total,
                           ROUND(SUM(CASE WHEN status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS coverable_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS uncoverable_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS redundant_rate,
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
                    SELECT i.project_name, i.file_path,
                           COUNT(*) AS total_uncovered,
                           SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) AS filled_total,
                           SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END) AS unfilled_total,
                           SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           ROUND(SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) * 100.0 / COUNT(*), 2) AS fill_rate,
                           ROUND(SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           MAX(a.updated_at) AS last_updated
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON {source_file_join_condition("i", "a")}
                    {full_where_sql}
                    GROUP BY i.project_name, i.file_path
                    ORDER BY i.project_name, i.file_path
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
                           SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           ROUND(SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) * 100.0 / COUNT(*), 2) AS fill_rate,
                           ROUND(SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
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
                           SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           ROUND(SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) * 100.0 / COUNT(*), 2) AS fill_rate,
                           ROUND(SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        if parsed_url.path == "/api/coverage/export":
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
                    project_headers, project_rows = db_manager.export_report("full_project_summary", project_name)
                    dir_headers, dir_rows = db_manager.export_report("full_dir_summary", project_name)
                    file_headers, file_rows = db_manager.export_report("full_file_summary", project_name)
                    progress_sections = [
                        ("项目进度", project_headers, project_rows),
                        ("目录进度", dir_headers, dir_rows),
                        ("文件进度", file_headers, file_rows),
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
        elif parsed_url.path == "/api/coverage/progress":
            project_name = query_params.get("project", [""])[0]
            if not project_name:
                self.send_error_response(400, "Missing 'project' parameter")
                return

            try:
                project_headers, project_rows = db_manager.export_report("full_project_summary", project_name)
                dir_headers, dir_rows = db_manager.export_report("full_dir_summary", project_name)
                file_headers, file_rows = db_manager.export_report("full_file_summary", project_name)
                data = {
                    "project": [dict(zip(project_headers, row)) for row in project_rows],
                    "dirs": [dict(zip(dir_headers, row)) for row in dir_rows],
                    "files": [dict(zip(file_headers, row)) for row in file_rows],
                }
            except Exception:
                self.send_error_response(500, "Failed to query progress")
                return

            self.send_json_response(200, {"status": "success", "data": data})
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
                self.send_json_response(200, {"status": "success", "message": "Record saved successfully"})
            else:
                self.send_error_response(500, "Failed to save record to database")
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
                                 review_scope="full", incremental_lines_by_file=None):
    depth = len(rel_path.split(os.sep)) - 1
    prefix = "../" * depth

    css_tag = f'<link rel="stylesheet" type="text/css" href="{prefix}coverage_enhance.css?v={ASSET_VERSION}">\n'
    js_tag = f'<script type="text/javascript" src="{prefix}coverage_enhance.js?v={ASSET_VERSION}"></script>\n'
    inject_code = f"{css_tag}{js_tag}</head>"

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    report_file_path = extract_report_file_path(content, rel_path)
    report_file_hash = calc_file_path_hash(report_file_path)
    review_line_numbers = None
    if review_scope == "incremental":
        review_line_numbers = get_incremental_lines_for_report(
            report_file_path, incremental_lines_by_file or {}
        )
        content = mark_incremental_review_lines(content, review_line_numbers)
    file_line_index_records = extract_line_index_records(
        content, rel_path, project_name, review_line_numbers
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
    if "coverage_enhance.js" in content:
        new_content = re.sub(r'(href="[^"]*coverage_enhance\.css)(?:\?v=[^"]*)?(")', rf'\1?v={ASSET_VERSION}\2', content)
        new_content = re.sub(r'(src="[^"]*coverage_enhance\.js)(?:\?v=[^"]*)?(")', rf'\1?v={ASSET_VERSION}\2', new_content)
        if new_content != content:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            updated = 1
    elif "</head>" in content:
        new_content = content.replace("</head>", inject_code, 1)
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
                           review_scope="full", incremental_lines_by_file=None):
    """
    非破坏性注入覆盖率报告：
    1. 若 output_dir 与 input_dir 不同，则先自动将整个 input_dir 复制至 output_dir (清除已有的 output_dir)
    2. 在 output_dir 中注入样式表、增强脚本并复制静态资源。
    3. ``review_scope=incremental`` 时，仅为 ``incremental_lines_by_file`` 中的
       未覆盖行注入可填写控件和数据库索引。
    """
    if not os.path.exists(input_dir):
        print(f"[Error] Input directory '{input_dir}' does not exist.")
        return

    real_input_html = os.path.join(input_dir, "html") if os.path.exists(os.path.join(input_dir, "html")) else input_dir
    real_output_html = os.path.join(output_dir, "html") if os.path.exists(os.path.join(input_dir, "html")) else output_dir

    if input_dir != output_dir:
        print(f"[Injector] Copying input directory '{input_dir}' to '{output_dir}' (protecting original files)...")
        try:
            if os.path.exists(output_dir):
                shutil.rmtree(output_dir)
            shutil.copytree(input_dir, output_dir)
            print("[Injector] Copy complete.")
        except Exception as e:
            print(f"[Error] Failed to copy directory: {e}")
            return
    else:
        print("[Warning] Input and output directories are the same. Original files will be modified.")

    if not os.path.exists(real_output_html):
        print(f"[Error] Real output html directory '{real_output_html}' does not exist.")
        return

    config = load_config()
    if project_name is None:
        project_name = config.get("project_name", DEFAULT_PROJECT_NAME)

    if render_mode is None:
        render_mode = config.get("render_mode", "lazy")
    if render_mode not in ("lazy", "immediate"):
        render_mode = "lazy"
    if review_scope not in ("full", "incremental"):
        raise ValueError("review_scope must be 'full' or 'incremental'")

    write_configured_enhance_js(
        os.path.join(real_output_html, "coverage_enhance.js"), project_name, render_mode, review_scope
    )
    shutil.copy2(CSS_SOURCE_PATH, os.path.join(real_output_html, "coverage_enhance.css"))
    write_progress_page_targets(output_dir, real_output_html, review_scope)
    print(f"[Injector] Copied static resources to: {real_output_html}")
    print(f"[Injector] Frontend project name: {project_name}")
    print(f"[Injector] Frontend render mode: {render_mode}")
    print(f"[Injector] Review scope: {review_scope}")
    index_manager = None
    indexed_records = 0
    indexed_files = 0
    active_file_hashes = set()
    try:
        index_manager = DatabaseManager(config, exit_on_error=False)
    except Exception as e:
        print(f"[Warning] Failed to initialize database for full coverage line index: {e}")
        print("[Warning] Inject will continue, but full export requires a successful line-index sync.")

    print(f"[Injector] Scanning report files under: {real_output_html}")
    gcov_files = []
    for root, dirs, files in os.walk(real_output_html):
        dirs.sort()
        for file in sorted(files):
            if file.endswith(".gcov.html"):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, real_output_html)
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
                file_path,
                rel_path,
                project_name,
                config,
                index_manager is not None,
                review_scope,
                incremental_lines_by_file,
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
            print(
                f"[Injector] Progress {file_index}/{total_files} ({percent:.1f}%) "
                f"elapsed={elapsed:.1f}s eta={remaining:.1f}s "
                f"uncovered={file_record_count} index={index_status} "
                f"total_indexed={indexed_records} file={rel_path}",
                flush=True
            )

    print(f"[Injector] Non-destructively enhanced {injected_count} new html report file(s), updated {updated_count} existing enhanced file(s) in: {output_dir}")
    if index_manager:
        index_manager = DatabaseManager(config, exit_on_error=False, init_schema=False)
        index_manager.prune_line_index_project_files(project_name, active_file_hashes)
        if index_manager.conn:
            index_manager.conn.close()
        print(f"[Injector] Synced full coverage line index: {indexed_records} record(s) across {indexed_files} file(s).")
    else:
        print("[Injector] Full coverage line index was not synced because database initialization failed.")


def find_report_page_links(report_html_dir):
    """Build a source-path -> report-page mapping from LCOV page titles."""
    report_pages = {}
    for root, dirs, files in os.walk(report_html_dir):
        dirs.sort()
        for filename in sorted(files):
            if not filename.endswith(".gcov.html"):
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


def write_incremental_summary_page(output_html_dir, project_name, result):
    """Create an auditable landing page for the generated incremental review site."""
    summary = result["summary"]
    report_pages = find_report_page_links(output_html_dir)
    details_by_file = {}
    for item in result["details"]:
        details_by_file.setdefault(item["file_path"], []).append(item)

    def escaped(value):
        return html.escape(str(value), quote=True)

    rows = []
    for source_path in sorted(details_by_file):
        items = details_by_file[source_path]
        counts = {status: 0 for status in (
            coverage_check.STATUS_COVERED,
            coverage_check.STATUS_UNCOVERED,
            coverage_check.STATUS_IGNORED,
            coverage_check.STATUS_MISSING,
        )}
        for item in items:
            counts[item["status"]] += 1
        page_link = get_report_page_link(source_path, report_pages)
        source_cell = escaped(source_path)
        if page_link:
            source_cell = '<a href="{}">{}</a>'.format(
                escaped(urllib.parse.quote(page_link, safe="/%#?=&-_.~")), source_cell
            )
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                source_cell,
                len(items),
                counts[coverage_check.STATUS_COVERED],
                counts[coverage_check.STATUS_UNCOVERED],
                counts[coverage_check.STATUS_IGNORED],
                counts[coverage_check.STATUS_MISSING],
            )
        )

    rate = summary["coverage_rate"]
    rate_text = "N/A" if rate is None else "{:.2f}%".format(rate)
    table_rows = "".join(rows) or '<tr><td colspan="6">本次 Git diff 没有新增代码行。</td></tr>'
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>增量覆盖率审查</title><style>
body{{margin:0;background:#f5f7fb;color:#172033;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:28px 22px 42px}}h1{{margin:0 0 6px}}.muted{{color:#64748b}}.links{{margin:16px 0}}a{{color:#1f5fbf}}.cards{{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:12px;margin:20px 0}}.card,section{{background:#fff;border:1px solid #d8e0ea;border-radius:6px}}.card{{padding:12px}}.label{{font-size:12px;color:#64748b}}.value{{font-size:24px;font-weight:800;margin-top:4px}}section{{overflow:auto}}h2{{margin:0;padding:12px 14px;font-size:16px;border-bottom:1px solid #d8e0ea}}table{{width:100%;border-collapse:collapse;min-width:720px}}th,td{{padding:9px 10px;border-bottom:1px solid #e7edf4;text-align:left}}th{{background:#f8fafc}}.warning{{color:#b45309}}@media(max-width:820px){{.cards{{grid-template-columns:repeat(2,minmax(120px,1fr))}}}}
</style></head><body><main>
<h1>增量覆盖率审查</h1>
<div class="muted">项目：{project}；Git 范围：<code>{oldgit}</code> → <code>{newgit}</code>；生成时间：{generated_at}</div>
<div class="links"><a href="coverage_progress.html?scope=incremental&amp;project={project_url}">查看填写进度</a>　<a href="incremental_coverage.json">下载计算结果 JSON</a>　<a href="incremental_coverage.xlsx">下载计算结果 Excel</a></div>
<div class="cards"><div class="card"><div class="label">新增行</div><div class="value">{changed}</div></div><div class="card"><div class="label">已覆盖</div><div class="value">{covered}</div></div><div class="card"><div class="label">增量未覆盖（可填写）</div><div class="value">{uncovered}</div></div><div class="card"><div class="label">有效增量覆盖率</div><div class="value">{rate}</div></div><div class="card"><div class="label">覆盖信息缺失</div><div class="value warning">{missing}</div></div></div>
<section><h2>文件明细（点击文件可打开可填写的源码页）</h2><table><thead><tr><th>文件</th><th>新增行</th><th>已覆盖</th><th>未覆盖</th><th>无需覆盖</th><th>覆盖信息缺失</th></tr></thead><tbody>{table_rows}</tbody></table></section>
</main></body></html>""".format(
        project=escaped(project_name),
        project_url=escaped(urllib.parse.quote(str(project_name), safe="")),
        oldgit=escaped(result["oldgit"]),
        newgit=escaped(result["newgit"]),
        generated_at=escaped(result["generated_at"]),
        changed=summary["changed_lines"],
        covered=summary["covered"],
        uncovered=summary["uncovered"],
        rate=rate_text,
        missing=summary["missing"],
        table_rows=table_rows,
    )
    with open(os.path.join(output_html_dir, "incremental_coverage.html"), "w", encoding="utf-8") as page_file:
        page_file.write(page)


def generate_incremental_review(repo_path, oldgit, newgit, info_path, input_dir, output_dir,
                                project_name, workers=None, render_mode=None, excel_path=None):
    """Calculate incremental coverage and build a fillable incremental review site."""
    if not os.path.isdir(repo_path):
        raise ValueError("Git 仓库目录不存在: {}".format(repo_path))
    if not os.path.isdir(input_dir):
        raise ValueError("LCOV HTML 报告目录不存在: {}".format(input_dir))

    result = coverage_check.calculate_incremental_coverage(
        repo_path, oldgit, newgit, info_path
    )
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
        incremental_lines_by_file=result["uncovered_lines_by_file"],
    )
    real_output_html = (
        os.path.join(output_dir, "html")
        if os.path.isdir(os.path.join(input_dir, "html")) else output_dir
    )
    if not os.path.isdir(real_output_html):
        raise RuntimeError("增量审查网页输出失败: {}".format(real_output_html))

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

    write_incremental_summary_page(real_output_html, project_name, result)
    print("[Incremental] Review home page: {}".format(
        os.path.join(real_output_html, "incremental_coverage.html")
    ))
    print("[Incremental] Result JSON: {}".format(output_json))
    print("[Incremental] Result Excel: {}".format(excel_path))
    return result


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

    httpd = ThreadingHTTPServer(server_address, CoverageHTTPRequestHandler)
    print(f"[Server] Microservice running on http://{host}:{port} ...")
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
    print("  python scripts/enhance_coverage.py inject --project <project_name> --dir <input_dir> --out <output_dir> [--mode <lazy|immediate>]")
    print("    - Scan and inject custom interactive forms into HTML reports.")
    print("    - --project is recommended and overrides coverage_config.json project_name.")
    print("    - --workers <N> controls parallel HTML parsing and line-index DB sync.")
    print("    - --mode <lazy|immediate> specifies the display mode (placeholder vs immediate controls).")
    print("    - Use --use-config-project only if you intentionally want coverage_config.json project_name.")
    print("  python scripts/enhance_coverage.py server")
    print("    - Start local bridge server for MySQL persistence.")
    print("  python scripts/enhance_coverage.py inherit --from <old_project> --to <new_project>")
    print("    - Reuse reviewed analysis for unchanged functions in a later project/version.")
    print("  python scripts/enhance_coverage.py incremental --project <project_name> --repo <git_repo> --oldgit <old_commit> --newgit <new_commit> --info <coverage.info|dir> --dir <lcov_html_dir> --out <output_dir>")
    print("    - Build an incremental review website; only Git-added, LCOV-uncovered lines are editable.")
    print("    - Optional: --workers <N> --mode <lazy|immediate> --excel <result.xlsx>")


def get_arg_value(args, name):
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
    return None


def has_arg(args, name):
    return name in args


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "inject":
        args = sys.argv[2:]
        dir_path = get_arg_value(args, "--dir")
        out_path = get_arg_value(args, "--out")
        project_name = get_arg_value(args, "--project")
        workers = get_arg_value(args, "--workers")
        render_mode = get_arg_value(args, "--mode")
        use_config_project = has_arg(args, "--use-config-project")

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
        inject_coverage_report(dir_path, out_path, project_name, workers, render_mode)
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
        info_path = get_arg_value(args, "--info")
        dir_path = get_arg_value(args, "--dir")
        out_path = get_arg_value(args, "--out")
        project_name = get_arg_value(args, "--project")
        workers = get_arg_value(args, "--workers")
        render_mode = get_arg_value(args, "--mode")
        excel_path = get_arg_value(args, "--excel")
        use_config_project = has_arg(args, "--use-config-project")

        required_values = {
            "--repo": repo_path,
            "--oldgit": oldgit,
            "--newgit": newgit,
            "--info": info_path,
            "--dir": dir_path,
            "--out": out_path,
        }
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
        print(f"[Main] Git repo : {repo_path}")
        print(f"[Main] Git range : {oldgit} -> {newgit}")
        print(f"[Main] LCOV info : {info_path}")
        print(f"[Main] Report input (ReadOnly) : {dir_path}")
        print(f"[Main] Report output (Enhanced) : {out_path}")
        try:
            generate_incremental_review(
                repo_path, oldgit, newgit, info_path, dir_path, out_path,
                project_name, workers, render_mode, excel_path,
            )
        except Exception as error:
            print("[Error] Failed to generate incremental review: {}".format(error))
            sys.exit(1)
    else:
        print_help()
        sys.exit(1)
