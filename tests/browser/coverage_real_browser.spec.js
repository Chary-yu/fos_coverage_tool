const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const http = require('http');
const { URL } = require('url');

const ROOT = path.join(__dirname, '../..');
const CLIENT_JS = fs.readFileSync(path.join(ROOT, 'web/assets/js/coverage_enhance.js'), 'utf8');
const CLIENT_CSS = fs.readFileSync(path.join(ROOT, 'web/assets/css/coverage_enhance.css'), 'utf8');
const TASK_JS = fs.readFileSync(path.join(ROOT, 'web/assets/js/incremental_developer_tasks.js'), 'utf8');

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
  inheritanceLifecycle = false } = {}) {
  const layout = makeLayout(large);
  const requests = [];
  const failedRequests = [];
  let activeRequests = 0;
  let maxConcurrent = 0;
  let failureUsed = false;
  const savedReviewers = new Map();
  let currentTaskPendingLines = taskPendingLines;
  let inheritanceRejected = false;
  let inheritanceRevision = 1;
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
        <meta name="coverage-report-id" content="report_browser_fixture">
        <meta name="coverage-scan-id" content="1">
        <meta name="coverage-repository-name" content="">
        <meta name="coverage-file-path" content="src/fixture.c">
        <meta name="coverage-render-mode" content="lazy_collapse">
        <meta name="coverage-review-scope" content="full">
        <style>${CLIENT_CSS}</style><script src="/coverage_enhance.js"></script>
        </head><body><pre class="source"></pre></body></html>`, 'text/html');
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
        </main><script>${TASK_JS}</script></body></html>`, 'text/html');
    }
    if (pathname === '/coverage_enhance.js') return sendStatic(CLIENT_JS, 'text/javascript');
    if (pathname === '/coverage_enhance.css') return sendStatic(CLIENT_CSS, 'text/css');

    if (pathname === '/api/coverage/incremental/unanalyzed') {
      const pending = currentTaskPendingLines;
      if (pending === null) {
        return json(404, { status: 'error', message: 'task fixture disabled' });
      }
      return json(200, {
        status: 'success',
        data: {
          project_name: 'TaskFixture',
          data_version: 1,
          total_unanalyzed: pending.length,
          files: [{ file_path: 'src/fixture.c', unanalyzed: pending.length, pending_line_numbers: pending }],
        },
      });
    }

    if (pathname === '/api/coverage/code-layout') {
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
    setTaskPendingLines(lines) { currentTaskPendingLines = lines; },
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
