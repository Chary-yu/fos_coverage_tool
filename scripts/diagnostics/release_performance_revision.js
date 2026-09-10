#!/usr/bin/env node
/*
 * Produce one release_performance_revision artifact from one exact Git checkout.
 *
 * This is deliberately a single-revision producer.  It never labels one DOM
 * implementation "baseline" and another "candidate" in the same process.
 * The same harness must be run independently against the pinned baseline and
 * Candidate source roots; release_performance_ab.js performs the later join.
 *
 * The workload uses real Chromium plus the exact checkout's production
 * coverage_enhance.js/CSS against a deterministic in-browser HTTP fixture.
 * It is performance-only evidence, not proof of the deployed Candidate HTTP
 * path; the air-gapped operator browser observation supplies that separate
 * release fact.
 */
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const { chromium } = require('@playwright/test');

const WORKLOAD_ID = 'coverage-release-browser-v1';
const TIERS = [
  ['Tier_A_1k', 1000],
  ['Tier_B_10k', 10000],
  ['Tier_C_50k', 50000],
  ['Tier_D_100k', 100000]
];
const VIEWPORT = { width: 1280, height: 800 };
const WORKLOAD_CONTRACT = {
  schema_version: 1,
  workload_id: WORKLOAD_ID,
  fixture: 'deterministic-code-detail-http-v1',
  production_assets: [
    'web/assets/js/coverage_enhance.js',
    'web/assets/css/coverage_enhance.css'
  ],
  tiers: TIERS,
  viewport: VIEWPORT,
  virtual_scroll_targets_100k: [25000, 50000, 75000, 100000, 1],
  resident_js_line_budget_100k: 8000,
  dom_line_budget_100k: 1500,
  code_line_request_budget_100k: 12
};

function usage() {
  return [
    'Usage:',
    '  node scripts/diagnostics/release_performance_revision.js',
    '    --source-root <exact-checkout>',
    '    --expected-revision <40-hex-sha>',
    '    --output <json>',
    '    [--expected-tree <40-hex-tree>]',
    '',
    'The source checkout must be clean and exactly match --expected-revision.'
  ].join('\n');
}

function parseArgs(argv) {
  const args = {};
  const allowed = new Set([
    '--source-root', '--expected-revision', '--expected-tree', '--output'
  ]);
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!allowed.has(key)) throw new Error(`unknown argument: ${key}`);
    if (index + 1 >= argv.length || argv[index + 1].startsWith('--')) {
      throw new Error(`missing value for ${key}`);
    }
    args[key.slice(2).replace(/-/g, '_')] = argv[index + 1];
    index += 1;
  }
  return args;
}

function sha256Bytes(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function sha256File(filePath) {
  return sha256Bytes(fs.readFileSync(filePath));
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (!value || typeof value !== 'object') return JSON.stringify(value);
  return `{${Object.keys(value).sort().map(key =>
    `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
}

function git(sourceRoot, args) {
  return execFileSync('git', args, {
    cwd: sourceRoot,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe']
  }).trim();
}

function inside(root, target) {
  const relative = path.relative(root, target);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

function exactRegularFile(sourceRoot, relativePath) {
  const absolute = path.resolve(sourceRoot, relativePath);
  if (!inside(sourceRoot, absolute)) {
    throw new Error(`production asset escapes source root: ${relativePath}`);
  }
  const stat = fs.lstatSync(absolute);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`production asset is not a regular file: ${relativePath}`);
  }
  return absolute;
}

function sourceIdentity(sourceRoot, expectedRevision, expectedTree) {
  const revision = git(sourceRoot, ['rev-parse', 'HEAD']).toLowerCase();
  const tree = git(sourceRoot, ['rev-parse', 'HEAD^{tree}']).toLowerCase();
  if (revision !== expectedRevision) {
    throw new Error(`source HEAD ${revision} does not match expected revision ${expectedRevision}`);
  }
  if (expectedTree && tree !== expectedTree) {
    throw new Error(`source tree ${tree} does not match expected tree ${expectedTree}`);
  }
  const dirty = git(sourceRoot, ['status', '--porcelain', '--untracked-files=no']);
  if (dirty) throw new Error('source checkout has tracked modifications');
  return { revision, tree };
}

