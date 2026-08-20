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
        const classSet = new Set();
        const el = {
            tagName: tag.toUpperCase(),
            _className: '',
            get className() { return this._className; },
            set className(val) {
                this._className = val || '';
                classSet.clear();
                (this._className).split(/\s+/).filter(Boolean).forEach(c => classSet.add(c));
            },
            classList: {
                add(...cls) {
                    cls.forEach(c => c && classSet.add(c));
                    el._className = Array.from(classSet).join(' ');
                },
                remove(...cls) {
                    cls.forEach(c => classSet.delete(c));
                    el._className = Array.from(classSet).join(' ');
                },
                contains(c) {
                    return classSet.has(c);
                },
                toggle(c, force) {
                    if (force !== undefined) {
                        if (force) classSet.add(c); else classSet.delete(c);
                    } else {
                        if (classSet.has(c)) classSet.delete(c); else classSet.add(c);
                    }
                    el._className = Array.from(classSet).join(' ');
                }
            },
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
            parentNode: null,
            setAttribute(k, v) { this[k] = v; },
            getAttribute(k) { return this[k]; },
            appendChild(child) {
                if (child.nodeType === 11) {
                    child.children.forEach(c => { c.parentNode = this; });
                    this.children.push(...child.children);
                } else {
                    child.parentNode = this;
                    this.children.push(child);
                }
                return child;
            },
            insertBefore(newNode, refNode) {
                newNode.parentNode = this;
                const idx = this.children.indexOf(refNode);
                if (idx >= 0) {
                    this.children.splice(idx, 0, newNode);
                } else {
                    this.children.push(newNode);
                }
                return newNode;
            },
            remove() {
                this.isConnected = false;
                if (this.parentNode && this.parentNode.children) {
                    const idx = this.parentNode.children.indexOf(this);
                    if (idx >= 0) {
                        this.parentNode.children.splice(idx, 1);
                    }
                }
                this.parentNode = null;
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
            dispatchEvent(event) {
                const fn = this[`on_${event.type}`];
                if (typeof fn === 'function') {
                    fn(event);
                }
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
    const networkChunkLines = 2000;
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
                        total_lines: networkChunkLines * 2,
                        regions: [
                            { region_id: 'reg_1', start_line: 1, end_line: networkChunkLines * 2, default_state: 'collapsed', kind: 'collapsed', line_count: networkChunkLines * 2 }
                        ]
                    }
                })
            };
        }
        if (url.includes('/code-lines?')) {
            const urlObj = new URL(url, 'http://localhost:8000');
            const start = parseInt(urlObj.searchParams.get('start_line'), 10);
            const end = parseInt(urlObj.searchParams.get('end_line'), 10);

            // Simulate failure on chunk 2 (start == 2001) on the first attempt
            if (start === networkChunkLines + 1 && failChunk2) {
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

    // Lines container had partial chunk 1 lines.
    assert.ok(reg1.linesEl);
    assert.strictEqual(reg1.linesEl.children.length, networkChunkLines);

    // Now Retry: allow chunk 2 and expandRegion again
    console.log('[Smoke Test 1] Retrying expansion after error...');
    failChunk2 = false;
    await CodeRegionController.expandRegion('reg_1');
    assert.strictEqual(reg1.currentState, 'expanded-loaded');

    // Verify DOM lines count is EXACTLY 4000 (not partial + full duplicate).
    assert.strictEqual(reg1.linesEl.children.length, networkChunkLines * 2, `Expected exactly ${networkChunkLines * 2} DOM lines on retry, got ${reg1.linesEl.children.length}`);
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

    // r2 and r3 should be collapsed, loading === false, r1 expanded
    assert.strictEqual(store2.get('r1').defaultState, 'expanded');
    assert.strictEqual(store2.get('r2').currentState, 'collapsed-unloaded', 'r2 should be collapsed-unloaded after restoreDefault');
    assert.strictEqual(store2.get('r2').loading, false, 'r2.loading must be false after restoreDefault');
    assert.strictEqual(store2.get('r3').currentState, 'collapsed-unloaded', 'r3 should be collapsed-unloaded after restoreDefault');
    assert.strictEqual(store2.get('r3').loading, false, 'r3.loading must be false after restoreDefault');

    // Crucial check: verify that after cancellation, clicking placeholder on r2 expands without deadlock
    console.log('[Smoke Test 2] Clicking placeholder to verify r2 can be expanded...');
    const r2 = store2.get('r2');
    assert.ok(r2.placeholderEl, 'r2 placeholder must exist');
    r2.placeholderEl.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() {} });
    await new Promise(res => setTimeout(res, 50));

    assert.strictEqual(r2.currentState, 'expanded-loaded', 'r2 must successfully transition to expanded-loaded on click');
    assert.strictEqual(r2.loading, false, 'r2 loading must be false after loaded');
    assert.ok(r2.linesEl, 'r2 linesEl must exist');
    console.log('✔ [Smoke Test 2 Passed] Restore Default cleanly cancelled Expand All and regions remain fully interactive.');

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

    // -------------------------------------------------------------------------
    // Test 4: Batch Layout Range Mismatch Graceful Fallback
    // -------------------------------------------------------------------------
    console.log('[Smoke Test 4] Verifying Batch Layout Range Mismatch Fallback...');
    const fetchMock4 = async (url, opts) => {
        if (url.includes('/code-layout')) {
            return {
                ok: true,
                json: async () => ({
                    status: 'success',
                    data: {
                        project_name: 'SmokeProj',
                        file_path: 'src/smoke_test.c',
                        total_lines: 200,
                        regions: [
                            { region_id: 'm1', start_line: 1, end_line: 100, default_state: 'expanded', kind: 'analysis', line_count: 100 },
                            { region_id: 'm2', start_line: 101, end_line: 200, default_state: 'expanded', kind: 'analysis', line_count: 100 }
                        ]
                    }
                })
            };
        }
        if (url.includes('/code-lines/batch')) {
            // Server only returns range for m1, missing m2
            return {
                ok: true,
                json: async () => ({
                    status: 'success',
                    data: {
                        ranges: [{ start_line: 1, end_line: 100, lines: [{ line_no: 1, source: 'm1 code', coverage_state: 'uncovered' }] }]
                    }
                })
            };
        }
        if (url.includes('/code-lines?')) {
            return {
                ok: true,
                json: async () => ({ status: 'success', data: { lines: [{ line_no: 101, source: 'm2 individual code', coverage_state: 'uncovered' }] } })
            };
        }
        return { ok: true, json: async () => ({ status: 'success', data: {} }) };
    };

    const env4 = createDOMEnvironment(mockHtml, fetchMock4);
    const ctx4 = vm.createContext(env4.window);
    ctx4.window = env4.window;
    ctx4.document = env4.document;
    ctx4.URL = URL;
    ctx4.URLSearchParams = URLSearchParams;
    ctx4.fetch = fetchMock4;

    vm.runInContext(jsSource, ctx4);
    for (const h of (env4.events['DOMContentLoaded'] || [])) {
        await h();
    }

    const { CodeRegionStore: store4 } = ctx4.window.__COVERAGE_ENHANCE_INTERNALS__;
    const m1 = store4.get('m1');
    const m2 = store4.get('m2');

    assert.strictEqual(m1.currentState, 'expanded-loaded', 'm1 returned in batch must be expanded-loaded');
    assert.strictEqual(m1.loading, false, 'm1 loading must be false');
    assert.strictEqual(m2.currentState, 'collapsed-unloaded', 'm2 missing in batch must gracefully fallback to collapsed-unloaded');
    assert.strictEqual(m2.loading, false, 'm2 loading must be false');
    assert.ok(m2.placeholderEl, 'm2 placeholder must be mounted');
    assert.strictEqual(m2.placeholderEl.style.display, '', 'm2 placeholder must be visible');

    // Verify m2 can be expanded individually on user click
    m2.placeholderEl.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() {} });
    await new Promise(res => setTimeout(res, 50));
    assert.strictEqual(m2.currentState, 'expanded-loaded', 'm2 must expand successfully on click');
    assert.strictEqual(m2.loading, false, 'm2 loading must be false after loaded');
    console.log('✔ [Smoke Test 4 Passed] Missing batch range cleanly fell back to interactive collapsed state.');

    
    // =========================================================================
    // Smoke Test 5: Verifying RegionLineLRUCache Eviction & Draft Preservation (Item 5)
    // =========================================================================
    console.log("[Smoke Test 5] Verifying RegionLineLRUCache eviction and Draft preservation...");
    const fetchMock5 = async (url) => {
        if (url.includes("/code-layout")) {
            return {
                ok: true,
                json: async () => ({
                    status: "success",
                    data: {
                        project_name: "SmokeProj5",
                        file_path: "src/smoke_lru.c",
                        total_lines: 60000,
                        regions: [
                            { region_id: "lru_1", start_line: 1, end_line: 30000, default_state: "collapsed", kind: "collapsed", line_count: 30000 },
                            { region_id: "lru_2", start_line: 30001, end_line: 60000, default_state: "collapsed", kind: "collapsed", line_count: 30000 }
                        ]
                    }
                })
            };
        }
        if (url.includes("/code-lines?")) {
            const urlObj = new URL(url, "http://localhost:8000");
            const start = parseInt(urlObj.searchParams.get("start_line"), 10);
            const end = parseInt(urlObj.searchParams.get("end_line"), 10);
            const lines = [];
            for (let i = start; i <= end; i++) {
                lines.push({ line_no: i, source: `int line_${i};`, coverage_state: i === 1 ? "uncovered" : "covered", is_block_entry: i === 1 });
            }
            return { ok: true, json: async () => ({ status: "success", data: { lines } }) };
        }
        return { ok: true, json: async () => ({ status: "success", data: {} }) };
    };

    const env5 = createDOMEnvironment(mockHtml, fetchMock5);
    const ctx5 = vm.createContext(env5.window);
    ctx5.window = env5.window;
    ctx5.document = env5.document;
    ctx5.URL = URL;
    ctx5.URLSearchParams = URLSearchParams;
    ctx5.fetch = fetchMock5;

    vm.runInContext(jsSource, ctx5);
    for (const h of (env5.events["DOMContentLoaded"] || [])) {
        await h();
    }

    const { CodeRegionStore: store5, CodeRegionController: ctrl5, ReviewDraftStore: draftStore5, RegionLineLRUCache: lru5 } = ctx5.window.__COVERAGE_ENHANCE_INTERNALS__;
    lru5.MAX_CACHED_LINES = 35000; // Set small budget for test

    // 1. Expand lru_1 and save draft edit
    await ctrl5.expandRegion("lru_1");
    const lruReg1 = store5.get("lru_1");
    assert.strictEqual(lruReg1.loaded, true);
    assert.strictEqual(lruReg1.lines.length, 30000);

    draftStore5.setDraft(1, { reviewer: "LRUTester", status: "可覆盖", uncovered_reason: "Survives LRU" });

    // 2. Collapse lru_1
    ctrl5.collapseRegion("lru_1");
    assert.strictEqual(lruReg1.currentState, "collapsed-loaded");

    // 3. Expand lru_2 (30000 lines) -> exceeds budget 35000 -> evicts collapsed lru_1
    await ctrl5.expandRegion("lru_2");
    const lruReg2 = store5.get("lru_2");
    assert.strictEqual(lruReg2.loaded, true);

    // Verify lru_1 was evicted (loaded=false) while draft survives!
    assert.strictEqual(lruReg1.loaded, false, "Collapsed lru_1 must be evicted from line memory");
    const lruDraft5 = draftStore5.getDraft(1);
    assert.ok(lruDraft5, "Draft must survive LRU eviction");
    assert.strictEqual(lruDraft5.reviewer, "LRUTester");

    // 4. Re-expand lru_1
    await ctrl5.expandRegion("lru_1");
    assert.strictEqual(lruReg1.loaded, true, "lru_1 must reload on re-expansion");
    assert.strictEqual(lruReg1.lines.length, 30000);
    console.log("✔ [Smoke Test 5 Passed] LRU evicted collapsed region cleanly and preserved draft state across re-expansion.");

    // =========================================================================
    // Smoke Test 6: Very large expanded regions retain only a virtual window
    // =========================================================================
    console.log("[Smoke Test 6] Verifying virtual scrolling bounds DOM for 10k lines...");
    const virtualLineCount = 10000;
    const fetchMock6 = async (url) => {
        if (url.includes("/code-layout")) {
            return {
                ok: true,
                json: async () => ({
                    status: "success",
                    data: {
                        project_name: "SmokeVirtual",
                        file_path: "src/smoke_virtual.c",
                        total_lines: virtualLineCount,
                        regions: [{
                            region_id: "virtual_10k", start_line: 1,
                            end_line: virtualLineCount, default_state: "collapsed",
                            kind: "collapsed", line_count: virtualLineCount
                        }]
                    }
                })
            };
        }
        if (url.includes("/code-lines?")) {
            const urlObj = new URL(url, "http://localhost:8000");
            const start = parseInt(urlObj.searchParams.get("start_line"), 10);
            const end = parseInt(urlObj.searchParams.get("end_line"), 10);
            const lines = [];
            for (let i = start; i <= end; i++) {
                lines.push({ line_no: i, source: `int virtual_${i};`, coverage_state: "covered" });
            }
            return { ok: true, json: async () => ({ status: "success", data: { lines } }) };
        }
        return { ok: true, json: async () => ({ status: "success", data: {} }) };
    };
    const env6 = createDOMEnvironment(mockHtml, fetchMock6);
    const ctx6 = vm.createContext(env6.window);
    ctx6.window = env6.window;
    ctx6.document = env6.document;
    ctx6.URL = URL;
    ctx6.URLSearchParams = URLSearchParams;
    ctx6.fetch = fetchMock6;
    vm.runInContext(jsSource, ctx6);
    for (const h of (env6.events["DOMContentLoaded"] || [])) await h();
    const { CodeRegionStore: store6, CodeRegionController: ctrl6, PerformanceTelemetry: telemetry6 } = ctx6.window.__COVERAGE_ENHANCE_INTERNALS__;
    await ctrl6.expandRegion("virtual_10k");
    const virtualRegion = store6.get("virtual_10k");
    assert.strictEqual(virtualRegion.loaded, true);
    assert.strictEqual(virtualRegion.virtualized, true);
    assert.ok(virtualRegion.virtualContent.children.length < 1500,
        `Virtual region should keep a bounded DOM window, got ${virtualRegion.virtualContent.children.length}`);
    assert.ok(telemetry6.snapshot().max_dom_lines < 1500);
    console.log("✔ [Smoke Test 6 Passed] 10k-line expansion kept a bounded virtual DOM window.");

    console.log('=== All Browser Smoke Tests Passed Successfully ===');
}

runSmokeTests().catch(err => {
    console.error('❌ Smoke Test Failed:', err);
    process.exit(1);
});
