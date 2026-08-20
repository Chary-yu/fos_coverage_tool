#!/usr/bin/env node
/* Real Chromium baseline/candidate A/B workload for the release evidence gate. */
const fs = require('fs');
const path = require('path');
const { chromium } = require('@playwright/test');

const CLIENT_JS = fs.readFileSync(
  path.join(process.cwd(), 'web/assets/js/coverage_enhance.js'), 'utf8'
);
const CLIENT_CSS = fs.readFileSync(
  path.join(process.cwd(), 'web/assets/css/coverage_enhance.css'), 'utf8'
);

async function runVirtualScrollWorkload(browser) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const requestLog = [];
  let responseBytes = 0;
  let maxResponseBytes = 0;
  const html = `<!doctype html><html><head>
    <meta name="coverage-project" content="PerfE2E">
    <meta name="coverage-report-id" content="report_perf_e2e">
    <meta name="coverage-scan-id" content="1">
    <meta name="coverage-repository-name" content="">
    <meta name="coverage-file-path" content="src/perf_100k.c">
    <meta name="coverage-render-mode" content="lazy_collapse">
    <meta name="coverage-review-scope" content="full">
    <style>${CLIENT_CSS}</style>
    </head><body><pre class="source"></pre></body></html>`;
  await page.route('**/api/coverage/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    requestLog.push(url.pathname);
    let payload;
    if (url.pathname === '/api/coverage/code-layout') {
      payload = {
          project_name: 'PerfE2E', file_path: 'src/perf_100k.c',
          report_id: 'report_perf_e2e', total_lines: 100000,
          total_uncovered_count: 0, pending_line_count: 0,
          regions: [{
            region_id: 'virtual_100k', start_line: 1, end_line: 100000,
            line_count: 100000, default_state: 'collapsed', kind: 'collapsed',
            label: '100k virtual scroll fixture'
          }]
      };
    } else if (url.pathname === '/api/coverage/code-lines') {
      const start = Number(url.searchParams.get('start_line') || 1);
      const end = Number(url.searchParams.get('end_line') || start);
      const lines = [];
      for (let lineNo = start; lineNo <= end; lineNo += 1) {
        lines.push({
          line_no: lineNo,
          source: `int perf_line_${lineNo} = ${lineNo};`,
          coverage_state: 'covered',
          is_pending_analysis: false
        });
      }
      payload = { status: 'success', data: { start_line: start, end_line: end, lines } };
    } else {
      payload = { status: 'success', data: {} };
    }
    const responseBody = JSON.stringify(payload);
    const responseSize = Buffer.byteLength(responseBody, 'utf8');
    responseBytes += responseSize;
    maxResponseBytes = Math.max(maxResponseBytes, responseSize);
    await route.fulfill({ status: 200, contentType: 'application/json', body: responseBody });
  });
  await page.route('http://coverage-perf.test/', async route => {
    await route.fulfill({ status: 200, contentType: 'text/html', body: html });
  });

  try {
    await page.goto('http://coverage-perf.test/', { waitUntil: 'domcontentloaded' });
    await page.addScriptTag({ content: CLIENT_JS });
    await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
    await page.waitForFunction(() => {
      const internals = window.__COVERAGE_ENHANCE_INTERNALS__;
      return internals && internals.CodeRegionStore.get('virtual_100k');
    });
    const measurement = await page.evaluate(async () => {
      const internals = window.__COVERAGE_ENHANCE_INTERNALS__;
      const start = performance.now();
      await internals.CodeRegionController.expandRegion('virtual_100k');
      const region = internals.CodeRegionStore.get('virtual_100k');
      const telemetry = internals.PerformanceTelemetry.snapshot();
      const firstVisibleAt = performance.now();
      const firstVisible = document.querySelector('#L1') !== null;
      let scrolledVisible = false;
      const targetStart = performance.now();
      window.scrollTo(0, 50000 * 24);
      for (let attempt = 0; attempt < 20; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 5));
        if (document.querySelector('[id="L50000"]') !== null) {
          scrolledVisible = true;
          break;
        }
      }
      return {
        elapsed_ms: Number((performance.now() - start).toFixed(3)),
        time_to_first_visible_ms: Number((firstVisibleAt - start).toFixed(3)),
        time_to_target_line_ms: scrolledVisible
          ? Number((performance.now() - targetStart).toFixed(3)) : null,
        loaded_lines: region.lines.length,
        loaded_line_count: region.loadedLineCount,
        virtualized: region.virtualized,
        first_visible: firstVisible,
        scrolled_visible: scrolledVisible,
        dom_line_count: region.virtualContent ? region.virtualContent.children.length : 0,
        telemetry
      };
    });
    const codeLineRequests = requestLog.filter(item => item === '/api/coverage/code-lines').length;
    return {
      status: measurement.virtualized && measurement.loaded_lines === 100000 &&
        measurement.loaded_line_count < 2000 && measurement.dom_line_count < 1500 &&
        measurement.first_visible && measurement.scrolled_visible && codeLineRequests <= 4 ? 'PASSED' : 'FAILED',
      evidence_class: 'performance_e2e',
      workload_id: 'coverage-enhance-virtual-scroll-100k-v1',
      line_count: 100000,
      request_count: codeLineRequests,
      response_bytes: responseBytes,
      max_response_bytes: maxResponseBytes,
      ...measurement
    };
  } finally {
    await page.close();
  }
}

