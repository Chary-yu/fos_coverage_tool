/* Coverage progress page runtime. Kept external so strict CSP can execute it. */
(function () {
  'use strict';

  const PROGRESS_PAGE_VERSION = 'visible-progress-20260813';
  const DEFAULT_REVIEW_SCOPE = 'full';
  const params = new URLSearchParams(window.location.search);
  const configuredScope = document.body.getAttribute('data-review-scope') || DEFAULT_REVIEW_SCOPE;
  const reviewScope = params.get('scope') === 'incremental' ? 'incremental' : configuredScope;
  const projectInput = document.getElementById('projectInput');
  const projectOptions = document.getElementById('projectOptions');
  const statusEl = document.getElementById('status');
  const csvLink = document.getElementById('csvLink');
  const excelLink = document.getElementById('excelLink');
  const detailExportBtn = document.getElementById('detailExportBtn');
  const detailDownloadLink = document.getElementById('detailDownloadLink');
  const jobProgress = document.getElementById('jobProgress');
  const jobStage = document.getElementById('jobStage');
  const jobPercent = document.getElementById('jobPercent');
  const jobBar = document.getElementById('jobBar');
  const jobMessage = document.getElementById('jobMessage');
  const PROGRESS_UPDATE_STORAGE_KEY = 'coverage-review-progress-updated';
  let resolvedApiBase = '';
  let activeLoadToken = 0;
  let currentFileRows = [];
  let currentDetailFile = '';
  let currentDetailPage = 1;

  document.documentElement.setAttribute('data-progress-version', PROGRESS_PAGE_VERSION);
  if (reviewScope === 'incremental') {
    document.title = 'Incremental Coverage Analysis Progress';
    document.getElementById('pageTitle').innerText = '增量覆盖率分析进度';
    document.getElementById('totalUncoveredLabel').innerText = '增量未覆盖行';
  }
  projectInput.value = params.get('project') || '';

  function asNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function fmtRate(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(1)}%` : '0.0%';
  }

  function bar(rate) {
    const value = Math.max(0, Math.min(100, asNumber(rate)));
    return `<span class="bar"><span style="width:${value}%"></span></span>`;
  }

  function metric(id, value) {
    document.getElementById(id).innerText = value;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function tableRows(rows, pathKey) {
    if (!rows || rows.length === 0) return '<tr><td>暂无数据</td></tr>';
    return rows.map(row => `
      <tr>
        <td class="path">${escapeHtml(row[pathKey] || '(root)')}</td>
        <td>${asNumber(row.total_uncovered)}</td><td>${asNumber(row.filled_total)}</td>
        <td>${asNumber(row.unfilled_total)}</td><td>${asNumber(row.confirmed_total)}</td>
        <td>${asNumber(row.coverable_total)}</td><td>${asNumber(row.uncoverable_total)}</td>
        <td>${asNumber(row.redundant_total)}</td>
        <td>${fmtRate(row.fill_rate)} ${bar(row.fill_rate)}</td>
        <td>${fmtRate(row.confirmed_rate)} ${bar(row.confirmed_rate)}</td>
      </tr>`).join('');
  }

  function renderTable(id, rows, pathKey) {
    document.getElementById(id).innerHTML = `
      <thead><tr><th>路径</th><th>未覆盖</th><th>已填</th><th>未填</th><th>已确认</th>
      <th>可覆盖</th><th>无法覆盖</th><th>冗余</th><th>填写率</th><th>确认率</th></tr></thead>
      <tbody>${tableRows(rows, pathKey)}</tbody>`;
  }

  function renderTeamTable(rows) {
    const body = !rows || rows.length === 0 ? '<tr><td colspan="14">暂无数据</td></tr>' : rows.map(row => `
      <tr><td>${escapeHtml(row.team || '')}</td><td>${escapeHtml(row.leader || '')}</td>
      <td class="path">${escapeHtml(row.module_names || '')}</td><td>${asNumber(row.file_total)}</td>
      <td>${asNumber(row.total_uncovered)}</td><td>${asNumber(row.filled_total)}</td>
      <td>${asNumber(row.unfilled_total)}</td><td>${asNumber(row.confirmed_total)}</td>
      <td>${asNumber(row.coverable_total)}</td><td>${asNumber(row.uncoverable_total)}</td>
      <td>${asNumber(row.redundant_total)}</td><td>${fmtRate(row.fill_rate)} ${bar(row.fill_rate)}</td>
      <td>${fmtRate(row.confirmed_rate)} ${bar(row.confirmed_rate)}</td>
      <td>${escapeHtml(row.last_updated || '')}</td></tr>`).join('');
    document.getElementById('teamTable').innerHTML = `
      <thead><tr><th>小组</th><th>组长</th><th>模块</th><th>文件数</th><th>未覆盖</th>
      <th>已填</th><th>未填</th><th>已确认</th><th>可覆盖</th><th>无法覆盖</th>
      <th>冗余</th><th>填写率</th><th>确认率</th><th>最后更新</th></tr></thead><tbody>${body}</tbody>`;
  }

  function renderFileTable(rows) {
    currentFileRows = Array.isArray(rows) ? rows : [];
    const body = currentFileRows.length === 0 ? '<tr><td colspan="14">暂无数据</td></tr>' : currentFileRows.map((row, index) => `
      <tr><td class="path"><button class="file-detail-link" type="button" data-file-index="${index}">${escapeHtml(row.file_path || '')}</button></td>
      <td>${escapeHtml(row.module || '')}</td><td>${escapeHtml(row.team || '')}</td>
      <td>${escapeHtml(row.leader || '')}</td>
      <td class="${row.ownership_status === '已匹配' ? 'matched' : 'unmatched'}">${escapeHtml(row.ownership_status || '')}</td>
      <td>${asNumber(row.total_uncovered)}</td><td>${asNumber(row.filled_total)}</td>
      <td>${asNumber(row.unfilled_total)}</td><td>${asNumber(row.confirmed_total)}</td>
      <td>${asNumber(row.coverable_total)}</td><td>${asNumber(row.uncoverable_total)}</td>
      <td>${asNumber(row.redundant_total)}</td><td>${fmtRate(row.fill_rate)} ${bar(row.fill_rate)}</td>
      <td>${fmtRate(row.confirmed_rate)} ${bar(row.confirmed_rate)}</td></tr>`).join('');
    document.getElementById('fileTable').innerHTML = `
      <thead><tr><th>文件路径</th><th>模块</th><th>小组</th><th>组长</th><th>匹配状态</th>
      <th>未覆盖</th><th>已填</th><th>未填</th><th>已确认</th><th>可覆盖</th>
      <th>无法覆盖</th><th>冗余</th><th>填写率</th><th>确认率</th></tr></thead><tbody>${body}</tbody>`;
  }

  function renderDetailTable(data) {
    const rows = Array.isArray(data.rows) ? data.rows : [];
    const body = rows.length ? rows.map(row => `
      <tr><td>${asNumber(row[2])}</td><td class="code">${escapeHtml(row[3] || '')}</td>
      <td>${escapeHtml(row[7] || '')}</td><td>${escapeHtml(row[8] || '')}</td>
      <td>${escapeHtml(row[9] || '')}</td><td>${escapeHtml(row[10] || '')}</td>
      <td>${escapeHtml(row[11] || '')}</td><td>${escapeHtml(row[12] || '')}</td></tr>`).join('') : '<tr><td colspan="8">暂无详细数据</td></tr>';
    document.getElementById('detailTable').innerHTML = `
      <thead><tr><th>行号</th><th>代码行</th><th>填写状态</th><th>结论</th><th>责任人</th>
      <th>覆盖方法</th><th>无法覆盖原因</th><th>更新时间</th></tr></thead><tbody>${body}</tbody>`;
    currentDetailPage = asNumber(data.page) || 1;
    const totalPages = asNumber(data.total_pages);
    document.getElementById('detailStatus').innerHTML = `
      <button id="detailPrevBtn" type="button" ${currentDetailPage <= 1 ? 'disabled' : ''}>上一页</button>
      <button id="detailNextBtn" type="button" ${!totalPages || currentDetailPage >= totalPages ? 'disabled' : ''}>下一页</button>
      <span>第 ${currentDetailPage} / ${totalPages || 0} 页，共 ${asNumber(data.total)} 行，每页 ${asNumber(data.page_size)} 行</span>`;
    document.getElementById('detailPrevBtn').addEventListener('click', () => loadFileDetails(currentDetailFile, currentDetailPage - 1));
    document.getElementById('detailNextBtn').addEventListener('click', () => loadFileDetails(currentDetailFile, currentDetailPage + 1));
  }

  function renderOwnershipStatus(ownership) {
    ownership = ownership || {};
    metric('matchedFiles', asNumber(ownership.matched_files));
    metric('unmatchedFiles', asNumber(ownership.unmatched_files));
    const ownershipStatus = document.getElementById('ownershipStatus');
    if (!ownership.available) {
      ownershipStatus.innerHTML = `<span class="warning">${escapeHtml(ownership.warning || '代码目录归属表不可用，文件已归入未匹配小组。')}</span>`;
      return;
    }
    const sourceName = String(ownership.xlsx_path || '').replace(/\\/g, '/').split('/').pop();
    ownershipStatus.innerHTML = `归属表：${escapeHtml(sourceName)}（更新时间 ${escapeHtml(ownership.modified_at || '-')}），`
      + `目录规则 ${asNumber(ownership.directory_rule_total)} 条，负责人规则 ${asNumber(ownership.owner_rule_total)} 条，`
      + `<span class="matched">已匹配 ${asNumber(ownership.matched_files)} 个文件</span>，`
      + `<span class="${asNumber(ownership.unmatched_files) ? 'unmatched' : 'matched'}">未匹配 ${asNumber(ownership.unmatched_files)} 个文件</span>。`;
  }

  function normalizeApiBase(value) {
    return String(value || '').replace(/\/+$/, '');
  }

  function unique(values) {
    return Array.from(new Set(values.filter(Boolean)));
  }

  function apiBaseCandidates() {
    const explicit = params.get('api');
    const origin = window.location.origin && window.location.origin !== 'null' ? window.location.origin : '';
    const candidates = [];
    if (explicit) candidates.push(normalizeApiBase(explicit));
    if (origin) {
      candidates.push(`${origin}/api/coverage`);
      if (window.location.pathname.startsWith('/coverage/')) candidates.push(`${origin}/coverage/api/coverage`);
      if (window.location.port !== '9528') candidates.push(`${window.location.protocol}//${window.location.hostname}:9528/api/coverage`);
    }
    candidates.push('http://127.0.0.1:9528/api/coverage');
    candidates.push('/api/coverage');
    return unique(candidates.map(normalizeApiBase));
  }

  function fetchJsonWithTimeout(url, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { signal: controller.signal })
      .finally(() => clearTimeout(timer))
      .then(response => response.json().catch(() => ({})).then(payload => {
        if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
        return payload;
      }));
  }

  function updateLinks(project, apiBase) {
    const encoded = encodeURIComponent(project);
    const base = normalizeApiBase(apiBase || resolvedApiBase || apiBaseCandidates()[0]);
    csvLink.href = `${base}/export?type=full_progress_summary&project=${encoded}`;
    excelLink.href = `${base}/export?type=review_excel_by_dir&project=${encoded}`;
  }

  const STAGE_LABELS = {
    connecting: '连接服务', queued: '等待执行', preparing: '检查项目', database: '数据库聚合',
    summary: '汇总项目/目录', ownership: '匹配文件归属', finalizing: '整理结果',
    counting: '统计详细行', exporting: '生成详细 CSV', completed: '已完成', failed: '失败'
  };

  function showJobProgress(job, title) {
    const percent = Math.max(0, Math.min(100, asNumber(job.percent)));
    jobProgress.classList.add('visible');
    jobProgress.classList.toggle('active', job.state !== 'completed' && job.state !== 'failed');
    jobStage.innerText = `${title || '后台任务'} · ${STAGE_LABELS[job.stage] || job.stage || '处理中'}`;
    jobPercent.innerText = `${percent}%`;
    jobBar.style.width = `${percent}%`;
    jobMessage.innerText = `${job.message || '正在处理'}（已用时 ${asNumber(job.elapsed_seconds).toFixed(1)} 秒）`;
  }

  function sleep(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
  }

  async function waitForJob(apiBase, job, title, token) {
    let current = job;
    let pollErrors = 0;
    while (current && current.state === 'running') {
      if (token != null && token !== activeLoadToken) throw new Error('已取消过期的页面加载');
      showJobProgress(current, title);
      await sleep(800);
      try {
        const payload = await fetchJsonWithTimeout(`${apiBase}/jobs/status?id=${encodeURIComponent(current.id)}`, 10000);
        current = payload.job || {};
        pollErrors = 0;
      } catch (error) {
        pollErrors += 1;
        if (pollErrors >= 5) throw error;
        jobMessage.innerText = `后台任务仍在执行，第 ${pollErrors} 次进度查询未响应，正在重试…`;
      }
    }
    showJobProgress(current || {}, title);
    if (!current || current.state !== 'completed') throw new Error(current && current.message ? current.message : '后台任务失败');
    return current;
  }

  function renderProgressData(project, apiBase, data) {
    const projectRows = Array.isArray(data.project) ? data.project : [];
    const projectRow = projectRows[0] || {};
    const meta = data.meta || {};
    metric('totalUncovered', asNumber(projectRow.total_uncovered));
    metric('filledTotal', asNumber(projectRow.filled_total));
    metric('fillRate', fmtRate(projectRow.fill_rate));
    metric('confirmedRate', fmtRate(projectRow.confirmed_rate));
    renderOwnershipStatus(data.ownership || {});
    renderTeamTable(data.teams || []);
    renderTable('dirTable', data.dirs || [], 'dir_path');
    renderFileTable(data.files || []);
    if (projectRows.length === 0) {
      statusEl.innerHTML = `<span class="warning">未找到项目“${escapeHtml(project)}”的审查行索引。请在数据库可连接时重新执行 incremental 或 inject。</span>`;
    } else {
      statusEl.innerText = `已加载 ${asNumber(meta.indexed_file_total)} 个文件的填写摘要；未传输任何逐行明细。项目：${project}，接口：${apiBase}`;
    }
  }

  async function loadProgress() {
    const project = projectInput.value.trim();
    if (!project) {
      statusEl.innerHTML = '<span class="error">请输入项目名。</span>';
      return;
    }
    const loadBtn = document.getElementById('loadBtn');
    loadBtn.disabled = true;
    loadBtn.innerText = '后台计算中...';
    const token = ++activeLoadToken;
    const startedAt = Date.now();
    let connectingApi = '';
    const showConnecting = () => showJobProgress({
      state: 'running', percent: 2, stage: 'connecting',
      message: `正在启动后台任务${connectingApi ? `：${connectingApi}` : ''}`,
      elapsed_seconds: (Date.now() - startedAt) / 1000
    }, '填写进展');
    showConnecting();
    const connectingTimer = window.setInterval(showConnecting, 250);
    const encodedProject = encodeURIComponent(project);
    const candidates = apiBaseCandidates();
    updateLinks(project, candidates[0]);
    let lastError = null;

    try {
      for (const apiBase of candidates) {
        try {
          connectingApi = apiBase;
          statusEl.innerText = `正在启动文件级进度任务：${apiBase}`;
          const payload = await fetchJsonWithTimeout(`${apiBase}/progress/start?project=${encodedProject}`, 10000);
          if (!payload || payload.status !== 'success') throw new Error(payload && payload.message ? payload.message : '加载失败');
          window.clearInterval(connectingTimer);
          const completedJob = await waitForJob(apiBase, payload.job || {}, '填写进展', token);
          resolvedApiBase = apiBase;
          updateLinks(project, apiBase);
          renderProgressData(project, apiBase, completedJob.data || {});
          const url = new URL(window.location.href);
          url.searchParams.set('project', project);
          if (params.get('api')) url.searchParams.set('api', apiBase);
          window.history.replaceState(null, '', url.toString());
          return;
        } catch (error) {
          lastError = error;
        }
      }
      throw lastError || new Error('无法连接接口');
    } catch (error) {
      showJobProgress({ state: 'failed', percent: 2, stage: 'failed', message: error.message, elapsed_seconds: (Date.now() - startedAt) / 1000 }, '填写进展');
      statusEl.innerHTML = `<span class="error">加载失败：${escapeHtml(error.message)}。已尝试：${escapeHtml(candidates.join(' , '))}</span>`;
    } finally {
      window.clearInterval(connectingTimer);
      if (token === activeLoadToken) {
        loadBtn.disabled = false;
        loadBtn.innerText = '查看进度';
      }
    }
  }

  async function loadFileDetails(filePath, page) {
    const project = projectInput.value.trim();
    if (!project || !filePath) return;
    currentDetailFile = filePath;
    const section = document.getElementById('detailSection');
    section.hidden = false;
    document.getElementById('detailTitle').innerText = `详细填写数据：${filePath}`;
    document.getElementById('detailStatus').innerText = '正在加载当前页…';
    const apiBase = resolvedApiBase || apiBaseCandidates()[0];
    try {
      const payload = await fetchJsonWithTimeout(
        `${apiBase}/details?project=${encodeURIComponent(project)}&file=${encodeURIComponent(filePath)}&page=${Math.max(1, page || 1)}&page_size=200`, 15000
      );
      renderDetailTable(payload.data || {});
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) {
      document.getElementById('detailStatus').innerHTML = `<span class="error">详细数据加载失败：${escapeHtml(error.message)}</span>`;
    }
  }

  async function exportFullDetails() {
    const project = projectInput.value.trim();
    if (!project) {
      statusEl.innerHTML = '<span class="error">请先输入项目名。</span>';
      return;
    }
    detailExportBtn.disabled = true;
    detailExportBtn.innerText = '详细 CSV 生成中...';
    detailDownloadLink.hidden = true;
    const startedAt = Date.now();
    showJobProgress({ state: 'running', percent: 2, stage: 'connecting', message: '正在启动详细导出任务', elapsed_seconds: 0 }, '详细数据导出');
    let lastError = null;
    try {
      for (const apiBase of apiBaseCandidates()) {
        try {
          const payload = await fetchJsonWithTimeout(`${apiBase}/export/start?type=full_detail&project=${encodeURIComponent(project)}`, 10000);
          const completedJob = await waitForJob(apiBase, payload.job || {}, '详细数据导出');
          resolvedApiBase = apiBase;
          detailDownloadLink.href = `${apiBase}/export/download?id=${encodeURIComponent(completedJob.id)}`;
          detailDownloadLink.hidden = false;
          detailDownloadLink.innerText = `下载详细 CSV（${asNumber(completedJob.row_count)} 行）`;
          statusEl.innerText = `详细 CSV 已在后台生成，共 ${asNumber(completedJob.row_count)} 行，正在开始下载。`;
          window.location.assign(detailDownloadLink.href);
          return;
        } catch (error) {
          lastError = error;
        }
      }
      throw lastError || new Error('无法连接导出接口');
    } catch (error) {
      showJobProgress({ state: 'failed', percent: 2, stage: 'failed', message: error.message, elapsed_seconds: (Date.now() - startedAt) / 1000 }, '详细数据导出');
      statusEl.innerHTML = `<span class="error">详细数据导出失败：${escapeHtml(error.message)}</span>`;
    } finally {
      detailExportBtn.disabled = false;
      detailExportBtn.innerText = '后台导出详细 CSV';
    }
  }

  async function loadProjectOptions() {
    let lastError = null;
    for (const apiBase of apiBaseCandidates()) {
      try {
        const payload = await fetchJsonWithTimeout(`${apiBase}/projects`, 10000);
        if (!payload || payload.status !== 'success') throw new Error(payload && payload.message ? payload.message : '项目列表加载失败');
        resolvedApiBase = apiBase;
        const projects = Array.isArray(payload.projects) ? payload.projects : [];
        projectOptions.innerHTML = projects.map(item => {
          const name = escapeHtml(item.project_name || '');
          const savedLabel = item.saved_total == null ? '已有数据' : `已存 ${asNumber(item.saved_total)}`;
          const indexLabel = item.indexed_total == null ? '进度页计算精确数量' : `索引 ${asNumber(item.indexed_total)}`;
          return `<option value="${name}" label="${escapeHtml(`${savedLabel} / ${indexLabel}`)}"></option>`;
        }).join('');
        if (!projectInput.value.trim() && projects.length === 1) {
          projectInput.value = projects[0].project_name || '';
          loadProgress();
        } else if (!projectInput.value.trim()) {
          statusEl.innerText = projects.length ? `已发现 ${projects.length} 个数据库项目，请输入或选择项目。` : '数据库中暂无覆盖率项目。';
        }
        return;
      } catch (error) {
        lastError = error;
      }
    }
    if (!projectInput.value.trim()) statusEl.innerHTML = `<span class="warning">项目列表加载失败：${escapeHtml(lastError ? lastError.message : '无法连接接口')}；仍可手工输入项目名。</span>`;
  }

  document.getElementById('loadBtn').addEventListener('click', loadProgress);
  detailExportBtn.addEventListener('click', exportFullDetails);
  document.getElementById('fileTable').addEventListener('click', event => {
    const button = event.target.closest('.file-detail-link');
    if (!button) return;
    const row = currentFileRows[asNumber(button.dataset.fileIndex)];
    if (row && row.file_path) loadFileDetails(row.file_path, 1);
  });
  document.getElementById('closeDetailBtn').addEventListener('click', () => {
    document.getElementById('detailSection').hidden = true;
  });
  projectInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') loadProgress();
  });
  window.addEventListener('storage', event => {
    if (event.key !== PROGRESS_UPDATE_STORAGE_KEY || !event.newValue) return;
    try {
      const update = JSON.parse(event.newValue);
      if (update.project_name === projectInput.value.trim()) loadProgress();
    } catch (error) {
      // Ignore malformed cross-tab notifications.
    }
  });
  if (projectInput.value.trim()) loadProgress();
  else loadProjectOptions();
}());
