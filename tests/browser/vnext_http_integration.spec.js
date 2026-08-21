const { test, expect } = require('@playwright/test');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.join(__dirname, '../..');

async function startFixture() {
  const python = process.env.COVERAGE_PYTHON || 'python3';
  const child = spawn(python, ['tests/browser/vnext_http_fixture.py'], {
    cwd: ROOT,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let stdout = '';
  let stderr = '';
  let settled = false;
  const info = await new Promise((resolve, reject) => {
    const fail = error => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      stdout += chunk;
      const newline = stdout.indexOf('\n');
      if (newline < 0 || settled) return;
      const line = stdout.slice(0, newline).trim();
      try {
        const parsed = JSON.parse(line);
        settled = true;
        resolve(parsed);
      } catch (error) {
        fail(new Error(`VNext fixture emitted invalid startup JSON: ${line}`));
      }
    });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.once('error', fail);
    child.once('exit', (code, signal) => {
      if (!settled) {
        fail(new Error(
          `VNext fixture exited before startup (code=${code}, signal=${signal}): ${stderr}`
        ));
      }
    });
  });
  return { child, info, getStderr: () => stderr };
}

async function stopFixture(fixture) {
  if (!fixture || !fixture.child || fixture.child.killed) return;
  fixture.child.stdin.end();
  await new Promise(resolve => {
    const timer = setTimeout(() => {
      fixture.child.kill('SIGTERM');
      resolve();
    }, 10000);
    fixture.child.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

test('canonical frontend talks to the real VNext HTTP server', async ({ page }) => {
  const fixture = await startFixture();
  const apiRequests = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/coverage')) {
      apiRequests.push({
        method: request.method(),
        path: url.pathname,
        postData: request.postData() || '',
      });
    }
  });

  try {
    await page.goto(`${fixture.info.base_url}/src/http_fixture.c.gcov.html`, {
      waitUntil: 'networkidle',
    });
    await expect(page.locator('pre.source > .coverage-region-container')).toHaveCount(1);
    await expect(page.locator('#L120')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('.coverage-analysis-panel')).toHaveCount(120);

    const batchRequest = apiRequests.find(item => (
      item.method === 'POST' && item.path === '/api/coverage/code-lines/batch'
    ));
    expect(batchRequest).toBeTruthy();
    const batchPayload = JSON.parse(batchRequest.postData);
    expect(String(batchPayload.scan_id)).toBe(String(fixture.info.scan_id));
    expect(batchPayload.report_id).toBe(fixture.info.report_id);
    expect(batchPayload.repository_name).toBe('repo-a');
    expect(batchPayload.ranges).toEqual([{ start_line: 1, end_line: 120 }]);

    const panel = page.locator('.coverage-analysis-panel').first();
    await panel.locator('select[data-panel-action="status"]').selectOption('可覆盖');
    await panel.locator('input.reviewer-input').fill('http-reviewer');
    await panel.locator('textarea[data-panel-action="method"]').fill('unit-test');
    await panel.locator('button[data-panel-action="save"]').click();
    await expect(panel.locator('button[data-panel-action="save"]')).toHaveClass(/saved/, {
      timeout: 15000,
    });

    const saveRequest = apiRequests.find(item => (
      item.method === 'POST' && item.path === '/api/coverage/analysis'
    ));
    expect(saveRequest).toBeTruthy();
    const savePayload = JSON.parse(saveRequest.postData);
    expect(String(savePayload.scan_id)).toBe(String(fixture.info.scan_id));
    expect(savePayload.repository_name).toBe('repo-a');
    expect(savePayload.records[0]).toMatchObject({
      line_start: 1,
      line_end: 1,
      file_path: 'src/http_fixture.c',
      reviewer: 'http-reviewer',
      status: '可覆盖',
    });

    const savedLine = await page.evaluate(async identity => {
      const query = new URLSearchParams(identity);
      query.set('start_line', '1');
      query.set('end_line', '1');
      const response = await fetch(`/api/coverage/code-lines?${query.toString()}`);
      return response.json();
    }, {
      scan_id: fixture.info.scan_id,
      report_id: fixture.info.report_id,
      repository_name: 'repo-a',
      file_path: 'src/http_fixture.c',
    });
    expect(savedLine.lines[0].analysis).toMatchObject({
      status: '可覆盖',
      reviewer: 'browser-reviewer',
    });

    const runtimeMetrics = await page.evaluate(async () => {
      const response = await fetch('/api/coverage/metrics');
      return response.json();
    });
    expect(runtimeMetrics.runtime).toBe('vnext');
    expect(runtimeMetrics.code_detail.sidecar_store_count).toBe(1);
    expect(runtimeMetrics.code_detail.sidecar_decode_count).toBeGreaterThan(0);
    expect(runtimeMetrics.code_detail.overlay_db_queries).toBeGreaterThan(0);
    expect(runtimeMetrics.code_detail.overlay_db_rows).toBeGreaterThanOrEqual(1);

    expect(apiRequests.some(item => item.path === '/api/coverage/batch')).toBe(false);
    expect(apiRequests.some(item => item.path === '/api/coverage/details')).toBe(false);
  } finally {
    await stopFixture(fixture);
  }
});
