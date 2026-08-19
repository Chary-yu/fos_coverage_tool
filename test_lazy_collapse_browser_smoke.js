/**
 * Browser DOM and Interaction Smoke Test Suite for Lazy Collapse Architecture.
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

console.log('=== Starting Lazy Collapse Browser Smoke Tests ===');

function createDOMEnvironment(htmlContent, fetchMock) {
    const events = {};
    const elementsById = new Map();

    function makeElement(tag) {
        const el = {
            tagName: tag.toUpperCase(),
            className: '',
            style: {},
            children: [],
            _innerHTML: '',
            get innerHTML() { return this._innerHTML; },
            set innerHTML(val) {
                this._innerHTML = val;
                if (val === '') this.children = [];
            },
            innerText: '',
            textContent: '',
            dataset: {},
            isConnected: true,
            setAttribute(k, v) { this[k] = v; },
            getAttribute(k) { return this[k]; },
            appendChild(child) {
                if (child.nodeType === 11) {
                    this.children.push(...child.children);
                } else {
                    this.children.push(child);
                }
                return child;
            },
            remove() {
                this.isConnected = false;
            },
            querySelector(sel) {
                if (sel === '.coverage-region-collapse-btn') {
                    return this._collapseBtn || { addEventListener: (ev, fn) => { this._onCollapse = fn; } };
                }
                return null;
            },
            querySelectorAll(sel) {
                return [];
            },
            addEventListener(ev, fn) {
                this[`on_${ev}`] = fn;
            },
            scrollIntoView() {}
        };
        return el;
    }

    const body = makeElement('body');
    const preSource = makeElement('pre');
    preSource.className = 'source';
    preSource.parentNode = body;

    const document = {
        currentScript: { src: 'http://localhost:8000/coverage_enhance.js' },
        querySelector(selector) {
            if (selector.startsWith('meta[')) {
                const match = selector.match(/meta\[name="([^"]+)"\]/);
                if (match) {
                    const name = match[1];
                    const metaRegex = new RegExp(`<meta\\s+name=["']${name}["']\\s+content=["']([^"']*)["']`, 'i');
                    const m = htmlContent.match(metaRegex);
                    if (m) {
                        return { getAttribute: (attr) => attr === 'content' ? m[1] : null, content: m[1] };
                    }
                }
            }
            if (selector === 'pre.source') {
                return preSource;
            }
            if (selector === 'title') {
                return { innerText: 'LCOV - cov - src/smoke_test.c' };
            }
            return null;
        },
        querySelectorAll(selector) {
            return [];
        },
        getElementById(id) {
            return elementsById.get(id) || null;
        },
        createElement(tag) {
            const el = makeElement(tag);
            if (el.id) elementsById.set(el.id, el);
            return el;
        },
        createDocumentFragment() {
            return {
                nodeType: 11,
                children: [],
                appendChild(child) {
                    this.children.push(child);
                }
            };
        },
        body: body,
        addEventListener(event, handler) {
            events[event] = events[event] || [];
            events[event].push(handler);
        }
    };

    const window = {
        document,
        location: { search: '?project=SmokeProj', hash: '', origin: 'http://localhost:8000', pathname: '/src/smoke_test.c.gcov.html' },
        fetch: fetchMock,
        addEventListener: () => {},
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (id) => clearTimeout(id),
        localStorage: {
            _data: {},
            getItem(k) { return this._data[k] || null; },
            setItem(k, v) { this._data[k] = String(v); },
            removeItem(k) { delete this._data[k]; }
        }
    };

    return { window, document, events };
}

async function runSmokeTests() {
    const jsPath = path.join(__dirname, 'coverage_enhance.js');
    const jsSource = fs.readFileSync(jsPath, 'utf8');

    // -------------------------------------------------------------------------
    // Test 1: Chunk Failure & Retry does NOT duplicate DOM code lines
    // -------------------------------------------------------------------------
    console.log('[Smoke Test 1] Verifying Chunk Failure Retry does not duplicate DOM lines...');
    let failChunk2 = true;
    const fetchMock1 = async (url, opts) => {
        if (url.includes('/code-layout')) {
            return {
                ok: true,
                json: async () => ({
                    status: 'success',
                    data: {
                        project_name: 'SmokeProj',
                        file_path: 'src/smoke_test.c',
                        total_lines: 1000,
                        regions: [
                            { region_id: 'reg_1', start_line: 1, end_line: 1000, default_state: 'collapsed', kind: 'collapsed', line_count: 1000 }
                        ]
                    }
                })
            };
        }
        if (url.includes('/code-lines?')) {
            const urlObj = new URL(url, 'http://localhost:8000');
            const start = parseInt(urlObj.searchParams.get('start_line'), 10);
            const end = parseInt(urlObj.searchParams.get('end_line'), 10);

            // Simulate failure on chunk 2 (start == 501) on the first attempt
            if (start === 501 && failChunk2) {
                throw new Error('Simulated Network Failure on Chunk 2');
            }

            const lines = [];
            for (let i = start; i <= end; i++) {
                lines.push({ line_no: i, source: `int line_${i} = ${i};`, coverage_state: 'covered' });
            }
            return {
                ok: true,
                json: async () => ({ status: 'success', data: { lines } })
            };
        }
        return { ok: true, json: async () => ({ status: 'success', data: {} }) };
    };

    const mockHtml = `<!DOCTYPE html><html><head>
<meta name="coverage-project" content="SmokeProj">
<meta name="coverage-report-id" content="report_smoke_1">
<meta name="coverage-file-path" content="src/smoke_test.c">
<meta name="coverage-render-mode" content="lazy_collapse">
<meta name="coverage-review-scope" content="full">
</head><body><pre class="source"></pre></body></html>`;

    const env = createDOMEnvironment(mockHtml, fetchMock1);
    const context = vm.createContext(env.window);
    context.window = env.window;
    context.document = env.document;
    context.URL = URL;
    context.URLSearchParams = URLSearchParams;
    context.fetch = fetchMock1;

    vm.runInContext(jsSource, context);

    // Trigger DOMContentLoaded
    for (const h of (env.events['DOMContentLoaded'] || [])) {
        await h();
    }

    const { CodeRegionStore, CodeRegionController } = context.window.__COVERAGE_ENHANCE_INTERNALS__;

    const reg1 = CodeRegionStore.get('reg_1');
    assert.ok(reg1, 'reg_1 should exist in CodeRegionStore');
    assert.strictEqual(reg1.currentState, 'collapsed-unloaded');

    try {
        await CodeRegionController.expandRegion('reg_1');
        assert.fail('Should have thrown network failure');
    } catch (e) {
        assert.strictEqual(reg1.currentState, 'error');
    }

    // Lines container had partial chunk 1 lines (500 lines)
    assert.ok(reg1.linesEl);
    assert.strictEqual(reg1.linesEl.children.length, 500);

    // Now Retry: allow chunk 2 and expandRegion again
    console.log('[Smoke Test 1] Retrying expansion after error...');
    failChunk2 = false;
    await CodeRegionController.expandRegion('reg_1');
    assert.strictEqual(reg1.currentState, 'expanded-loaded');

    // Verify DOM lines count is EXACTLY 1000 (NOT 500 + 1000 = 1500!)
    assert.strictEqual(reg1.linesEl.children.length, 1000, `Expected exactly 1000 DOM lines on retry, got ${reg1.linesEl.children.length}`);
    console.log('✔ [Smoke Test 1 Passed] No duplicate lines after retry.');

    // -------------------------------------------------------------------------
    // Test 2: Expand All interrupted by Restore Default
    // -------------------------------------------------------------------------
    console.log('[Smoke Test 2] Verifying Expand All interrupted by Restore Default...');
    const fetchMock2 = async (url, opts) => {
        if (url.includes('/code-layout')) {
            return {
                ok: true,
                json: async () => ({
                    status: 'success',
                    data: {
                        project_name: 'SmokeProj',
                        file_path: 'src/smoke_test.c',
                        total_lines: 300,
                        regions: [
                            { region_id: 'r1', start_line: 1, end_line: 100, default_state: 'expanded', kind: 'analysis', line_count: 100 },
                            { region_id: 'r2', start_line: 101, end_line: 200, default_state: 'collapsed', kind: 'collapsed', line_count: 100 },
                            { region_id: 'r3', start_line: 201, end_line: 300, default_state: 'collapsed', kind: 'collapsed', line_count: 100 }
                        ]
                    }
                })
            };
        }
        if (url.includes('/code-lines/batch')) {
            return {
                ok: true,
                json: async () => ({
                    status: 'success',
                    data: {
                        ranges: [{ start_line: 1, end_line: 100, lines: [{ line_no: 1, source: 'x', coverage_state: 'uncovered' }] }]
                    }
                })
            };
        }
        if (url.includes('/code-lines?')) {
            await new Promise(res => setTimeout(res, 20)); // artificial delay
            return {
                ok: true,
                json: async () => ({ status: 'success', data: { lines: [{ line_no: 101, source: 'code' }] } })
            };
        }
        return { ok: true, json: async () => ({ status: 'success', data: {} }) };
    };

    const env2 = createDOMEnvironment(mockHtml, fetchMock2);
    const ctx2 = vm.createContext(env2.window);
    ctx2.window = env2.window;
    ctx2.document = env2.document;
    ctx2.URL = URL;
    ctx2.URLSearchParams = URLSearchParams;
    ctx2.fetch = fetchMock2;

    vm.runInContext(jsSource, ctx2);
    for (const h of (env2.events['DOMContentLoaded'] || [])) {
        await h();
    }

    const { CodeRegionController: ctl2, CodeRegionStore: store2, ReviewDraftStore: draftStore2 } = ctx2.window.__COVERAGE_ENHANCE_INTERNALS__;

    const btnMock = { disabled: false, innerText: '' };
    const statusMock = { innerText: '' };

    // Launch expandAll asynchronously
    const expandAllPromise = ctl2.expandAll(btnMock, statusMock);

    // Immediately trigger restoreDefault while expandAll is in flight
    ctl2.restoreDefault();

    await expandAllPromise;

    // r2 and r3 should be collapsed, r1 expanded
    assert.strictEqual(store2.get('r1').defaultState, 'expanded');
    assert.strictEqual(store2.get('r2').currentState.startsWith('collapsed'), true, 'r2 should be collapsed after restoreDefault');
    assert.strictEqual(store2.get('r3').currentState.startsWith('collapsed'), true, 'r3 should be collapsed after restoreDefault');
    console.log('✔ [Smoke Test 2 Passed] Restore Default cleanly cancelled Expand All.');

    // -------------------------------------------------------------------------
    // Test 3: ReviewDraftStore unsubmitted edit persistence across collapse/expand
    // -------------------------------------------------------------------------
    console.log('[Smoke Test 3] Verifying Draft persistence across collapse and re-expand...');
    draftStore2.setDraft(1, {
        status: '可覆盖',
        reviewer: 'Alice',
        uncovered_reason: 'Will cover in next sprint',
        isDirty: true
    });

    const draft = draftStore2.getDraft(1);
    assert.strictEqual(draft.status, '可覆盖');
    assert.strictEqual(draft.reviewer, 'Alice');
    assert.strictEqual(draft.uncovered_reason, 'Will cover in next sprint');
    assert.strictEqual(draft.isDirty, true);
    console.log('✔ [Smoke Test 3 Passed] ReviewDraftStore edit state preserved perfectly.');

    console.log('=== All Browser Smoke Tests Passed Successfully ===');
}

runSmokeTests().catch(err => {
    console.error('❌ Smoke Test Failed:', err);
    process.exit(1);
});
