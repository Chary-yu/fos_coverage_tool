/**
 * 覆盖率 HTML 报告增强脚本 (ES6) - 待分析函数优先 + 懒加载折叠架构 v11
 * 
 * 核心架构：
 * 1. ReviewDraftStore: 用户未保存编辑状态持久存储 (防止收起/重绘丢失数据)
 * 2. CodeRegionStore: 区域状态与行缓存管理
 * 3. CodeRegionLoader: 区间/Chunk/Batch 数据加载、去重与流式加载
 * 4. CodeLineRenderer: 统一代码行与分析面板渲染器 (唯一事实来源)
 * 5. CodeRegionController: 区域交互、分批 DOM 渲染调度与展开/折叠控制
 */
(function() {
    const ENHANCE_VERSION = 'lazy-collapse-20260819_v11_3';
    const SERVER_URL = '/api/coverage';
    const DEFAULT_PROJECT = 'Gemini-NOS';
    const DEFAULT_REPORT_ID = '';
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
    let currentReportId = DEFAULT_REPORT_ID || '';
    let currentFilePath = '';
    let dirtyPanelStartLines = new Set();
    let panelsMap = new Map(); // startLine -> panelState
    let batchToolbarState = null;
    let reviewControlsReady = false;
    let totalUncovered = 0;
    let blocks = [];
    let foldBars = [];
    let isFoldedModeActive = false;

    // =========================================================================
    // 1. ReviewDraftStore: 独立编辑状态存储 (保证收起/展开不丢失未保存编辑)
    // =========================================================================
    const ReviewDraftStore = {
        _drafts: new Map(), // blockStartLine -> { reviewer, status, coverage_method, uncovered_reason, isDirty, isDraft }

        setDraft(blockStartLine, data) {
            const line = Number(blockStartLine);
            const existing = this._drafts.get(line) || {};
            this._drafts.set(line, {
                reviewer: data.reviewer !== undefined ? data.reviewer : (existing.reviewer || ''),
                status: data.status !== undefined ? data.status : (existing.status || '未确认'),
                coverage_method: data.coverage_method !== undefined ? data.coverage_method : (existing.coverage_method || ''),
                uncovered_reason: data.uncovered_reason !== undefined ? data.uncovered_reason : (existing.uncovered_reason || ''),
                isDirty: data.isDirty !== undefined ? Boolean(data.isDirty) : (existing.isDirty !== undefined ? existing.isDirty : false),
                isDraft: data.isDraft !== undefined ? Boolean(data.isDraft) : (existing.isDraft !== undefined ? existing.isDraft : false),
            });
        },

        getDraft(blockStartLine) {
            return this._drafts.get(Number(blockStartLine)) || null;
        },

        clearDraft(blockStartLine) {
            this._drafts.delete(Number(blockStartLine));
        },

        hasDirty(blockStartLine) {
            const d = this._drafts.get(Number(blockStartLine));
            return !!(d && d.isDirty);
        },

        hasAnyDirtyInRegion(startLine, endLine) {
            for (const [line, draft] of this._drafts.entries()) {
                if (line >= startLine && line <= endLine && draft.isDirty) {
                    return true;
                }
            }
            return false;
        }
    };

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
        if (panel.values && typeof panel.values[key] === 'string') {
            return panel.values[key];
        }
        return '';
    }

    function setStoredPanelValues(panel, values) {
        if (!panel || !values) return;
        panel.values = Object.assign({}, panel.values || {}, values);
        if (panel.select && values.status !== undefined) {
            panel.select.value = values.status;
        }
        if (panel.reviewerInput && values.reviewerInput !== undefined) {
            panel.reviewerInput.value = values.reviewerInput;
        }
        if (panel.methodInput && values.methodInput !== undefined) {
            panel.methodInput.value = values.methodInput;
        }
        if (panel.reasonInput && values.reasonInput !== undefined) {
            panel.reasonInput.value = values.reasonInput;
        }
        if (panel.lineNum !== undefined) {
            ReviewDraftStore.setDraft(panel.lineNum, {
                reviewer: values.reviewerInput,
                status: values.status,
                coverage_method: values.methodInput,
                uncovered_reason: values.reasonInput,
                isDirty: values.isDirty !== undefined ? Boolean(values.isDirty) : false,
                isDraft: values.isDraft !== undefined ? Boolean(values.isDraft) : (panel.values && panel.values.isDraft !== undefined ? Boolean(panel.values.isDraft) : false),
            });
        }
    }

    function markPanelDirty(startLineNum) {
        dirtyPanelStartLines.add(Number(startLineNum));
        const panel = panelsMap.get(Number(startLineNum));
        if (panel) {
            if (panel.values) {
                panel.values.isDirty = true;
            }
            ReviewDraftStore.setDraft(startLineNum, { isDirty: true });
            if (panel.saveBtn) {
                panel.saveBtn.innerText = 'Save';
                panel.saveBtn.className = 'coverage-analysis-btn';
            }
        }
        updateBatchToolbar();
    }

    function clearPanelDirty(startLineNum, resetSaved = true) {
        dirtyPanelStartLines.delete(Number(startLineNum));
        const panel = panelsMap.get(Number(startLineNum));
        if (panel) {
            if (panel.values) {
                panel.values.isDirty = false;
            }
            ReviewDraftStore.setDraft(startLineNum, { isDirty: false });
            if (resetSaved && panel.saveBtn) {
                const isDraft = panel.values && panel.values.isDraft;
                const status = panel.values ? panel.values.status : '';
                panel.saveBtn.innerText = isDraft ? '已暂存' : (CONFIRMED_STATUS_SET.has(status) ? '已确认' : 'Save');
                if (isDraft || CONFIRMED_STATUS_SET.has(status)) {
                    panel.saveBtn.className = 'coverage-analysis-btn saved';
                } else {
                    panel.saveBtn.className = 'coverage-analysis-btn';
                }
            }
        }
        updateBatchToolbar();
    }

    function updateBatchToolbar() {
        if (!batchToolbarState) return;
        const dirtyCount = dirtyPanelStartLines.size;
        batchToolbarState.count.innerText = `未提交修改: ${dirtyCount} 项`;
        batchToolbarState.draftBtn.innerText = `暂存草稿 (${dirtyCount})`;
        batchToolbarState.confirmBtn.innerText = `确认提交 (${dirtyCount})`;
        batchToolbarState.draftBtn.disabled = dirtyCount === 0 || batchToolbarState.submitting;
        batchToolbarState.confirmBtn.disabled = dirtyCount === 0 || batchToolbarState.submitting;
    }

    function isPanelAwaitingReview(panel) {
        if (!panel) return false;
        const status = getStoredPanelValue(panel, 'status');
        const isDraft = panel.values && panel.values.isDraft === true;
        return !status || status === '未确认' || isDraft;
    }

    function updateReviewNavigation() {
        const sortedPanels = Array.from(panelsMap.entries()).sort((a, b) => a[0] - b[0]);
        sortedPanels.forEach(([lineNum, panel], idx) => {
            if (panel.previousBtn) {
                panel.previousBtn.disabled = idx === 0;
            }
            if (panel.nextBtn) {
                panel.nextBtn.disabled = idx === sortedPanels.length - 1;
            }
        });
    }

    function focusReviewPanel(panel) {
        if (!panel) return;
        if (panel.select && panel.select.isConnected) {
            panel.select.scrollIntoView({ behavior: 'smooth', block: 'center' });
            panel.select.focus();
            return;
        }
        const lineNum = panel.lineNum || (panel.block && panel.block.startLine);
        if (lineNum) {
            const lineEl = document.getElementById(`L${lineNum}`);
            if (lineEl && lineEl.isConnected) {
                lineEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                return;
            }
            if (typeof CodeRegionStore !== 'undefined' && CodeRegionStore.getRegionForLine) {
                const region = CodeRegionStore.getRegionForLine(lineNum);
                if (region && region.placeholderEl && region.placeholderEl.isConnected) {
                    region.placeholderEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            }
        }
    }

    function navigateReviewPanel(currentLineNum, direction) {
        const sortedLines = Array.from(panelsMap.keys()).sort((a, b) => a - b);
        const currentIndex = sortedLines.indexOf(Number(currentLineNum));
        if (currentIndex === -1) return;
        const targetIndex = currentIndex + direction;
        if (targetIndex >= 0 && targetIndex < sortedLines.length) {
            const targetPanel = panelsMap.get(sortedLines[targetIndex]);
            focusReviewPanel(targetPanel);
        }
    }

    function findPreviousFilledPanel(currentLineNum) {
        const sorted = Array.from(panelsMap.entries())
            .filter(([l]) => l < currentLineNum)
            .sort((a, b) => b[0] - a[0]);
        for (const [, panel] of sorted) {
            const status = getStoredPanelValue(panel, 'status');
            const reviewer = getStoredPanelValue(panel, 'reviewerInput');
            if ((status && status !== '未确认') || reviewer) {
                return panel;
            }
        }
        return null;
    }

    function findPreviousFilledPanelEntry(currentLineNum) {
        const sorted = Array.from(panelsMap.entries())
            .filter(([l]) => l < currentLineNum)
            .sort((a, b) => b[0] - a[0]);
        for (const entry of sorted) {
            const panel = entry[1];
            const status = getStoredPanelValue(panel, 'status');
            const reviewer = getStoredPanelValue(panel, 'reviewerInput');
            if ((status && status !== '未确认') || reviewer) {
                return entry;
            }
        }
        return null;
    }

    async function saveReviewBlocksBatch(filePath, payloadBlocks, actionType = 'confirm') {
        const isDraft = actionType === 'draft';
        const records = payloadBlocks.map(b => ({
            line_numbers: b.line_numbers,
            reviewer: b.reviewer || '',
            status: b.status || '未确认',
            coverage_method: b.coverage_method || '',
            uncovered_reason: b.uncovered_reason || '',
            is_draft: isDraft
        }));

        const result = await requestCoverageApi('/batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_name: DEFAULT_PROJECT,
                file_path: filePath,
                records: records
            })
        });

        // Update ReviewDraftStore and clear dirty
        payloadBlocks.forEach(b => {
            const sLine = b.line_numbers[0];
            ReviewDraftStore.setDraft(sLine, {
                reviewer: b.reviewer,
                status: b.status,
                coverage_method: b.coverage_method,
                uncovered_reason: b.uncovered_reason,
                isDirty: false,
                isDraft: isDraft
            });
        });

        return result;
    }

    async function submitDirtyPanels(filePath, actionType = 'confirm') {
        if (!dirtyPanelStartLines.size) {
            alert('没有未保存的修改。');
            return;
        }

        const isDraft = actionType === 'draft';
        const dirtyLines = Array.from(dirtyPanelStartLines).sort((a, b) => a - b);
        const payloadBlocks = [];

        for (const lineNum of dirtyLines) {
            const panel = panelsMap.get(lineNum);
            if (!panel) continue;
            const reviewerVal = getStoredPanelValue(panel, 'reviewerInput').trim();
            const statusVal = getStoredPanelValue(panel, 'status') || '未确认';
            const methodVal = getStoredPanelValue(panel, 'methodInput').trim();
            const reasonVal = getStoredPanelValue(panel, 'reasonInput').trim();

            if (!isDraft) {
                if (statusVal === '未确认') {
                    alert(`第 ${lineNum} 行：请将状态变更为“可覆盖”或“无法覆盖”！`);
                    focusReviewPanel(panel);
                    return;
                }
                if (!reviewerVal) {
                    alert(`第 ${lineNum} 行：请输入确认人！`);
                    focusReviewPanel(panel);
                    return;
                }
                if (!methodVal && !reasonVal) {
                    alert(`第 ${lineNum} 行：“条件覆盖方法”与“无条件覆盖原因”必须填写其中之一！`);
                    focusReviewPanel(panel);
                    return;
                }
            }

            const blockLineNums = panel.block ? panel.block.lineNums : [lineNum];
            payloadBlocks.push({
                line_numbers: blockLineNums,
                reviewer: reviewerVal,
                status: statusVal,
                coverage_method: methodVal,
                uncovered_reason: reasonVal
            });
        }

        batchToolbarState.submitting = true;
        updateBatchToolbar();

        try {
            await saveReviewBlocksBatch(filePath, payloadBlocks, actionType);
            dirtyLines.forEach(l => {
                const p = panelsMap.get(l);
                if (p) {
                    if (p.values) {
                        p.values.isDraft = isDraft;
                        p.values.isDirty = false;
                    }
                    clearPanelDirty(l, true);
                }
            });
            notifyProgressChanged();
            updateHeaderStatistics();
            showToast(isDraft ? `已暂存 ${dirtyLines.length} 个分析草稿` : `已确认提交 ${dirtyLines.length} 项覆盖分析`);
        } catch (err) {
            alert(`批量保存失败: ${err.message}`);
        } finally {
            batchToolbarState.submitting = false;
            updateBatchToolbar();
        }
    }

    function createResizeGrip(textarea, onResize) {
        const grip = document.createElement('span');
        grip.className = 'coverage-resize-grip';
        grip.title = '拖拽调节输入框大小';

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
            if (currentReportId) {
                progressUrl.searchParams.set('report_id', currentReportId);
            }
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
                alert('当前已展开区域没有待填写的控件。');
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
            if (dirtyPanelStartLines.size > 0) {
                const message = `当前页面有 ${dirtyPanelStartLines.size} 项覆盖分析尚未暂存或提交！`;
                event.preventDefault();
                event.returnValue = message;
                return message;
            }
        });
    }

    function createModeToggler() {
        const container = document.createElement('div');
        container.className = 'coverage-mode-toggler';
        container.setAttribute('contenteditable', 'false');

        const label = document.createElement('span');
        label.innerText = '模式: ';

        const select = document.createElement('select');
        select.className = 'coverage-mode-select';
        [
            { value: 'lazy_collapse', text: '待分析折叠 (默认)' },
            { value: 'lazy', text: '按需渲染 (Lazy)' },
            { value: 'immediate', text: '即时全量 (Immediate)' }
        ].forEach(opt => {
            const option = document.createElement('option');
            option.value = opt.value;
            option.text = opt.text;
            if (opt.value === ACTIVE_MODE) option.selected = true;
            select.appendChild(option);
        });

        select.addEventListener('change', function() {
            const newUrl = new URL(window.location.href);
            newUrl.searchParams.set('mode', select.value);
            window.location.href = newUrl.toString();
        });

        container.appendChild(label);
        container.appendChild(select);

        // Only show legacy fold toggle button in non-lazy_collapse modes
        if (ACTIVE_MODE !== 'lazy_collapse') {
            const foldToggleBtn = document.createElement('button');
            foldToggleBtn.type = 'button';
            foldToggleBtn.className = 'coverage-fold-toggle-btn';
            foldToggleBtn.innerText = isFoldedModeActive ? '展开全部源码' : '上下文折叠';
            foldToggleBtn.title = '折叠非待分析上下文代码';
            foldToggleBtn.addEventListener('click', function() {
                if (isFoldedModeActive) {
                    applyFrontendFolding(true);
                    foldToggleBtn.innerText = '上下文折叠';
                } else {
                    applyFrontendFolding(false);
                    foldToggleBtn.innerText = '展开全部源码';
                }
            });
            container.appendChild(foldToggleBtn);
        }

        document.body.appendChild(container);
    }

    function updateHeaderStatistics() {
        let totalUncov = totalUncovered;
        let confirmedCount = 0;

        if (ACTIVE_MODE === 'lazy_collapse' && CodeRegionStore._layoutMeta) {
            totalUncov = CodeRegionStore._layoutMeta.total_uncovered_count;
            confirmedCount = CodeRegionStore._layoutMeta.confirmed_count || 0;

            // Adjust by local live panel states
            panelsMap.forEach((panel) => {
                const status = panel.select ? panel.select.value : (panel.values && panel.values.status);
                const isDraft = panel.values && panel.values.isDraft === true;
                const origSaved = panel.values && panel.values._origSavedConfirmed;
                const nowConfirmed = !isDraft && CONFIRMED_STATUS_SET.has(status);
                const bLen = panel.block ? (panel.block.length || (panel.block.lineNums ? panel.block.lineNums.length : 1)) : 1;

                if (nowConfirmed && !origSaved) {
                    confirmedCount += bLen;
                } else if (!nowConfirmed && origSaved) {
                    confirmedCount -= bLen;
                }
            });
            confirmedCount = Math.max(0, Math.min(totalUncov, confirmedCount));
        } else {
            panelsMap.forEach((panel) => {
                const status = panel.select ? panel.select.value : (panel.values && panel.values.status);
                const isDraft = panel.values && panel.values.isDraft === true;
                if (!isDraft && CONFIRMED_STATUS_SET.has(status)) {
                    const bLen = panel.block ? (panel.block.length || (panel.block.lineNums ? panel.block.lineNums.length : 1)) : 1;
                    confirmedCount += bLen;
                }
            });
        }

        const confirmedRatio = totalUncov > 0
            ? ((confirmedCount / totalUncov) * 100).toFixed(1)
            : (totalUncov === 0 ? '100.0' : '0.0');

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
        } else if (ratioFloat >= 90.0) {
            td4.className = 'coverage-ratio-hi';
        } else {
            td4.className = 'coverage-ratio-med';
        }

        const td5 = document.createElement('td');
        td5.className = 'headerValue';
        td5.style.paddingLeft = '4px';
        td5.innerText = `${confirmedCount} / ${totalUncov}`;

        reviewTr.appendChild(td0);
        reviewTr.appendChild(td1);
        reviewTr.appendChild(td2);
        reviewTr.appendChild(td3);
        reviewTr.appendChild(td4);
        reviewTr.appendChild(td5);
    }

    function setPanelPersistedState(panel) {
        if (!panel || !panel.saveBtn) return;
        const isDraft = panel.values && panel.values.isDraft === true;
        const status = panel.values ? panel.values.status : '';
        panel.saveBtn.innerText = isDraft ? '已暂存' : (CONFIRMED_STATUS_SET.has(status) ? '已确认' : 'Save');
        if (isDraft || CONFIRMED_STATUS_SET.has(status)) {
            panel.saveBtn.className = 'coverage-analysis-btn saved';
        }
    }

    function expandBlockPanel(startLineNum) {
        const panelState = panelsMap.get(startLineNum);
        if (panelState) {
            const values = panelState.values || {};
            setStoredPanelValues(panelState, values);
        }
        return panelState;
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
    // 2. CodeRegionStore: 区域状态与行缓存存储
    // =========================================================================
    const CodeRegionStore = {
        _regions: new Map(), // regionId -> regionState
        _fileMeta: { totalLines: 0, filePath: '', projectName: '' },
        _layoutMeta: null,

        init(layoutData) {
            this._regions.clear();
            this._layoutMeta = layoutData;
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
    // 3. CodeRegionLoader: 区间/Chunk/Batch 数据请求与流式加载
    // =========================================================================
    const CodeRegionLoader = {
        _inflightPromises: new Map(), // regionId or 'batch' -> Promise

        async loadInitialBatch(filePath, expandedRegions) {
            if (!expandedRegions.length) return [];
            
            // Mark all target regions as loading immediately to prevent duplicate triggers
            expandedRegions.forEach(r => CodeRegionStore.setLoading(r.id, '正在加载…'));

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
                    report_id: currentReportId,
                    scope: REVIEW_SCOPE,
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

        async loadRegion(filePath, region, onChunkProgress) {
            if (region.loaded) {
                return region.lines;
            }

            if (this._inflightPromises.has(region.id)) {
                return this._inflightPromises.get(region.id);
            }

            const loadPromise = (async () => {
                CodeRegionStore.setLoading(region.id, '正在加载…');
                const totalLinesToLoad = region.endLine - region.startLine + 1;

                if (totalLinesToLoad > LOAD_CHUNK_SIZE) {
                    // Chunked streaming loading for large regions
                    const allLines = [];
                    region.lines = allLines;

                    for (let start = region.startLine; start <= region.endLine; start += LOAD_CHUNK_SIZE) {
                        const end = Math.min(start + LOAD_CHUNK_SIZE - 1, region.endLine);
                        const query = new URLSearchParams({
                            project: DEFAULT_PROJECT,
                            file: filePath,
                            start_line: start,
                            end_line: end,
                            scope: REVIEW_SCOPE
                        });
                        if (currentReportId) query.set('report_id', currentReportId);

                        const chunkData = await requestCoverageApi(`/code-lines?${query.toString()}`, { method: 'GET' });
                        const chunkLines = chunkData && chunkData.data && chunkData.data.lines ? chunkData.data.lines : [];
                        allLines.push(...chunkLines);

                        if (typeof onChunkProgress === 'function') {
                            await onChunkProgress(chunkLines, start, end, allLines.length, totalLinesToLoad);
                        }
                    }
                    CodeRegionStore.setLoaded(region.id, allLines);
                    return allLines;
                } else {
                    const query = new URLSearchParams({
                        project: DEFAULT_PROJECT,
                        file: filePath,
                        start_line: region.startLine,
                        end_line: region.endLine,
                        scope: REVIEW_SCOPE
                    });
                    if (currentReportId) query.set('report_id', currentReportId);

                    const data = await requestCoverageApi(`/code-lines?${query.toString()}`, { method: 'GET' });
                    const lines = data && data.data && data.data.lines ? data.data.lines : [];
                    CodeRegionStore.setLoaded(region.id, lines);
                    return lines;
                }
            })();

            this._inflightPromises.set(region.id, loadPromise);
            try {
                const res = await loadPromise;
                return res;
            } finally {
                this._inflightPromises.delete(region.id);
            }
        }
    };

    // =========================================================================
    // 4. CodeLineRenderer: 唯一 Line DTO -> DOM 渲染器 (支持 Draft Store 状态融合)
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

            // Merge draft store if user had unsaved edits
            const draft = ReviewDraftStore.getDraft(startLineNum);
            const initialStatus = draft && draft.status !== undefined ? draft.status : (lineData.analysis_state || '未确认');
            const initialReviewer = draft && draft.reviewer !== undefined ? draft.reviewer : (lineData.reviewer || '');
            const initialMethod = draft && draft.coverage_method !== undefined ? draft.coverage_method : (lineData.coverage_method || '');
            const initialReason = draft && draft.uncovered_reason !== undefined ? draft.uncovered_reason : (lineData.uncovered_reason || '');
            const isDirty = draft && draft.isDirty !== undefined ? Boolean(draft.isDirty) : false;
            const isDraft = draft && draft.isDraft !== undefined ? Boolean(draft.isDraft) : Boolean(lineData.is_draft);
            const isConfirmed = !isDraft && CONFIRMED_STATUS_SET.has(initialStatus);
            const origSavedConfirmed = !lineData.is_draft && CONFIRMED_STATUS_SET.has(lineData.analysis_state);

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
                if (opt === initialStatus) option.selected = true;
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
            reviewerInput.value = initialReviewer;

            // Method textarea
            const methodInput = document.createElement('textarea');
            methodInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            methodInput.placeholder = '条件覆盖方法';
            methodInput.value = initialMethod;
            const methodGrip = createResizeGrip(methodInput);

            // Reason textarea
            const reasonInput = document.createElement('textarea');
            reasonInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            reasonInput.placeholder = '无条件覆盖原因';
            reasonInput.value = initialReason;
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
            if (isDirty) {
                saveBtn.innerText = 'Save';
                saveBtn.className = 'coverage-analysis-btn';
            } else if (isDraft) {
                saveBtn.innerText = '已暂存';
                saveBtn.className = 'coverage-analysis-btn saved';
            } else if (isConfirmed) {
                saveBtn.innerText = '已确认';
                saveBtn.className = 'coverage-analysis-btn saved';
            } else {
                saveBtn.innerText = 'Save';
                saveBtn.className = 'coverage-analysis-btn';
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
                    status: initialStatus,
                    reviewerInput: initialReviewer,
                    methodInput: initialMethod,
                    reasonInput: initialReason,
                    isDirty: isDirty,
                    isDraft: isDraft,
                    _origSavedConfirmed: origSavedConfirmed
                }
            };

            panelsMap.set(startLineNum, panelState);
            if (isDirty) {
                dirtyPanelStartLines.add(startLineNum);
            }

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
                }], 'confirm').then(() => {
                    setStoredPanelValues(panelState, {
                        status: statusVal,
                        reviewerInput: reviewerVal,
                        methodInput: methodVal,
                        reasonInput: reasonVal,
                        isDraft: false,
                        isDirty: false,
                        _origSavedConfirmed: true
                    });
                    ReviewDraftStore.setDraft(startLineNum, {
                        reviewer: reviewerVal,
                        status: statusVal,
                        coverage_method: methodVal,
                        uncovered_reason: reasonVal,
                        isDraft: false,
                        isDirty: false
                    });
                    clearPanelDirty(startLineNum, false);
                    saveBtn.innerText = '已确认';
                    saveBtn.className = 'coverage-analysis-btn saved';
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
                    reasonInput: getStoredPanelValue(previous, 'reasonInput'),
                    isDirty: true
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
                    reasonInput: getStoredPanelValue(sourcePanel, 'reasonInput'),
                    isDirty: true
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
                setStoredPanelValues(panelState, { status: select.value, isDirty: true });
                markPanelDirty(startLineNum);
            });

            [reviewerInput, methodInput, reasonInput].forEach(input => {
                input.addEventListener('input', function() {
                    setStoredPanelValues(panelState, {
                        reviewerInput: reviewerInput.value,
                        methodInput: methodInput.value,
                        reasonInput: reasonInput.value,
                        isDirty: true
                    });
                    markPanelDirty(startLineNum);
                });
            });

            return panel;
        }
    };

    // =========================================================================
    // 5. CodeRegionController: 区域交互与分批 DOM 渲染调度
    // =========================================================================
    const CodeRegionController = {
        filePath: '',
        container: null,
        toolbarEl: null,

        async init(layoutData, preSource) {
            this.filePath = layoutData.file_path || '';
            this.container = preSource;
            CodeRegionStore.init(layoutData);

            // Calculate initial total uncovered from layout
            totalUncovered = layoutData.total_uncovered_count || layoutData.pending_line_count || 0;

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
            const region = typeof regionId === 'string' ? CodeRegionStore.get(regionId) : regionId;
            if (!region) return;

            if (region.loaded) {
                // Directly render from cache without network request
                await this.renderRegionLines(region);
                return;
            }

            // Load lines via Loader with chunk streaming
            this.updatePlaceholderState(region);
            try {
                this.prepareRegionLinesContainer(region);

                await CodeRegionLoader.loadRegion(this.filePath, region, async (chunkLines, start, end, loaded, total) => {
                    region.progressText = `正在展开 ${loaded} / ${total} 行…`;
                    this.updatePlaceholderState(region);
                    await this.appendChunkLines(region, chunkLines);
                });

                await this.finalizeRegionLoaded(region);
            } catch (err) {
                console.error(`[CodeRegionController] Failed to expand region ${region.id}:`, err);
                CodeRegionStore.setError(region.id, err.message || String(err));
                this.updatePlaceholderState(region);
                throw err;
            }
        },

        prepareRegionLinesContainer(region) {
            const container = region.domContainer;
            if (!container) return;

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

            if (!region.linesEl) {
                const linesContainer = document.createElement('div');
                linesContainer.className = 'coverage-region-lines';
                region.linesEl = linesContainer;
                container.appendChild(linesContainer);
            } else {
                region.linesEl.innerHTML = '';
            }
        },

        async appendChunkLines(region, chunkLines) {
            if (!region.linesEl || !chunkLines || !chunkLines.length) return;
            const fragment = document.createDocumentFragment();
            for (const line of chunkLines) {
                fragment.appendChild(CodeLineRenderer.renderCodeLine(line, this.filePath));
            }
            region.linesEl.appendChild(fragment);
            await yieldToBrowser();
        },

        async finalizeRegionLoaded(region) {
            if (region.placeholderEl) {
                region.placeholderEl.style.display = 'none';
            }
            CodeRegionStore.setExpanded(region.id);
            updateReviewNavigation();
            updateHeaderStatistics();
        },

        collapseRegion(regionId) {
            const region = CodeRegionStore.get(regionId);
            if (!region || !region.domContainer) return;

            // Free DOM memory while preserving DraftStore edits
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

            if (region.placeholderEl) {
                region.placeholderEl.style.display = 'none';
            }

            this.prepareRegionLinesContainer(region);
            await this.renderLinesInBatches(region.lines, region.linesEl);
            await this.finalizeRegionLoaded(region);
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
                    if (reg.currentState === 'expanded-loaded' && reg.linesEl) {
                        loadedLinesCount += reg.lineCount;
                        continue;
                    }
                    await this.expandRegion(reg);
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
        // Extract meta tag parameters
        const metaReportId = document.querySelector('meta[name="coverage-report-id"]');
        const metaFilePath = document.querySelector('meta[name="coverage-file-path"]');

        currentReportId = (metaReportId && metaReportId.content)
            || URL_PARAMS.get('report_id')
            || URL_PARAMS.get('report')
            || DEFAULT_REPORT_ID
            || '';

        let filePath = (metaFilePath && metaFilePath.content) || '';

        if (!filePath) {
            const titleElement = document.querySelector('title');
            if (titleElement) {
                const titleText = titleElement.innerText;
                const match = titleText.match(/LCOV\s+-\s+.*?\s+-\s+(.+)/);
                if (match && match[1]) {
                    filePath = match[1].trim();
                }
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
        currentFilePath = filePath;

        console.log('[CoverageEnhance] Current file path:', filePath);
        console.log('[CoverageEnhance] Report ID:', currentReportId);
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

        createModeToggler();
        createBatchToolbar(filePath);

        // Branch by ACTIVE_MODE
        if (ACTIVE_MODE === 'lazy_collapse') {
            try {
                const query = new URLSearchParams({
                    project: DEFAULT_PROJECT,
                    file: filePath,
                    scope: REVIEW_SCOPE
                });
                if (currentReportId) query.set('report_id', currentReportId);

                const layoutResp = await requestCoverageApi(`/code-layout?${query.toString()}`, { method: 'GET' });
                if (layoutResp && layoutResp.data) {
                    await CodeRegionController.init(layoutResp.data, preSource);
                    return;
                }
            } catch (err) {
                console.warn('[CoverageEnhance] Failed to initialize via backend layout:', err.message);
                // If the source was already stripped, display clear error message
                if (preSource.children.length === 0 && preSource.textContent.trim() === '') {
                    const errBanner = document.createElement('div');
                    errBanner.className = 'coverage-region-placeholder error';
                    errBanner.innerHTML = `
                        <div class="placeholder-left">
                            <span class="placeholder-icon">⚠️</span>
                            <span class="placeholder-text">无法加载代码布局与源码数据：${err.message}</span>
                        </div>
                    `;
                    preSource.appendChild(errBanner);
                    return;
                }
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
        const query = new URLSearchParams({
            project: DEFAULT_PROJECT,
            file: filePath
        });
        if (currentReportId) query.set('report_id', currentReportId);

        requestCoverageApi(`?${query.toString()}`, { method: 'GET' })
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
                                reasonInput: rec.uncovered_reason || '',
                                _origSavedConfirmed: !rec.is_draft && CONFIRMED_STATUS_SET.has(rec.status)
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
