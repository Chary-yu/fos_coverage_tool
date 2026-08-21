#!/usr/bin/env node
/*
 * Collect Gate E evidence from a real Candidate HTTP endpoint.
 *
 * This command is intentionally separate from tests/browser fixtures and from
 * the synthetic DOM benchmark.  It proves the deployed release identity first
 * and only then runs the 100k virtual-scroll workload.  A report is written to
 * --output; --evidence-output wraps that report with the provenance and SHA256
 * fields consumed by gate_matrix.py.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('@playwright/test');

const WORKLOAD_ID = 'vnext-real-http-virtual-scroll-100k-v1';
const API_PREFIX = '/api/coverage';
const REQUIRED_LINE_COUNT = 100000;

function usage() {
  return [
    'Usage:',
    '  node scripts/diagnostics/real_browser_evidence.js',
    '    --url <candidate-html-url>',
    '    --expected-revision <exact-40-char-sha>',
    '    --output <workload-json>',
    '    [--evidence-output <gate-e-performance-json>]',
    '    [--browser-evidence-output <gate-e-browser-json>]',
    '    [--header <name=value>] ...',
    '    [--timeout-ms <milliseconds>]',
    '',
    'The target must be a real HTTP Candidate page. Headers are accepted for',
    'a trusted reverse proxy, but header values are never written to evidence.'
  ].join('\n');
}

function parseArgs(argv) {
  const args = { headers: [] };
  const valueArgs = new Set([
    '--url', '--expected-revision', '--output', '--evidence-output',
    '--browser-evidence-output', '--header', '--timeout-ms'
  ]);
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--help' || arg === '-h') {
      args.help = true;
      continue;
    }
    if (!valueArgs.has(arg)) throw new Error(`unknown argument: ${arg}`);
    if (index + 1 >= argv.length || argv[index + 1].startsWith('--')) {
      throw new Error(`missing value for ${arg}`);
    }
    const value = argv[index + 1];
    if (arg === '--header') {
      const separator = value.indexOf('=');
      if (separator <= 0) throw new Error('--header must use Name=Value');
      args.headers.push({
        name: value.slice(0, separator).trim(),
        value: value.slice(separator + 1),
      });
    } else {
      args[arg.slice(2).replace(/-/g, '_')] = value;
    }
    index += 1;
  }
  return args;
}

function isSha(value) {
  const normalized = String(value || '');
  return /^[0-9a-f]{40}$/i.test(normalized) && !/^0{40}$/i.test(normalized);
}

function now() {
  return new Date().toISOString();
}

function sha256File(filePath) {
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex');
}

function writeJson(filePath, payload) {
  const absolutePath = path.resolve(filePath);
  fs.mkdirSync(path.dirname(absolutePath), { recursive: true });
  const temporaryPath = `${absolutePath}.tmp-${process.pid}`;
  fs.writeFileSync(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`);
  fs.renameSync(temporaryPath, absolutePath);
  return absolutePath;
}

function safeUrl(raw) {
  try {
    const value = new URL(raw);
    return `${value.origin}${value.pathname}`;
  } catch (_) {
    return '<invalid-url>';
  }
}

function safeCommand(argv) {
  const result = [];
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--header' && index + 1 < argv.length) {
      result.push('--header', '<redacted>');
      index += 1;
    } else if (argv[index] === '--url' && index + 1 < argv.length) {
      result.push('--url', safeUrl(argv[index + 1]));
      index += 1;
    } else {
      result.push(argv[index]);
    }
  }
  return `node scripts/diagnostics/real_browser_evidence.js ${result.join(' ')}`;
}

function hostIdentity() {
  return {
    hostname: os.hostname(),
    platform: os.platform(),
    release: os.release(),
    arch: os.arch(),
    node: process.version,
  };
}

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function apiPath(urlValue) {
  try {
    return new URL(urlValue).pathname;
  } catch (_) {
    return '';
  }
}

function normalizeRelease(payload) {
  if (!payload || typeof payload !== 'object') return {};
  if (payload.release && typeof payload.release === 'object') return payload.release;
  if (payload.data && payload.data.release && typeof payload.data.release === 'object') {
    return payload.data.release;
  }
  return payload;
}

async function fetchJson(page, urlPath) {
  return page.evaluate(async pathValue => {
    const response = await fetch(pathValue, { cache: 'no-store' });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    return { ok: response.ok, status: response.status, payload };
  }, urlPath);
}

function metaSnapshot(page) {
  return page.evaluate(() => {
    const result = {};
    document.querySelectorAll('meta[name^="coverage-"]').forEach(node => {
      const name = node.getAttribute('name') || '';
      if (name) result[name] = node.getAttribute('content') || '';
    });
    return result;
  });
}

async function runWorkload(page, lineCount) {
  return page.evaluate(async expectedLineCount => {
    const internals = window.__COVERAGE_ENHANCE_INTERNALS__;
    if (!internals) throw new Error('canonical frontend internals are unavailable');
    const regions = internals.CodeRegionStore.getAll();
    const region = regions.find(item => Number(item.lineCount) === expectedLineCount) ||
      regions.find(item => Number(item.endLine) - Number(item.startLine) + 1 === expectedLineCount);
    if (!region) {
      throw new Error(`page has no ${expectedLineCount}-line code region`);
    }

    const started = performance.now();
    await internals.CodeRegionController.expandRegion(region.id);
    const firstVisibleAt = performance.now();
    const firstVisible = Boolean(document.querySelector('#L1'));
    const sweep = [];
    const expandDurations = [firstVisibleAt - started];
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
        resident_js_lines: Number(region.loadedLineCount || 0),
        dom_line_count: region.virtualContent ? region.virtualContent.children.length : 0,
      });
    }
    const sortedDurations = expandDurations.slice().sort((left, right) => left - right);
    const p95Index = Math.min(
      sortedDurations.length - 1,
      Math.ceil(sortedDurations.length * 0.95) - 1
    );
    const telemetry = internals.PerformanceTelemetry.snapshot();
    const residentPeak = Math.max(
      Number(region.loadedLineCount || 0),
      ...sweep.map(item => Number(item.resident_js_lines || 0))
    );
    const maxDomLines = Math.max(
      Number(telemetry.max_dom_lines || 0),
      ...sweep.map(item => Number(item.dom_line_count || 0))
    );
    const status = firstVisible && region.virtualized &&
      Number(region.loadedLineCount || 0) <= 8000 &&
      residentPeak <= 8000 && maxDomLines < 1500 &&
      sweep.every(item => item.visible);
    return {
      status: status ? 'PASSED' : 'FAILED',
      evidence_class: 'real_http_chromium_performance',
      workload_id: 'vnext-real-http-virtual-scroll-100k-v1',
      line_count: Number(expectedLineCount),
      logical_line_count: Number(region.lineCount || 0),
      virtualized: Boolean(region.virtualized),
      time_to_first_visible_ms: Number((firstVisibleAt - started).toFixed(3)),
      time_to_target_line_ms: Number((performance.now() - started).toFixed(3)),
      p95_expand_ms: Number(sortedDurations[p95Index].toFixed(3)),
      resident_js_lines: Number(region.loadedLineCount || 0),
      resident_js_lines_peak: residentPeak,
      dom_line_count: region.virtualContent ? region.virtualContent.children.length : 0,
      max_dom_lines: maxDomLines,
      sweep,
      telemetry,
      telemetry_after_scroll: internals.PerformanceTelemetry.snapshot(),
    };
  }, lineCount);
}

function buildFailure(args, startedAt, message, details = {}) {
  const finishedAt = now();
  return {
    schema_version: 1,
    status: 'INCOMPLETE',
    evidence_class: 'real_http_chromium_performance',
    gate: 'gate-e',
    synthetic: false,
    release_eligible: false,
    candidate_revision: args.expected_revision || '',
    release_identity: {},
    host_identity: hostIdentity(),
    command_or_action: safeCommand(process.argv.slice(2)),
    started_at: startedAt,
    finished_at: finishedAt,
    exit_code: 1,
    page_url: safeUrl(args.url || ''),
    violations: [message],
    ...details,
  };
}

function buildEnvelope(report, reportPath, args) {
  const absoluteReport = path.resolve(reportPath);
  return {
    ...report,
    evidence_id: 'gate-e-real-browser',
    artifact_path: absoluteReport,
    artifact_sha256: sha256File(absoluteReport),
    source_inputs_sha256: [],
    report_artifact_path: absoluteReport,
    report_artifact_sha256: sha256File(absoluteReport),
    command_or_action: safeCommand(process.argv.slice(2)),
    page_url: safeUrl(args.url || ''),
  };
}

function browserEvidenceReport(report) {
  const browserStatus = report.browser_status === 'PASSED' ? 'PASSED' : report.status;
  const workload = report.coverage_virtual_scroll_100k || {};
  return {
    ...report,
    status: browserStatus,
    evidence_class: 'real_http_chromium_browser',
    release_eligible: browserStatus === 'PASSED',
    exit_code: browserStatus === 'PASSED' ? 0 : 1,
    coverage_virtual_scroll_100k: {
      ...workload,
      status: workload.workload_status || workload.status || 'INCOMPLETE',
    },
  };
}

async function collect(args, startedAt) {
  const requestLog = [];
  const failedRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  const responsePromises = [];
  let responseBytes = 0;
  let maxResponseBytes = 0;
  let browser;
  let report;

  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: Object.fromEntries(args.headers.map(item => [item.name, item.value])),
    });
    const page = await context.newPage();
    page.on('request', request => {
      const pathname = apiPath(request.url());
      if (!pathname.startsWith(API_PREFIX)) return;
      requestLog.push({ method: request.method(), path: pathname });
    });
    page.on('response', response => {
      const pathname = apiPath(response.url());
      if (!pathname.startsWith(API_PREFIX)) return;
      responsePromises.push(response.body().then(body => {
        const size = body.length;
        responseBytes += size;
        maxResponseBytes = Math.max(maxResponseBytes, size);
      }).catch(() => {}));
    });
    page.on('requestfailed', request => {
      if (apiPath(request.url()).startsWith(API_PREFIX)) {
        failedRequests.push({ method: request.method(), path: apiPath(request.url()) });
      }
    });
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    page.on('pageerror', error => pageErrors.push(String(error && error.message || error)));

    const timeout = Number(args.timeout_ms || 120000);
    await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout });
    await page.locator('pre.source').waitFor({ state: 'attached', timeout });

    const releaseResponse = await fetchJson(page, `${API_PREFIX}/release`);
    const releaseIdentity = normalizeRelease(releaseResponse.payload);
    if (!releaseResponse.ok || !isSha(releaseIdentity.commit_sha) ||
        releaseIdentity.commit_sha !== args.expected_revision) {
      throw new Error(
        `release identity mismatch: expected ${args.expected_revision}, observed ${releaseIdentity.commit_sha || '<missing>'}`
      );
    }

    await page.waitForFunction(() => Boolean(window.__COVERAGE_ENHANCE_INTERNALS__), null, { timeout });
    await page.waitForFunction(expected => {
      const internals = window.__COVERAGE_ENHANCE_INTERNALS__;
      return Boolean(internals && internals.CodeRegionStore.getAll().some(region => (
        Number(region.lineCount) === expected ||
        Number(region.endLine) - Number(region.startLine) + 1 === expected
      )));
    }, REQUIRED_LINE_COUNT, { timeout });

    const metadata = await metaSnapshot(page);
    const workload = await runWorkload(page, REQUIRED_LINE_COUNT);
    const metricsResponse = await fetchJson(page, `${API_PREFIX}/metrics`);
    await Promise.all(responsePromises);
    const serverMetrics = metricsResponse.payload || {};
    const codeDetailMetrics = serverMetrics.code_detail || {};
    const processMetrics = serverMetrics.process || {};
    const telemetry = workload.telemetry_after_scroll || workload.telemetry || {};
    const apiPaths = requestLog.map(item => item.path);
    const legacyApiRequests = apiPaths.filter(item => (
      item === '/api/coverage/batch' || item === '/api/coverage/details' ||
      item === '/api/coverage/layout'
    ));
    const crossLayerReady = metricsResponse.ok &&
      finite(Number(codeDetailMetrics.overlay_db_queries)) &&
      finite(Number(codeDetailMetrics.overlay_db_rows)) &&
      finite(Number(codeDetailMetrics.sidecar_decode_count)) &&
      finite(Number(processMetrics.peak_rss_bytes)) &&
      Number(processMetrics.peak_rss_bytes) > 0 &&
      Number(codeDetailMetrics.overlay_db_queries) > 0 &&
      Number(codeDetailMetrics.sidecar_decode_count) > 0 &&
      finite(Number(workload.p95_expand_ms));
    const browserStatus = workload.status === 'PASSED' &&
      releaseIdentity.commit_sha === args.expected_revision &&
      failedRequests.length === 0 && consoleErrors.length === 0 &&
      pageErrors.length === 0 && legacyApiRequests.length === 0;
    const crossLayer = {
      ...workload,
      workload_status: workload.status,
      status: browserStatus && crossLayerReady ? 'PASSED' : 'FAILED',
      evidence_class: 'real_http_chromium_performance',
      synthetic: false,
      release_eligible: browserStatus && crossLayerReady,
      comparison_type: 'single_live_candidate',
      candidate_revision: args.expected_revision,
      environment_identity: {
        browser: await page.evaluate(() => navigator.userAgent),
        browser_name: 'chromium',
        node: process.version,
        platform: process.platform,
        arch: process.arch,
      },
      request_count: requestLog.filter(item => item.path === `${API_PREFIX}/code-lines`).length,
      response_bytes: responseBytes,
      max_response_bytes: maxResponseBytes,
      api_requests: Number(telemetry.api_requests || 0),
      network_chunks: Number(telemetry.network_chunks || 0),
      network_lines: Number(telemetry.network_lines || 0),
      max_dom_lines: Number(workload.max_dom_lines || telemetry.max_dom_lines || 0),
      overlay_db_queries: Number(codeDetailMetrics.overlay_db_queries || 0),
      overlay_db_rows: Number(codeDetailMetrics.overlay_db_rows || 0),
      sidecar_decode_count: Number(codeDetailMetrics.sidecar_decode_count || 0),
      peak_rss_bytes: Number(processMetrics.peak_rss_bytes || 0),
      server_metrics: serverMetrics,
    };
    report = {
      schema_version: 1,
      status: browserStatus && crossLayerReady ? 'PASSED' : 'FAILED',
      evidence_class: 'real_http_chromium_performance',
      gate: 'gate-e',
      synthetic: false,
      release_eligible: browserStatus && crossLayerReady,
      candidate_revision: args.expected_revision,
      release_identity: releaseIdentity,
      host_identity: hostIdentity(),
      command_or_action: safeCommand(process.argv.slice(2)),
      started_at: startedAt,
      finished_at: now(),
      exit_code: browserStatus && crossLayerReady ? 0 : 1,
      page_url: safeUrl(args.url),
      page_metadata: metadata,
      browser_status: browserStatus ? 'PASSED' : 'FAILED',
      request_log: requestLog,
      failed_requests: failedRequests,
      console_errors: consoleErrors,
      page_errors: pageErrors,
      legacy_api_requests: legacyApiRequests,
      browser_functional: {
        status: browserStatus ? 'PASSED' : 'FAILED',
        release_endpoint: releaseResponse.status,
        metrics_endpoint: metricsResponse.status,
        request_count: requestLog.length,
      },
      coverage_virtual_scroll_100k: crossLayer,
    };
    return report;
  } catch (error) {
    return buildFailure(args, startedAt, error && error.message ? error.message : String(error), {
      request_log: requestLog,
      failed_requests: failedRequests,
      console_errors: consoleErrors,
      page_errors: pageErrors,
      coverage_virtual_scroll_100k: {
        status: 'INCOMPLETE',
        evidence_class: 'real_http_chromium_performance',
        workload_id: WORKLOAD_ID,
        line_count: REQUIRED_LINE_COUNT,
        request_count: requestLog.filter(item => item.path === `${API_PREFIX}/code-lines`).length,
        response_bytes: responseBytes,
        max_response_bytes: maxResponseBytes,
      },
    });
  } finally {
    if (browser) await browser.close();
  }
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n${usage()}\n`);
    process.exitCode = 2;
    return;
  }
  if (args.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }
  const required = ['url', 'expected_revision', 'output'];
  const missing = required.filter(name => !args[name]);
  if (missing.length) {
    process.stderr.write(`missing required arguments: ${missing.join(', ')}\n${usage()}\n`);
    process.exitCode = 2;
    return;
  }
  if (!isSha(args.expected_revision)) {
    process.stderr.write('--expected-revision must be an exact 40-character commit SHA\n');
    process.exitCode = 2;
    return;
  }
  let target;
  try {
    target = new URL(args.url);
  } catch (_) {
    process.stderr.write('--url must be an absolute HTTP(S) URL\n');
    process.exitCode = 2;
    return;
  }
  if (!['http:', 'https:'].includes(target.protocol)) {
    process.stderr.write('--url must use HTTP or HTTPS\n');
    process.exitCode = 2;
    return;
  }
  if (args.timeout_ms !== undefined &&
      (!Number.isFinite(Number(args.timeout_ms)) || Number(args.timeout_ms) < 1000)) {
    process.stderr.write('--timeout-ms must be at least 1000\n');
    process.exitCode = 2;
    return;
  }

  const startedAt = now();
  let report;
  try {
    report = await collect(args, startedAt);
  } catch (error) {
    report = buildFailure(args, startedAt, error && error.message ? error.message : String(error));
  }
  const reportPath = writeJson(args.output, report);
  const evidencePath = args.evidence_output || `${reportPath}.evidence.json`;
  const envelope = buildEnvelope(report, reportPath, args);
  writeJson(evidencePath, envelope);
  if (args.browser_evidence_output) {
    writeJson(
      args.browser_evidence_output,
      buildEnvelope(browserEvidenceReport(report), reportPath, args)
    );
  }
  process.stdout.write(JSON.stringify({
    status: report.status,
    report_path: reportPath,
    evidence_path: path.resolve(evidencePath),
    candidate_revision: report.candidate_revision,
    release_eligible: report.release_eligible,
  }, null, 2) + '\n');
  if (report.status !== 'PASSED') process.exitCode = 1;
}

main().catch(error => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});
