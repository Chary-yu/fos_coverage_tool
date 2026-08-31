const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const http = require('http');
const { URL } = require('url');

const ROOT = path.join(__dirname, '../..');
const CLIENT_JS = fs.readFileSync(path.join(ROOT, 'web/assets/js/coverage_enhance.js'), 'utf8');
const CLIENT_CSS = fs.readFileSync(path.join(ROOT, 'web/assets/css/coverage_enhance.css'), 'utf8');
const PENDING_JS = fs.readFileSync(path.join(ROOT, 'web/assets/js/pending_snapshot.js'), 'utf8');
const TASK_JS = fs.readFileSync(path.join(ROOT, 'web/assets/js/incremental_developer_tasks.js'), 'utf8');
const INCREMENTAL_JS = fs.readFileSync(path.join(ROOT, 'web/assets/js/incremental_coverage.js'), 'utf8');

function makeLines(start, end, withPanels, savedReviewers, inheritance = null) {
  const lines = [];
  for (let lineNo = start; lineNo <= end; lineNo += 1) {
    const suggestedReviewer = lineNo === start ? 'Alice' : (lineNo === start + 1 ? 'Bob' : '');
    const line = {
      line_no: lineNo,
      source: `int fixture_line_${lineNo} = ${lineNo};`,
      coverage_state: 'uncovered',
      is_block_entry: withPanels && (lineNo === start || lineNo === start + 1 || ((lineNo - start) % 100 === 0)),
      block_start_line: lineNo,
      block_end_line: lineNo,
      analysis_state: '未确认',
      suggested_reviewer: suggestedReviewer,
      reviewer: (savedReviewers && savedReviewers.get(lineNo)) || '',
      is_draft: false,
      coverage_method: '',
      uncovered_reason: '',
      is_pending_analysis: true,
    };
    if (inheritance && lineNo === start) {
      line.analysis = {
        line_id: inheritance.lineId,
        review_state: 'INHERITED_PENDING',
        relation_origin: 'INHERITED',
        relation_is_active: 1,
        relation_revision: inheritance.relationRevision,
        conclusion_status: '可覆盖',
        reviewed_by: 'git-alice',
        coverage_method: 'unit',
        uncovered_reason: '',
        is_draft: 1,
      };
    }
    lines.push(line);
  }
  return lines;
}

function makeLayout(large) {
  const regionSize = large ? 17000 : 600;
  const regions = [0, 1, 2].map(index => ({
    region_id: `region-${index + 1}`,
    start_line: index * regionSize + 1,
    end_line: (index + 1) * regionSize,
    line_count: regionSize,
    default_state: 'collapsed',
    kind: 'collapsed',
    label: `Fixture region ${index + 1}`,
  }));
  return {
    project_name: 'BrowserFixture',
    file_path: 'src/fixture.c',
    report_id: 'report_browser_fixture',
    scan_id: 1,
    repository_name: '',
    total_lines: regionSize * 3,
    total_uncovered_count: regionSize * 3,
    pending_line_count: regionSize * 3,
    confirmed_count: 0,
    regions,
  };
}