function atomicJson(outputPath, payload) {
  const absolute = path.resolve(outputPath);
  fs.mkdirSync(path.dirname(absolute), { recursive: true });
  const temporary = `${absolute}.part-${process.pid}-${crypto.randomBytes(4).toString('hex')}`;
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(temporary, absolute);
  } finally {
    try { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); } catch (_) {}
  }
  return absolute;
}

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

async function waitForTarget(page, internals, regionId, targetLine) {
  let visible = false;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await page.waitForTimeout(5);
    visible = await page.evaluate(line => document.querySelector(`[id="L${line}"]`) !== null, targetLine);
    if (visible) return true;
  }
  return page.evaluate(async ({ regionId: id, target }) => {
    const i = window.__COVERAGE_ENHANCE_INTERNALS__;
    const region = i.CodeRegionStore.get(id);
    const bounds = i.CodeRegionController.virtualWindowBounds(region, target - 1);
    await i.CodeRegionLoader.ensureVirtualWindow(
      i.CodeRegionController.filePath, region, bounds.start, bounds.end
    );
    i.CodeRegionController.renderVirtualWindow(region, target - 1);
    return document.querySelector(`[id="L${target}"]`) !== null;
  }, { regionId, target: targetLine });
}

async function measureTier(browser, clientJs, clientCss, tierName, lineCount) {
  const context = await browser.newContext({ viewport: VIEWPORT });
  const page = await context.newPage();
  const requestLog = [];
  let responseBytes = 0;
  let maxResponseBytes = 0;
  const regionId = `release_perf_${lineCount}`;
  const filePath = `src/release_perf_${lineCount}.c`;
  const html = `<!doctype html><html><head>
<meta charset="utf-8">
<meta name="coverage-project" content="ReleasePerf">
<meta name="coverage-report-mode" content="VNEXT_ARTIFACT_READY">
<meta name="coverage-report-id" content="release_perf_report">
<meta name="coverage-scan-id" content="1">
<meta name="coverage-repository-name" content="">
<meta name="coverage-file-path" content="${filePath}">
<meta name="coverage-render-mode" content="lazy_collapse">
<meta name="coverage-review-scope" content="full">
<style>${clientCss}</style>
</head><body><pre class="source"></pre></body></html>`;

  await page.route('**/api/coverage/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    requestLog.push(url.pathname);
    let payload;
    if (url.pathname === '/api/coverage/code-layout') {
      payload = {
        project_name: 'ReleasePerf', file_path: filePath,
        report_id: 'release_perf_report', total_lines: lineCount,
        total_uncovered_count: 0, pending_line_count: 0,
        regions: [{
          region_id: regionId, start_line: 1, end_line: lineCount,
          line_count: lineCount, default_state: 'collapsed', kind: 'collapsed',
          label: `${tierName} exact-revision fixture`
        }]
      };
    } else if (url.pathname === '/api/coverage/code-lines') {
      const start = Number(url.searchParams.get('start_line') || 1);
      const end = Math.min(lineCount, Number(url.searchParams.get('end_line') || start));
      const lines = [];
      for (let lineNo = start; lineNo <= end; lineNo += 1) {
        lines.push({
          line_no: lineNo,
          source: `int release_perf_line_${lineNo} = ${lineNo};`,
          coverage_state: 'covered',
          is_pending_analysis: false
        });
      }
      payload = { status: 'success', data: { start_line: start, end_line: end, lines } };
    } else {
      payload = { status: 'success', data: {} };
    }
    const body = JSON.stringify(payload);
    const bytes = Buffer.byteLength(body, 'utf8');
    responseBytes += bytes;
    maxResponseBytes = Math.max(maxResponseBytes, bytes);
    await route.fulfill({ status: 200, contentType: 'application/json', body });
  });
  await page.route('http://coverage-release-perf.test/', route =>
    route.fulfill({ status: 200, contentType: 'text/html', body: html })
  );

  try {
    await page.goto('http://coverage-release-perf.test/', { waitUntil: 'domcontentloaded' });
    await page.addScriptTag({ content: clientJs });
    await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
    await page.waitForFunction(id => {
      const i = window.__COVERAGE_ENHANCE_INTERNALS__;
      return i && i.CodeRegionStore && i.CodeRegionStore.get(id);
    }, regionId);

    const start = Date.now();
    const core = await page.evaluate(async id => {
      const i = window.__COVERAGE_ENHANCE_INTERNALS__;
      const started = performance.now();
      await i.CodeRegionController.expandRegion(id);
      const region = i.CodeRegionStore.get(id);
      return {
        expand_elapsed_ms: Number((performance.now() - started).toFixed(3)),
        logical_line_count: region.lines.length,
        loaded_line_count: region.loadedLineCount,
        virtualized: Boolean(region.virtualized),
        data_virtualized: Boolean(region.virtualized && region.loadedLineCount < region.lines.length),
        first_visible: document.querySelector('#L1') !== null,
        dom_line_count: region.virtualContent ? region.virtualContent.children.length : document.querySelectorAll('[id^="L"]').length
      };
    }, regionId);

    const sweep = [];
    const targets = lineCount === 100000
      ? WORKLOAD_CONTRACT.virtual_scroll_targets_100k
      : [Math.max(1, lineCount), 1];
    for (const target of targets) {
      await page.evaluate(line => window.scrollTo(0, Math.max(0, (line - 1) * 24)), target);
      const visible = await waitForTarget(page, null, regionId, target);
      const resident = await page.evaluate(id => {
        const i = window.__COVERAGE_ENHANCE_INTERNALS__;
        const region = i.CodeRegionStore.get(id);
        return {
          resident_js_lines: Number(region.loadedLineCount || 0),
          dom_line_count: region.virtualContent ? region.virtualContent.children.length : document.querySelectorAll('[id^="L"]').length
        };
      }, regionId);
      sweep.push({ target_line: target, visible, ...resident });
    }
    const elapsedMs = Date.now() - start;
    const codeLineRequests = requestLog.filter(item => item === '/api/coverage/code-lines').length;
    const residentPeak = Math.max(core.loaded_line_count || 0, ...sweep.map(item => item.resident_js_lines));
    const domPeak = Math.max(core.dom_line_count || 0, ...sweep.map(item => item.dom_line_count));
    const targetsVisible = sweep.every(item => item.visible);
    const structural = core.logical_line_count === lineCount && core.first_visible && targetsVisible;
    const virtual100k = lineCount !== 100000 || (
      core.virtualized && core.data_virtualized &&
      residentPeak <= WORKLOAD_CONTRACT.resident_js_line_budget_100k &&
      domPeak < WORKLOAD_CONTRACT.dom_line_budget_100k &&
      codeLineRequests <= WORKLOAD_CONTRACT.code_line_request_budget_100k
    );
    const status = structural && virtual100k && finite(core.expand_elapsed_ms) ? 'PASSED' : 'FAILED';
    return {
      status,
      workload_id: WORKLOAD_ID,
      tier: tierName,
      line_count: lineCount,
      measured_ms: Number(elapsedMs.toFixed(3)),
      expand_elapsed_ms: core.expand_elapsed_ms,
      logical_line_count: core.logical_line_count,
      loaded_line_count: core.loaded_line_count,
      virtualized: core.virtualized,
      data_virtualized: core.data_virtualized,
      first_visible: core.first_visible,
      targets_visible: targetsVisible,
      resident_js_lines_peak: residentPeak,
      dom_line_count_peak: domPeak,
      request_count: codeLineRequests,
      response_bytes: responseBytes,
      max_response_bytes: maxResponseBytes,
      sustained_scroll_sweep: sweep
    };
  } finally {
    await context.close();
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
  if (!args.source_root || !args.expected_revision || !args.output) {
    process.stderr.write(`--source-root, --expected-revision and --output are required\n${usage()}\n`);
    process.exitCode = 2;
    return;
  }
  const expectedRevision = String(args.expected_revision).trim().toLowerCase();
  const expectedTree = String(args.expected_tree || '').trim().toLowerCase();
  if (!/^[0-9a-f]{40}$/.test(expectedRevision) || (expectedTree && !/^[0-9a-f]{40}$/.test(expectedTree))) {
    process.stderr.write('expected revision/tree must be full 40-hex Git identities\n');
    process.exitCode = 2;
    return;
  }

  const sourceRoot = fs.realpathSync(path.resolve(args.source_root));
  let identity;
  let clientJsPath;
  let clientCssPath;
  let clientJs;
  let clientCss;
  let browser;
  try {
    identity = sourceIdentity(sourceRoot, expectedRevision, expectedTree);
    clientJsPath = exactRegularFile(sourceRoot, 'web/assets/js/coverage_enhance.js');
    clientCssPath = exactRegularFile(sourceRoot, 'web/assets/css/coverage_enhance.css');
    clientJs = fs.readFileSync(clientJsPath, 'utf8');
    clientCss = fs.readFileSync(clientCssPath, 'utf8');

    const harnessSha256 = sha256File(__filename);
    const workloadHash = sha256Bytes(stableJson({
      contract: WORKLOAD_CONTRACT,
      harness_sha256: harnessSha256
    }));
    browser = await chromium.launch({ headless: true });
    const environmentIdentity = {
      browser_name: 'chromium',
      browser_version: browser.version(),
      node_version: process.version,
      platform: process.platform,
      arch: process.arch,
      viewport: `${VIEWPORT.width}x${VIEWPORT.height}`,
      workload_driver: 'playwright'
    };

    const tiers = {};
    for (const [tierName, lineCount] of TIERS) {
      tiers[tierName] = await measureTier(browser, clientJs, clientCss, tierName, lineCount);
    }
    const virtualSource = tiers.Tier_D_100k;
    const virtual = {
      status: virtualSource.status,
      workload_id: WORKLOAD_ID,
      elapsed_ms: virtualSource.measured_ms,
      logical_line_count: virtualSource.logical_line_count,
      loaded_line_count: virtualSource.loaded_line_count,
      virtualized: virtualSource.virtualized,
      data_virtualized: virtualSource.data_virtualized,
      first_visible: virtualSource.first_visible,
      targets_visible: virtualSource.targets_visible,
      resident_js_lines_peak: virtualSource.resident_js_lines_peak,
      dom_line_count: virtualSource.dom_line_count_peak,
      request_count: virtualSource.request_count,
      response_bytes: virtualSource.response_bytes,
      max_response_bytes: virtualSource.max_response_bytes,
      sustained_scroll_sweep: virtualSource.sustained_scroll_sweep
    };
    const violations = Object.keys(tiers)
      .filter(name => tiers[name].status !== 'PASSED')
      .map(name => `${name} workload failed`);
    if (virtual.status !== 'PASSED' && !violations.includes('Tier_D_100k workload failed')) {
      violations.push('coverage_virtual_scroll_100k workload failed');
    }
    const passed = violations.length === 0;
    const result = {
      schema_version: 1,
      status: passed ? 'PASSED' : 'FAILED',
      evidence_class: 'release_performance_revision',
      comparison_type: 'single_revision',
      release_scope: 'performance_only',
      real_browser: true,
      real_http_candidate: false,
      deterministic_fixture: true,
      revision: identity.revision,
      tree_sha: identity.tree,
      workload_id: WORKLOAD_ID,
      workload_hash: workloadHash,
      harness_sha256: harnessSha256,
      environment_identity: environmentIdentity,
      source_identity: {
        revision: identity.revision,
        tree_sha: identity.tree,
        coverage_enhance_js_sha256: sha256File(clientJsPath),
        coverage_enhance_css_sha256: sha256File(clientCssPath)
      },
      tiers,
      Tier_A_1k: tiers.Tier_A_1k,
      Tier_B_10k: tiers.Tier_B_10k,
      Tier_C_50k: tiers.Tier_C_50k,
      Tier_D_100k: tiers.Tier_D_100k,
      coverage_virtual_scroll_100k: virtual,
      exit_code: passed ? 0 : 1,
      violations,
      producer_environment: {
        hostname_recorded: false,
        os_release: os.release(),
        ci_run: process.env.GITHUB_RUN_ID || ''
      }
    };
    const output = atomicJson(args.output, result);
    process.stdout.write(`${JSON.stringify({ status: result.status, output, revision: identity.revision, tree_sha: identity.tree, workload_hash: workloadHash })}\n`);
    if (!passed) process.exitCode = 1;
  } catch (error) {
    const failure = {
      schema_version: 1,
      status: 'FAILED',
      evidence_class: 'release_performance_revision',
      comparison_type: 'single_revision',
      revision: expectedRevision,
      workload_id: WORKLOAD_ID,
      exit_code: 1,
      violations: [error && error.message ? error.message : String(error)]
    };
    try { atomicJson(args.output, failure); } catch (_) {}
    process.stderr.write(`${failure.violations[0]}\n`);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main().catch(error => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});

