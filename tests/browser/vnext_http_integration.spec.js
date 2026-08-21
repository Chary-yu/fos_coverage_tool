const { test, expect } = require('@playwright/test');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.join(__dirname, '../..');

async function startFixture(envOverrides = {}) {
  const python = process.env.COVERAGE_PYTHON || 'python3';
  const child = spawn(python, ['tests/browser/vnext_http_fixture.py'], {
    cwd: ROOT,
    env: { ...process.env, ...envOverrides },
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

function currentRevision() {
  if (process.env.GITHUB_SHA) return process.env.GITHUB_SHA;
  try {
    return execFileSync('git', ['rev-parse', 'HEAD'], {
      cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch (_) {
    return '';
  }
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

test('instrumented real VNext HTTP fixture records 100k cross-layer workload', async ({ page, browserName }) => {
  test.setTimeout(120000);
  expect(browserName).toBe('chromium');
  const outputPath = process.env.COVERAGE_CROSS_LAYER_OUTPUT || '';
  const fixture = await startFixture({ COVERAGE_HTTP_FIXTURE_LINES: '100000' });
  const apiRequests = [];
  const responsePromises = [];
  let responseBytes = 0;
  let maxResponseBytes = 0;
  page.on('request', request => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith('/api/coverage')) return;
    apiRequests.push({ method: request.method(), path: url.pathname });
  });
  page.on('response', response => {
    const url = new URL(response.url());
    if (!url.pathname.startsWith('/api/coverage')) return;
    responsePromises.push(response.body().then(body => {
      const size = body.length;
      responseBytes += size;
      maxResponseBytes = Math.max(maxResponseBytes, size);
    }).catch(() => {}));
  });

  try {
    await page.goto(`${fixture.info.base_url}/src/http_fixture.c.gcov.html`, {
      waitUntil: 'domcontentloaded',
    });
    const seedAnalysis = await page.evaluate(async identity => {
      const response = await fetch('/api/coverage/analysis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_name: 'HttpFixture',
          scan_id: identity.scan_id,
          records: [{
            line_number: 1,
            line_start: 1,
            line_end: 1,
            file_path: 'src/http_fixture.c',
            repository_name: 'repo-a',
            status: '可覆盖',
            coverage_method: 'fixture-seed',
          }],
        }),
      });
      return { ok: response.ok, status: response.status };
    }, { scan_id: fixture.info.scan_id });
    expect(seedAnalysis.ok).toBe(true);
    const workload = await page.evaluate(async identity => {
      const started = performance.now();
      await new Promise(resolve => {
        const check = () => {
          const internals = window.__COVERAGE_ENHANCE_INTERNALS__;
          if (internals && internals.CodeRegionStore.getAll().some(item => (
            Number(item.startLine) === 1 && Number(item.endLine) === 100000
          ))) return resolve();
          window.setTimeout(check, 10);
        };
        check();
      });
      const internals = window.__COVERAGE_ENHANCE_INTERNALS__;
      const region = internals.CodeRegionStore.getAll().find(item => (
        Number(item.startLine) === 1 && Number(item.endLine) === 100000
      ));
      await internals.CodeRegionController.expandRegion(region.id);
      const firstVisibleAt = performance.now();
      const firstVisible = Boolean(document.querySelector('#L1'));
      const expandDurations = [firstVisibleAt - started];
      const sweep = [];
      const targets = [25000, 50000, 75000, 100000, 1];
      for (const target of targets) {
        const targetStarted = performance.now();
        const bounds = internals.CodeRegionController.virtualWindowBounds(region, target - 1);
        await internals.CodeRegionLoader.ensureVirtualWindow(
          internals.CodeRegionController.filePath, region, bounds.start, bounds.end
        );
        internals.CodeRegionController.renderVirtualWindow(region, target - 1);
        const targetNode = document.querySelector(`[id="L${target}"]`);
        expandDurations.push(performance.now() - targetStarted);
        sweep.push({
          target_line: target,
          visible: Boolean(targetNode),
          resident_js_lines: region.loadedLineCount,
          dom_line_count: region.virtualContent ? region.virtualContent.children.length : 0,
        });
      }
      const sortedDurations = expandDurations.slice().sort((a, b) => a - b);
      const p95Index = Math.min(sortedDurations.length - 1, Math.ceil(sortedDurations.length * 0.95) - 1);
      const telemetry = internals.PerformanceTelemetry.snapshot();
      return {
        status: firstVisible && sweep.every(item => item.visible) &&
          region.virtualized && region.loadedLineCount <= 8000 &&
          telemetry.max_dom_lines < 1500 ? 'PASSED' : 'FAILED',
        workload_id: 'vnext-http-virtual-scroll-100k-v1',
        line_count: Number(identity.line_count),
        logical_line_count: region.lineCount,
        time_to_first_visible_ms: Number((firstVisibleAt - started).toFixed(3)),
        time_to_target_line_ms: Number((performance.now() - started).toFixed(3)),
        p95_expand_ms: Number(sortedDurations[p95Index].toFixed(3)),
        resident_js_lines: region.loadedLineCount,
        resident_js_lines_peak: Math.max(...sweep.map(item => item.resident_js_lines), region.loadedLineCount),
        dom_line_count: region.virtualContent ? region.virtualContent.children.length : 0,
        sweep,
        telemetry,
        telemetry_after_scroll: internals.PerformanceTelemetry.snapshot(),
      };
    }, { line_count: 100000 });

    const responseMetrics = await page.evaluate(async () => {
      const response = await fetch('/api/coverage/metrics');
      return response.json();
    });
    await Promise.all(responsePromises);
    const telemetry = workload.telemetry_after_scroll || workload.telemetry || {};
    const codeLineRequests = apiRequests.filter(item => item.path === '/api/coverage/code-lines').length;
    const crossLayer = {
      ...workload,
      status: workload.status === 'PASSED' && responseMetrics.process &&
        Number(responseMetrics.process.peak_rss_bytes) > 0 &&
        Number(responseMetrics.code_detail.overlay_db_queries) > 0 &&
        Number(responseMetrics.code_detail.sidecar_decode_count) > 0 ? 'PASSED' : 'FAILED',
      evidence_class: 'cross_layer_performance_fixture',
      synthetic: true,
      release_eligible: false,
      comparison_type: 'single_runtime_fixture',
      candidate_revision: currentRevision(),
      environment_identity: {
        browser: await page.evaluate(() => navigator.userAgent),
        node: process.version,
        platform: process.platform,
        arch: process.arch,
        ci_run: process.env.GITHUB_RUN_ID || '',
        fixture_lines: 100000,
      },
      request_count: codeLineRequests,
      response_bytes: responseBytes,
      max_response_bytes: maxResponseBytes,
      api_requests: telemetry.api_requests,
      network_chunks: telemetry.network_chunks,
      network_lines: telemetry.network_lines,
      max_dom_lines: telemetry.max_dom_lines,
      overlay_db_queries: Number(responseMetrics.code_detail.overlay_db_queries || 0),
      overlay_db_rows: Number(responseMetrics.code_detail.overlay_db_rows || 0),
      sidecar_decode_count: Number(responseMetrics.code_detail.sidecar_decode_count || 0),
      peak_rss_bytes: Number((responseMetrics.process || {}).peak_rss_bytes || 0),
      server_metrics: responseMetrics,
    };
    if (outputPath) {
      fs.mkdirSync(path.dirname(outputPath), { recursive: true });
      fs.writeFileSync(outputPath, JSON.stringify({
        status: crossLayer.status,
        evidence_class: crossLayer.evidence_class,
        synthetic: true,
        release_eligible: false,
        comparison_type: crossLayer.comparison_type,
        candidate_revision: crossLayer.candidate_revision,
        environment_identity: crossLayer.environment_identity,
        coverage_virtual_scroll_100k: crossLayer,
      }, null, 2));
    }
    expect(crossLayer.status).toBe('PASSED');
    expect(crossLayer.logical_line_count).toBe(100000);
    expect(crossLayer.resident_js_lines_peak).toBeLessThanOrEqual(8000);
    expect(crossLayer.sweep.every(item => (
      item.resident_js_lines <= 8000 && item.dom_line_count < 1500
    ))).toBe(true);
    expect(crossLayer.sweep[crossLayer.sweep.length - 1].target_line).toBe(1);
    expect(crossLayer.max_dom_lines).toBeLessThan(1500);
  } finally {
    await stopFixture(fixture);
  }
});