function createHarness({ large = false, failOnce = false, taskPendingLines = null,
  taskPendingFiles = null, inheritanceLifecycle = false,
  reorderPendingResponses = false, reportMode = 'VNEXT_ARTIFACT_READY' } = {}) {
  const layout = makeLayout(large);
  const requests = [];
  const failedRequests = [];
  let activeRequests = 0;
  let maxConcurrent = 0;
  let failureUsed = false;
  const savedReviewers = new Map();
  let currentTaskPendingLines = taskPendingLines;
  let currentTaskPendingFiles = taskPendingFiles;
  let pendingPageRequests = 0;
  let pendingRequestSequence = 0;
  let inheritanceRejected = false;
  let inheritanceRevision = 1;
  const reportModeMeta = reportMode
    ? `<meta name="coverage-report-mode" content="${reportMode}">`
    : '';
  const staticSource = reportMode !== 'VNEXT_ARTIFACT_READY'
    ? '<span id="L1" class="lineNoCov">1: return 0;</span>\n'
      + '<span id="L2" class="lineCov">2: return 1;</span>'
    : '';
  const inheritanceLineId = 9001;
  const inheritanceCandidate = () => ({
    candidate_line_id: inheritanceLineId,
    line_number: 1,
    file_path: 'src/fixture.c',
    repository_name: '',
    relation_revision: inheritanceRevision,
    conclusion_status: '可覆盖',
    reviewed_by: 'git-alice',
    coverage_method: 'unit',
    uncovered_reason: '',
  });
  const incrementalRows = Array.from({ length: 401 }, (_, index) => {
    const repository = index < 201 ? 'repo-a' : 'repo-b';
    const filePath = index === 0 ? 'src/fixture.c' : `src/pending-${index}.c`;
    const fileKey = `${repository}::${filePath}`;
    const team = repository === 'repo-a' ? 'core' : 'platform';
    const leader = repository === 'repo-a' ? 'Alice' : 'Bob';
    const module = repository === 'repo-a' ? 'coverage' : 'runtime';
    return `<tr data-repo="${repository}" data-module="${module}" data-team="${team}" data-leader="${leader}" data-ownership="${team} / ${leader}" data-file-key="${fileKey}">
      <td data-sort-value="${repository}">${repository}</td>
      <td data-sort-value="${team}">${team}</td>
      <td data-sort-value="${leader}">${leader}</td>
      <td data-sort-value="${module}">${module}</td>
      <td data-sort-value="${filePath}">${filePath}</td>
      <td data-sort-value="1">1</td><td data-sort-value="0">0</td>
      <td data-sort-value="1">1</td><td data-sort-value="0">0</td>
      <td class="js-unanalyzed-count" data-sort-value="1">1</td>
    </tr>`;
  }).join('');

  const server = http.createServer((req, res) => {
    const parsed = new URL(req.url, 'http://127.0.0.1');
    const pathname = parsed.pathname;
    const json = (status, payload) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(payload));
    };
    const sendStatic = (content, type) => {
      res.writeHead(200, { 'Content-Type': type });
      res.end(content);
    };

    if (pathname === '/' || pathname.endsWith('.gcov.html')) {
      return sendStatic(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <meta name="coverage-project" content="BrowserFixture">
        ${reportModeMeta}
        <meta name="coverage-report-id" content="report_browser_fixture">
        <meta name="coverage-scan-id" content="1">
        <meta name="coverage-repository-name" content="">
        <meta name="coverage-file-path" content="src/fixture.c">
        <meta name="coverage-render-mode" content="lazy_collapse">
        <meta name="coverage-review-scope" content="full">
        <style>${CLIENT_CSS}</style><script src="/coverage_enhance.js"></script>
        </head><body><pre class="source">${staticSource}</pre></body></html>`, 'text/html');
    }
    if (pathname === '/tasks.html') {
      return sendStatic(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body>
        <main data-project="TaskFixture">
          <section id="developer-alice">
            <div><strong class="js-dev-review-files">1</strong><strong class="js-dev-uncovered-lines">2</strong></div>
            <table><tbody>
              <tr data-file-key="src/fixture.c" data-page-link="/fixture.c.gcov.html" data-changed="2" data-owner-specific="true" data-owned-lines="1, 2">
                <td class="js-task-unanalyzed">2</td><td class="js-task-action">填写 2 行</td>
              </tr>
            </tbody></table>
          </section>
          <table><tbody><tr data-dev-anchor="developer-alice"><td class="js-summary-review-files">1</td><td class="js-summary-uncovered-lines">2</td></tr></tbody></table>
        </main><script>${PENDING_JS}</script><script>${TASK_JS}</script></body></html>`, 'text/html');
    }
    if (pathname === '/incremental.html') {
      return sendStatic(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body>
        <main data-project="TaskFixture">
          <div id="incremental-unanalyzed-total">1</div>
          <div class="filters">
            <select id="repo-filter"><option value="">全部仓库</option></select>
            <select id="team-filter"><option value="">全部小组</option></select>
            <select id="leader-filter"><option value="">全部组长</option></select>
            <select id="module-filter"><option value="">全部组件</option></select>
            <input id="file-search" type="text"><button id="reset-filters-btn" type="button">重置</button>
            <span id="filter-count"></span>
          </div>
          <table id="incremental-file-table"><thead><tr>
            <th><button class="sort-button" data-sort-key="repository" data-sort-type="text">仓库</button></th>
            <th><button class="sort-button" data-sort-key="team" data-sort-type="text">小组</button></th>
            <th><button class="sort-button" data-sort-key="leader" data-sort-type="text">组长</button></th>
            <th><button class="sort-button" data-sort-key="module" data-sort-type="text">组件</button></th>
            <th><button class="sort-button" data-sort-key="file" data-sort-type="text">文件</button></th>
            <th><button class="sort-button" data-sort-key="changed" data-sort-type="number">新增</button></th>
            <th><button class="sort-button" data-sort-key="covered" data-sort-type="number">已覆盖</button></th>
            <th><button class="sort-button" data-sort-key="uncovered" data-sort-type="number">未覆盖</button></th>
            <th><button class="sort-button" data-sort-key="ignored" data-sort-type="number">忽略</button></th>
            <th><button class="sort-button" data-sort-key="unanalyzed" data-sort-type="number">待分析</button></th>
          </tr></thead><tbody>${incrementalRows}</tbody></table>
        </main><script>${PENDING_JS}</script><script>${INCREMENTAL_JS}</script></body></html>`, 'text/html');
    }
    if (pathname === '/coverage_enhance.js') return sendStatic(CLIENT_JS, 'text/javascript');
    if (pathname === '/coverage_enhance.css') return sendStatic(CLIENT_CSS, 'text/css');

    if (pathname === '/api/coverage/incremental/unanalyzed') {
      if (currentTaskPendingLines === null && currentTaskPendingFiles === null) {
        return json(404, { status: 'error', message: 'task fixture disabled' });
      }
      pendingPageRequests += 1;
      const pendingFiles = currentTaskPendingFiles !== null
        ? currentTaskPendingFiles
        : [{ file_path: 'src/fixture.c', unanalyzed: currentTaskPendingLines.length,
          pending_line_numbers: currentTaskPendingLines }];
      const requestSequence = ++pendingRequestSequence;
      const pageSize = Math.min(200, Math.max(1,
        Number(parsed.searchParams.get('page_size') || 200)));
      const rawCursor = parsed.searchParams.get('cursor');
      const offset = rawCursor === null ? 0 : Number(rawCursor);
      if (!Number.isInteger(offset) || offset < 0 || offset > pendingFiles.length) {
        return json(409, { status: 'error', error: 'PAGINATION_CURSOR_STALE' });
      }
      const files = pendingFiles.slice(offset, offset + pageSize);
      const nextOffset = offset + files.length;
      const hasMore = nextOffset < pendingFiles.length;
      const totalUnanalyzed = pendingFiles.reduce((sum, file) => {
        const count = Number(file.unanalyzed);
        return sum + (Number.isFinite(count) ? count :
          (Array.isArray(file.pending_line_numbers) ? file.pending_line_numbers.length : 0));
      }, 0);
      const payload = {
        project_name: 'TaskFixture',
        scan_id: 1,
        data_version: 1,
        repository_name: parsed.searchParams.get('repository_name') || '',
        total_unanalyzed: totalUnanalyzed,
        has_more: hasMore,
        next_cursor: hasMore ? String(nextOffset) : null,
        files,
      };
      // Keep the first snapshot in flight while a newer generation is
      // requested.  This is a real HTTP reordering fixture: the old response
      // must not overwrite the complete snapshot applied by the newer one.
      if (reorderPendingResponses && requestSequence === 1) {
        return setTimeout(() => json(200, payload), 3500);
      }
      return json(200, payload);
    }

    if (pathname === '/api/coverage/code-layout') {
      requests.push({
        path: pathname,
        query: Object.fromEntries(parsed.searchParams.entries()),
      });
      return json(200, layout);
    }
    if (pathname === '/api/coverage/code-lines/batch' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body || '{}');
        const batches = (payload.ranges || []).map(range => ({
          start_line: Number(range.start_line),
          end_line: Number(range.end_line),
          lines: makeLines(Number(range.start_line), Number(range.end_line), !large, savedReviewers,
            inheritanceLifecycle && !inheritanceRejected
              ? { lineId: inheritanceLineId, relationRevision: inheritanceRevision } : null),
        }));
        json(200, { scan_id: 1, report_id: 'report_browser_fixture', batches });
      });
      return undefined;
    }
    if (pathname === '/api/coverage/code-lines' && req.method === 'GET') {
      const start = Number(parsed.searchParams.get('start_line') || 1);
      const end = Number(parsed.searchParams.get('end_line') || start);
      requests.push({ start, end });
      activeRequests += 1;
      maxConcurrent = Math.max(maxConcurrent, activeRequests);
      const shouldFail = failOnce && !failureUsed && start === 601;
      if (shouldFail) failureUsed = true;
      const delay = start % 1000 === 1 ? 35 : 5;
      return setTimeout(() => {
        activeRequests -= 1;
        if (shouldFail) {
          failedRequests.push({ start, end });
          return json(503, { status: 'error', message: 'intentional retry fixture failure' });
        }
        return json(200, {
          status: 'success',
          data: { start_line: start, end_line: end, lines: makeLines(start, end, !large, savedReviewers,
            inheritanceLifecycle && !inheritanceRejected
              ? { lineId: inheritanceLineId, relationRevision: inheritanceRevision } : null) },
        });
      }, delay);
    }
    if (inheritanceLifecycle && pathname === '/api/coverage/scans/1/inheritance/pending' && req.method === 'GET') {
      return json(200, { items: inheritanceRejected ? [] : [inheritanceCandidate()], has_more: false });
    }
    if (inheritanceLifecycle && pathname === '/api/coverage/scans/1/inheritance/relation' && req.method === 'GET') {
      return json(200, { item: inheritanceRejected ? null : inheritanceCandidate() });
    }
    if (inheritanceLifecycle && pathname === '/api/coverage/scans/1/inheritance/reject' && req.method === 'POST') {
      inheritanceRejected = true;
      inheritanceRevision += 1;
      return json(200, {
        rejection: { id: 7001, rejection_revision: 1, rejected_relation_revision: inheritanceRevision - 1 },
      });
    }
    if (inheritanceLifecycle && pathname === '/api/coverage/scans/1/inheritance/rejections/7001/undo' && req.method === 'POST') {
      inheritanceRejected = false;
      inheritanceRevision += 1;
      return json(200, {
        rejection: { id: 7001, rejection_revision: 2, terminal_reason: 'UNDONE' },
      });
    }
    if (pathname === '/api/coverage/analysis' && req.method === 'POST') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        try {
          const payload = JSON.parse(body);
          requests.push({ batch: payload });
          (payload.records || []).forEach(record => {
            const start = Number(record.line_start || record.line_number);
            const end = Number(record.line_end || start);
            for (let lineNumber = start; lineNumber <= end; lineNumber += 1) {
              savedReviewers.set(lineNumber, record.reviewer || '');
            }
          });
        } catch (_) { /* client error is asserted by HTTP status */ }
        json(200, { status: 'success', data_version: 2 });
      });
      return undefined;
    }
    failedRequests.push({ pathname, status: 404 });
    return json(404, { status: 'error', message: 'not found' });
  });

  return {
    server,
    layout,
    requests,
    failedRequests,
    get maxConcurrent() { return maxConcurrent; },
    get failureUsed() { return failureUsed; },
    get pendingPageRequests() { return pendingPageRequests; },
    setTaskPendingLines(lines) { currentTaskPendingLines = lines; },
    setTaskPendingFiles(files) { currentTaskPendingFiles = files; },
  };
}

async function startHarness(options) {
  const harness = createHarness(options);
  await new Promise(resolve => harness.server.listen(0, '127.0.0.1', resolve));
  harness.baseUrl = `http://127.0.0.1:${harness.server.address().port}`;
  return harness;
}

async function stopHarness(harness) {
  await new Promise(resolve => harness.server.close(resolve));
}

test('real Chromium coverage lifecycle: collapse, expand, draft survival, restore and batch save', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const harness = await startHarness();
  const consoleErrors = [];
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  try {
    await page.goto(`${harness.baseUrl}/fixture.c.gcov.html`, { waitUntil: 'networkidle' });

    // Scenarios 1-4: real page bootstrap, empty shell, three regions, and toolbar.
    await expect(page.locator('pre.source > .coverage-region-container')).toHaveCount(3);
    await expect(page.locator('.coverage-lazy-toolbar')).toBeVisible();
    await expect(page.locator('pre.source > span')).toHaveCount(0);
    const layoutRequest = harness.requests.find(item => item.path === '/api/coverage/code-layout');
    expect(layoutRequest.query).toMatchObject({
      scan_id: '1',
      report_id: 'report_browser_fixture',
      repository_name: '',
      file_path: 'src/fixture.c',
      scope: 'full',
    });

    // Scenarios 5-8: expand, exact line order, no duplicate DOM, and bounded chunks.
    await page.locator('.coverage-region-placeholder').nth(0).click();
    await expect(page.locator('#L600')).toBeVisible({ timeout: 15000 });
    expect(await page.locator('pre.source > .coverage-region-container').nth(0).locator('span[id^="L"]').count()).toBe(600);
    expect(await page.locator('pre.source span[id^="L"]').evaluateAll(nodes => new Set(nodes.map(node => node.id)).size)).toBe(600);
    expect(harness.maxConcurrent).toBeLessThanOrEqual(3);

    // Scenarios 9-12: draft editing, dirty toolbar, collapse, and re-expand.
    const panel = page.locator('.coverage-analysis-panel').first();
    await panel.locator('select').selectOption('未确认');
    await panel.locator('input.reviewer-input').fill('browser-draft');
    await panel.locator('textarea').first().fill('browser batch fixture');
    await expect(page.locator('.coverage-batch-btn.draft')).toBeEnabled();
    await page.locator('.coverage-region-collapse-btn').first().click();
    await expect(page.locator('#L1')).toHaveCount(0);
    await page.locator('.coverage-region-placeholder').nth(0).click();
    await expect(page.locator('#L600')).toBeVisible({ timeout: 15000 });
    const restoredPanel = page.locator('.coverage-analysis-panel').first();
    await expect(restoredPanel.locator('input.reviewer-input')).toHaveValue('browser-draft');
    await expect(restoredPanel.locator('textarea').first()).toHaveValue('browser batch fixture');

    // Scenarios 13-16: actual POST batch save, server response, expand-all, restore-default.
    await page.locator('.coverage-batch-btn.draft').click();
    await expect.poll(() => harness.requests.filter(item => item.batch).length).toBe(1);
    await page.locator('.coverage-lazy-toolbar button.primary').click();
    await expect(page.locator('#L1800')).toBeVisible({ timeout: 30000 });
    await page.locator('.coverage-lazy-toolbar button:not(.primary)').click();
    await expect(page.locator('.coverage-region-placeholder')).toHaveCount(3);

    // Scenarios 17-20: no unexpected network failures, no browser errors, and unique IDs after redraw.
    expect(harness.failedRequests).toEqual([]);
    expect(consoleErrors).toEqual([]);
    expect(await page.locator('pre.source span[id^="L"]').evaluateAll(nodes => new Set(nodes.map(node => node.id)).size)).toBe(0);
    expect(await page.locator('.coverage-batch-toolbar').count()).toBe(1);
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium chunk ordering and retry are fail-closed and retryable', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const harness = await startHarness({ failOnce: true });
  const consoleErrors = [];
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  try {
    await page.goto(`${harness.baseUrl}/fixture.c.gcov.html`, { waitUntil: 'networkidle' });
    await page.locator('.coverage-region-placeholder').nth(1).click();
    await expect(page.locator('.coverage-region-placeholder.error').nth(0)).toBeVisible({ timeout: 15000 });
    expect(harness.failureUsed).toBe(true);
    await page.locator('.coverage-region-placeholder.error').nth(0).click();
    await expect(page.locator('#L1200')).toBeVisible({ timeout: 15000 });
    const numbers = await page.locator('pre.source span[id^="L"]').evaluateAll(nodes => nodes.map(node => Number(node.id.slice(1))));
    expect(numbers).toEqual([...Array(600)].map((_, index) => index + 601));
    expect(harness.maxConcurrent).toBeLessThanOrEqual(3);
    expect(harness.failedRequests.length).toBe(1);
    // The first failure is intentional and is not a stuck-loading state.
    expect(consoleErrors.some(message => message.includes('Failed to expand region'))).toBe(true);
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium incremental reviewer suggestions split adjacent blocks and survive DB refresh', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const harness = await startHarness();
  try {
    await page.goto(`${harness.baseUrl}/fixture.c.gcov.html`, { waitUntil: 'networkidle' });
    await page.locator('.coverage-region-placeholder').nth(0).click();
    await expect(page.locator('#L600')).toBeVisible({ timeout: 15000 });

    const panels = page.locator('.coverage-analysis-panel');
    await expect(panels).toHaveCount(7);
    await expect(panels.nth(0).locator('input.reviewer-input')).toHaveValue('Alice');
    await expect(panels.nth(1).locator('input.reviewer-input')).toHaveValue('Bob');
    await expect(page.locator('#L1')).toHaveAttribute('data-coverage-reviewer', 'Alice');
    await expect(page.locator('#L2')).toHaveAttribute('data-coverage-reviewer', 'Bob');

    await panels.nth(0).locator('input.reviewer-input').fill('database-owner');
    await page.locator('.coverage-batch-btn.draft').click();
    await expect.poll(() => harness.requests.filter(item => item.batch).length).toBe(1);

    await page.reload({ waitUntil: 'networkidle' });
    await page.locator('.coverage-region-placeholder').nth(0).click();
    await expect(page.locator('#L600')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('.coverage-analysis-panel').first().locator('input.reviewer-input')).toHaveValue('database-owner');
    await page.locator('.coverage-region-collapse-btn').first().click();
    await page.locator('.coverage-region-placeholder').nth(0).click();
    await expect(page.locator('.coverage-analysis-panel').first().locator('input.reviewer-input')).toHaveValue('database-owner');
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium inheritance reject and undo keep the relation reviewable', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const harness = await startHarness({ inheritanceLifecycle: true });
  page.on('dialog', dialog => dialog.accept());
  try {
    await page.goto(`${harness.baseUrl}/fixture.c.gcov.html`, { waitUntil: 'networkidle' });
    await page.locator('.coverage-region-placeholder').nth(0).click();
    await expect(page.locator('#L600')).toBeVisible({ timeout: 15000 });

    const panel = page.locator('.coverage-analysis-panel').first();
    await expect(panel.locator('.coverage-inherit-reject-btn')).toBeVisible();
    await expect(panel.locator('select')).toHaveValue('可覆盖');
    await panel.locator('.coverage-inherit-reject-btn').click();
    await expect(panel.locator('.coverage-inherit-undo-btn')).toBeVisible();
    await expect(panel.locator('.coverage-inherit-reject-btn')).toBeHidden();
    await expect(panel.locator('select')).toHaveValue('未确认');

    await panel.locator('.coverage-inherit-undo-btn').click();
    await expect(panel.locator('.coverage-inherit-reject-btn')).toBeVisible();
    await expect(panel.locator('.coverage-inherit-undo-btn')).toBeHidden();
    await expect(panel.locator('select')).toHaveValue('可覆盖');
    await expect(panel.locator('textarea').first()).toHaveValue('unit');
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium Legacy static mode stays offline and disables VNext controls', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const harness = await startHarness({ reportMode: 'LEGACY_STATIC' });
  const apiRequests = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/coverage')) apiRequests.push(url.pathname);
  });
  try {
    await page.goto(`${harness.baseUrl}/fixture.c.gcov.html`, { waitUntil: 'networkidle' });
    await expect(page.locator('#L1')).toBeVisible();
    await expect(page.locator('#L1 .coverage-analysis-panel')).toHaveCount(1);
    await expect(page.locator('#L1 select[data-panel-action="status"]')).toBeDisabled();
    await expect(page.locator('#L1 input.reviewer-input')).toBeDisabled();
    expect(apiRequests).toEqual([]);
    expect(harness.failedRequests).toEqual([]);
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium missing report mode defaults to safe Legacy static behavior', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const harness = await startHarness({ reportMode: '' });
  const apiRequests = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/coverage')) apiRequests.push(url.pathname);
  });
  try {
    await page.goto(`${harness.baseUrl}/fixture.c.gcov.html`, { waitUntil: 'networkidle' });
    await expect(page.locator('#L1 .coverage-analysis-panel')).toHaveCount(1);
    await expect(page.locator('#L1 .coverage-analysis-panel')).toHaveAttribute(
      'data-report-mode', 'LEGACY_STATIC'
    );
    await expect(page.locator('#L1 select[data-panel-action="status"]')).toBeDisabled();
    expect(apiRequests).toEqual([]);
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium virtualizes data and reuses cached viewport windows', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const harness = await startHarness({ large: true });
  try {
    await page.goto(`${harness.baseUrl}/fixture.c.gcov.html`, { waitUntil: 'networkidle' });
    for (let index = 0; index < 3; index += 1) {
      await page.locator('.coverage-region-placeholder').nth(index).click();
      await expect(page.locator(`#L${index * 17000 + 1}`)).toBeVisible({ timeout: 30000 });
      expect(await page.locator('pre.source span[id^="L"]').count()).toBeLessThan(1500);
      await page.locator('.coverage-region-collapse-btn').nth(0).click();
    }
    // Collapsing removes DOM while the resident sparse data stays bounded to
    // the viewport windows rather than all 51k source rows.
    expect(await page.locator('#L1').count()).toBe(0);
    const beforeReload = harness.requests.filter(item => item.start === 1).length;
    await page.locator('.coverage-region-placeholder').nth(0).click();
    await expect(page.locator('#L1')).toBeVisible({ timeout: 30000 });
    expect(harness.requests.filter(item => item.start === 1).length).toBe(beforeReload);
    await page.addStyleTag({
      content: '#L101 { min-height: 180px !important; line-height: 180px !important; }',
    });
    const variableHeight = await page.evaluate(() => {
      const internals = window.__COVERAGE_ENHANCE_INTERNALS__;
      const region = internals.CodeRegionStore.get('region-1');
      internals.CodeRegionController.renderVirtualWindow(region, 0);
      return Number(region.virtualMeasuredHeights.get(100) || 0);
    });
    expect(variableHeight).toBeGreaterThan(100);
    await page.evaluate(() => window.scrollTo(0, 16000 * 24));
    await expect(page.locator('#L16000')).toBeVisible({ timeout: 30000 });
    expect(await page.locator('pre.source span[id^="L"]').count()).toBeLessThan(1500);
    const residentSweep = await page.evaluate(async () => {
      const internals = window.__COVERAGE_ENHANCE_INTERNALS__;
      const region = internals.CodeRegionStore.get('region-1');
      const samples = [];
      for (const target of [25000, 50000, 75000, 100000, 1]) {
        const bounds = internals.CodeRegionController.virtualWindowBounds(region, target - 1);
        await internals.CodeRegionLoader.ensureVirtualWindow(
          internals.CodeRegionController.filePath, region, bounds.start, bounds.end
        );
        internals.CodeRegionController.renderVirtualWindow(region, target - 1);
        samples.push(region.loadedLineCount);
      }
      return { samples, peak: Math.max(...samples) };
    });
    expect(residentSweep.peak).toBeLessThanOrEqual(8000);
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium developer task refresh intersects owner lines with live pending lines', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const harness = await startHarness({ taskPendingLines: [2] });
  try {
    await page.goto(`${harness.baseUrl}/tasks.html`, { waitUntil: 'networkidle' });
    const row = page.locator('tr[data-owner-specific="true"]');
    await expect(row.locator('.js-task-unanalyzed')).toHaveText('1');
    await expect(page.locator('.js-dev-uncovered-lines')).toHaveText('1');
    await expect(page.locator('.js-summary-uncovered-lines')).toHaveText('1');

    harness.setTaskPendingLines([]);
    await page.waitForTimeout(2200);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(row.locator('.js-task-unanalyzed')).toHaveText('0', { timeout: 10000 });
    await expect(page.locator('.js-dev-uncovered-lines')).toHaveText('0');
    await expect(row.locator('.js-task-action')).toContainText('已全部填写完成');
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium developer task consumes the complete paginated pending snapshot', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const taskPendingFiles = Array.from({ length: 401 }, (_, index) => ({
    file_path: index === 0 ? 'src/fixture.c' : `src/pending-${index}.c`,
    unanalyzed: index === 0 ? 1 : 0,
    pending_line_numbers: index === 0 ? [2] : [],
  }));
  const harness = await startHarness({ taskPendingFiles });
  try {
    await page.goto(`${harness.baseUrl}/tasks.html`, { waitUntil: 'networkidle' });
    await expect.poll(() => harness.pendingPageRequests).toBe(3);
    await expect(page.locator('tr[data-owner-specific="true"] .js-task-unanalyzed')).toHaveText('1');
    await expect(page.locator('.js-dev-uncovered-lines')).toHaveText('1');
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium incremental page consumes complete pages, preserves filters, and applies zero transition', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const taskPendingFiles = Array.from({ length: 401 }, (_, index) => ({
    repository_name: index < 201 ? 'repo-a' : 'repo-b',
    file_path: index === 0 ? 'src/fixture.c' : `src/pending-${index}.c`,
    unanalyzed: index === 0 ? 1 : 0,
    pending_line_numbers: index === 0 ? [2] : [],
  }));
  const harness = await startHarness({ taskPendingFiles });
  try {
    await page.goto(`${harness.baseUrl}/incremental.html`, { waitUntil: 'networkidle' });
    await expect.poll(() => harness.pendingPageRequests).toBe(3);

    const target = page.locator('tr[data-file-key="repo-a::src/fixture.c"]');
    await expect(target.locator('.js-unanalyzed-count')).toHaveText('1');
    await expect(page.locator('#incremental-unanalyzed-total')).toHaveText('1');

    await page.locator('#repo-filter').selectOption('repo-a');
    await expect(page.locator('#filter-count')).toHaveText('已筛选出 201 / 401 个文件');
    await page.locator('.sort-button[data-sort-key="unanalyzed"]').click();
    await expect(target).toBeVisible();

    harness.setTaskPendingFiles(taskPendingFiles.map(file => ({
      ...file,
      unanalyzed: 0,
      pending_line_numbers: [],
    })));
    await page.waitForTimeout(2200);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(target.locator('.js-unanalyzed-count')).toHaveText('0', { timeout: 10000 });
    await expect(target.locator('.js-unanalyzed-count')).toHaveAttribute('data-sort-value', '0');
    await expect(page.locator('#incremental-unanalyzed-total')).toHaveText('0');
    await expect(page.locator('#filter-count')).toHaveText('已筛选出 201 / 401 个文件');
  } finally {
    await stopHarness(harness);
  }
});

