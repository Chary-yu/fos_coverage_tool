/**
 * 覆盖率 HTML 报告增强脚本 (ES6) - 基本块合并与控制流隔离终极版
 * 自动识别连续未覆盖代码段 (基本块)，只在首行渲染分析控件并显示范围徽章；
 * 强行将 C 语言控制流分支关键字所在的行 (if, else, for, while, do, switch, case, default) 进行物理隔离单行展示，确保科学细致的分析。
 */
(function() {
    const ENHANCE_VERSION = 'progress-refresh-20260812';
    const SERVER_URL = '/api/coverage';
    const DEFAULT_PROJECT = 'Gemini-NOS';
    const RENDER_MODE = 'lazy'; // 'lazy' or 'immediate'
    const REVIEW_SCOPE = 'full'; // 'full' or 'incremental'
    const URL_PARAMS = new URLSearchParams(window.location.search);
    const QUERY_MODE = URL_PARAMS.get('mode');
    const ACTIVE_MODE = (QUERY_MODE === 'lazy' || QUERY_MODE === 'immediate') ? QUERY_MODE : RENDER_MODE;
    const STATUS_OPTIONS = ['未确认', '可覆盖', '无法覆盖', '冗余代码'];
    const CONFIRMED_STATUS_SET = new Set(['可覆盖', '无法覆盖', '冗余代码']);
    const RENDER_BATCH_SIZE = 1000;
    const RENDER_FRAME_BUDGET_MS = 50;
    const RENDER_PROGRESS_MIN_BLOCKS = 300;

    // 控制流分支关键字侦测正则 (边界隔离)
    const CONTROL_FLOW_REGEX = /\b(if|else|for|while|do|switch|case|default)\b/;

    let totalUncovered = 0;       // 物理未覆盖代码行总数
    let blocks = [];              // 合并与隔离切分后的 Block 列表 (元素为 [{span, lineNum}])
    let panelsMap = new Map();     // startLine -> {select, reviewerInput, methodInput, reasonInput, saveBtn, block}
    let legacyPanelSyncers = [];
    let legacyRefreshRequested = false;
    let requestLegacyPanelRefresh = function() {};
    let dirtyPanelStartLines = new Set();
    let batchToolbarState = null;
    let reviewControlsReady = false;
    const PROGRESS_UPDATE_STORAGE_KEY = 'coverage-review-progress-updated';

    function getBlockStartItem(block) {
        return block.startItem || block[0];
    }

    function getBlockLength(block) {
        return block.length || (block.lineNums ? block.lineNums.length : 0);
    }

    function getBlockEndLineNum(block) {
        if (block.endLineNum !== undefined) {
            return block.endLineNum;
        }
        if (block.lineNums && block.lineNums.length > 0) {
            return block.lineNums[block.lineNums.length - 1];
        }
        return block.length > 0 ? block[block.length - 1].lineNum : 0;
    }

    function getBlockLineNums(block) {
        return block.lineNums || block.map(item => item.lineNum);
    }

    function forEachBlockItem(block, callback) {
        if (Array.isArray(block)) {
            block.forEach(callback);
            return;
        }
        if (block.startItem) {
            callback(block.startItem, 0);
        }
    }

    function getStoredPanelValue(panel, key) {
        if (!panel) {
            return '';
        }
        if (key === 'status' && panel.select && typeof panel.select.value === 'string') {
            return panel.select.value;
        }
        if (panel[key] && typeof panel[key].value === 'string') {
            return panel[key].value;
        }
        return panel.values && typeof panel.values[key] === 'string' ? panel.values[key] : '';
    }

    function setStoredPanelValues(panel, values) {
        panel.values = Object.assign({
            status: '未确认',
            isDraft: false,
            reviewerInput: '',
            methodInput: '',
            reasonInput: ''
        }, panel.values || {}, values || {});

        if (panel.select) {
            panel.select.value = panel.values.status || '未确认';
        }
        if (panel.reviewerInput) {
            panel.reviewerInput.value = panel.values.reviewerInput || '';
        }
        if (panel.methodInput) {
            panel.methodInput.value = panel.values.methodInput || '';
        }
        if (panel.reasonInput) {
            panel.reasonInput.value = panel.values.reasonInput || '';
        }
    }

    function setPanelPersistedState(panel) {
        if (!panel || !panel.saveBtn) {
            return;
        }
        const status = getStoredPanelValue(panel, 'status');
        panel.saveBtn.className = 'coverage-analysis-btn saved';
        panel.saveBtn.innerText = panel.values && panel.values.isDraft ? '已暂存' : (status === '未确认' ? '已保存' : '已确认');
    }

    function updateBatchToolbar() {
        if (!batchToolbarState) {
            return;
        }
        const count = dirtyPanelStartLines.size;
        const submitting = batchToolbarState.submitting === true;
        batchToolbarState.count.innerText = `待暂存 ${count} 项`;
        batchToolbarState.locateBtn.disabled = !reviewControlsReady || submitting;
        batchToolbarState.draftBtn.innerText = submitting ? '保存中...' : `暂存草稿 (${count})`;
        batchToolbarState.confirmBtn.innerText = submitting ? '保存中...' : `确认提交 (${count})`;
        batchToolbarState.draftBtn.disabled = count === 0 || submitting;
        batchToolbarState.confirmBtn.disabled = count === 0 || submitting;
        batchToolbarState.container.classList.toggle('has-pending', count > 0);
    }

    function markPanelDirty(startLineNum) {
        const panel = panelsMap.get(startLineNum);
        if (!panel) {
            return;
        }
        dirtyPanelStartLines.add(startLineNum);
        if (panel.saveBtn) {
            panel.saveBtn.className = 'coverage-analysis-btn pending';
            panel.saveBtn.innerText = '待暂存';
        }
        updateBatchToolbar();
    }

    function isPanelAwaitingReview(panel) {
        if (!panel) {
            return false;
        }
        const isDraft = panel.values && panel.values.isDraft === true;
        return isDraft || getStoredPanelValue(panel, 'status') === '未确认';
    }

    function clearPanelDirty(startLineNum, isDraft) {
        const panel = panelsMap.get(startLineNum);
        dirtyPanelStartLines.delete(startLineNum);
        if (panel) {
            setStoredPanelValues(panel, { isDraft: isDraft === true });
        }
        setPanelPersistedState(panel);
        updateBatchToolbar();
    }

    function getPanelBatchPayload(panel) {
        return {
            line_numbers: getBlockLineNums(panel.block),
            reviewer: getStoredPanelValue(panel, 'reviewerInput').trim(),
            status: getStoredPanelValue(panel, 'status') || '未确认',
            coverage_method: getStoredPanelValue(panel, 'methodInput').trim(),
            uncovered_reason: getStoredPanelValue(panel, 'reasonInput').trim()
        };
    }

    function notifyProgressChanged() {
        try {
            window.localStorage.setItem(PROGRESS_UPDATE_STORAGE_KEY, JSON.stringify({
                project_name: DEFAULT_PROJECT,
                updated_at: Date.now()
            }));
        } catch (error) {
            // Storage can be disabled for file:// reports. Saving review data must not fail because of it.
            console.debug('[CoverageEnhance] Progress refresh notification skipped:', error);
        }
    }

    function saveReviewBlocksBatch(filePath, blocks, mode) {
        return fetch(`${SERVER_URL}/batch`, {
            method: 'POST',
            mode: 'cors',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                project_name: DEFAULT_PROJECT,
                file_path: filePath,
                mode: mode,
                blocks: blocks
            })
        }).then(response => {
            return response.json().then(data => {
                if (!response.ok || !data || data.status !== 'success') {
                    throw new Error(data && data.message ? data.message : `HTTP ${response.status}`);
                }
                return data;
            });
        });
    }

    function summarizeStatus(values) {
        const status = values.status || '未确认';
        if (status !== '未确认') {
            return status;
        }
        if (values.reviewerInput || values.methodInput || values.reasonInput) {
            return '已填写';
        }
        return '分析';
    }

    function decoratePlaceholder(placeholder, values, block) {
        const statusText = summarizeStatus(values || {});
        const blockLength = getBlockLength(block);
        const startLineNum = getBlockStartItem(block).lineNum;
        const endLineNum = getBlockEndLineNum(block);
        const label = blockLength > 1 ? `${statusText} L${startLineNum}-${endLineNum}` : statusText;
        if (placeholder.dataset) {
            placeholder.dataset.coverageLabel = label;
        } else {
            placeholder.innerText = label;
        }
        placeholder.classList.toggle('saved', statusText !== '分析' && statusText !== '未确认');
        placeholder.classList.toggle('coverage-analysis-placeholder-saved', statusText !== '分析' && statusText !== '未确认');
        placeholder.title = '点击展开覆盖率分析输入框';
    }

    document.addEventListener('DOMContentLoaded', function() {
        // 1. 自动提取当前文件的相对路径
        let filePath = '';
        const titleElement = document.querySelector('title');
        if (titleElement) {
            const titleText = titleElement.innerText;
            const match = titleText.match(/LCOV\s+-\s+.*?\s+-\s+(.+)/);
            if (match && match[1]) {
                filePath = match[1].trim();
            }
        }

        if (!filePath) {
            const headerVals = document.querySelectorAll('.headerValue');
            if (headerVals.length >= 2) {
                filePath = window.location.pathname.split('/html/')[1] || window.location.pathname;
                filePath = filePath.replace('.gcov.html', '');
            } else {
                filePath = window.location.pathname;
            }
        }

        console.log('[CoverageEnhance] Current file path:', filePath);
        console.log('[CoverageEnhance] Version:', ENHANCE_VERSION);

        // 2. 控制流隔离与基本块探测算法：扫描 DOM 树，遇到已覆盖行或控制流关键字时切分
        const preSource = document.querySelector('pre.source');
        if (!preSource) {
            console.log('[CoverageEnhance] No source container found.');
            return;
        }
        if (REVIEW_SCOPE === 'incremental') {
            const scopeNotice = document.createElement('div');
            scopeNotice.className = 'coverage-review-scope-notice';
            scopeNotice.innerText = '增量覆盖率审查：仅“Git 新增且未覆盖”的代码行显示填写控件。';
            preSource.parentNode.insertBefore(scopeNotice, preSource);
        }
        preSource.addEventListener('click', function(e) {
            const placeholder = e.target.closest('.coverage-analysis-placeholder, .coverage-analysis-placeholder-anchor');
            if (!placeholder || !preSource.contains(placeholder)) {
                return;
            }
            e.preventDefault();
            e.stopPropagation();
            const startLineNum = parseInt(placeholder.dataset.startLine || '', 10);
            if (!Number.isNaN(startLineNum)) {
                expandBlockPanel(startLineNum);
            }
        });

        function createSourceLineAccess(pre) {
            const modernLineNodes = pre.querySelectorAll('span[id^="L"]');
            if (modernLineNodes.length > 0) {
                return {
                    length: modernLineNodes.length,
                    get(index) {
                        const span = modernLineNodes[index];
                        if (!span) {
                            return null;
                        }
                        const lineNum = parseInt(span.id.replace('L', ''), 10);
                        if (Number.isNaN(lineNum)) {
                            return null;
                        }
                        return {
                            span,
                            lineNum,
                            legacyInline: false
                        };
                    }
                };
            }

            const legacyLineNumNodes = pre.querySelectorAll('span.lineNum');
            function getSameLineInfo(lineNumSpan) {
                let node = lineNumSpan.nextSibling;
                let codeSpan = null;
                let lineText = '';
                while (node) {
                    if (node.nodeType === Node.TEXT_NODE && node.nodeValue.includes('\n')) {
                        lineText += node.nodeValue.substring(0, node.nodeValue.indexOf('\n'));
                        break;
                    }
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        if (node.matches('.lineNum')) {
                            break;
                        }
                        if (node.matches('.lineCov, .lineNoCov, .tlaGNC, .tlaUNC, .tlaBgGNC, .tlaBgUNC')) {
                            codeSpan = node;
                        }
                        lineText += node.textContent || '';
                    } else if (node.nodeType === Node.TEXT_NODE) {
                        lineText += node.nodeValue;
                    }
                    node = node.nextSibling;
                }
                return { codeSpan, lineText };
            }

            return {
                length: legacyLineNumNodes.length,
                get(index) {
                    const lineNumSpan = legacyLineNumNodes[index];
                    if (!lineNumSpan) {
                        return null;
                    }
                    const lineNum = parseInt(lineNumSpan.textContent, 10);
                    const sameLineInfo = getSameLineInfo(lineNumSpan);
                    const codeSpan = sameLineInfo.codeSpan || lineNumSpan;
                    return {
                        span: codeSpan,
                        lineNumSpan,
                        lineText: sameLineInfo.lineText || codeSpan.textContent || '',
                        lineNum: Number.isNaN(lineNum) ? index + 1 : lineNum,
                        legacyInline: true
                    };
                }
            };
        }

        const sourceLines = createSourceLineAccess(preSource);
        const countedUncoveredLines = new Set();

        function addBlock(block) {
            const uniqueBlock = [];
            const seen = new Set();
            block.forEach(item => {
                if (!item || !isUncoveredLine(item) || seen.has(item.lineNum)) {
                    return;
                }
                seen.add(item.lineNum);
                uniqueBlock.push(item);
                countedUncoveredLines.add(item.lineNum);
            });
            if (uniqueBlock.length > 0) {
                blocks.push(uniqueBlock);
            }
        }

        function isUncoveredLine(item) {
            if (!item || !item.span) {
                return false;
            }
            const uncovered = item.span.matches('.tlaUNC, .tlaBgUNC, .lineNoCov') ||
                item.span.querySelector('.tlaUNC, .tlaBgUNC, .lineNoCov') !== null;
            if (!uncovered || REVIEW_SCOPE !== 'incremental') {
                return uncovered;
            }
            return item.span.getAttribute('data-coverage-review') === 'incremental' ||
                item.span.querySelector('[data-coverage-review="incremental"]') !== null;
        }

        function isCoveredLine(item) {
            if (!item || !item.span) {
                return false;
            }
            return item.span.matches('.tlaGNC, .tlaBgGNC, .lineCov') || item.span.querySelector('.tlaGNC, .tlaBgGNC, .lineCov') !== null;
        }

        function getLineText(item) {
            return item.lineText || item.span.textContent || '';
        }

        function getCodeText(item) {
            const lineText = getLineText(item);
            const colonIndex = lineText.indexOf(':');
            return (colonIndex >= 0 ? lineText.substring(colonIndex + 1) : lineText).trim();
        }

        function isControlFlowLine(item) {
            return CONTROL_FLOW_REGEX.test(getCodeText(item));
        }

        function isFunctionEntryLine(item) {
            const codeText = getCodeText(item)
                .replace(/\/\*.*?\*\//g, '')
                .replace(/\s+/g, ' ')
                .trim();
            if (!codeText || isControlFlowLine(item) || codeText.endsWith(';')) {
                return false;
            }
            if (/^(return|typedef|struct|enum|union)\b/.test(codeText)) {
                return false;
            }
            return /^[A-Za-z_][\w\s\*]*\s+[A-Za-z_]\w*\s*\([^;]*\)\s*(\{|$)/.test(codeText);
        }

        function isIgnorableStructuralLine(item) {
            const text = getCodeText(item);
            return text === '' || /^[{}]+;?$/.test(text);
        }

        function stripLineComment(text) {
            return text.replace(/\/\/.*$/, '').trim();
        }

        function isJumpLine(item) {
            return /^(return|goto|break|continue)\b/.test(stripLineComment(getCodeText(item)));
        }

        function isSimpleAutoGroupLine(item) {
            const text = stripLineComment(getCodeText(item))
                .replace(/\/\*.*?\*\//g, '')
                .trim();
            if (!text || isControlFlowLine(item) || isFunctionEntryLine(item) || isJumpLine(item)) {
                return false;
            }
            if (/^[{}]+;?$/.test(text) || /^(case\b.*:|default\s*:|[A-Za-z_]\w*\s*:)$/.test(text)) {
                return false;
            }
            if (!text.endsWith(';')) {
                return false;
            }
            const hasAssignment = /(^|[^=!<>])=([^=]|$)/.test(text) ||
                /\b(\+=|-=|\*=|\/=|%=|&=|\|=|\^=|<<=|>>=)\b/.test(text);
            const isSimpleDeclaration = /^(?:const\s+|static\s+|volatile\s+|register\s+|unsigned\s+|signed\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|[A-Za-z_]\w*\s+)+[*\s]*[A-Za-z_]\w*(?:\s*=\s*[^;]+)?\s*;$/.test(text);
            return hasAssignment || isSimpleDeclaration;
        }

        function buildSemanticBlock(startIndex) {
            const start = sourceLines.get(startIndex);
            if (!start) {
                return { block: [], consumedUntil: startIndex };
            }
            const block = [start];
            const startIsFunction = isFunctionEntryLine(start);
            let consumedUntil = startIndex;

            for (let j = startIndex + 1; j < sourceLines.length; j++) {
                const next = sourceLines.get(j);
                if (!next) {
                    continue;
                }
                if (isCoveredLine(next)) {
                    break;
                }

                if (isUncoveredLine(next)) {
                    if (isControlFlowLine(next) || isFunctionEntryLine(next)) {
                        break;
                    }
                    if (startIsFunction && !isSimpleAutoGroupLine(next)) {
                        break;
                    }
                    if (!startIsFunction && (!isSimpleAutoGroupLine(start) || !isSimpleAutoGroupLine(next))) {
                        break;
                    }
                    block.push(next);
                    consumedUntil = j;
                    continue;
                }

                if (!startIsFunction) {
                    break;
                }
                if (isControlFlowLine(next) || isFunctionEntryLine(next)) {
                    break;
                }
                if (startIsFunction && !isIgnorableStructuralLine(next)) {
                    continue;
                }
                if (!isIgnorableStructuralLine(next)) {
                    break;
                }
            }

            return { block, consumedUntil };
        }

        for (let i = 0; i < sourceLines.length; i++) {
            const item = sourceLines.get(i);
            if (!isUncoveredLine(item) || countedUncoveredLines.has(item.lineNum)) {
                continue;
            }

            if (isControlFlowLine(item)) {
                addBlock([item]);
            } else {
                const { block, consumedUntil } = buildSemanticBlock(i);
                addBlock(block);
                i = Math.max(i, consumedUntil);
            }
        }

        totalUncovered = countedUncoveredLines.size;
        countedUncoveredLines.clear();

        if (totalUncovered === 0) {
            console.log('[CoverageEnhance] No uncovered lines found.');
            return;
        }

        console.log(`[CoverageEnhance] Total uncovered lines: ${totalUncovered}. Consolidated into ${blocks.length} block(s).`);

        function createResizeGrip(textarea, onResize) {
            const grip = document.createElement('span');
            grip.className = 'coverage-resize-grip';
            grip.title = '拖拽调整输入框大小';

            grip.addEventListener('mousedown', function(e) {
                e.preventDefault();
                e.stopPropagation();

                const startX = e.clientX;
                const startY = e.clientY;
                const startWidth = textarea.offsetWidth;
                const startHeight = textarea.offsetHeight;
                textarea.classList.add('resizing');

                function onMouseMove(moveEvent) {
                    const nextWidth = Math.max(120, startWidth + moveEvent.clientX - startX);
                    const nextHeight = Math.max(24, startHeight + moveEvent.clientY - startY);
                    textarea.style.setProperty('width', `${nextWidth}px`, 'important');
                    textarea.style.setProperty('height', `${nextHeight}px`, 'important');
                    onResize();
                }

                function onMouseUp() {
                    textarea.classList.remove('resizing');
                    document.removeEventListener('mousemove', onMouseMove);
                    document.removeEventListener('mouseup', onMouseUp);
                    onResize();
                }

                document.addEventListener('mousemove', onMouseMove);
                document.addEventListener('mouseup', onMouseUp);
            });

            return grip;
        }

        function createRenderProgress(totalBlocks) {
            if (totalBlocks < RENDER_PROGRESS_MIN_BLOCKS) {
                return null;
            }
            const progress = document.createElement('div');
            progress.className = 'coverage-render-progress';
            progress.setAttribute('role', 'status');
            progress.innerText = `Coverage controls: 0/${totalBlocks}`;
            document.body.appendChild(progress);
            return progress;
        }

        function scheduleNextRender(callback) {
            setTimeout(callback, 0);
        }

        function runLegacyPanelRefresh() {
            legacyPanelSyncers.forEach(syncer => syncer());
        }

        requestLegacyPanelRefresh = function(repeatCount = 2) {
            if (legacyPanelSyncers.length === 0) {
                return;
            }
            if (legacyRefreshRequested) {
                return;
            }
            legacyRefreshRequested = true;

            function refreshFrame(left) {
                requestAnimationFrame(() => {
                    runLegacyPanelRefresh();
                    if (left > 1) {
                        refreshFrame(left - 1);
                    } else {
                        legacyRefreshRequested = false;
                    }
                });
            }

            refreshFrame(repeatCount);
        };

        function renderControlsInBatches(onComplete) {
            let index = 0;
            const totalBlocks = blocks.length;
            const progress = createRenderProgress(totalBlocks);
            const startedAt = performance.now();

            function renderBatch() {
                const deadline = performance.now() + RENDER_FRAME_BUDGET_MS;
                const end = Math.min(index + RENDER_BATCH_SIZE, totalBlocks);
                for (; index < end; index++) {
                    if (ACTIVE_MODE === 'immediate') {
                        renderBlockImmediate(blocks[index]);
                    } else {
                        renderBlockPlaceholder(blocks[index]);
                    }
                    if (ACTIVE_MODE === 'immediate' && index + 1 < totalBlocks && performance.now() > deadline) {
                        index += 1;
                        break;
                    }
                }
                requestLegacyPanelRefresh(2);

                if (progress) {
                    const percent = totalBlocks > 0 ? ((index * 100) / totalBlocks).toFixed(1) : '100.0';
                    progress.innerText = `Coverage controls: ${index}/${totalBlocks} (${percent}%)`;
                }

                if (index < totalBlocks) {
                    scheduleNextRender(renderBatch);
                    return;
                }

                if (progress) {
                    const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
                    progress.innerText = `Coverage controls ready: ${totalBlocks} (${elapsed}s)`;
                    setTimeout(() => progress.remove(), 1500);
                }
                blocks = [];
                requestLegacyPanelRefresh(4);
                updateReviewNavigation();
                onComplete();
            }

            scheduleNextRender(renderBatch);
        }

        function getLineCodeInfo(span) {
            const lineText = span.textContent || '';
            const colonIndex = lineText.indexOf(':');
            if (colonIndex === -1) {
                return { prefixLen: 0, codeLen: lineText.length };
            }
            const prefixLen = colonIndex + 2;
            const codeText = lineText.substring(prefixLen);
            return { prefixLen, codeLen: codeText.length };
        }

        function getBlockLayout(block) {
            if (block.layout) {
                return block.layout;
            }
            let maxCodeLen = 0;
            let commonPrefixLen = 0;
            forEachBlockItem(block, item => {
                const info = getLineCodeInfo(item.span);
                if (info.codeLen > maxCodeLen) {
                    maxCodeLen = info.codeLen;
                }
                if (info.prefixLen > 0) {
                    commonPrefixLen = info.prefixLen;
                }
            });
            const targetCodeCol = maxCodeLen <= 120 ? 121 : (maxCodeLen + 2);
            return {
                maxCodeLen,
                commonPrefixLen,
                targetCodeCol,
                absoluteCol: commonPrefixLen + targetCodeCol
            };
        }

        function insertPanelLikeElement(block, element, isLegacyInline, layout) {
            const startLineItem = getBlockStartItem(block);
            if (isLegacyInline) {
                const legacyAlignSpacer = document.createElement('span');
                legacyAlignSpacer.className = 'coverage-legacy-align-spacer';
                legacyAlignSpacer.setAttribute('aria-hidden', 'true');
                legacyAlignSpacer.style.width = `${Math.max(2, layout.targetCodeCol - layout.maxCodeLen)}ch`;
                startLineItem.span.appendChild(legacyAlignSpacer);
                startLineItem.span.appendChild(element);
                element._coverageAlignSpacer = legacyAlignSpacer;
                return;
            }
            element.style.left = `${layout.absoluteCol}ch`;
            startLineItem.span.appendChild(element);
        }

        function renderBlockPlaceholder(block) {
            const startLineItem = getBlockStartItem(block);
            const startLineNum = startLineItem.lineNum;
            const isLegacyInline = startLineItem.legacyInline === true;
            const layout = getBlockLayout(block);

            forEachBlockItem(block, item => {
                if (!isLegacyInline) {
                    item.span.style.setProperty('padding-right', '180px', 'important');
                }
            });

            const placeholder = startLineItem.span;
            placeholder.classList.add('coverage-analysis-placeholder-anchor');
            placeholder.classList.toggle('legacy-inline-fast', isLegacyInline);
            placeholder.setAttribute('contenteditable', 'false');
            placeholder.dataset.startLine = String(startLineNum);
            placeholder.style.setProperty(
                isLegacyInline ? '--coverage-placeholder-margin' : '--coverage-placeholder-left',
                `${isLegacyInline ? Math.max(2, layout.targetCodeCol - layout.maxCodeLen) : layout.absoluteCol}ch`
            );

            const compactBlock = {
                startItem: startLineItem,
                lineNums: getBlockLineNums(block),
                length: getBlockLength(block),
                endLineNum: getBlockEndLineNum(block),
                legacyInline: isLegacyInline,
                layout
            };
            const panelState = {
                block: compactBlock,
                placeholder,
                placeholderIsAnchor: true,
                expanded: false,
                values: {
                    status: '未确认',
                    reviewerInput: '',
                    methodInput: '',
                    reasonInput: ''
                }
            };
            panelsMap.set(startLineNum, panelState);
            decoratePlaceholder(placeholder, panelState.values, compactBlock);

        }

        function renderBlockImmediate(block) {
            const panel = renderBlockPanel(block);
            setStoredPanelValues(panel, {
                status: '未确认',
                reviewerInput: '',
                methodInput: '',
                reasonInput: ''
            });
        }

        function expandBlockPanel(startLineNum) {
            const current = panelsMap.get(startLineNum);
            if (!current || current.expanded) {
                return current;
            }
            const placeholder = current.placeholder;
            const block = current.block;
            const values = Object.assign({}, current.values || {});
            const panel = renderBlockPanel(block);
            setStoredPanelValues(panel, values);
            if ((values.isDraft || (values.status && values.status !== '未确认')) && panel.saveBtn) {
                setPanelPersistedState(panel);
            }
            if (placeholder) {
                if (current.placeholderIsAnchor) {
                    placeholder.classList.remove(
                        'coverage-analysis-placeholder-anchor',
                        'coverage-analysis-placeholder-saved',
                        'saved',
                        'error',
                        'legacy-inline-fast'
                    );
                    placeholder.removeAttribute('data-start-line');
                    placeholder.removeAttribute('data-coverage-label');
                    placeholder.style.removeProperty('--coverage-placeholder-margin');
                    placeholder.style.removeProperty('--coverage-placeholder-left');
                    placeholder.removeAttribute('title');
                } else if (placeholder._coverageAlignSpacer) {
                    placeholder._coverageAlignSpacer.remove();
                    placeholder.remove();
                } else {
                    placeholder.remove();
                }
            }
            return panel;
        }

        function getReviewPanelLineNumbers() {
            return Array.from(panelsMap.keys()).sort((left, right) => left - right);
        }

        function updateReviewNavigation() {
            const lineNumbers = getReviewPanelLineNumbers();
            panelsMap.forEach((panel, lineNumber) => {
                if (!panel.previousBtn || !panel.nextBtn) {
                    return;
                }
                const index = lineNumbers.indexOf(lineNumber);
                const previousLine = index > 0 ? lineNumbers[index - 1] : null;
                const nextLine = index >= 0 && index < lineNumbers.length - 1 ? lineNumbers[index + 1] : null;
                panel.previousBtn.disabled = previousLine === null;
                panel.nextBtn.disabled = nextLine === null;
                panel.previousBtn.title = previousLine === null ? '已是当前文件第一处可填写控件' : `跳转到第 ${previousLine} 行的可填写控件`;
                panel.nextBtn.title = nextLine === null ? '已是当前文件最后一处可填写控件' : `跳转到第 ${nextLine} 行的可填写控件`;
            });
        }

        function focusReviewPanel(panel) {
            const focusTarget = panel && (panel.select || panel.placeholder);
            if (!focusTarget) {
                return;
            }
            if (typeof focusTarget.scrollIntoView === 'function') {
                try {
                    focusTarget.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
                } catch (e) {
                    focusTarget.scrollIntoView(true);
                }
            }
            window.setTimeout(function() {
                if (typeof focusTarget.focus === 'function') {
                    focusTarget.focus();
                }
            }, 350);
        }

        function navigateReviewPanel(currentLineNum, direction) {
            const lineNumbers = getReviewPanelLineNumbers();
            const currentIndex = lineNumbers.indexOf(currentLineNum);
            const targetIndex = currentIndex + direction;
            if (currentIndex === -1 || targetIndex < 0 || targetIndex >= lineNumbers.length) {
                return;
            }
            const targetLineNum = lineNumbers[targetIndex];
            let targetPanel = panelsMap.get(targetLineNum);
            if (targetPanel && !targetPanel.expanded) {
                targetPanel = expandBlockPanel(targetLineNum);
            }
            updateReviewNavigation();
            focusReviewPanel(targetPanel);
        }

        // 3. 构建并注入表单 DOM，仅在 Block 的第一行注入
        function renderBlockPanel(block) {
            const startLineItem = getBlockStartItem(block);
            const startLineNum = startLineItem.lineNum;
            const endLineNum = getBlockEndLineNum(block);
            const isLegacyInline = block.legacyInline !== undefined ? block.legacyInline : startLineItem.legacyInline === true;
            const blockLength = getBlockLength(block);
            const isMultiLine = !isLegacyInline && blockLength > 1;

            // 动态为当前 Block 的所有物理行 span 设置 padding-right，以防止其内容与悬浮的表单面板重叠
            forEachBlockItem(block, item => {
                if (!isLegacyInline) {
                    item.span.style.setProperty('padding-right', '550px', 'important');
                }
            });

            const panel = document.createElement('span');
            panel.className = 'coverage-analysis-panel' + (isMultiLine ? ' multiline' : '') + (isLegacyInline ? ' legacy-overlay' : '');
            panel.setAttribute('contenteditable', 'false');

            const layout = getBlockLayout(block);

            // 完美等宽字体对位
            if (!isLegacyInline) {
                panel.style.left = `${layout.absoluteCol}ch`;
            }

            if (isMultiLine && !isLegacyInline) {
                // 完美契合 Block 的多行物理高度 (每行 24px)
                panel.style.height = `${blockLength * 24 - 4}px`;
            }

            // 状态下拉框 (默认第一列为未确认/可覆盖/无法覆盖)
            const select = document.createElement('select');
            select.className = 'coverage-analysis-select';
            STATUS_OPTIONS.forEach(opt => {
                const option = document.createElement('option');
                option.value = opt;
                option.text = opt;
                select.appendChild(option);
            });

            // 确认人输入框 (第二列)
            const reviewerInput = document.createElement('input');
            reviewerInput.type = 'text';
            reviewerInput.className = 'coverage-analysis-input reviewer-input';
            reviewerInput.placeholder = '确认人';

            // 条件覆盖方法
            const methodInput = document.createElement('textarea');
            methodInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            methodInput.placeholder = '条件覆盖方法';

            // 无条件覆盖原因
            const reasonInput = document.createElement('textarea');
            reasonInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            reasonInput.placeholder = '无条件覆盖原因';
            let onPanelResize = function() {};
            const methodResizeGrip = isLegacyInline ? null : createResizeGrip(methodInput, () => onPanelResize());
            const reasonResizeGrip = isLegacyInline ? null : createResizeGrip(reasonInput, () => onPanelResize());

            // 徽章渲染逻辑 (只在合并区间大于 1 行时才展示跨行徽章，避免视觉冗余)
            let badgeSpan = null;
            if (isMultiLine) {
                badgeSpan = document.createElement('span');
                badgeSpan.className = 'coverage-block-badge';
                badgeSpan.innerText = `L${startLineNum}-${endLineNum}`;
                badgeSpan.title = `此分析跨越并同时保存第 ${startLineNum} 至 ${endLineNum} 行代码`;
            }

            // 保存按钮
            const saveBtn = document.createElement('button');
            saveBtn.className = 'coverage-analysis-btn';
            saveBtn.innerText = 'Save';

            const inheritBtn = document.createElement('button');
            inheritBtn.className = 'coverage-inherit-btn';
            inheritBtn.type = 'button';
            inheritBtn.innerText = '继承';
            inheritBtn.title = '继承上一条已填写的分析结果';

            const previousBtn = document.createElement('button');
            previousBtn.className = 'coverage-navigation-btn';
            previousBtn.type = 'button';
            previousBtn.innerText = '上一个';
            previousBtn.setAttribute('aria-label', '跳转到上一个可填写控件');

            const nextBtn = document.createElement('button');
            nextBtn.className = 'coverage-navigation-btn';
            nextBtn.type = 'button';
            nextBtn.innerText = '下一个';
            nextBtn.setAttribute('aria-label', '跳转到下一个可填写控件');

            // 组合面板
            panel.appendChild(select);
            // 导航按钮必须紧邻状态选择框：后面的两个说明输入框较宽，
            // 放在末尾时容易落到报告页面的横向可视区域之外。
            panel.appendChild(previousBtn);
            panel.appendChild(nextBtn);
            panel.appendChild(inheritBtn);
            panel.appendChild(reviewerInput);
            panel.appendChild(methodInput);
            if (methodResizeGrip) {
                panel.appendChild(methodResizeGrip);
            }
            panel.appendChild(reasonInput);
            if (reasonResizeGrip) {
                panel.appendChild(reasonResizeGrip);
            }
            if (badgeSpan) {
                panel.appendChild(badgeSpan);
            }
            panel.appendChild(saveBtn);

            function positionLegacyPanel() {
                const lineNumEl = startLineItem.lineNumSpan || startLineItem.span.previousElementSibling || startLineItem.span;
                const lineRects = lineNumEl.getClientRects();
                const lineRect = lineRects.length > 0 ? lineRects[0] : lineNumEl.getBoundingClientRect();
                const codeRects = startLineItem.span.getClientRects();
                const codeRect = codeRects.length > 0 ? codeRects[codeRects.length - 1] : startLineItem.span.getBoundingClientRect();
                if (!lineRect || !codeRect || (lineRect.top === 0 && codeRect.top === 0)) {
                    return;
                }

                const preStyle = window.getComputedStyle(preSource);
                const canvas = positionLegacyPanel.canvas || (positionLegacyPanel.canvas = document.createElement('canvas'));
                const ctx = canvas.getContext('2d');
                ctx.font = `${preStyle.fontStyle} ${preStyle.fontVariant} ${preStyle.fontWeight} ${preStyle.fontSize} ${preStyle.fontFamily}`;
                const sampleWidth = ctx.measureText('M').width || parseFloat(preStyle.fontSize) || 12;
                const codeText = startLineItem.span.innerText || '';
                const colonIndex = codeText.indexOf(':');
                const codePrefix = colonIndex >= 0 ? codeText.substring(0, colonIndex + 2) : '';
                const codeStartX = codeRect.left + ctx.measureText(codePrefix).width;
                const defaultX = codeStartX + sampleWidth * 120;
                const codeEndX = codeRect.left + ctx.measureText(codeText).width;
                const left = window.scrollX + Math.max(defaultX, codeEndX + 24, lineRect.right + 24);
                const anchorRect = startLineItem.span === lineNumEl ? lineRect : codeRect;
                const rowTop = anchorRect.top;
                const rowBottom = anchorRect.bottom;
                const panelHeight = panel.offsetHeight || 20;
                const top = window.scrollY + rowTop + Math.max(0, (rowBottom - rowTop - panelHeight) / 2);

                panel.style.setProperty('left', `${left}px`, 'important');
                panel.style.setProperty('top', `${top}px`, 'important');
                panel.style.setProperty('visibility', 'visible', 'important');
            }

            let legacyRowSpacer = null;
            let legacySpacerHeight = -1;

            function ensureLegacyRowSpacer() {
                if (!legacyRowSpacer) {
                    legacyRowSpacer = document.createElement('span');
                    legacyRowSpacer.className = 'coverage-row-spacer';
                    legacyRowSpacer.setAttribute('aria-hidden', 'true');
                    startLineItem.span.appendChild(legacyRowSpacer);
                }
                return legacyRowSpacer;
            }

            function syncLegacyRowHeight() {
                if (!legacyRowSpacer) {
                    return;
                }
                const lineNumEl = startLineItem.lineNumSpan || startLineItem.span.previousElementSibling || startLineItem.span;
                const lineRect = lineNumEl.getBoundingClientRect();
                const panelHeight = Math.ceil(panel.getBoundingClientRect().height || panel.offsetHeight || 0);
                const lineHeight = Math.ceil(lineRect.height || parseFloat(window.getComputedStyle(preSource).lineHeight) || 20);
                const nextHeight = panelHeight > lineHeight + 6 ? panelHeight + 4 : 0;
                if (nextHeight !== legacySpacerHeight) {
                    legacySpacerHeight = nextHeight;
                    legacyRowSpacer.style.height = `${nextHeight}px`;
                    requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
                }
            }

            // 追加控件。旧 genhtml 的源码区是 <pre> 文本流，不能把控件插入其中，否则会破坏原始排版。
            if (isLegacyInline) {
                panel.classList.remove('legacy-overlay');
                panel.classList.add('legacy-inline-fast');
                const legacyAlignSpacer = document.createElement('span');
                legacyAlignSpacer.className = 'coverage-legacy-align-spacer';
                legacyAlignSpacer.setAttribute('aria-hidden', 'true');
                legacyAlignSpacer.style.width = `${Math.max(2, layout.targetCodeCol - layout.maxCodeLen)}ch`;
                startLineItem.span.appendChild(legacyAlignSpacer);
                startLineItem.span.appendChild(panel);
                onPanelResize = function() {};
            } else {
                startLineItem.span.appendChild(panel);
                onPanelResize = function() {};
            }

            // 存储该 Block 面板的映射，key 设为首行行号
            panelsMap.set(startLineNum, {
                select,
                reviewerInput,
                methodInput,
                reasonInput,
                saveBtn,
                previousBtn,
                nextBtn,
                block,
                expanded: true,
                values: {
                    status: select.value,
                    reviewerInput: reviewerInput.value,
                    methodInput: methodInput.value,
                    reasonInput: reasonInput.value
                }
            });

            updateReviewNavigation();

            // 点击 Save 进行强规则校验和并发批量入库
            saveBtn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();

                const reviewerVal = reviewerInput.value.trim();
                const statusVal = select.value;
                const methodVal = methodInput.value.trim();
                const reasonVal = reasonInput.value.trim();

                // 规则一：状态必须非默认值（“可覆盖”或“无法覆盖”）
                if (statusVal === '未确认') {
                    alert(`[校验失败]：请将第一列状态变更为“可覆盖”或“无法覆盖”！`);
                    select.focus();
                    return;
                }

                // 规则二：第二列“确认人”必填
                if (!reviewerVal) {
                    alert(`[校验失败]：请输入第二列确认人！`);
                    reviewerInput.focus();
                    return;
                }

                // 规则三：条件覆盖方法与无条件覆盖原因必须二选一
                if (!methodVal && !reasonVal) {
                    alert(`[校验失败]：“条件覆盖方法”与“无条件覆盖原因”必须填写其中之一！`);
                    methodInput.focus();
                    return;
                }

                // 批量保存该 Block 内的所有行号
                saveBlockLineAnalysis(filePath, block, reviewerVal, statusVal, methodVal, reasonVal, saveBtn);
            });

            inheritBtn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();

                const previous = findPreviousFilledPanel(startLineNum);
                if (!previous) {
                    alert('没有找到上一条已填写的分析结果。');
                    return;
                }

                select.value = getStoredPanelValue(previous, 'status') || '未确认';
                reviewerInput.value = getStoredPanelValue(previous, 'reviewerInput');
                methodInput.value = getStoredPanelValue(previous, 'methodInput');
                reasonInput.value = getStoredPanelValue(previous, 'reasonInput');
                saveBtn.innerText = 'Save';
                saveBtn.className = 'coverage-analysis-btn';
                markPanelDirty(startLineNum);
            });

            previousBtn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();
                navigateReviewPanel(startLineNum, -1);
            });

            nextBtn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();
                navigateReviewPanel(startLineNum, 1);
            });

            select.addEventListener('change', function() {
                if (saveBtn.className.includes('saved')) {
                    saveBtn.innerText = 'Save';
                    saveBtn.className = 'coverage-analysis-btn';
                }
                const panel = panelsMap.get(startLineNum);
                if (panel) {
                    setStoredPanelValues(panel, { status: select.value });
                }
                markPanelDirty(startLineNum);
            });

            [reviewerInput, methodInput, reasonInput].forEach(input => {
                input.addEventListener('input', function() {
                    const panel = panelsMap.get(startLineNum);
                    if (!panel) {
                        return;
                    }
                    setStoredPanelValues(panel, {
                        reviewerInput: reviewerInput.value,
                        methodInput: methodInput.value,
                        reasonInput: reasonInput.value
                    });
                    markPanelDirty(startLineNum);
                });
            });

            return panelsMap.get(startLineNum);
        }

        function validatePanelForConfirm(startLineNum, panel) {
            const values = getPanelBatchPayload(panel);
            if (!CONFIRMED_STATUS_SET.has(values.status)) {
                return `第 ${startLineNum} 行：请选择“可覆盖”、“无法覆盖”或“冗余代码”。`;
            }
            if (!values.reviewer) {
                return `第 ${startLineNum} 行：请输入确认人。`;
            }
            if (!values.coverage_method && !values.uncovered_reason) {
                return `第 ${startLineNum} 行：请填写条件覆盖方法或无条件覆盖原因。`;
            }
            return '';
        }

        function submitDirtyPanels(mode) {
            const startLineNumbers = Array.from(dirtyPanelStartLines)
                .filter(startLineNum => panelsMap.has(startLineNum))
                .sort((left, right) => left - right);
            if (!startLineNumbers.length || !batchToolbarState || batchToolbarState.submitting) {
                return;
            }

            const panels = startLineNumbers.map(startLineNum => ({
                startLineNum,
                panel: panelsMap.get(startLineNum)
            }));
            if (mode === 'confirm') {
                for (const item of panels) {
                    const message = validatePanelForConfirm(item.startLineNum, item.panel);
                    if (message) {
                        alert(`[校验失败] ${message}`);
                        focusReviewPanel(item.panel);
                        return;
                    }
                }
            }

            batchToolbarState.submitting = true;
            updateBatchToolbar();
            saveReviewBlocksBatch(
                filePath,
                panels.map(item => getPanelBatchPayload(item.panel)),
                mode
            ).then(result => {
                panels.forEach(item => clearPanelDirty(item.startLineNum, mode === 'draft'));
                notifyProgressChanged();
                updateHeaderStatistics();
                requestLegacyPanelRefresh(3);
                console.log(`[CoverageEnhance] ${mode} batch saved: ${result.saved_blocks} block(s), ${result.saved_lines} line(s).`);
            }).catch(error => {
                console.error('[CoverageEnhance] Batch save failed:', error);
                alert(`批量${mode === 'confirm' ? '确认提交' : '暂存'}失败：${error.message}`);
            }).then(() => {
                batchToolbarState.submitting = false;
                updateBatchToolbar();
            });
        }

        function createBatchToolbar() {
            const container = document.createElement('div');
            container.className = 'coverage-batch-toolbar';
            container.setAttribute('contenteditable', 'false');
            container.setAttribute('aria-label', '批量暂存和确认提交');

            const count = document.createElement('span');
            count.className = 'coverage-batch-count';

            const locateBtn = document.createElement('button');
            locateBtn.className = 'coverage-batch-btn locate';
            locateBtn.type = 'button';
            locateBtn.innerText = '定位首个待填写';
            locateBtn.title = '展开并跳转到当前文件首个未确认或暂存的填写控件';

            const draftBtn = document.createElement('button');
            draftBtn.className = 'coverage-batch-btn draft';
            draftBtn.type = 'button';
            draftBtn.title = '将当前文件内已修改的控件作为草稿批量保存，可暂不填写完整信息';

            const confirmBtn = document.createElement('button');
            confirmBtn.className = 'coverage-batch-btn confirm';
            confirmBtn.type = 'button';
            confirmBtn.title = '校验当前文件内已修改的控件后批量确认提交';

            locateBtn.addEventListener('click', function() {
                const pendingEntry = Array.from(panelsMap.entries())
                    .sort((left, right) => left[0] - right[0])
                    .find(entry => isPanelAwaitingReview(entry[1]));
                if (!pendingEntry) {
                    alert('当前文件没有待填写的控件。');
                    return;
                }
                let targetPanel = pendingEntry[1];
                if (!targetPanel.expanded) {
                    targetPanel = expandBlockPanel(pendingEntry[0]);
                }
                updateReviewNavigation();
                focusReviewPanel(targetPanel);
            });
            draftBtn.addEventListener('click', function() {
                submitDirtyPanels('draft');
            });
            confirmBtn.addEventListener('click', function() {
                submitDirtyPanels('confirm');
            });

            container.appendChild(count);
            container.appendChild(locateBtn);
            container.appendChild(draftBtn);
            container.appendChild(confirmBtn);
            document.body.appendChild(container);
            batchToolbarState = { container, count, locateBtn, draftBtn, confirmBtn, submitting: false };
            updateBatchToolbar();

            window.addEventListener('beforeunload', function(event) {
                if (!dirtyPanelStartLines.size) {
                    return undefined;
                }
                const message = '当前页面有未暂存的填写内容。';
                event.preventDefault();
                event.returnValue = message;
                return message;
            });
        }

        function createModeToggler() {
            const container = document.createElement('div');
            container.className = 'coverage-mode-toggler-container';
            container.setAttribute('contenteditable', 'false');

            const label = document.createElement('span');
            label.className = 'coverage-mode-toggler-label';
            label.innerText = '显示模式: ';

            const select = document.createElement('select');
            select.className = 'coverage-mode-toggler-select';

            const optLazy = document.createElement('option');
            optLazy.value = 'lazy';
            optLazy.innerText = '轻量占位 (懒加载)';
            if (ACTIVE_MODE === 'lazy') optLazy.selected = true;

            const optImmediate = document.createElement('option');
            optImmediate.value = 'immediate';
            optImmediate.innerText = '完整显示 (立即生成)';
            if (ACTIVE_MODE === 'immediate') optImmediate.selected = true;

            select.appendChild(optLazy);
            select.appendChild(optImmediate);

            select.addEventListener('change', function() {
                const newMode = select.value;
                const url = new URL(window.location.href);
                url.searchParams.set('mode', newMode);
                window.location.href = url.toString();
            });

            container.appendChild(label);
            container.appendChild(select);
            document.body.appendChild(container);
        }

        renderControlsInBatches(function() {
            // 4. 异步拉取并回显已有数据
            fetchLineAnalysis(filePath, function() {
                reviewControlsReady = true;
                updateBatchToolbar();
            });
        });

        // 5. 创建高级浮动显示模式切换器
        createModeToggler();
        createBatchToolbar();
    });

    function findPreviousFilledPanel(currentLineNum) {
        const candidates = Array.from(panelsMap.entries())
            .filter(([lineNum, panel]) => {
                const status = panel.select ? panel.select.value : (panel.values && panel.values.status);
                return lineNum < currentLineNum && status !== '未确认';
            })
            .sort((a, b) => b[0] - a[0]);

        for (const [, panel] of candidates) {
            const reviewer = panel.reviewerInput ? panel.reviewerInput.value : (panel.values && panel.values.reviewerInput);
            const method = panel.methodInput ? panel.methodInput.value : (panel.values && panel.values.methodInput);
            const reason = panel.reasonInput ? panel.reasonInput.value : (panel.values && panel.values.reasonInput);
            const hasContent = (reviewer || '').trim() || (method || '').trim() || (reason || '').trim();
            if (hasContent) {
                return panel;
            }
        }
        return null;
    }

    /**
     * 动态计算并更新表头中的 Analysis 统计行 (最下方展示，高精度物理行比对)
     */
    function updateHeaderStatistics() {
        let confirmedCount = 0;

        // 统计已分析确认的行数 (只要 Block 首行分析被保存，其所覆盖的所有物理行数均计入)
        panelsMap.forEach((panel, startLineNum) => {
            const status = panel.select ? panel.select.value : (panel.values && panel.values.status);
            const isDraft = panel.values && panel.values.isDraft === true;
            if (!isDraft && CONFIRMED_STATUS_SET.has(status)) {
                confirmedCount += panel.block.length;
            }
        });

        // 分析确认率 (Confirmed Rate)，无有效保存时默认 0.0%
        const confirmedRatio = totalUncovered > 0 ? ((confirmedCount / totalUncovered) * 100).toFixed(1) : '0.0';

        const linesTd = Array.from(document.querySelectorAll('td.headerItem')).find(td => td.innerText.startsWith('Lines:'));
        if (!linesTd) return;

        const linesTr = linesTd.parentElement;
        if (!linesTr) return;

        const tableBody = linesTr.parentNode;

        let reviewTr = document.getElementById('coverage-review-statistics-tr');
        if (!reviewTr) {
            reviewTr = document.createElement('tr');
            reviewTr.id = 'coverage-review-statistics-tr';
            tableBody.appendChild(reviewTr);
        }

        reviewTr.innerHTML = '';

        // td 0: Analysis 标题
        const td0 = document.createElement('td');
        td0.className = 'headerItem';
        td0.innerText = 'Analysis:';
        
        // td 1: 标记文字 (指示分析确认率)
        const td1 = document.createElement('td');
        td1.className = 'headerValue';
        td1.innerText = 'Confirmed Rate';

        // td 2: 空白间隔
        const td2 = document.createElement('td');

        // td 3: 统计列标题
        const td3 = document.createElement('td');
        td3.className = 'headerItem';
        td3.innerText = 'Review:';

        // td 4: 分析确认率 (默认 0.0%，并支持三段过渡高亮)
        const td4 = document.createElement('td');
        td4.style.textAlign = 'right';
        td4.style.fontWeight = 'bold';
        td4.style.paddingRight = '4px';
        td4.innerText = `${confirmedRatio} %`;
        
        const ratioFloat = parseFloat(confirmedRatio);
        if (ratioFloat === 0.0) {
            td4.className = 'coverage-ratio-low'; // 0% 红色
        } else if (ratioFloat === 100.0) {
            td4.className = 'coverage-ratio-hi';  // 100% 绿色
        } else {
            td4.className = 'coverage-ratio-med'; // 0% ~ 100% 橙色
        }

        // td 5: 未覆盖总行数
        const td5 = document.createElement('td');
        td5.className = 'headerCovTableEntry';
        td5.style.textAlign = 'right';
        td5.style.fontWeight = 'bold';
        td5.innerText = totalUncovered;

        // td 6: 已分析确认物理行数
        const td6 = document.createElement('td');
        td6.className = 'headerCovTableEntry';
        td6.style.textAlign = 'right';
        td6.style.fontWeight = 'bold';
        td6.innerText = confirmedCount;

        reviewTr.appendChild(td0);
        reviewTr.appendChild(td1);
        reviewTr.appendChild(td2);
        reviewTr.appendChild(td3);
        reviewTr.appendChild(td4);
        reviewTr.appendChild(td5);
        reviewTr.appendChild(td6);
    }

    /**
     * 向后端获取已有分析记录并回显到表单中
     */
    function fetchLineAnalysis(filePath, onComplete) {
        const url = `${SERVER_URL}?project=${encodeURIComponent(DEFAULT_PROJECT)}&file=${encodeURIComponent(filePath)}`;
        
        fetch(url, {
            method: 'GET',
            mode: 'cors'
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            if (data && data.records) {
                console.log(`[CoverageEnhance] Fetched ${data.records.length} records from database.`);
                
                const dbRecordsMap = new Map();
                data.records.forEach(rec => {
                    dbRecordsMap.set(rec.line_number, rec);
                });

                // 对每一个被我们隔离出来的 Block，检查其首行数据库是否已被填报过
                panelsMap.forEach((panel, startLineNum) => {
                    const rec = dbRecordsMap.get(startLineNum);
                    if (rec && !dirtyPanelStartLines.has(startLineNum)) {
                        setStoredPanelValues(panel, {
                            status: rec.status || '未确认',
                            isDraft: rec.is_draft === true || rec.is_draft === 1 || rec.is_draft === '1',
                            reviewerInput: rec.reviewer || '',
                            methodInput: rec.coverage_method || '',
                            reasonInput: rec.uncovered_reason || ''
                        });
                        setPanelPersistedState(panel);
                        if (panel.placeholder) {
                            decoratePlaceholder(panel.placeholder, panel.values, panel.block);
                        }
                    }
                });
            }
            requestLegacyPanelRefresh(4);
            updateHeaderStatistics();
            if (typeof onComplete === 'function') {
                onComplete();
            }
        })
        .catch(err => {
            console.warn('[CoverageEnhance] Failed to fetch existing records:', err.message);
            panelsMap.forEach(panel => {
                if (panel.saveBtn) {
                    panel.saveBtn.innerText = 'Offline';
                    panel.saveBtn.classList.add('error');
                    panel.saveBtn.title = '无法连接到本地持久化服务，请检查 enhance_coverage.py 服务是否在运行。';
                }
                if (panel.placeholder) {
                    panel.placeholder.classList.add('error');
                    panel.placeholder.classList.add('coverage-analysis-placeholder-error');
                    panel.placeholder.title = '无法连接到本地持久化服务，点击仍可填写，保存前请检查 enhance_coverage.py 服务。';
                }
            });
            requestLegacyPanelRefresh(4);
            updateHeaderStatistics();
            if (typeof onComplete === 'function') {
                onComplete();
            }
        });
    }

    /**
     * Confirm and persist one analysis block through the same transactional
     * batch endpoint used by the page-level draft/confirm toolbar.
     */
    function saveBlockLineAnalysis(filePath, block, reviewer, status, method, reason, btn) {
        btn.innerText = 'Saving...';
        btn.className = 'coverage-analysis-btn saving';

        const lineNums = getBlockLineNums(block);
        const startLineNum = lineNums[0];
        saveReviewBlocksBatch(filePath, [{
            line_numbers: lineNums,
            reviewer: reviewer,
            status: status,
            coverage_method: method,
            uncovered_reason: reason
        }], 'confirm').then(result => {
            const panel = panelsMap.get(startLineNum);
            if (panel) {
                setStoredPanelValues(panel, {
                    status: status,
                    reviewerInput: reviewer,
                    methodInput: method,
                    reasonInput: reason
                });
            }
            clearPanelDirty(startLineNum, false);
            notifyProgressChanged();
            console.log(`[CoverageEnhance] Confirmed block range L${lineNums[0]}-${lineNums[lineNums.length - 1]} (${result.saved_lines} line(s)).`);
            updateHeaderStatistics();
            requestLegacyPanelRefresh(3);
        }).catch(err => {
            console.error('[CoverageEnhance] Block save failed:', err);
            btn.innerText = 'Error';
            btn.className = 'coverage-analysis-btn error';
            btn.title = `保存失败: ${err.message}. 请重试。`;
            window.setTimeout(() => {
                markPanelDirty(startLineNum);
            }, 3000);
        });
    }
})();
