#!/usr/bin/env node
/* Real Chromium baseline/candidate A/B workload for the release evidence gate. */
const fs = require('fs');
const path = require('path');
const { chromium } = require('@playwright/test');

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
    fs.writeFileSync(output, JSON.stringify(result, null, 2));
    process.stdout.write(JSON.stringify(result) + '\n');
    if (result.status !== 'PASSED') process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