test('real Chromium ignores a reordered stale pending snapshot response', async ({ page, browserName }) => {
  expect(browserName).toBe('chromium');
  const taskPendingFiles = [{
    repository_name: 'repo-a', file_path: 'src/fixture.c',
    unanalyzed: 1, pending_line_numbers: [2],
  }];
  const harness = await startHarness({
    taskPendingFiles, reorderPendingResponses: true,
  });
  try {
    await page.goto(`${harness.baseUrl}/incremental.html`, { waitUntil: 'domcontentloaded' });
    await expect.poll(() => harness.pendingPageRequests).toBe(1);
    const target = page.locator('tr[data-file-key="repo-a::src/fixture.c"]');
    await expect(target.locator('.js-unanalyzed-count')).toHaveText('1');

    harness.setTaskPendingFiles([{
      repository_name: 'repo-a', file_path: 'src/fixture.c',
      unanalyzed: 0, pending_line_numbers: [],
    }]);
    await page.waitForTimeout(2200);
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect.poll(() => harness.pendingPageRequests).toBe(2);
    await expect(target.locator('.js-unanalyzed-count')).toHaveText('0', { timeout: 10000 });

    // The delayed first response arrives after the new generation and must be
    // ignored instead of restoring the old server-rendered count.
    await page.waitForTimeout(1800);
    await expect(target.locator('.js-unanalyzed-count')).toHaveText('0');
    await expect(page.locator('#incremental-unanalyzed-total')).toHaveText('0');
  } finally {
    await stopHarness(harness);
  }
});
