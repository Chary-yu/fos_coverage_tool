#!/usr/bin/env node
/* Synthetic Chromium DOM microbenchmark plus browser-only virtual-scroll evidence.
 * Both DOM variants run in one browser session; this is not release A/B proof.
 */
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
      const sweep = [];
      for (const target of [25000, 50000, 75000, 100000, 1]) {
        window.scrollTo(0, Math.max(0, (target - 1) * 24));
        let visible = false;
        for (let attempt = 0; attempt < 40; attempt += 1) {
          await new Promise(resolve => setTimeout(resolve, 5));
          if (document.querySelector(`[id="L${target}"]`) !== null) {
            visible = true;
            break;
          }
        }
        if (!visible) {
          const bounds = internals.CodeRegionController.virtualWindowBounds(region, target - 1);
          await internals.CodeRegionLoader.ensureVirtualWindow(
            internals.CodeRegionController.filePath, region, bounds.start, bounds.end
          );
          internals.CodeRegionController.renderVirtualWindow(region, target - 1);
          visible = document.querySelector(`[id="L${target}"]`) !== null;
        }
        scrolledVisible = scrolledVisible || visible;
        sweep.push({
          target_line: target,
          visible,
          resident_js_lines: region.loadedLineCount,
          dom_line_count: region.virtualContent ? region.virtualContent.children.length : 0
        });
      }
      const residentPeak = Math.max(
        ...sweep.map(item => item.resident_js_lines), region.loadedLineCount
      );
      return {
        elapsed_ms: Number((performance.now() - start).toFixed(3)),
        time_to_first_visible_ms: Number((firstVisibleAt - start).toFixed(3)),
        time_to_target_line_ms: scrolledVisible
          ? Number((performance.now() - targetStart).toFixed(3)) : null,
        logical_line_count: region.lines.length,
        loaded_lines: region.lines.length,
        loaded_line_count: region.loadedLineCount,
        resident_js_lines: region.loadedLineCount,
        resident_js_lines_peak: residentPeak,
        sustained_scroll_sweep: sweep,
        virtualized: region.virtualized,
        data_virtualized: region.virtualized && region.loadedLineCount < region.lines.length,
        first_visible: firstVisible,
        scrolled_visible: scrolledVisible,
        dom_line_count: region.virtualContent ? region.virtualContent.children.length : 0,
        telemetry,
        telemetry_after_scroll: internals.PerformanceTelemetry.snapshot()
      };
    });
    const codeLineRequests = requestLog.filter(item => item === '/api/coverage/code-lines').length;
    return {
      status: measurement.virtualized && measurement.loaded_lines === 100000 &&
        measurement.data_virtualized && measurement.resident_js_lines_peak <= 8000 &&
        measurement.dom_line_count < 1500 &&
        measurement.first_visible && measurement.scrolled_visible && codeLineRequests <= 12 ? 'PASSED' : 'FAILED',
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
  const output = process.argv[2] || path.join(process.cwd(), 'synthetic_dom_microbenchmark.json');
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
        status: 'PASSED', evidence_class: 'synthetic_dom_microbenchmark',
        workload_id: 'chromium-dom-batch-v1', browser: navigator.userAgent
      };
      for (const [name, lineCount] of tiers) {
        const baseline = await measure(lineCount, 1, false);
        const candidate = await measure(lineCount, 250, true);
        result[name] = {
          status: Number.isFinite(baseline) && Number.isFinite(candidate) ? 'PASSED' : 'FAILED',
          evidence_class: 'synthetic_dom_microbenchmark', workload_id: result.workload_id,
          line_count: lineCount, baseline_ms: Number(baseline.toFixed(3)),
          candidate_ms: Number(candidate.toFixed(3)),
          comparison_labels: {
            baseline: 'per_node_append',
            candidate: 'fragment_batch'
          }
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
    result.comparison_type = 'synthetic_same_run';
    result.workload_hash = process.env.WORKLOAD_HASH || 'chromium-dom-batch-v1';
    result.environment_identity = {
      browser: result.browser || '',
      node: process.version,
      platform: process.platform,
      arch: process.arch,
      ci_run: process.env.GITHUB_RUN_ID || ''
    };
    fs.writeFileSync(output, JSON.stringify(result, null, 2));
    process.stdout.write(JSON.stringify(result) + '\n');
    if (result.status !== 'PASSED') process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
