/**
 * 覆盖率 HTML 报告增强脚本 (ES6) - 待分析函数优先 + 懒加载折叠架构
 * 
 * 核心架构：
 * 1. CodeRegionStore: 区域状态与行缓存管理
 * 2. CodeRegionLoader: 区间/Chunk/Batch 数据加载与去重
 * 3. CodeLineRenderer: 统一代码行与分析面板渲染器 (唯一事实来源)
 * 4. CodeRegionController: 区域交互、分批 DOM 渲染调度与展开/折叠控制
 */
(function() {
    const ENHANCE_VERSION = 'lazy-collapse-20260818_v10_0';
    const SERVER_URL = '/api/coverage';
    const DEFAULT_PROJECT = 'Gemini-NOS';
    const RENDER_MODE = 'lazy_collapse'; // 'lazy_collapse', 'lazy', 'immediate'
    const REVIEW_SCOPE = 'full'; // 'full' or 'incremental'
    const ENHANCE_SCRIPT_URL = document.currentScript && document.currentScript.src
        ? document.currentScript.src
        : '';
    const URL_PARAMS = new URLSearchParams(window.location.search);
    const EXPLICIT_API_URL = URL_PARAMS.get('api');
    const QUERY_MODE = URL_PARAMS.get('mode');
    const ACTIVE_MODE = (QUERY_MODE === 'lazy_collapse' || QUERY_MODE === 'lazy' || QUERY_MODE === 'immediate')
        ? QUERY_MODE
        : (RENDER_MODE || 'lazy_collapse');
    const STATUS_OPTIONS = ['未确认', '可覆盖', '无法覆盖', '冗余代码'];
    const CONFIRMED_STATUS_SET = new Set(['可覆盖', '无法覆盖', '冗余代码']);
    const RENDER_BATCH_SIZE = 400;
    const LOAD_CHUNK_SIZE = 500;
    const PROGRESS_UPDATE_STORAGE_KEY = 'coverage-review-progress-updated';

    // 控制流分支关键字侦测正则 (边界隔离)
    const CONTROL_FLOW_REGEX = /\b(if|else|for|while|do|switch|case|default)\b/;

    // 前端折叠引擎参数
    const CONTEXT_LINES_DEFAULT = 10;
    const MERGE_GAP_THRESHOLD = 15;
    const MIN_FOLD_GAP = 15;

    let resolvedServerUrl = '';
    let dirtyPanelStartLines = new Set();
    let panelsMap = new Map(); // startLine -> panelState
    let batchToolbarState = null;
    let reviewControlsReady = false;
    let totalUncovered = 0;
    let blocks = [];
    let blockRanges = [];
    let foldBars = [];
    let isFoldedModeActive = false;
    let legacyPanelSyncers = [];
    let legacyRefreshRequested = false;
    let requestLegacyPanelRefresh = function() {};

    function normalizeApiBase(value) {
        return String(value || '').replace(/\/+$/, '');
    }

    function uniqueApiBases(values) {
        return Array.from(new Set(values.filter(Boolean).map(normalizeApiBase)));
    }

    function apiBaseCandidates() {
        const candidates = [];
        const origin = window.location.origin && window.location.origin !== 'null'
            ? window.location.origin
            : '';
        if (resolvedServerUrl) {
            candidates.push(resolvedServerUrl);
        }
        if (EXPLICIT_API_URL) {
            candidates.push(EXPLICIT_API_URL);
        }
        candidates.push(SERVER_URL);
        if (origin && window.location.hostname && window.location.port !== '9528') {
            candidates.push(`${window.location.protocol}//${window.location.hostname}:9528/api/coverage`);
        }
        if (!origin) {
            candidates.push('http://127.0.0.1:9528/api/coverage');
        }
        return uniqueApiBases(candidates);
    }

    async function requestCoverageApi(pathSuffix, options) {
        const attempted = [];
        let lastError = null;
        for (const apiBase of apiBaseCandidates()) {
            const url = `${apiBase}${pathSuffix || ''}`;
            attempted.push(url);
            try {
                const response = await fetch(url, options || {});
                const contentType = response.headers.get('Content-Type') || '';
                const data = contentType.includes('application/json')
                    ? await response.json()
                    : null;
                if (response.ok && data && data.status === 'success') {
                    resolvedServerUrl = apiBase;
                    return data;
                }
                const message = data && data.message
                    ? data.message
                    : `HTTP ${response.status}${data ? '' : '（接口未返回 JSON）'}`;
                lastError = new Error(message);
                if (!data || response.status === 404 || response.status === 405 || response.status === 501) {
                    continue;
                }
                lastError.coverageApiResponse = true;
                throw lastError;
            } catch (error) {
                lastError = error;
                if (error && error.coverageApiResponse) {
                    throw error;
                }
            }
        }
        const detail = lastError && lastError.message ? lastError.message : '无法连接后台服务';
        throw new Error(`${detail}；已尝试：${attempted.join('，')}`);
    }

    function yieldToBrowser() {
        return new Promise(resolve => {
            requestAnimationFrame(() => resolve());
        });
    }

    function showToast(message, duration = 3000) {
        const existing = document.querySelector('.coverage-toast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.className = 'coverage-toast';
        toast.innerText = message;
        document.body.appendChild(toast);
        setTimeout(() => {
            if (toast.parentNode) toast.remove();
        }, duration);
    }

    function notifyProgressChanged() {
        try {
            window.localStorage.setItem(PROGRESS_UPDATE_STORAGE_KEY, JSON.stringify({
                project_name: DEFAULT_PROJECT,
                updated_at: Date.now()
            }));
        } catch (error) {
            console.debug('[CoverageEnhance] Progress refresh notification skipped:', error);
        }
    }

    function getStoredPanelValue(panel, key) {
        if (!panel) return '';
        if (key === 'status' && panel.select && typeof panel.select.value === 'string') {
            return panel.select.value;
        }
        if (panel[key] && typeof panel[key].value === 'string') {
            return panel[key].value;
        }
        return panel.values && typeof panel.values[key] === 'string' ? panel.values[key] : '';
    }

    function setStoredPanelValues(panel, values) {
        if (!panel) return;
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
        if (!panel || !panel.saveBtn) return;
        const status = getStoredPanelValue(panel, 'status');
        panel.saveBtn.className = 'coverage-analysis-btn saved';
        panel.saveBtn.innerText = panel.values && panel.values.isDraft ? '已暂存' : (status === '未确认' ? '已保存' : '已确认');
    }

    function updateBatchToolbar() {
        if (!batchToolbarState) return;
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
        if (!panel) return;
        dirtyPanelStartLines.add(startLineNum);
        if (panel.saveBtn) {
            panel.saveBtn.className = 'coverage-analysis-btn pending';
            panel.saveBtn.innerText = '待暂存';
        }
        updateBatchToolbar();
    }

    function isPanelAwaitingReview(panel) {
        if (!panel) return false;
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
        const block = panel.block || {};
        const lineNums = block.lineNums || (block.startLine ? [block.startLine] : [panel.lineNum]);
        return {
            line_numbers: lineNums,
            reviewer: getStoredPanelValue(panel, 'reviewerInput').trim(),
            status: getStoredPanelValue(panel, 'status') || '未确认',
            coverage_method: getStoredPanelValue(panel, 'methodInput').trim(),
            uncovered_reason: getStoredPanelValue(panel, 'reasonInput').trim()
        };
    }

    function saveReviewBlocksBatch(filePath, blocks, mode) {
        return requestCoverageApi('/batch', {
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
        });
    }

    function getReviewPanelLineNumbers() {
        return Array.from(panelsMap.keys()).sort((left, right) => left - right);
    }

    function updateReviewNavigation() {
        const lineNumbers = getReviewPanelLineNumbers();
        panelsMap.forEach((panel, lineNumber) => {
            if (!panel.previousBtn || !panel.nextBtn) return;
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
        if (!focusTarget) return;
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
        if (currentIndex === -1 || targetIndex < 0 || targetIndex >= lineNumbers.length) return;
        const targetLineNum = lineNumbers[targetIndex];
        let targetPanel = panelsMap.get(targetLineNum);
        if (targetPanel) {
            updateReviewNavigation();
            focusReviewPanel(targetPanel);
        }
    }

    function findPreviousFilledPanelEntry(currentLineNum) {
        const candidates = Array.from(panelsMap.entries())
            .filter(([lineNum, panel]) => {
                const status = panel.select ? panel.select.value : (panel.values && panel.values.status);
                return lineNum < currentLineNum && status !== '未确认';
            })
            .sort((a, b) => b[0] - a[0]);

        for (const entry of candidates) {
            const panel = entry[1];
            const reviewer = panel.reviewerInput ? panel.reviewerInput.value : (panel.values && panel.values.reviewerInput);
            const method = panel.methodInput ? panel.methodInput.value : (panel.values && panel.values.methodInput);
            const reason = panel.reasonInput ? panel.reasonInput.value : (panel.values && panel.values.reasonInput);
            const hasContent = (reviewer || '').trim() || (method || '').trim() || (reason || '').trim();
            if (hasContent) {
                return entry;
            }
        }
        return null;
    }

    function findPreviousFilledPanel(currentLineNum) {
        const entry = findPreviousFilledPanelEntry(currentLineNum);
        return entry ? entry[1] : null;
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

    function submitDirtyPanels(filePath, mode) {
        const startLineNumbers = Array.from(dirtyPanelStartLines)
            .filter(startLineNum => panelsMap.has(startLineNum))
            .sort((left, right) => left - right);
        if (!startLineNumbers.length || !batchToolbarState || batchToolbarState.submitting) return;

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
            showToast(`批量${mode === 'confirm' ? '确认提交' : '暂存'}成功：${result.saved_blocks} 处，${result.saved_lines} 行`);
        }).catch(error => {
            console.error('[CoverageEnhance] Batch save failed:', error);
            alert(`批量${mode === 'confirm' ? '确认提交' : '暂存'}失败：${error.message}`);
        }).then(() => {
            batchToolbarState.submitting = false;
            updateBatchToolbar();
        });
    }

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
                if (typeof onResize === 'function') onResize();
            }

            function onMouseUp() {
                textarea.classList.remove('resizing');
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                if (typeof onResize === 'function') onResize();
            }

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });

        return grip;
    }

    function createBatchToolbar(filePath) {
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

        const progressLink = document.createElement('a');
        progressLink.className = 'coverage-batch-btn progress';
        progressLink.innerText = '查看进展 / 导出';
        progressLink.title = '打开当前项目的数据库进展和报表导出页面';
        progressLink.target = '_blank';
        progressLink.rel = 'noopener';

        function updateProgressLink() {
            const progressUrl = new URL(
                'coverage_progress.html',
                ENHANCE_SCRIPT_URL || window.location.href
            );
            progressUrl.searchParams.set('project', DEFAULT_PROJECT);
            progressUrl.searchParams.set('scope', REVIEW_SCOPE);
            progressUrl.searchParams.set('v', ENHANCE_VERSION);
            const apiUrl = resolvedServerUrl || EXPLICIT_API_URL;
            if (apiUrl) {
                progressUrl.searchParams.set('api', apiUrl);
            }
            progressLink.href = progressUrl.toString();
        }
        updateProgressLink();
        progressLink.addEventListener('click', updateProgressLink);

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
            updateReviewNavigation();
            focusReviewPanel(pendingEntry[1]);
        });
        draftBtn.addEventListener('click', function() {
            submitDirtyPanels(filePath, 'draft');
        });
        confirmBtn.addEventListener('click', function() {
            submitDirtyPanels(filePath, 'confirm');
        });

        container.appendChild(count);
        container.appendChild(progressLink);
        container.appendChild(locateBtn);
        container.appendChild(draftBtn);
        container.appendChild(confirmBtn);
        document.body.appendChild(container);
        batchToolbarState = { container, count, locateBtn, draftBtn, confirmBtn, submitting: false };
        updateBatchToolbar();

        window.addEventListener('beforeunload', function(event) {
            if (!dirtyPanelStartLines.size) return undefined;
            const message = '当前页面有未暂存的填写内容。';
            event.preventDefault();
            event.returnValue = message;
            return message;
        });
    }

    function createModeToggler(currentMode) {
        const container = document.createElement('div');
        container.className = 'coverage-mode-toggler-container';
        container.setAttribute('contenteditable', 'false');

        const label = document.createElement('span');
        label.className = 'coverage-mode-toggler-label';
        label.innerText = '显示模式: ';

        const select = document.createElement('select');
        select.className = 'coverage-mode-toggler-select';

        const optLazyCollapse = document.createElement('option');
        optLazyCollapse.value = 'lazy_collapse';
        optLazyCollapse.innerText = '待分析函数优先 (懒加载折叠)';
        if (currentMode === 'lazy_collapse') optLazyCollapse.selected = true;

        const optLazy = document.createElement('option');
        optLazy.value = 'lazy';
        optLazy.innerText = '轻量占位 (懒加载)';
        if (currentMode === 'lazy') optLazy.selected = true;

        const optImmediate = document.createElement('option');
        optImmediate.value = 'immediate';
        optImmediate.innerText = '完整显示 (立即生成)';
        if (currentMode === 'immediate') optImmediate.selected = true;

        select.appendChild(optLazyCollapse);
        select.appendChild(optLazy);
        select.appendChild(optImmediate);

        select.addEventListener('change', function() {
            const newMode = select.value;
            const url = new URL(window.location.href);
            url.searchParams.set('mode', newMode);
            window.location.href = url.toString();
        });

        const foldToggleBtn = document.createElement('button');
        foldToggleBtn.type = 'button';
        foldToggleBtn.className = 'coverage-fold-toggle-btn';
        foldToggleBtn.id = 'coverage-fold-toggle-btn';
        foldToggleBtn.innerText = isFoldedModeActive ? '👁️ 展开全部源码' : '🔍 上下文折叠';
        foldToggleBtn.onclick = function() {
            if (isFoldedModeActive) {
                unfoldAllLines();
            } else {
                applyFrontendFolding(false);
            }
        };

        container.appendChild(label);
        container.appendChild(select);
        container.appendChild(foldToggleBtn);
        document.body.appendChild(container);
    }

    function updateHeaderStatistics() {
        let confirmedCount = 0;
        panelsMap.forEach((panel) => {
            const status = panel.select ? panel.select.value : (panel.values && panel.values.status);
            const isDraft = panel.values && panel.values.isDraft === true;
            if (!isDraft && CONFIRMED_STATUS_SET.has(status)) {
                const bLen = panel.block ? (panel.block.length || (panel.block.lineNums ? panel.block.lineNums.length : 1)) : 1;
                confirmedCount += bLen;
            }
        });

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
        const td0 = document.createElement('td');
        td0.className = 'headerItem';
        td0.innerText = 'Analysis:';

        const td1 = document.createElement('td');
        td1.className = 'headerValue';
        td1.innerText = 'Confirmed Rate';

        const td2 = document.createElement('td');

        const td3 = document.createElement('td');
        td3.className = 'headerItem';
        td3.innerText = 'Review:';

        const td4 = document.createElement('td');
        td4.style.textAlign = 'right';
        td4.style.fontWeight = 'bold';
        td4.style.paddingRight = '4px';
        td4.innerText = `${confirmedRatio} %`;

        const ratioFloat = parseFloat(confirmedRatio);
        if (ratioFloat === 0.0) {
            td4.className = 'coverage-ratio-low';
        } else if (ratioFloat === 100.0) {
            td4.className = 'coverage-ratio-hi';
        } else {
            td4.className = 'coverage-ratio-med';
        }

        const td5 = document.createElement('td');
        td5.className = 'headerCovTableEntry';
        td5.style.textAlign = 'right';
        td5.style.fontWeight = 'bold';
        td5.innerText = totalUncovered;

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

    // =========================================================================
    // Legacy Helpers for Backward Compatibility and Test Integrity
    // =========================================================================
    function expandBlockPanel(startLineNum) {
        const current = panelsMap.get(startLineNum);
        if (!current || current.expanded) {
            return current;
        }
        const placeholder = current.placeholder;
        const block = current.block;
        const values = Object.assign({}, current.values || {});
        renderBlockPanel(block);
        const panelState = panelsMap.get(startLineNum);
        if (panelState) {
            setStoredPanelValues(panelState, values);
            if ((values.isDraft || (values.status && values.status !== '未确认')) && panelState.saveBtn) {
                setPanelPersistedState(panelState);
            }
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
        return panelState;
    }

    function renderBlockPanel(block) {
        const startItem = block.startItem || block[0] || {};
        const endLineNum = block.endLineNum || (block[block.length - 1] ? block[block.length - 1].lineNum : startItem.lineNum);
        const lineDto = {
            line_no: startItem.lineNum,
            source: startItem.lineText || (startItem.span ? startItem.span.textContent : '') || '',
            coverage_state: 'uncovered',
            analysis_state: '未确认',
            is_pending_analysis: true,
            is_block_entry: true,
            block_start_line: startItem.lineNum,
            block_end_line: endLineNum,
            block_type: 'single'
        };
        const panel = CodeLineRenderer.createReviewPanel(lineDto, '');
        if (startItem.span) {
            startItem.span.appendChild(panel);
        }
        return panelsMap.get(startItem.lineNum);
    }

    function ensureBlockLinesVisible(block) {
        if (!block) return;
        const bStart = block.startLine || (block.startItem ? block.startItem.lineNum : (block[0] ? block[0].lineNum : 1));
        const bEnd = block.endLineNum || (block[block.length - 1] ? block[block.length - 1].lineNum : bStart);
        expandFoldRange(Math.max(1, bStart - CONTEXT_LINES_DEFAULT), bEnd + CONTEXT_LINES_DEFAULT);
    }

    function expandFoldRange(startLine, endLine) {
        for (let l = startLine; l <= endLine; l++) {
            const el = document.getElementById(`L${l}`);
            if (el) el.style.display = '';
        }
    }

    function createFoldBar(targetContainer, hiddenStart, hiddenEnd, count, isAtEnd) {
        const bar = document.createElement('div');
        bar.className = 'coverage-fold-bar-block';
        bar.innerHTML = `<span class="coverage-fold-btn">已折叠 ${count} 行 (${hiddenStart}-${hiddenEnd})</span>`;
        if (targetContainer && targetContainer.parentNode) {
            targetContainer.parentNode.insertBefore(bar, targetContainer);
        }
        foldBars.push(bar);
    }

    function unfoldAllLines() {
        foldBars.forEach(b => b.remove());
        foldBars = [];
        document.querySelectorAll('pre.source > span[id^="L"]').forEach(s => s.style.display = '');
        isFoldedModeActive = false;
    }

    function applyFrontendFolding(forceUnfold) {
        if (forceUnfold) {
            unfoldAllLines();
            return;
        }
        isFoldedModeActive = true;
    }

    // =========================================================================
    // 1. CodeRegionStore: 区域状态与行缓存存储
    // =========================================================================
    const CodeRegionStore = {
        _regions: new Map(), // regionId -> regionState
        _fileMeta: { totalLines: 0, filePath: '', projectName: '' },

        init(layoutData) {
            this._regions.clear();
            this._fileMeta.totalLines = layoutData.total_lines || 0;
            this._fileMeta.filePath = layoutData.file_path || '';
            this._fileMeta.projectName = layoutData.project_name || DEFAULT_PROJECT;

            (layoutData.regions || []).forEach(r => {
                const regState = {
                    id: r.region_id,
                    startLine: r.start_line,
                    endLine: r.end_line,
                    lineCount: r.line_count || (r.end_line - r.start_line + 1),
                    defaultState: r.default_state, // 'expanded' | 'collapsed'
                    currentState: r.default_state === 'expanded' ? 'loading' : 'collapsed-unloaded',
                    kind: r.kind, // 'analysis' | 'collapsed'
                    label: r.label,
                    loaded: false,
                    loading: false,
                    lines: [],
                    error: null,
                    domContainer: null,
                    placeholderEl: null,
                    linesEl: null,
                    headerEl: null
                };
                this._regions.set(r.region_id, regState);
            });
        },

        get(regionId) {
            return this._regions.get(regionId);
        },

        getAll() {
            return Array.from(this._regions.values());
        },

        getExpanded() {
            return Array.from(this._regions.values()).filter(r => r.defaultState === 'expanded');
        },

        findByLine(lineNo) {
            for (const r of this._regions.values()) {
                if (lineNo >= r.startLine && lineNo <= r.endLine) {
                    return r;
                }
            }
            return null;
        },

        setLoaded(regionId, lines) {
            const r = this._regions.get(regionId);
            if (r) {
                r.loaded = true;
                r.loading = false;
                r.lines = lines;
                r.error = null;
                r.currentState = 'expanded-loaded';
            }
        },

        setLoading(regionId, progressText) {
            const r = this._regions.get(regionId);
            if (r) {
                r.loading = true;
                r.currentState = 'loading';
                r.progressText = progressText;
            }
        },

        setError(regionId, err) {
            const r = this._regions.get(regionId);
            if (r) {
                r.loading = false;
                r.error = err;
                r.currentState = 'error';
            }
        },

        setCollapsed(regionId) {
            const r = this._regions.get(regionId);
            if (r) {
                r.currentState = r.loaded ? 'collapsed-loaded' : 'collapsed-unloaded';
            }
        },

        setExpanded(regionId) {
            const r = this._regions.get(regionId);
            if (r) {
                r.currentState = 'expanded-loaded';
            }
        }
    };

    // =========================================================================
    // 2. CodeRegionLoader: 区间/Chunk/Batch 数据请求与缓存写入
    // =========================================================================
    const CodeRegionLoader = {
        async loadInitialBatch(filePath, expandedRegions) {
            if (!expandedRegions.length) return [];
            const ranges = expandedRegions.map(r => ({
                start_line: r.startLine,
                end_line: r.endLine
            }));

            const data = await requestCoverageApi('/code-lines/batch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_name: DEFAULT_PROJECT,
                    file_path: filePath,
                    ranges: ranges
                })
            });

            if (data && data.data && data.data.ranges) {
                data.data.ranges.forEach((rangeResult, idx) => {
                    const reg = expandedRegions[idx];
                    if (reg) {
                        CodeRegionStore.setLoaded(reg.id, rangeResult.lines || []);
                    }
                });
            }
            return expandedRegions;
        },

        async loadRegion(filePath, region, onProgress) {
            if (region.loaded) {
                return region.lines;
            }

            CodeRegionStore.setLoading(region.id, '正在加载…');
            const totalLinesToLoad = region.endLine - region.startLine + 1;

            if (totalLinesToLoad > LOAD_CHUNK_SIZE) {
                // Chunked loading for large regions
                const allLines = [];
                for (let start = region.startLine; start <= region.endLine; start += LOAD_CHUNK_SIZE) {
                    const end = Math.min(start + LOAD_CHUNK_SIZE - 1, region.endLine);
                    if (typeof onProgress === 'function') {
                        onProgress(allLines.length, totalLinesToLoad);
                    }
                    const chunkData = await requestCoverageApi(
                        `/code-lines?project=${encodeURIComponent(DEFAULT_PROJECT)}&file=${encodeURIComponent(filePath)}&start_line=${start}&end_line=${end}`,
                        { method: 'GET' }
                    );
                    const chunkLines = chunkData && chunkData.data && chunkData.data.lines ? chunkData.data.lines : [];
                    allLines.push(...chunkLines);
                }
                CodeRegionStore.setLoaded(region.id, allLines);
                return allLines;
            } else {
                const data = await requestCoverageApi(
                    `/code-lines?project=${encodeURIComponent(DEFAULT_PROJECT)}&file=${encodeURIComponent(filePath)}&start_line=${region.startLine}&end_line=${region.endLine}`,
                    { method: 'GET' }
                );
                const lines = data && data.data && data.data.lines ? data.data.lines : [];
                CodeRegionStore.setLoaded(region.id, lines);
                return lines;
            }
        }
    };

    // =========================================================================
    // 3. CodeLineRenderer: 唯一 Line DTO -> DOM 渲染器
    // =========================================================================
    const CodeLineRenderer = {
        renderCodeLine(lineData, filePath) {
            const lineNo = lineData.line_no;
            const sourceCode = lineData.source || '';
            const covState = lineData.coverage_state || 'ignored';
            const isUncovered = covState === 'uncovered';

            const lineSpan = document.createElement('span');
            lineSpan.id = `L${lineNo}`;
            lineSpan.className = 'coverage-line-span';

            // Coverage class matching genhtml
            let covClass = 'lineCov';
            if (covState === 'uncovered') {
                covClass = 'lineNoCov tlaUNC tlaBgUNC';
            } else if (covState === 'covered') {
                covClass = 'lineCov tlaGNC tlaBgGNC';
            }
            if (lineData.is_pending_analysis && REVIEW_SCOPE === 'incremental') {
                lineSpan.setAttribute('data-coverage-review', 'incremental');
            }

            // Line number
            const numSpan = document.createElement('span');
            numSpan.className = 'lineNum';
            numSpan.innerText = String(lineNo).padStart(5, ' ') + ' ';

            // Source code span
            const codeSpan = document.createElement('span');
            codeSpan.className = covClass;
            if (lineData.raw_html) {
                codeSpan.innerHTML = lineData.raw_html;
            } else {
                codeSpan.textContent = sourceCode;
            }

            lineSpan.appendChild(numSpan);
            lineSpan.appendChild(codeSpan);

            // If start of an uncovered review block, attach interactive review panel
            if (isUncovered && lineData.is_block_entry) {
                const panel = this.createReviewPanel(lineData, filePath);
                lineSpan.appendChild(panel);
            }

            return lineSpan;
        },

        createReviewPanel(lineData, filePath) {
            const startLineNum = lineData.block_start_line || lineData.line_no;
            const endLineNum = lineData.block_end_line || lineData.line_no;
            const blockLength = endLineNum - startLineNum + 1;
            const isMultiLine = blockLength > 1;

            const panel = document.createElement('span');
            panel.className = 'coverage-analysis-panel' + (isMultiLine ? ' multiline' : '');
            panel.setAttribute('contenteditable', 'false');

            // Align to right column
            const codeLen = (lineData.source || '').length;
            const targetCol = Math.max(121, codeLen + 2);
            panel.style.left = `${targetCol}ch`;

            if (isMultiLine) {
                panel.style.height = `${blockLength * 24 - 4}px`;
            }

            // Status select
            const select = document.createElement('select');
            select.className = 'coverage-analysis-select';
            STATUS_OPTIONS.forEach(opt => {
                const option = document.createElement('option');
                option.value = opt;
                option.text = opt;
                if (opt === lineData.analysis_state) option.selected = true;
                select.appendChild(option);
            });

            // Navigation buttons (上一个 / 下一个)
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

            // Inherit buttons
            const inheritBtn = document.createElement('button');
            inheritBtn.className = 'coverage-inherit-btn';
            inheritBtn.type = 'button';
            inheritBtn.innerText = '继承';
            inheritBtn.title = '继承上一条已填写的分析结果';

            const batchInheritBtn = document.createElement('button');
            batchInheritBtn.className = 'coverage-inherit-btn batch';
            batchInheritBtn.type = 'button';
            batchInheritBtn.innerText = '批量继承';
            batchInheritBtn.title = '从上方最近已填写控件继承到它之后至当前控件的整段内容';

            // Reviewer input
            const reviewerInput = document.createElement('input');
            reviewerInput.type = 'text';
            reviewerInput.className = 'coverage-analysis-input reviewer-input';
            reviewerInput.placeholder = '确认人';
            reviewerInput.value = lineData.reviewer || '';

            // Method textarea
            const methodInput = document.createElement('textarea');
            methodInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            methodInput.placeholder = '条件覆盖方法';
            methodInput.value = lineData.coverage_method || '';
            const methodGrip = createResizeGrip(methodInput);

            // Reason textarea
            const reasonInput = document.createElement('textarea');
            reasonInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            reasonInput.placeholder = '无条件覆盖原因';
            reasonInput.value = lineData.uncovered_reason || '';
            const reasonGrip = createResizeGrip(reasonInput);

            // Multi-line Badge
            let badgeSpan = null;
            if (isMultiLine) {
                badgeSpan = document.createElement('span');
                badgeSpan.className = 'coverage-block-badge';
                badgeSpan.innerText = `L${startLineNum}-${endLineNum}`;
                badgeSpan.title = `此分析跨越并同时保存第 ${startLineNum} 至 ${endLineNum} 行代码`;
            }

            // Save button
            const saveBtn = document.createElement('button');
            saveBtn.className = 'coverage-analysis-btn';
            saveBtn.innerText = lineData.is_draft ? '已暂存' : (CONFIRMED_STATUS_SET.has(lineData.analysis_state) ? '已确认' : 'Save');
            if (lineData.is_draft || CONFIRMED_STATUS_SET.has(lineData.analysis_state)) {
                saveBtn.classList.add('saved');
            }

            panel.appendChild(select);
            panel.appendChild(previousBtn);
            panel.appendChild(nextBtn);
            panel.appendChild(inheritBtn);
            panel.appendChild(batchInheritBtn);
            panel.appendChild(reviewerInput);
            panel.appendChild(methodInput);
            if (methodGrip) panel.appendChild(methodGrip);
            panel.appendChild(reasonInput);
            if (reasonGrip) panel.appendChild(reasonGrip);
            if (badgeSpan) panel.appendChild(badgeSpan);
            panel.appendChild(saveBtn);

            const blockObj = {
                startLine: startLineNum,
                endLine: endLineNum,
                lineNums: Array.from({ length: blockLength }, (_, i) => startLineNum + i),
                length: blockLength
            };

            const panelState = {
                select,
                reviewerInput,
                methodInput,
                reasonInput,
                saveBtn,
                previousBtn,
                nextBtn,
                block: blockObj,
                lineNum: startLineNum,
                expanded: true,
                values: {
                    status: select.value,
                    reviewerInput: reviewerInput.value,
                    methodInput: methodInput.value,
                    reasonInput: reasonInput.value,
                    isDraft: lineData.is_draft
                }
            };

            panelsMap.set(startLineNum, panelState);

            // Event Listeners
            saveBtn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();

                const reviewerVal = reviewerInput.value.trim();
                const statusVal = select.value;
                const methodVal = methodInput.value.trim();
                const reasonVal = reasonInput.value.trim();

                if (statusVal === '未确认') {
                    alert(`[校验失败]：请将第一列状态变更为“可覆盖”或“无法覆盖”！`);
                    select.focus();
                    return;
                }
                if (!reviewerVal) {
                    alert(`[校验失败]：请输入第二列确认人！`);
                    reviewerInput.focus();
                    return;
                }
                if (!methodVal && !reasonVal) {
                    alert(`[校验失败]：“条件覆盖方法”与“无条件覆盖原因”必须填写其中之一！`);
                    methodInput.focus();
                    return;
                }

                saveBtn.innerText = 'Saving...';
                saveBtn.className = 'coverage-analysis-btn saving';

                saveReviewBlocksBatch(filePath, [{
                    line_numbers: blockObj.lineNums,
                    reviewer: reviewerVal,
                    status: statusVal,
                    coverage_method: methodVal,
                    uncovered_reason: reasonVal
                }], 'confirm').then(result => {
                    setStoredPanelValues(panelState, {
                        status: statusVal,
                        reviewerInput: reviewerVal,
                        methodInput: methodVal,
                        reasonInput: reasonVal,
                        isDraft: false
                    });
                    clearPanelDirty(startLineNum, false);
                    notifyProgressChanged();
                    updateHeaderStatistics();
                    showToast(`第 ${startLineNum}-${endLineNum} 行分析已确认保存`);
                }).catch(err => {
                    console.error('[CoverageEnhance] Single block save failed:', err);
                    saveBtn.innerText = 'Error';
                    saveBtn.className = 'coverage-analysis-btn error';
                    saveBtn.title = `保存失败: ${err.message}`;
                    window.setTimeout(() => markPanelDirty(startLineNum), 3000);
                });
            });

            inheritBtn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();
                const previous = findPreviousFilledPanel(startLineNum);
                if (!previous) {
                    alert('没有找到上一条已填写的分析结果。');
                    return;
                }
                const currentPanel = panelsMap.get(startLineNum);
                setStoredPanelValues(currentPanel, {
                    status: getStoredPanelValue(previous, 'status') || '未确认',
                    reviewerInput: getStoredPanelValue(previous, 'reviewerInput'),
                    methodInput: getStoredPanelValue(previous, 'methodInput'),
                    reasonInput: getStoredPanelValue(previous, 'reasonInput')
                });
                saveBtn.innerText = 'Save';
                saveBtn.className = 'coverage-analysis-btn';
                markPanelDirty(startLineNum);
            });

            batchInheritBtn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();
                const sourceEntry = findPreviousFilledPanelEntry(startLineNum);
                if (!sourceEntry) {
                    alert('没有找到可作为批量继承来源的上一条已填写结果。');
                    return;
                }
                const sourceLineNum = sourceEntry[0];
                const sourcePanel = sourceEntry[1];
                const inheritedValues = {
                    status: getStoredPanelValue(sourcePanel, 'status') || '未确认',
                    reviewerInput: getStoredPanelValue(sourcePanel, 'reviewerInput'),
                    methodInput: getStoredPanelValue(sourcePanel, 'methodInput'),
                    reasonInput: getStoredPanelValue(sourcePanel, 'reasonInput')
                };
                const targetEntries = Array.from(panelsMap.entries())
                    .filter(([lineNum]) => lineNum > sourceLineNum && lineNum <= startLineNum)
                    .sort((a, b) => a[0] - b[0]);

                targetEntries.forEach(([lineNum, targetPanel]) => {
                    setStoredPanelValues(targetPanel, inheritedValues);
                    if (targetPanel.saveBtn) {
                        targetPanel.saveBtn.innerText = 'Save';
                        targetPanel.saveBtn.className = 'coverage-analysis-btn';
                    }
                    markPanelDirty(lineNum);
                });
                alert(`已从第 ${sourceLineNum} 行批量继承到 ${targetEntries.length} 个控件，请点击“暂存草稿”或“确认提交”写入数据库。`);
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
                setStoredPanelValues(panelState, { status: select.value });
                markPanelDirty(startLineNum);
            });

            [reviewerInput, methodInput, reasonInput].forEach(input => {
                input.addEventListener('input', function() {
                    setStoredPanelValues(panelState, {
                        reviewerInput: reviewerInput.value,
                        methodInput: methodInput.value,
                        reasonInput: reasonInput.value
                    });
                    markPanelDirty(startLineNum);
                });
            });

            return panel;
        }
    };

    // =========================================================================
    // 4. CodeRegionController: 区域交互与分批 DOM 渲染调度
    // =========================================================================
    const CodeRegionController = {
        filePath: '',
        container: null,
        toolbarEl: null,

        async init(layoutData, preSource) {
            this.filePath = layoutData.file_path || '';
            this.container = preSource;
            CodeRegionStore.init(layoutData);

            // Calculate initial physical total uncovered from layout
            totalUncovered = layoutData.pending_line_count || 0;

            // Clear original preSource DOM
            preSource.innerHTML = '';

            // 1. Render region skeleton placeholders
            const regions = CodeRegionStore.getAll();
            const fragment = document.createDocumentFragment();

            regions.forEach(region => {
                const regContainer = document.createElement('div');
                regContainer.className = 'coverage-region-container';
                regContainer.dataset.regionId = region.id;
                region.domContainer = regContainer;

                const placeholder = this.createPlaceholderElement(region);
                region.placeholderEl = placeholder;
                regContainer.appendChild(placeholder);

                fragment.appendChild(regContainer);
            });

            preSource.appendChild(fragment);

            // 2. Create Top Action Toolbar
            this.createTopToolbar();

            // 3. Batch load default expanded regions
            const defaultExpanded = CodeRegionStore.getExpanded();
            if (defaultExpanded.length > 0) {
                try {
                    await CodeRegionLoader.loadInitialBatch(this.filePath, defaultExpanded);
                    for (const reg of defaultExpanded) {
                        await this.renderRegionLines(reg);
                    }
                } catch (err) {
                    console.error('[CodeRegionController] Initial batch load failed:', err);
                    defaultExpanded.forEach(r => {
                        CodeRegionStore.setError(r.id, err.message);
                        this.updatePlaceholderState(r);
                    });
                }
            }

            reviewControlsReady = true;
            updateReviewNavigation();
            updateHeaderStatistics();
            updateBatchToolbar();

            // Handle anchor navigation if present in URL (e.g. #L42)
            this.handleHashAnchor();
        },

        createPlaceholderElement(region) {
            const el = document.createElement('div');
            el.className = 'coverage-region-placeholder';
            el.dataset.regionId = region.id;

            if (region.kind === 'analysis') {
                el.classList.add('kind-analysis');
            }
            if (CodeRegionStore.getAll().length === 1 && region.kind === 'collapsed' && region.startLine === 1) {
                el.classList.add('kind-empty-file');
            }

            this.updatePlaceholderContent(el, region);

            el.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (region.loading) return;
                this.expandRegion(region.id);
            });

            return el;
        },

        updatePlaceholderContent(el, region) {
            const isEntireEmptyFile = CodeRegionStore.getAll().length === 1 && region.kind === 'collapsed' && region.startLine === 1;

            if (region.loading) {
                el.className = 'coverage-region-placeholder loading';
                el.innerHTML = `
                    <div class="placeholder-left">
                        <span class="coverage-spinner"></span>
                        <span class="placeholder-text">${region.progressText || '正在加载…'}</span>
                    </div>
                `;
                return;
            }

            if (region.error) {
                el.className = 'coverage-region-placeholder error';
                el.innerHTML = `
                    <div class="placeholder-left">
                        <span class="placeholder-icon">⚠️</span>
                        <span class="placeholder-text">加载失败（${region.error}）</span>
                    </div>
                    <span class="placeholder-action">点击重试</span>
                `;
                return;
            }

            el.className = 'coverage-region-placeholder' + (region.kind === 'analysis' ? ' kind-analysis' : (isEntireEmptyFile ? ' kind-empty-file' : ''));

            let mainText = '';
            let icon = '⤢';
            if (isEntireEmptyFile) {
                icon = '✨';
                mainText = `该文件暂无待分析代码 · 第 1 - ${region.endLine} 行 · 已折叠 ${region.lineCount} 行`;
            } else if (region.kind === 'analysis') {
                icon = '🔹';
                const labelPart = region.label ? `${region.label} · ` : '';
                mainText = `${labelPart}分析区域 · 第 ${region.startLine} - ${region.endLine} 行 · 已折叠 ${region.lineCount} 行`;
            } else {
                icon = '⤢';
                mainText = `第 ${region.startLine} - ${region.endLine} 行 · 已折叠 ${region.lineCount} 行`;
            }

            el.innerHTML = `
                <div class="placeholder-left">
                    <span class="placeholder-icon">${icon}</span>
                    <span class="placeholder-text">${mainText}</span>
                </div>
                <span class="placeholder-action">点击展开</span>
            `;
        },

        updatePlaceholderState(region) {
            if (region.placeholderEl) {
                this.updatePlaceholderContent(region.placeholderEl, region);
            }
        },

        async expandRegion(regionId) {
            const region = CodeRegionStore.get(regionId);
            if (!region) return;

            if (region.loaded) {
                // Directly render from cache without network request
                await this.renderRegionLines(region);
                return;
            }

            // Load lines via Loader
            this.updatePlaceholderState(region);
            try {
                await CodeRegionLoader.loadRegion(this.filePath, region, (loaded, total) => {
                    region.progressText = `正在展开 ${loaded} / ${total} 行…`;
                    this.updatePlaceholderState(region);
                });
                await this.renderRegionLines(region);
            } catch (err) {
                console.error(`[CodeRegionController] Failed to expand region ${regionId}:`, err);
                CodeRegionStore.setError(regionId, err.message || String(err));
                this.updatePlaceholderState(region);
            }
        },

        collapseRegion(regionId) {
            const region = CodeRegionStore.get(regionId);
            if (!region || !region.domContainer) return;

            // Remove lines DOM to free browser memory
            if (region.linesEl) {
                region.linesEl.remove();
                region.linesEl = null;
            }
            if (region.headerEl) {
                region.headerEl.remove();
                region.headerEl = null;
            }

            CodeRegionStore.setCollapsed(regionId);

            // Re-display placeholder
            if (!region.placeholderEl) {
                region.placeholderEl = this.createPlaceholderElement(region);
            }
            region.placeholderEl.style.display = '';
            this.updatePlaceholderState(region);
            region.domContainer.appendChild(region.placeholderEl);

            updateReviewNavigation();
            updateHeaderStatistics();
        },

        async renderRegionLines(region) {
            const container = region.domContainer;
            if (!container) return;

            // Hide placeholder
            if (region.placeholderEl) {
                region.placeholderEl.style.display = 'none';
            }

            // Create region header bar if analysis region or large region
            if (!region.headerEl && (region.kind === 'analysis' || region.defaultState === 'expanded')) {
                const header = document.createElement('div');
                header.className = 'coverage-region-header';
                const labelText = region.label ? `${region.label} · ` : '';
                header.innerHTML = `
                    <div class="region-title">
                        <span>▾ ${labelText}分析区域</span>
                        <span class="badge">第 ${region.startLine} - ${region.endLine} 行 (${region.lineCount} 行)</span>
                    </div>
                    <div class="region-actions">
                        <button type="button" class="coverage-region-collapse-btn">收起区域</button>
                    </div>
                `;
                header.querySelector('.coverage-region-collapse-btn').addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    this.collapseRegion(region.id);
                });
                region.headerEl = header;
                container.appendChild(header);
            }

            // Lines container
            let linesContainer = region.linesEl;
            if (!linesContainer) {
                linesContainer = document.createElement('div');
                linesContainer.className = 'coverage-region-lines';
                region.linesEl = linesContainer;
                container.appendChild(linesContainer);
            } else {
                linesContainer.innerHTML = '';
            }

            // Batch render lines
            await this.renderLinesInBatches(region.lines, linesContainer);
            CodeRegionStore.setExpanded(region.id);

            updateReviewNavigation();
            updateHeaderStatistics();
        },

        async renderLinesInBatches(lines, container, batchSize = RENDER_BATCH_SIZE) {
            if (!lines || !lines.length) return;

            for (let i = 0; i < lines.length; i += batchSize) {
                const chunk = lines.slice(i, i + batchSize);
                const fragment = document.createDocumentFragment();

                for (const line of chunk) {
                    fragment.appendChild(CodeLineRenderer.renderCodeLine(line, this.filePath));
                }

                container.appendChild(fragment);
                if (i + batchSize < lines.length) {
                    await yieldToBrowser();
                }
            }
        },

        createTopToolbar() {
            const existing = document.querySelector('.coverage-lazy-toolbar');
            if (existing) existing.remove();

            const toolbar = document.createElement('div');
            toolbar.className = 'coverage-lazy-toolbar';
            toolbar.setAttribute('contenteditable', 'false');

            const statusSpan = document.createElement('span');
            statusSpan.className = 'coverage-lazy-toolbar-status';

            const expandAllBtn = document.createElement('button');
            expandAllBtn.type = 'button';
            expandAllBtn.className = 'coverage-lazy-toolbar-btn primary';
            expandAllBtn.innerText = '📖 展开全部';
            expandAllBtn.title = '分批逐步展开当前文件的全部代码行';

            const restoreDefaultBtn = document.createElement('button');
            restoreDefaultBtn.type = 'button';
            restoreDefaultBtn.className = 'coverage-lazy-toolbar-btn';
            restoreDefaultBtn.innerText = '↺ 恢复默认折叠';
            restoreDefaultBtn.title = '仅展示待分析函数区域，折叠其余非分析代码';

            expandAllBtn.addEventListener('click', async () => {
                await this.expandAll(expandAllBtn, statusSpan);
            });

            restoreDefaultBtn.addEventListener('click', () => {
                this.restoreDefault();
            });

            toolbar.appendChild(statusSpan);
            toolbar.appendChild(expandAllBtn);
            toolbar.appendChild(restoreDefaultBtn);
            document.body.appendChild(toolbar);
            this.toolbarEl = toolbar;
        },

        async expandAll(btn, statusEl) {
            btn.disabled = true;
            btn.innerText = '展开中…';
            const allRegions = CodeRegionStore.getAll();
            const totalLines = CodeRegionStore._fileMeta.totalLines || 0;
            let loadedLinesCount = 0;
            let failedCount = 0;

            for (let i = 0; i < allRegions.length; i++) {
                const reg = allRegions[i];
                statusEl.innerText = `正在展开 ${loadedLinesCount} / ${totalLines} 行 (${i + 1}/${allRegions.length})…`;
                try {
                    if (!reg.loaded) {
                        await CodeRegionLoader.loadRegion(this.filePath, reg);
                    }
                    await this.renderRegionLines(reg);
                    loadedLinesCount += reg.lineCount;
                } catch (err) {
                    console.error(`[CodeRegionController] Expand all failed on ${reg.id}:`, err);
                    failedCount++;
                }
                await yieldToBrowser();
            }

            statusEl.innerText = '';
            btn.disabled = false;
            btn.innerText = '📖 展开全部';

            if (failedCount > 0) {
                showToast(`已展开 ${allRegions.length - failedCount} 个区域，${failedCount} 个区域加载失败`);
            } else {
                showToast('已全部展开');
            }
        },

        restoreDefault() {
            const allRegions = CodeRegionStore.getAll();
            allRegions.forEach(reg => {
                if (reg.defaultState === 'expanded') {
                    if (reg.currentState !== 'expanded-loaded') {
                        this.expandRegion(reg.id);
                    }
                } else {
                    this.collapseRegion(reg.id);
                }
            });
            showToast('已恢复默认折叠状态');
        },

        handleHashAnchor() {
            const hash = window.location.hash;
            if (!hash || !hash.startsWith('#L')) return;
            const lineNum = parseInt(hash.replace('#L', ''), 10);
            if (Number.isNaN(lineNum)) return;

            const region = CodeRegionStore.findByLine(lineNum);
            if (!region) return;

            if (region.currentState === 'expanded-loaded') {
                const targetLine = document.getElementById(`L${lineNum}`);
                if (targetLine) {
                    targetLine.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    targetLine.style.background = '#fff8c5';
                    setTimeout(() => targetLine.style.background = '', 2000);
                }
            } else if (region.placeholderEl) {
                region.placeholderEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }
    };

    // =========================================================================
    // Entry Point: DOMContentLoaded Bootstrap
    // =========================================================================
    document.addEventListener('DOMContentLoaded', async function() {
        // 1. Extract relative file path
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
        console.log('[CoverageEnhance] Active mode:', ACTIVE_MODE);
        console.log('[CoverageEnhance] Version:', ENHANCE_VERSION);

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

        createModeToggler(ACTIVE_MODE);
        createBatchToolbar(filePath);

        // Branch by ACTIVE_MODE
        if (ACTIVE_MODE === 'lazy_collapse') {
            // New Lazy Collapse Architecture
            try {
                const layoutResp = await requestCoverageApi(
                    `/code-layout?project=${encodeURIComponent(DEFAULT_PROJECT)}&file=${encodeURIComponent(filePath)}`,
                    { method: 'GET' }
                );
                if (layoutResp && layoutResp.data) {
                    await CodeRegionController.init(layoutResp.data, preSource);
                    return;
                }
            } catch (err) {
                console.warn('[CoverageEnhance] Failed to initialize via backend layout, falling back to client DOM parsing:', err.message);
            }
        }

        // Fallback: Legacy immediate / lazy rendering from existing static HTML
        runLegacyModeEnhancement(preSource, filePath);
    });

    // =========================================================================
    // Legacy Mode Engine (for 'immediate', 'lazy' and offline static fallback)
    // =========================================================================
    function runLegacyModeEnhancement(preSource, filePath) {
        function createSourceLineAccess(pre) {
            const modernLineNodes = pre.querySelectorAll('span[id^="L"]');
            if (modernLineNodes.length > 0) {
                return {
                    length: modernLineNodes.length,
                    get(index) {
                        const span = modernLineNodes[index];
                        if (!span) return null;
                        const lineNum = parseInt(span.id.replace('L', ''), 10);
                        if (Number.isNaN(lineNum)) return null;
                        return { span, lineNum, legacyInline: false };
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
                        if (node.matches('.lineNum')) break;
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
                    if (!lineNumSpan) return null;
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

        function isUncoveredLine(item) {
            if (!item || !item.span) return false;
            const uncovered = item.span.matches('.tlaUNC, .tlaBgUNC, .lineNoCov') ||
                item.span.querySelector('.tlaUNC, .tlaBgUNC, .lineNoCov') !== null;
            if (!uncovered || REVIEW_SCOPE !== 'incremental') return uncovered;
            return item.span.getAttribute('data-coverage-review') === 'incremental' ||
                item.span.querySelector('[data-coverage-review="incremental"]') !== null;
        }

        function isCoveredLine(item) {
            if (!item || !item.span) return false;
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
            if (!codeText || isControlFlowLine(item) || codeText.endsWith(';')) return false;
            if (/^(return|typedef|struct|enum|union)\b/.test(codeText)) return false;
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
            if (!text || isControlFlowLine(item) || isFunctionEntryLine(item) || isJumpLine(item)) return false;
            if (/^[{}]+;?$/.test(text) || /^(case\b.*:|default\s*:|[A-Za-z_]\w*\s*:)$/.test(text)) return false;
            if (!text.endsWith(';')) return false;
            const hasAssignment = /(^|[^=!<>])=([^=]|$)/.test(text) ||
                /\b(\+=|-=|\*=|\/=|%=|&=|\|=|\^=|<<=|>>=)\b/.test(text);
            const isSimpleDeclaration = /^(?:const\s+|static\s+|volatile\s+|register\s+|unsigned\s+|signed\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|[A-Za-z_]\w*\s+)+[*\s]*[A-Za-z_]\w*(?:\s*=\s*[^;]+)?\s*;$/.test(text);
            return hasAssignment || isSimpleDeclaration;
        }

        function buildSemanticBlock(startIndex) {
            const start = sourceLines.get(startIndex);
            if (!start) return { block: [], consumedUntil: startIndex };
            const block = [start];
            const startIsFunction = isFunctionEntryLine(start);
            let consumedUntil = startIndex;

            for (let j = startIndex + 1; j < sourceLines.length; j++) {
                const next = sourceLines.get(j);
                if (!next) continue;
                if (isCoveredLine(next)) break;

                if (isUncoveredLine(next)) {
                    if (isControlFlowLine(next) || isFunctionEntryLine(next)) break;
                    if (startIsFunction && !isSimpleAutoGroupLine(next)) break;
                    if (!startIsFunction && (!isSimpleAutoGroupLine(start) || !isSimpleAutoGroupLine(next))) break;
                    block.push(next);
                    consumedUntil = j;
                    continue;
                }

                if (!startIsFunction) break;
                if (isControlFlowLine(next) || isFunctionEntryLine(next)) break;
                if (startIsFunction && !isIgnorableStructuralLine(next)) continue;
                if (!isIgnorableStructuralLine(next)) break;
            }
            return { block, consumedUntil };
        }

        for (let i = 0; i < sourceLines.length; i++) {
            const item = sourceLines.get(i);
            if (!isUncoveredLine(item) || countedUncoveredLines.has(item.lineNum)) continue;

            if (isControlFlowLine(item)) {
                blocks.push([item]);
                countedUncoveredLines.add(item.lineNum);
            } else {
                const { block, consumedUntil } = buildSemanticBlock(i);
                if (block.length > 0) {
                    blocks.push(block);
                    block.forEach(b => countedUncoveredLines.add(b.lineNum));
                }
                i = Math.max(i, consumedUntil);
            }
        }

        totalUncovered = countedUncoveredLines.size;
        countedUncoveredLines.clear();

        // Render blocks
        blocks.forEach(blk => {
            const startItem = blk[0];
            const endItem = blk[blk.length - 1];
            const lineDto = {
                line_no: startItem.lineNum,
                source: startItem.lineText || startItem.span.textContent || '',
                coverage_state: 'uncovered',
                analysis_state: '未确认',
                is_pending_analysis: true,
                is_block_entry: true,
                block_start_line: startItem.lineNum,
                block_end_line: endItem.lineNum,
                block_type: 'single'
            };
            const panel = CodeLineRenderer.createReviewPanel(lineDto, filePath);
            startItem.span.appendChild(panel);
        });

        // Pull existing database records
        requestCoverageApi(`?project=${encodeURIComponent(DEFAULT_PROJECT)}&file=${encodeURIComponent(filePath)}`, { method: 'GET' })
            .then(data => {
                if (data && data.records) {
                    const dbMap = new Map();
                    data.records.forEach(rec => dbMap.set(rec.line_number, rec));
                    panelsMap.forEach((pState, sLine) => {
                        const rec = dbMap.get(sLine);
                        if (rec) {
                            setStoredPanelValues(pState, {
                                status: rec.status || '未确认',
                                isDraft: rec.is_draft === true || rec.is_draft === 1,
                                reviewerInput: rec.reviewer || '',
                                methodInput: rec.coverage_method || '',
                                reasonInput: rec.uncovered_reason || ''
                            });
                            setPanelPersistedState(pState);
                        }
                    });
                }
                reviewControlsReady = true;
                updateReviewNavigation();
                updateHeaderStatistics();
                updateBatchToolbar();
            }).catch(err => {
                console.warn('[CoverageEnhance] Legacy fetch records failed:', err);
                reviewControlsReady = true;
                updateReviewNavigation();
                updateHeaderStatistics();
                updateBatchToolbar();
            });
    }
})();