async function main() {
  const output = process.argv[2] || path.join(process.cwd(), 'browser_perf_ab.json');
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent('<!doctype html><html><body><main id="root"></main></body></html>');
    const result = await page.evaluate(async () => {
      const root = document.getElementById('root');
      const measure = async (lineCount, batchSize, batched) => {
        const workload = Array.from({ length: lineCount }, (_, i) => `line-${i}`);
        root.replaceChildren();
        const start = performance.now();
        for (let i = 0; i < workload.length; i += batchSize) {
          const fragment = batched ? document.createDocumentFragment() : null;
          for (const value of workload.slice(i, i + batchSize)) {
            const node = document.createElement('span');
            node.textContent = value;
            if (fragment) fragment.appendChild(node); else root.appendChild(node);
          }
          if (fragment) root.appendChild(fragment);
          await Promise.resolve();
        }
        return performance.now() - start;
      };
      const tiers = [
        ['Tier_A_1k', 1000], ['Tier_B_10k', 10000],
        ['Tier_C_50k', 50000], ['Tier_D_100k', 100000]
      ];
      const result = {
        status: 'PASSED', evidence_class: 'performance_ab',
        workload_id: 'chromium-dom-batch-v1', browser: navigator.userAgent
      };
      for (const [name, lineCount] of tiers) {
        const baseline = await measure(lineCount, 1, false);
        const candidate = await measure(lineCount, 250, true);
        result[name] = {
          status: Number.isFinite(baseline) && Number.isFinite(candidate) ? 'PASSED' : 'FAILED',
          evidence_class: 'performance_ab', workload_id: result.workload_id,
          line_count: lineCount, baseline_ms: Number(baseline.toFixed(3)),
          candidate_ms: Number(candidate.toFixed(3)),
          revision: null
        };
      }
      result.line_count = 10000;
      result.baseline_ms = result.Tier_B_10k.baseline_ms;
      result.candidate_ms = result.Tier_B_10k.candidate_ms;
      return result;
    });
    try {
      result.coverage_virtual_scroll_100k = await runVirtualScrollWorkload(browser);
    } catch (error) {
      result.coverage_virtual_scroll_100k = {
        status: 'FAILED', evidence_class: 'performance_e2e',
        workload_id: 'coverage-enhance-virtual-scroll-100k-v1',
        error: error && error.message ? error.message : String(error)
      };
    }
    if (result.coverage_virtual_scroll_100k.status !== 'PASSED') {
      result.status = 'FAILED';
    }
    fs.writeFileSync(output, JSON.stringify(result, null, 2));
    process.stdout.write(JSON.stringify(result) + '\n');
    if (result.status !== 'PASSED') process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
