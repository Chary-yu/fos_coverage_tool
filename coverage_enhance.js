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
    function getMetaContent(name, fallback = '') {
        try {
            const meta = document.querySelector(`meta[name="${name}"]`);
            return meta ? (meta.getAttribute('content') || fallback) : fallback;
        } catch (e) {
            return fallback;
        }
    }

    const ENHANCE_VERSION = 'lazy-collapse-20260828_v1';
    const SERVER_URL = '/api/coverage';
    const DEFAULT_PROJECT = getMetaContent('coverage-project') || 'Gemini-NOS';
    const DEFAULT_REPORT_ID = getMetaContent('coverage-report-id') || '';
    const DEFAULT_SCAN_ID = getMetaContent('coverage-scan-id') || '';
    const DEFAULT_REPOSITORY_NAME = getMetaContent('coverage-repository-name') || '';
    const DECLARED_REPORT_MODE = getMetaContent('coverage-report-mode');
    // Report mode is a persisted artifact fact. Missing or invalid metadata
    // must remain the safe static path; complete-looking identity fields may
    // not promote a historical page to VNext by browser-side inference.
    const REPORT_MODE = DECLARED_REPORT_MODE === 'VNEXT_ARTIFACT_READY'
        ? 'VNEXT_ARTIFACT_READY' : 'LEGACY_STATIC';
    const RENDER_MODE = getMetaContent('coverage-render-mode') || 'lazy_collapse'; // 'lazy_collapse', 'lazy', 'immediate'
    const REVIEW_SCOPE = getMetaContent('coverage-review-scope') || 'full'; // 'full' or 'incremental'
    const ENHANCE_SCRIPT_URL = document.currentScript && document.currentScript.src
        ? document.currentScript.src
        : '';
    const URL_PARAMS = new URLSearchParams(window.location.search);
    const QUERY_MODE = URL_PARAMS.get('mode');
    const ACTIVE_MODE = (QUERY_MODE === 'lazy_collapse' || QUERY_MODE === 'lazy' || QUERY_MODE === 'immediate')
        ? QUERY_MODE
        : (RENDER_MODE || 'lazy_collapse');
    const STATUS_OPTIONS = ['未确认', '可覆盖', '无法覆盖', '冗余代码'];
    const CONFIRMED_STATUS_SET = new Set(['可覆盖', '无法覆盖', '冗余代码']);
    const RENDER_BATCH_SIZE = 250;
    // Match the server-side Sidecar physical chunk size. DOM rendering remains
    // independently bounded by RENDER_BATCH_SIZE/RENDER_BATCH_LINES.
    const NETWORK_CHUNK_LINES = 2000;
    const RENDER_BATCH_LINES = 250;
    const MAX_CHUNK_CONCURRENCY = 3;
    const MAX_CODE_DETAIL_BATCH_RANGES = 1000;
    const MAX_CODE_DETAIL_BATCH_LOGICAL_LINES = 20000;
    const MAX_INITIAL_BATCH_CONCURRENCY = 3;
    const MAX_INITIAL_BATCH_RETRIES = 2;
    const VIRTUAL_SCROLL_THRESHOLD = 5000;
    const VIRTUAL_OVERSCAN_LINES = 300;
    const VIRTUAL_LINE_HEIGHT = 24;
    // Resident data is bounded by physical network chunks, independently of
    // the DOM render batch. Dirty/active review chunks are pinned below.
    const MAX_VIRTUAL_CACHED_LINES = 8000;
    const PROGRESS_UPDATE_STORAGE_KEY = 'coverage-review-progress-updated';

    // 控制流分支关键字侦测正则 (边界隔离)
    const CONTROL_FLOW_REGEX = /\b(if|else|for|while|do|switch|case|default)\b/;

    // 前端折叠引擎参数
    const CONTEXT_LINES_DEFAULT = 10;
    const MERGE_GAP_THRESHOLD = 15;
    const MIN_FOLD_GAP = 15;

    let resolvedServerUrl = '';
    let currentReportId = DEFAULT_REPORT_ID || '';
    let currentScanId = DEFAULT_SCAN_ID || '';
    let currentRepositoryName = DEFAULT_REPOSITORY_NAME || '';
    let currentFilePath = '';
    let dirtyPanelStartLines = new Set();
    let panelsMap = new Map(); // startLine -> panelState
    let panelLineNumbers = [];
    let batchToolbarState = null;
    let reviewControlsReady = false;
    let totalUncovered = 0;
    let blocks = [];
    let foldBars = [];
    let isFoldedModeActive = false;

    const PerformanceTelemetry = {
        apiRequests: 0,
        apiFailures: 0,
        networkChunks: 0,
        networkLines: 0,
        virtualRenders: 0,
        virtualDomLines: 0,
        virtualChunkEvictions: 0,
        maxDomLines: 0,
        layoutStart: 0,
        layoutMs: 0,
        reset() {
            this.apiRequests = 0;
            this.apiFailures = 0;
            this.networkChunks = 0;
            this.networkLines = 0;
            this.virtualRenders = 0;
            this.virtualDomLines = 0;
            this.virtualChunkEvictions = 0;
            this.maxDomLines = 0;
            this.layoutStart = 0;
            this.layoutMs = 0;
        },
        recordDomLineCount(count, virtualized) {
            const value = Number(count) || 0;
            this.maxDomLines = Math.max(this.maxDomLines, value);
            if (virtualized) {
                this.virtualDomLines = value;
            }
        },
        snapshot() {
            return {
                api_requests: this.apiRequests,
                api_failures: this.apiFailures,
                network_chunks: this.networkChunks,
                network_lines: this.networkLines,
                virtual_renders: this.virtualRenders,
                virtual_dom_lines: this.virtualDomLines,
                virtual_chunk_evictions: this.virtualChunkEvictions,
                max_dom_lines: this.maxDomLines,
                layout_ms: this.layoutMs
            };
        }
    };

    function insertPanelLineNumber(lineNumber) {
        const value = Number(lineNumber);
        if (!Number.isFinite(value) || panelLineNumbers.indexOf(value) !== -1) return;
        let low = 0;
        let high = panelLineNumbers.length;
        while (low < high) {
            const middle = (low + high) >> 1;
            if (panelLineNumbers[middle] < value) low = middle + 1;
            else high = middle;
        }
        panelLineNumbers.splice(low, 0, value);
    }

    function initialPanelValues(lineData, startLineNum) {
        const draft = ReviewDraftStore.getDraft(startLineNum);
        return {
            status: draft && draft.status !== undefined
                ? draft.status : (lineData.analysis_state || '未确认'),
            reviewerInput: draft && draft.reviewer !== undefined
                ? draft.reviewer : (lineData.reviewer || lineData.suggested_reviewer || ''),
            methodInput: draft && draft.coverage_method !== undefined
                ? draft.coverage_method : (lineData.coverage_method || ''),
            reasonInput: draft && draft.uncovered_reason !== undefined
                ? draft.uncovered_reason : (lineData.uncovered_reason || ''),
            isDirty: draft && draft.isDirty !== undefined ? Boolean(draft.isDirty) : false,
            isDraft: draft && draft.isDraft !== undefined
                ? Boolean(draft.isDraft) : Boolean(lineData.is_draft),
            _origSavedConfirmed: !lineData.is_draft
                && CONFIRMED_STATUS_SET.has(lineData.analysis_state)
        };
    }

    function inheritanceMetadata(lineData) {
        const analysis = lineData && lineData.analysis && typeof lineData.analysis === 'object'
            ? lineData.analysis : lineData;
        const state = String(
            (lineData && (lineData.analysis_relation_state || lineData.review_state)) ||
            (analysis && analysis.review_state) || ''
        );
        const relationActive = analysis && analysis.relation_is_active !== undefined
            ? Number(analysis.relation_is_active) === 1
            : Number(lineData && lineData.relation_is_active) !== 0;
        return {
            lineId: Number((analysis && analysis.line_id) || (lineData && lineData.line_id) || 0),
            relationRevision: Number(
                (analysis && analysis.relation_revision) || (lineData && lineData.relation_revision) || 0
            ),
            state,
            relationActive,
            rejectionId: Number(
                (analysis && analysis.rejection_id) || (lineData && lineData.rejection_id) || 0
            ),
            rejectionRevision: Number(
                (analysis && analysis.rejection_revision) || (lineData && lineData.rejection_revision) || 0
            ),
            inheritedPending: relationActive &&
                (state === 'INHERITED_PENDING' || state === 'CARRIED_COVERED'),
            rejected: !relationActive && state === 'INHERITANCE_REJECTED'
        };
    }

    function updateInheritanceControls(panel) {
        if (!panel) return;
        const meta = panel.inheritance || {};
        if (panel.rejectBtn) {
            panel.rejectBtn.style.display = meta.inheritedPending ? '' : 'none';
        }
        if (panel.undoRejectBtn) {
            panel.undoRejectBtn.style.display = meta.rejected ? '' : 'none';
        }
    }

    function registerReviewPanelMetadata(lineData) {
        if (!lineData || !lineData.is_block_entry) return null;
        const startLineNum = Number(lineData.block_start_line || lineData.line_no);
        const endLineNum = Number(lineData.block_end_line || lineData.line_no);
        let panelState = panelsMap.get(startLineNum);
        if (!panelState) {
            panelState = {
                select: null, reviewerInput: null, methodInput: null,
                reasonInput: null, saveBtn: null, previousBtn: null,
                rejectBtn: null, undoRejectBtn: null,
                nextBtn: null, block: {
                    startLine: startLineNum, endLine: endLineNum,
                    length: Math.max(1, endLineNum - startLineNum + 1)
                },
                lineNum: startLineNum,
                expanded: false,
                inheritance: inheritanceMetadata(lineData),
                values: initialPanelValues(lineData, startLineNum)
            };
            panelsMap.set(startLineNum, panelState);
            insertPanelLineNumber(startLineNum);
            if (panelState.values.isDirty) dirtyPanelStartLines.add(startLineNum);
        } else {
            panelState.block.startLine = startLineNum;
            panelState.block.endLine = endLineNum;
            panelState.block.length = Math.max(1, endLineNum - startLineNum + 1);
            panelState.inheritance = inheritanceMetadata(lineData);
        }
        updateInheritanceControls(panelState);
        return panelState;
    }

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
        return uniqueApiBases([SERVER_URL]);
    }

    function codeDetailIdentity(filePath) {
        const identity = {
            scan_id: currentScanId,
            report_id: currentReportId,
            repository_name: currentRepositoryName,
            file_path: filePath || currentFilePath
        };
        if (!identity.scan_id || !identity.report_id || !identity.file_path) {
            throw new Error('Code Detail requires scan_id, report_id and file_path');
        }
        return identity;
    }

    function codeDetailQuery(filePath, startLine, endLine) {
        const identity = codeDetailIdentity(filePath);
        const query = new URLSearchParams(identity);
        if (startLine !== undefined) query.set('start_line', String(startLine));
        if (endLine !== undefined) query.set('end_line', String(endLine));
        query.set('scope', REVIEW_SCOPE);
        return query;
    }

    function responseLines(payload) {
        if (!payload) return [];
        const lines = Array.isArray(payload.lines) ? payload.lines
            : (payload.data && Array.isArray(payload.data.lines) ? payload.data.lines : []);
        return lines.map(line => {
            const analysis = line && line.analysis;
            if (!analysis || typeof analysis !== 'object') return line;
            // VNext keeps the overlay nested to preserve the source-line DTO.
            // The renderer consumes one flat view, so copy only review fields
            // and never allow DB identity columns to overwrite line identity.
            return Object.assign({}, line, {
                analysis_state: analysis.analysis_state || analysis.conclusion_status || line.analysis_state,
                reviewer: analysis.reviewer || line.reviewer || '',
                coverage_method: analysis.coverage_method || line.coverage_method || '',
                uncovered_reason: analysis.uncovered_reason || line.uncovered_reason || '',
                is_draft: analysis.is_draft !== undefined ? analysis.is_draft : line.is_draft,
                review_state: analysis.review_state || line.review_state || '',
                analysis_relation_state: analysis.review_state || line.analysis_relation_state || '',
                relation_origin: analysis.relation_origin || line.relation_origin || '',
                relation_is_active: analysis.relation_is_active !== undefined
                    ? analysis.relation_is_active : line.relation_is_active,
                line_id: analysis.line_id || line.line_id,
                relation_revision: analysis.relation_revision || line.relation_revision,
                source_scan_id: analysis.source_scan_id || line.source_scan_id,
                source_line_id: analysis.source_line_id || line.source_line_id,
                rejection_id: analysis.rejection_id || line.rejection_id,
                rejection_revision: analysis.rejection_revision || line.rejection_revision
            });
        });
    }
    async function requestCoverageApi(pathSuffix, options) {
        const attempted = [];
        let lastError = null;
        for (const apiBase of apiBaseCandidates()) {
            const url = `${apiBase}${pathSuffix || ''}`;
            attempted.push(url);
            try {
                PerformanceTelemetry.apiRequests += 1;
                const response = await fetch(url, options || {});
                const contentType = (response.headers && typeof response.headers.get === 'function'
                    ? response.headers.get('Content-Type')
                    : 'application/json') || '';
                const data = (contentType.includes('application/json') || typeof response.json === 'function')
                    ? await response.json()
                    : null;
                if (!response.ok) PerformanceTelemetry.apiFailures += 1;
                if (response.ok && data) {
                    resolvedServerUrl = apiBase;
                    // Compatibility transports (including older injected
                    // pages) may wrap the canonical payload in
                    // {status: "success", data: ...}.  Normalize that
                    // envelope once so layout, batch and line consumers all
                    // see the same VNext contract.
                    if (data.status === 'success' && data.data &&
                        typeof data.data === 'object') {
                        return data.data;
                    }
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
                if (!(error && error.coverageApiResponse)) {
                    PerformanceTelemetry.apiFailures += 1;
                }
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
            if (typeof requestAnimationFrame === 'function') {
                requestAnimationFrame(() => resolve());
            } else {
                setTimeout(resolve, 0);
            }
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
        panelLineNumbers.forEach((lineNum, idx) => {
            const panel = panelsMap.get(lineNum);
            if (panel.previousBtn) {
                panel.previousBtn.disabled = idx === 0;
            }
            if (panel.nextBtn) {
                panel.nextBtn.disabled = idx === panelLineNumbers.length - 1;
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
            if (typeof CodeRegionStore !== 'undefined' && CodeRegionStore.findByLine) {
                const region = CodeRegionStore.findByLine(lineNum);
                if (region && region.virtualized && typeof CodeRegionController !== 'undefined') {
                    CodeRegionController.revealLine(region, lineNum).then(() => {
                        window.setTimeout(() => focusReviewPanel(panel), 0);
                    });
                    return;
                }
                if (region && region.placeholderEl && region.placeholderEl.isConnected) {
                    region.placeholderEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            }
        }
    }

    function navigateReviewPanel(currentLineNum, direction) {
        const currentIndex = panelLineNumbers.indexOf(Number(currentLineNum));
        if (currentIndex === -1) return;
        const targetIndex = currentIndex + direction;
        if (targetIndex >= 0 && targetIndex < panelLineNumbers.length) {
            const targetPanel = panelsMap.get(panelLineNumbers[targetIndex]);
            focusReviewPanel(targetPanel);
        }
    }

    function findPreviousFilledPanel(currentLineNum) {
        for (let i = panelLineNumbers.length - 1; i >= 0; i -= 1) {
            const lineNum = panelLineNumbers[i];
            if (lineNum >= currentLineNum) continue;
            const panel = panelsMap.get(lineNum);
            const status = getStoredPanelValue(panel, 'status');
            const reviewer = getStoredPanelValue(panel, 'reviewerInput');
            if ((status && status !== '未确认') || reviewer) {
                return panel;
            }
        }
        return null;
    }

    function findPreviousFilledPanelEntry(currentLineNum) {
        for (let i = panelLineNumbers.length - 1; i >= 0; i -= 1) {
            const lineNum = panelLineNumbers[i];
            if (lineNum >= currentLineNum) continue;
            const panel = panelsMap.get(lineNum);
            const status = getStoredPanelValue(panel, 'status');
            const reviewer = getStoredPanelValue(panel, 'reviewerInput');
            if ((status && status !== '未确认') || reviewer) {
                return [lineNum, panel];
            }
        }
        return null;
    }

    async function saveReviewBlocksBatch(filePath, payloadBlocks, actionType = 'confirm') {
        const isDraft = actionType === 'draft';
        const records = payloadBlocks.map(b => ({
            line_start: b.line_start,
            line_end: b.line_end,
            file_path: filePath,
            repository_name: currentRepositoryName,
            reviewer: b.reviewer || '',
            status: b.status || '未确认',
            coverage_method: b.coverage_method || '',
            uncovered_reason: b.uncovered_reason || '',
            is_draft: isDraft
        }));

        if (REPORT_MODE !== 'VNEXT_ARTIFACT_READY') {
            throw new Error('LEGACY_STATIC_REPORT');
        }
        const result = await requestCoverageApi('/analysis', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project_name: DEFAULT_PROJECT,
                scan_id: currentScanId,
                repository_name: currentRepositoryName,
                file_path: filePath,
                records
            })
        });

        // Update ReviewDraftStore and clear dirty
        payloadBlocks.forEach(b => {
            const sLine = b.line_start;
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

            const blockStart = panel.block ? panel.block.startLine : lineNum;
            const blockEnd = panel.block ? panel.block.endLine : lineNum;
            payloadBlocks.push({
                line_start: blockStart,
                line_end: blockEnd,
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
            if (currentScanId) {
                progressUrl.searchParams.set('scan_id', currentScanId);
            }
            if (currentRepositoryName) {
                progressUrl.searchParams.set('repository_name', currentRepositoryName);
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
            const pendingLine = panelLineNumbers.find(lineNum => isPanelAwaitingReview(panelsMap.get(lineNum)));
            const pendingEntry = pendingLine === undefined ? null : [pendingLine, panelsMap.get(pendingLine)];
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
                const bLen = panel.block ? (panel.block.length || 1) : 1;

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
                    const bLen = panel.block ? (panel.block.length || 1) : 1;
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
            panelsMap.clear();
            panelLineNumbers = [];
            dirtyPanelStartLines.clear();
            PerformanceTelemetry.reset();

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
                    loadedLineCount: 0,
                    error: null,
                    domContainer: null,
                    placeholderEl: null,
                    linesEl: null,
                    headerEl: null,
                    loadGeneration: 0,
                    virtualized: false,
                    virtualTopSpacer: null,
                    virtualContent: null,
                    virtualBottomSpacer: null,
                    virtualStart: 0,
                    virtualEnd: 0,
                    virtualRenderPending: false,
                    virtualLineHeight: VIRTUAL_LINE_HEIGHT,
                    virtualMeasuredHeights: new Map(),
                    virtualHeightBreaks: [],
                    virtualRequest: null,
                    virtualChunks: new Map(),
                    virtualChunkClock: 0,
                    virtualProtectedChunks: new Set()
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
                r.loadedLineCount = (lines || []).length;
                r.virtualChunks = new Map();
                r.virtualProtectedChunks = new Set();
                r.error = null;
                r.currentState = 'expanded-loaded';
                (lines || []).forEach(registerReviewPanelMetadata);
                if (typeof RegionLineLRUCache !== 'undefined' && RegionLineLRUCache.touch) {
                    RegionLineLRUCache.touch(regionId, (lines || []).length);
                    RegionLineLRUCache.evictIfOverBudget();
                }
            }
        },

        mergeLoadedLines(regionId, lines, startLine, totalLines) {
            const r = this._regions.get(regionId);
            if (!r) return;
            if (!Array.isArray(r.lines) || r.lines.length !== Number(totalLines || r.lineCount)) {
                r.lines = new Array(Number(totalLines || r.lineCount));
            }
            const physicalChunks = new Map();
            (lines || []).forEach(line => {
                const index = Number(line.line_no) - Number(r.startLine);
                if (index < 0 || index >= r.lines.length) {
                    return;
                }
                const chunkIndex = Math.floor(index / NETWORK_CHUNK_LINES);
                if (!physicalChunks.has(chunkIndex)) physicalChunks.set(chunkIndex, new Map());
                physicalChunks.get(chunkIndex).set(index, line);
                if (!r.lines[index]) r.loadedLineCount += 1;
                r.lines[index] = line;
            });
            physicalChunks.forEach((chunkLines, chunkIndex) => {
                let chunk = r.virtualChunks.get(chunkIndex);
                if (!chunk) {
                    chunk = { lines: new Map(), access: 0 };
                    r.virtualChunks.set(chunkIndex, chunk);
                }
                chunkLines.forEach((line, index) => chunk.lines.set(index, line));
                chunk.access = ++r.virtualChunkClock;
            });
            r.loaded = true;
            r.loading = false;
            r.error = null;
            r.currentState = 'expanded-loaded';
            (lines || []).forEach(registerReviewPanelMetadata);
            if (typeof RegionLineLRUCache !== 'undefined' && RegionLineLRUCache.touch) {
                RegionLineLRUCache.touch(regionId);
                RegionLineLRUCache.evictIfOverBudget();
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
                r.loading = false;
                r.error = null;
                r.progressText = '';
                r.currentState = r.loaded ? 'collapsed-loaded' : 'collapsed-unloaded';
            }
        },

        setExpanded(regionId) {
            const r = this._regions.get(regionId);
            if (r) {
                r.loading = false;
                r.error = null;
                r.progressText = '';
                r.currentState = 'expanded-loaded';
            }
        }
    };

    // =========================================================================
    const RegionLineLRUCache = {
        _accessMap: new Map(),
        _clock: 0,
        MAX_CACHED_LINES: 50000,
        MAX_VIRTUAL_CACHED_LINES: MAX_VIRTUAL_CACHED_LINES,
        touch(regionId) { this._accessMap.set(regionId, ++this._clock); },
        protectVirtualWindow(region, startIndex, endIndex) {
            if (!region || !region.virtualized) return;
            region.virtualProtectedChunks.clear();
            const start = Math.max(0, Math.floor(Number(startIndex) || 0));
            const end = Math.max(start, Math.ceil(Number(endIndex) || start));
            const first = Math.floor(start / NETWORK_CHUNK_LINES);
            const last = Math.max(first, Math.floor(Math.max(start, end - 1) / NETWORK_CHUNK_LINES));
            for (let index = first; index <= last; index += 1) {
                region.virtualProtectedChunks.add(index);
            }
        },
        _hasPinnedReviewPanel(region, chunkIndex) {
            const chunkStart = Number(region.startLine) + chunkIndex * NETWORK_CHUNK_LINES;
            const chunkEnd = Math.min(
                Number(region.endLine), chunkStart + NETWORK_CHUNK_LINES - 1
            );
            for (const [lineNumber, panel] of panelsMap.entries()) {
                if (lineNumber < chunkStart || lineNumber > chunkEnd) continue;
                const values = panel && panel.values;
                if (dirtyPanelStartLines.has(Number(lineNumber)) ||
                    (panel && panel.expanded) || (values && values.isDirty)) {
                    return true;
                }
            }
            return false;
        },
        evictVirtualChunks(region) {
            if (!region || !region.virtualized || !region.virtualChunks) return;
            let resident = 0;
            const candidates = [];
            region.virtualChunks.forEach((chunk, chunkIndex) => {
                resident += chunk.lines.size;
                if (!region.virtualProtectedChunks.has(chunkIndex) &&
                    !this._hasPinnedReviewPanel(region, chunkIndex)) {
                    candidates.push([chunkIndex, chunk]);
                }
            });
            candidates.sort((left, right) => left[1].access - right[1].access);
            for (const [chunkIndex, chunk] of candidates) {
                if (resident <= this.MAX_VIRTUAL_CACHED_LINES) break;
                chunk.lines.forEach((line, index) => {
                    if (region.lines[index] === line) {
                        region.lines[index] = undefined;
                        region.loadedLineCount = Math.max(0, region.loadedLineCount - 1);
                    }
                });
                region.virtualChunks.delete(chunkIndex);
                PerformanceTelemetry.virtualChunkEvictions += 1;
                resident -= chunk.lines.size;
            }
        },
        evictIfOverBudget() {
            const regions = CodeRegionStore.getAll();
            let totalLines = 0;
            const loadedCollapsed = [];
            regions.forEach(r => {
                if (r.virtualized && r.currentState !== "collapsed-loaded" &&
                    r.currentState !== "collapsed-unloaded") {
                    this.evictVirtualChunks(r);
                }
                if (r.loaded && r.lines && r.loadedLineCount) {
                    totalLines += r.loadedLineCount;
                    if (r.currentState === "collapsed-loaded" || r.currentState === "collapsed-unloaded") {
                        loadedCollapsed.push(r);
                    }
                }
            });
            if (totalLines > this.MAX_CACHED_LINES && loadedCollapsed.length > 0) {
                loadedCollapsed.sort((a, b) => (this._accessMap.get(a.id) || 0) - (this._accessMap.get(b.id) || 0));
                for (const r of loadedCollapsed) {
                    if (totalLines <= this.MAX_CACHED_LINES) break;
                    totalLines -= (r.loadedLineCount || 0);
                    r.loaded = false;
                    r.lines = [];
                    r.currentState = "collapsed-unloaded";
                    if (r.linesEl) r.linesEl.innerHTML = "";
                }
            }
            return totalLines;
        }
    };

    // 3. CodeRegionLoader: 区间/Chunk/Batch 数据请求与流式加载
    // =========================================================================
    const CodeRegionLoader = {
        _inflightPromises: new Map(), // regionId or 'batch' -> Promise
        _initialBatchControllers: new Set(),
        _initialBatchToken: 0,

        cancelInitialBatch() {
            this._initialBatchToken += 1;
            this._initialBatchControllers.forEach(controller => {
                try { controller.abort(); } catch (error) { /* already cancelled */ }
            });
            this._initialBatchControllers.clear();
        },

        _initialBatchGroups(regions) {
            const groups = [];
            let current = [];
            let logicalLines = 0;
            regions.forEach(region => {
                const span = Number(region.lineCount ||
                    (Number(region.endLine) - Number(region.startLine) + 1));
                if (current.length && (
                    current.length >= MAX_CODE_DETAIL_BATCH_RANGES ||
                    logicalLines + span > MAX_CODE_DETAIL_BATCH_LOGICAL_LINES
                )) {
                    groups.push(current);
                    current = [];
                    logicalLines = 0;
                }
                current.push(region);
                logicalLines += span;
            });
            if (current.length) groups.push(current);
            return groups;
        },

        async _requestInitialBatch(filePath, regions, token, generations) {
            const ranges = regions.map(region => ({
                start_line: region.startLine, end_line: region.endLine
            }));
            let lastError = null;
            for (let attempt = 0; attempt <= MAX_INITIAL_BATCH_RETRIES; attempt += 1) {
                if (token !== this._initialBatchToken) return null;
                const controller = typeof AbortController === 'function'
                    ? new AbortController() : null;
                if (controller) this._initialBatchControllers.add(controller);
                try {
                    const options = {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    };
                    if (controller) options.signal = controller.signal;
                    return await requestCoverageApi('/code-lines/batch', {
                        ...options,
                        body: JSON.stringify({
                            scan_id: currentScanId,
                            file_path: filePath,
                            repository_name: currentRepositoryName,
                            report_id: currentReportId,
                            ranges
                        })
                    });
                } catch (error) {
                    lastError = error;
                    if (token !== this._initialBatchToken ||
                        regions.some(region => region.loadGeneration !== generations.get(region.id))) {
                        return null;
                    }
                    if (attempt < MAX_INITIAL_BATCH_RETRIES) {
                        await new Promise(resolve => setTimeout(resolve, 50 * (attempt + 1)));
                    }
                } finally {
                    if (controller) this._initialBatchControllers.delete(controller);
                }
            }
            // A failed batch is returned as a per-batch failure.  The caller
            // may explicitly retry that region through the bounded GET path;
            // no other batch is discarded and no local data is fabricated.
            return { __batch_error__: lastError || new Error('batch request failed') };
        },

        async loadInitialBatch(filePath, expandedRegions) {
            if (!expandedRegions || expandedRegions.length === 0) {
                return [];
            }

            this.cancelInitialBatch();
            const token = this._initialBatchToken;
            const generations = new Map(expandedRegions.map(region => [
                region.id, region.loadGeneration || 0
            ]));

            expandedRegions.forEach(r => CodeRegionStore.setLoading(r.id, '正在加载…'));

            const batchRegions = expandedRegions.filter(
                region => Number(region.lineCount || 0) <= 10000
            );
            if (!batchRegions.length) return expandedRegions;

            const groups = this._initialBatchGroups(batchRegions);
            const responses = new Array(groups.length);
            let nextGroup = 0;
            const worker = async () => {
                while (nextGroup < groups.length) {
                    const index = nextGroup++;
                    responses[index] = await this._requestInitialBatch(
                        filePath, groups[index], token, generations
                    );
                }
            };
            await Promise.all(Array.from(
                { length: Math.min(MAX_INITIAL_BATCH_CONCURRENCY, groups.length) }, worker
            ));
            if (token !== this._initialBatchToken) return expandedRegions;

            responses.forEach((data, groupIndex) => {
                const group = groups[groupIndex];
                if (!data || data.__batch_error__) {
                    group.forEach(region => {
                        if (region.loadGeneration === generations.get(region.id)) {
                            CodeRegionStore.setCollapsed(region.id);
                        }
                    });
                    return;
                }
                // The VNext endpoint names the collection ``batches``; accept
                // the shared ``ranges`` spelling used by newer transports.
                const responseRanges = Array.isArray(data.ranges)
                    ? data.ranges
                    : (Array.isArray(data.batches) ? data.batches : null);
                const rangeMap = new Map();
                (responseRanges || []).forEach((range, index) => {
                    const fallback = group[index];
                    if (!fallback) return;
                    const start = range.start_line !== undefined ? range.start_line : fallback.startLine;
                    const end = range.end_line !== undefined ? range.end_line : fallback.endLine;
                    rangeMap.set(`${start}-${end}`, responseLines(range));
                });
                group.forEach(region => {
                    if (region.loadGeneration !== generations.get(region.id)) return;
                    const key = `${region.startLine}-${region.endLine}`;
                    if (rangeMap.has(key)) CodeRegionStore.setLoaded(region.id, rangeMap.get(key));
                    else CodeRegionStore.setCollapsed(region.id);
                });
            });
            return expandedRegions;
        },

        async fetchVirtualRange(filePath, region, startIndex, endIndex) {
            const startLine = Number(region.startLine) + Number(startIndex);
            const endLine = Math.min(
                Number(region.endLine), Number(region.startLine) + Number(endIndex) - 1
            );
            if (endLine < startLine) return [];
            const query = codeDetailQuery(filePath, startLine, endLine);
            const data = await requestCoverageApi(`/code-lines?${query.toString()}`, { method: 'GET' });
            const lines = responseLines(data);
            PerformanceTelemetry.networkChunks += 1;
            PerformanceTelemetry.networkLines += lines.length;
            CodeRegionStore.mergeLoadedLines(region.id, lines, startLine, region.lineCount);
            return lines;
        },

        async ensureVirtualWindow(filePath, region, startIndex, endIndex) {
            if (!region || !region.virtualized) return;
            const start = Math.max(0, Math.floor(startIndex));
            const end = Math.min(Number(region.lineCount), Math.ceil(endIndex));
            RegionLineLRUCache.protectVirtualWindow(region, start, end);
            let missingStart = -1;
            const missing = [];
            for (let index = start; index < end; index += 1) {
                if (!region.lines[index]) {
                    if (missingStart < 0) missingStart = index;
                } else if (missingStart >= 0) {
                    missing.push([missingStart, index]);
                    missingStart = -1;
                }
            }
            if (missingStart >= 0) missing.push([missingStart, end]);
            if (!missing.length) return;
            const requests = [];
            const physicalRequests = new Map();
            missing.forEach(([from, to]) => {
                const firstChunk = Math.floor(from / NETWORK_CHUNK_LINES);
                const lastChunk = Math.floor(Math.max(from, to - 1) / NETWORK_CHUNK_LINES);
                for (let chunk = firstChunk; chunk <= lastChunk; chunk += 1) {
                    const physicalStart = chunk * NETWORK_CHUNK_LINES;
                    physicalRequests.set(chunk, [
                        physicalStart,
                        Math.min(Number(region.lineCount), physicalStart + NETWORK_CHUNK_LINES)
                    ]);
                }
            });
            physicalRequests.forEach(item => requests.push(item));
            const key = `${region.id}:${requests.map(item => item.join('-')).join(',')}`;
            if (this._inflightPromises.has(key)) {
                return this._inflightPromises.get(key);
            }
            const promise = (async () => {
                let next = 0;
                const worker = async () => {
                    while (next < requests.length) {
                        const index = next++;
                        await this.fetchVirtualRange(
                            filePath, region, requests[index][0], requests[index][1]
                        );
                    }
                };
                await Promise.all(Array.from(
                    { length: Math.min(MAX_CHUNK_CONCURRENCY, requests.length) }, worker
                ));
            })();
            this._inflightPromises.set(key, promise);
            try {
                return await promise;
            } finally {
                this._inflightPromises.delete(key);
            }
        },

        async loadRegion(filePath, region, onChunkProgress) {
            if (region.loaded) {
                if (region.virtualized) {
                    const bounds = CodeRegionController.virtualWindowBounds(region);
                    await this.ensureVirtualWindow(filePath, region, bounds.start, bounds.end);
                }
                return region.lines;
            }

            if (this._inflightPromises.has(region.id)) {
                return this._inflightPromises.get(region.id);
            }

            const loadPromise = (async () => {
                CodeRegionStore.setLoading(region.id, '正在加载…');
                const generation = region.loadGeneration || 0;
                const totalLinesToLoad = region.endLine - region.startLine + 1;

                if (region.virtualized) {
                    const bounds = CodeRegionController.virtualWindowBounds(region);
                    await this.ensureVirtualWindow(
                        filePath, region, bounds.start, bounds.end
                    );
                    if (region.loadGeneration === generation) {
                        CodeRegionStore.setExpanded(region.id);
                    }
                    return region.lines;
                }

                if (totalLinesToLoad > NETWORK_CHUNK_LINES) {
                    // Item 2 & 3: Bounded Concurrency Chunk Streaming
                    const chunks = [];
                    let cIdx = 0;
                    for (let start = region.startLine; start <= region.endLine; start += NETWORK_CHUNK_LINES) {
                        const end = Math.min(start + NETWORK_CHUNK_LINES - 1, region.endLine);
                        chunks.push({ index: cIdx++, start, end });
                    }

                    const allLines = [];
                    region.lines = allLines;
                    const chunkResults = new Map();
                    let nextTaskIdx = 0;
                    let nextEmitIdx = 0;
                    let fetchError = null;

                    async function chunkWorker() {
                        while (nextTaskIdx < chunks.length && !fetchError) {
                            if (region.loadGeneration !== generation) return;
                            const task = chunks[nextTaskIdx++];
                            try {
                                const query = codeDetailQuery(filePath, task.start, task.end);
                                const chunkData = await requestCoverageApi(`/code-lines?${query.toString()}`, { method: 'GET' });
                                if (region.loadGeneration !== generation) return;
                                const chunkLines = responseLines(chunkData);
                                PerformanceTelemetry.networkChunks += 1;
                                PerformanceTelemetry.networkLines += chunkLines.length;
                                chunkResults.set(task.index, { task, chunkLines });
                            } catch (err) {
                                fetchError = err;
                                return;
                            }
                        }
                    }

                    const workerCount = Math.min(MAX_CHUNK_CONCURRENCY, chunks.length);
                    const workerPromises = [];
                    for (let w = 0; w < workerCount; w++) {
                        workerPromises.push(chunkWorker());
                    }

                    while (nextEmitIdx < chunks.length) {
                        if (region.loadGeneration !== generation) return allLines;

                        if (chunkResults.has(nextEmitIdx)) {
                            const item = chunkResults.get(nextEmitIdx);
                            chunkResults.delete(nextEmitIdx);
                            allLines.push(...item.chunkLines);
                            if (typeof onChunkProgress === 'function' && region.loadGeneration === generation) {
                                await onChunkProgress(item.chunkLines, item.task.start, item.task.end, allLines.length, totalLinesToLoad);
                            }
                            nextEmitIdx++;
                        } else if (fetchError) {
                            throw fetchError;
                        } else {
                            await new Promise(r => setTimeout(r, 10));
                        }
                    }
                    await Promise.all(workerPromises);
                    if (fetchError) throw fetchError;

                    if (region.loadGeneration === generation) {
                        CodeRegionStore.setLoaded(region.id, allLines);
                        RegionLineLRUCache.touch(region.id);
                        RegionLineLRUCache.evictIfOverBudget();
                    }
                    return allLines;
                } else {
                    const query = codeDetailQuery(filePath, region.startLine, region.endLine);

                    const data = await requestCoverageApi(`/code-lines?${query.toString()}`, { method: 'GET' });
                    if (region.loadGeneration !== generation) {
                        return [];
                    }
                    const lines = responseLines(data);
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
            const suggestedReviewer = lineData.suggested_reviewer || '';
            if (suggestedReviewer) {
                lineSpan.setAttribute('data-coverage-reviewer', suggestedReviewer);
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
            const existingPanelState = registerReviewPanelMetadata(lineData);
            const inheritance = inheritanceMetadata(lineData);

            // Merge draft store if user had unsaved edits
            const draft = ReviewDraftStore.getDraft(startLineNum);
            const existingValues = existingPanelState && existingPanelState.values
                ? existingPanelState.values : initialPanelValues(lineData, startLineNum);
            const initialStatus = draft && draft.status !== undefined ? draft.status
                : (existingValues.status !== undefined ? existingValues.status : (lineData.analysis_state || '未确认'));
            const initialReviewer = draft && draft.reviewer !== undefined ? draft.reviewer
                : (existingValues.reviewerInput !== undefined ? existingValues.reviewerInput
                    : (lineData.reviewer || lineData.suggested_reviewer || ''));
            const initialMethod = draft && draft.coverage_method !== undefined ? draft.coverage_method
                : (existingValues.methodInput !== undefined ? existingValues.methodInput : (lineData.coverage_method || ''));
            const initialReason = draft && draft.uncovered_reason !== undefined ? draft.uncovered_reason
                : (existingValues.reasonInput !== undefined ? existingValues.reasonInput : (lineData.uncovered_reason || ''));
            const isDirty = draft && draft.isDirty !== undefined ? Boolean(draft.isDirty)
                : Boolean(existingValues.isDirty);
            const isDraft = draft && draft.isDraft !== undefined ? Boolean(draft.isDraft)
                : Boolean(existingValues.isDraft);
            const isConfirmed = !isDraft && CONFIRMED_STATUS_SET.has(initialStatus);
            const origSavedConfirmed = existingValues._origSavedConfirmed !== undefined
                ? existingValues._origSavedConfirmed
                : (!lineData.is_draft && CONFIRMED_STATUS_SET.has(lineData.analysis_state));

            const panel = document.createElement('span');
            panel.className = 'coverage-analysis-panel' + (isMultiLine ? ' multiline' : '');
            panel.setAttribute('contenteditable', 'false');
            panel.setAttribute('data-panel-start-line', String(startLineNum));

            // Align to right column
            const codeLen = (lineData.source || '').length;
            const targetCol = Math.max(121, codeLen + 2);
            panel.style.left = `${targetCol}ch`;

            if (isMultiLine) {
                // The controls are variable-height (textarea resizing,
                // localized fonts). Keep a minimum footprint and let the
                // virtual row measurement account for the actual panel.
                panel.style.minHeight = `${Math.max(20, blockLength * VIRTUAL_LINE_HEIGHT - 4)}px`;
            }

            // Status select
            const select = document.createElement('select');
            select.className = 'coverage-analysis-select';
            select.setAttribute('data-panel-action', 'status');
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
            previousBtn.setAttribute('data-panel-action', 'previous');

            const nextBtn = document.createElement('button');
            nextBtn.className = 'coverage-navigation-btn';
            nextBtn.type = 'button';
            nextBtn.innerText = '下一个';
            nextBtn.setAttribute('aria-label', '跳转到下一个可填写控件');
            nextBtn.setAttribute('data-panel-action', 'next');

            // Inherit buttons
            const inheritBtn = document.createElement('button');
            inheritBtn.className = 'coverage-inherit-btn';
            inheritBtn.type = 'button';
            inheritBtn.innerText = '继承';
            inheritBtn.title = '仅确认服务器提供的精确自动继承关系';
            inheritBtn.setAttribute('data-panel-action', 'inherit');

            const batchInheritBtn = document.createElement('button');
            batchInheritBtn.className = 'coverage-inherit-btn batch';
            batchInheritBtn.type = 'button';
            batchInheritBtn.innerText = '手工复制上一条';
            batchInheritBtn.title = '明确创建 MANUAL 草稿：从上方最近已填写控件复制到当前区域';
            batchInheritBtn.setAttribute('data-panel-action', 'manual-copy');

            const rejectBtn = document.createElement('button');
            rejectBtn.className = 'coverage-inherit-btn reject coverage-inherit-reject-btn';
            rejectBtn.type = 'button';
            rejectBtn.innerText = '拒绝继承';
            rejectBtn.title = '拒绝本次自动继承，不让旧结论继续传递';
            rejectBtn.setAttribute('data-panel-action', 'reject-inheritance');
            rejectBtn.style.display = inheritance.inheritedPending ? '' : 'none';

            const undoRejectBtn = document.createElement('button');
            undoRejectBtn.className = 'coverage-inherit-btn undo coverage-inherit-undo-btn';
            undoRejectBtn.type = 'button';
            undoRejectBtn.innerText = '撤销拒绝';
            undoRejectBtn.title = '恢复本次继承并回到待复核状态';
            undoRejectBtn.setAttribute('data-panel-action', 'undo-rejection');
            undoRejectBtn.style.display = inheritance.rejected ? '' : 'none';

            // Reviewer input
            const reviewerInput = document.createElement('input');
            reviewerInput.type = 'text';
            reviewerInput.className = 'coverage-analysis-input reviewer-input';
            reviewerInput.placeholder = '确认人';
            reviewerInput.value = initialReviewer;
            reviewerInput.setAttribute('data-panel-action', 'reviewer');

            // Method textarea
            const methodInput = document.createElement('textarea');
            methodInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            methodInput.placeholder = '条件覆盖方法';
            methodInput.value = initialMethod;
            methodInput.setAttribute('data-panel-action', 'method');
            const methodGrip = createResizeGrip(methodInput);

            // Reason textarea
            const reasonInput = document.createElement('textarea');
            reasonInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            reasonInput.placeholder = '无条件覆盖原因';
            reasonInput.value = initialReason;
            reasonInput.setAttribute('data-panel-action', 'reason');
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
            saveBtn.type = 'button';
            saveBtn.setAttribute('data-panel-action', 'save');
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
            panel.appendChild(rejectBtn);
            panel.appendChild(undoRejectBtn);
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
                length: blockLength
            };

            const panelState = existingPanelState || {
                select,
                reviewerInput,
                methodInput,
                reasonInput,
                saveBtn,
                previousBtn,
                nextBtn,
                rejectBtn,
                undoRejectBtn,
                block: blockObj,
                lineNum: startLineNum,
                expanded: true,
                inheritance: inheritance,
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
            panelState.select = select;
            panelState.reviewerInput = reviewerInput;
            panelState.methodInput = methodInput;
            panelState.reasonInput = reasonInput;
            panelState.saveBtn = saveBtn;
            panelState.previousBtn = previousBtn;
            panelState.nextBtn = nextBtn;
            panelState.rejectBtn = rejectBtn;
            panelState.undoRejectBtn = undoRejectBtn;
            panelState.block = blockObj;
            panelState.lineNum = startLineNum;
            panelState.expanded = true;
            panelState.inheritance = inheritance;
            updateInheritanceControls(panelState);
            panelState.values = Object.assign({}, panelState.values || {}, {
                status: initialStatus,
                reviewerInput: initialReviewer,
                methodInput: initialMethod,
                reasonInput: initialReason,
                isDirty: isDirty,
                isDraft: isDraft,
                _origSavedConfirmed: origSavedConfirmed
            });
            panelsMap.set(startLineNum, panelState);
            if (REPORT_MODE === 'LEGACY_STATIC') {
                [select, previousBtn, nextBtn, inheritBtn, batchInheritBtn,
                 rejectBtn, undoRejectBtn, reviewerInput, methodInput,
                 reasonInput, saveBtn].forEach(control => {
                    control.disabled = true;
                });
                panel.setAttribute('data-report-mode', 'LEGACY_STATIC');
                panel.title = '历史静态报告：VNext 分析操作不可用';
            }
            if (isDirty) {
                dirtyPanelStartLines.add(startLineNum);
            }

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
        _panelDelegationRoot: null,
        _virtualListenerInstalled: false,

        installPanelDelegation(container) {
            if (!container || this._panelDelegationRoot === container) return;
            this._panelDelegationRoot = container;
            container.addEventListener('click', event => this.handlePanelClick(event));
            container.addEventListener('change', event => this.handlePanelChange(event));
            container.addEventListener('input', event => this.handlePanelInput(event));
        },

        installVirtualScrollListener() {
            if (this._virtualListenerInstalled || typeof window === 'undefined' || !window.addEventListener) return;
            this._virtualListenerInstalled = true;
            const refresh = () => {
                CodeRegionStore.getAll().forEach(region => {
                    if (region.virtualized && region.linesEl && region.loaded) {
                        this.scheduleVirtualRender(region);
                    }
                });
            };
            window.addEventListener('scroll', refresh, { passive: true });
            window.addEventListener('resize', refresh);
        },

        ensureVirtualScaffold(region) {
            if (!region.linesEl || !region.virtualized) return;
            if (region.virtualTopSpacer && region.virtualContent && region.virtualBottomSpacer) return;
            region.linesEl.innerHTML = '';
            const top = document.createElement('div');
            top.className = 'coverage-virtual-spacer';
            top.setAttribute('aria-hidden', 'true');
            const content = document.createElement('div');
            content.className = 'coverage-virtual-content';
            const bottom = document.createElement('div');
            bottom.className = 'coverage-virtual-spacer';
            bottom.setAttribute('aria-hidden', 'true');
            region.virtualTopSpacer = top;
            region.virtualContent = content;
            region.virtualBottomSpacer = bottom;
            region.linesEl.appendChild(top);
            region.linesEl.appendChild(content);
            region.linesEl.appendChild(bottom);
        },

        rebuildVirtualHeightIndex(region) {
            if (!region) return;
            const base = Math.max(16, Number(region.virtualLineHeight) || VIRTUAL_LINE_HEIGHT);
            const entries = Array.from((region.virtualMeasuredHeights || new Map()).entries())
                .filter(([index, height]) => Number.isFinite(Number(index)) && Number.isFinite(Number(height)))
                .map(([index, height]) => ({ index: Number(index), height: Math.max(1, Number(height)) }))
                .sort((left, right) => left.index - right.index);
            let cumulativeDelta = 0;
            region.virtualHeightBreaks = entries.map(entry => {
                cumulativeDelta += entry.height - base;
                return { index: entry.index, cumulativeDelta };
            });
        },

        virtualOffsetForIndex(region, index) {
            const target = Math.max(0, Math.floor(Number(index) || 0));
            const base = Math.max(16, Number(region.virtualLineHeight) || VIRTUAL_LINE_HEIGHT);
            const breaks = region.virtualHeightBreaks || [];
            let low = 0;
            let high = breaks.length;
            while (low < high) {
                const middle = (low + high) >> 1;
                if (breaks[middle].index < target) low = middle + 1;
                else high = middle;
            }
            const cumulativeDelta = low > 0 ? breaks[low - 1].cumulativeDelta : 0;
            return Math.max(0, target * base + cumulativeDelta);
        },

        virtualIndexAtOffset(region, offset) {
            const total = Math.max(Number(region.lineCount) || 0, (region.lines || []).length);
            if (!total) return 0;
            const target = Math.max(0, Number(offset) || 0);
            let low = 0;
            let high = total;
            while (low < high) {
                const middle = (low + high) >> 1;
                if (this.virtualOffsetForIndex(region, middle + 1) <= target) low = middle + 1;
                else high = middle;
            }
            return Math.min(total - 1, low);
        },

        virtualWindowBounds(region, forcedStart) {
            const total = Math.max(Number(region.lineCount) || 0, (region.lines || []).length);
            const lineHeight = Math.max(16, Number(region.virtualLineHeight) || VIRTUAL_LINE_HEIGHT);
            const viewportHeight = Number(window.innerHeight) || 800;
            const visibleRows = Math.max(1, Math.ceil(viewportHeight / lineHeight));
            if (Number.isFinite(forcedStart)) {
                const centered = Math.floor(visibleRows / 2);
                const start = Math.max(0, Math.min(total, Math.floor(forcedStart) - centered - VIRTUAL_OVERSCAN_LINES));
                return {
                    start,
                    end: Math.min(total, start + visibleRows + (VIRTUAL_OVERSCAN_LINES * 2))
                };
            }
            let viewportStart = 0;
            if (region.linesEl && region.linesEl.getBoundingClientRect) {
                const rect = region.linesEl.getBoundingClientRect();
                const scrollTop = Number(window.pageYOffset || window.scrollY || 0);
                viewportStart = Math.max(0, scrollTop - (Number(rect.top) + scrollTop));
            }
            // Use a variable-size prefix index. A resized/multi-line review
            // row contributes its measured delta instead of poisoning every
            // later spacer with one global average.
            const firstVisible = this.virtualIndexAtOffset(region, viewportStart);
            const lastVisible = this.virtualIndexAtOffset(region, viewportStart + viewportHeight);
            const start = Math.max(0, firstVisible - VIRTUAL_OVERSCAN_LINES);
            return {
                start,
                end: Math.min(total, lastVisible + 1 + (VIRTUAL_OVERSCAN_LINES * 2))
            };
        },

        renderVirtualWindow(region, forcedStart) {
            if (!region || !region.virtualized || !region.linesEl) return;
            this.ensureVirtualScaffold(region);
            if (!region.virtualContent) return;
            const bounds = this.virtualWindowBounds(region, forcedStart);
            RegionLineLRUCache.protectVirtualWindow(region, bounds.start, bounds.end);
            RegionLineLRUCache.touch(region.id);
            const availableEnd = Math.min(bounds.end, (region.lines || []).length);
            const fragment = document.createDocumentFragment();
            const renderedIndexes = [];
            for (let index = bounds.start; index < availableEnd; index += 1) {
                const line = region.lines[index];
                if (line) {
                    fragment.appendChild(CodeLineRenderer.renderCodeLine(line, this.filePath));
                    renderedIndexes.push(index);
                }
            }
            region.virtualContent.innerHTML = '';
            region.virtualContent.appendChild(fragment);
            region.virtualStart = bounds.start;
            region.virtualEnd = availableEnd;
            const measuredHeights = [];
            Array.from(region.virtualContent.children).forEach((element, childIndex) => {
                const height = Number(element.getBoundingClientRect
                    ? element.getBoundingClientRect().height : 0);
                const lineIndex = renderedIndexes[childIndex];
                if (lineIndex === undefined || height <= 0) return;
                region.virtualMeasuredHeights.set(lineIndex, height);
                measuredHeights.push(height);
            });
            if (measuredHeights.length) {
                // Median is robust to a tall review panel and gives
                // unmeasured source rows a stable local estimate.
                measuredHeights.sort((left, right) => left - right);
                const middle = Math.floor(measuredHeights.length / 2);
                const median = measuredHeights.length % 2
                    ? measuredHeights[middle]
                    : (measuredHeights[middle - 1] + measuredHeights[middle]) / 2;
                region.virtualLineHeight = Math.max(16, Math.min(160, median));
            }
            this.rebuildVirtualHeightIndex(region);
            region.virtualTopSpacer.style.height = `${this.virtualOffsetForIndex(region, bounds.start)}px`;
            const totalHeight = this.virtualOffsetForIndex(
                region, Math.max(region.lineCount, region.lines.length)
            );
            region.virtualBottomSpacer.style.height = `${Math.max(
                0, totalHeight - this.virtualOffsetForIndex(region, availableEnd)
            )}px`;
            PerformanceTelemetry.virtualRenders += 1;
            PerformanceTelemetry.recordDomLineCount(region.virtualContent.children.length, true);
        },

        scheduleVirtualRender(region, forcedStart) {
            if (!region || !region.virtualized || region.virtualRenderPending) return;
            region.virtualRenderPending = true;
            const render = async () => {
                region.virtualRenderPending = false;
                const bounds = this.virtualWindowBounds(region, forcedStart);
                try {
                    await CodeRegionLoader.ensureVirtualWindow(
                        this.filePath, region, bounds.start, bounds.end
                    );
                    this.renderVirtualWindow(region, bounds.start);
                } catch (error) {
                    CodeRegionStore.setError(region.id, error.message || String(error));
                    this.updatePlaceholderState(region);
                }
            };
            if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
                window.requestAnimationFrame(render);
            } else {
                setTimeout(render, 0);
            }
        },

        async revealLine(region, lineNum) {
            if (!region) return;
            if (!region.loaded) await this.expandRegion(region.id);
            if (region.virtualized) {
                this.renderVirtualWindow(region, Number(lineNum) - Number(region.startLine));
            }
            const targetLine = document.getElementById(`L${lineNum}`);
            if (targetLine && targetLine.scrollIntoView) {
                targetLine.scrollIntoView({ behavior: 'smooth', block: 'center' });
                targetLine.style.background = '#fff8c5';
                setTimeout(() => targetLine.style.background = '', 2000);
            }
        },

        delegatedPanelTarget(target, root) {
            let current = target;
            while (current && current !== root) {
                if (current.dataset && current.dataset.panelStartLine) {
                    return current;
                }
                current = current.parentNode;
            }
            return null;
        },

        delegatedActionTarget(target, root) {
            let current = target;
            while (current && current !== root) {
                if (current.dataset && current.dataset.panelAction) {
                    return current;
                }
                current = current.parentNode;
            }
            return null;
        },

        panelForEvent(event) {
            const panelEl = this.delegatedPanelTarget(event.target, this._panelDelegationRoot);
            if (!panelEl) return null;
            return panelsMap.get(Number(panelEl.dataset.panelStartLine));
        },

        handlePanelClick(event) {
            const actionEl = this.delegatedActionTarget(event.target, this._panelDelegationRoot);
            if (!actionEl) return;
            const action = actionEl.dataset.panelAction;
            const panel = this.panelForEvent(event);
            if (!panel) return;
            event.preventDefault();
            event.stopPropagation();
            if (action === 'save') this.savePanel(panel);
            else if (action === 'previous') navigateReviewPanel(panel.lineNum, -1);
            else if (action === 'next') navigateReviewPanel(panel.lineNum, 1);
            else if (action === 'inherit') this.inheritPanel(panel);
            else if (action === 'batch-inherit' || action === 'manual-copy') this.batchInheritPanel(panel);
            else if (action === 'reject-inheritance') this.rejectServerInheritance(panel);
            else if (action === 'undo-rejection') this.undoServerInheritance(panel);
        },

        handlePanelChange(event) {
            const actionEl = this.delegatedActionTarget(event.target, this._panelDelegationRoot);
            if (!actionEl || actionEl.dataset.panelAction !== 'status') return;
            const panel = this.panelForEvent(event);
            if (!panel) return;
            if (panel.saveBtn && String(panel.saveBtn.className).includes('saved')) {
                panel.saveBtn.innerText = 'Save';
                panel.saveBtn.className = 'coverage-analysis-btn';
            }
            setStoredPanelValues(panel, {
                status: panel.select ? panel.select.value : event.target.value,
                isDirty: true
            });
            markPanelDirty(panel.lineNum);
        },

        handlePanelInput(event) {
            const actionEl = this.delegatedActionTarget(event.target, this._panelDelegationRoot);
            if (!actionEl || !['reviewer', 'method', 'reason'].includes(actionEl.dataset.panelAction)) return;
            const panel = this.panelForEvent(event);
            if (!panel) return;
            setStoredPanelValues(panel, {
                reviewerInput: panel.reviewerInput ? panel.reviewerInput.value : '',
                methodInput: panel.methodInput ? panel.methodInput.value : '',
                reasonInput: panel.reasonInput ? panel.reasonInput.value : '',
                isDirty: true
            });
            markPanelDirty(panel.lineNum);
        },

        savePanel(panel) {
            const reviewerVal = getStoredPanelValue(panel, 'reviewerInput').trim();
            const statusVal = getStoredPanelValue(panel, 'status');
            const methodVal = getStoredPanelValue(panel, 'methodInput').trim();
            const reasonVal = getStoredPanelValue(panel, 'reasonInput').trim();
            const fail = message => {
                if (typeof alert === 'function') alert(message);
            };
            if (statusVal === '未确认') {
                fail('[校验失败]：请将第一列状态变更为“可覆盖”或“无法覆盖”！');
                if (panel.select) panel.select.focus();
                return;
            }
            if (!reviewerVal) {
                fail('[校验失败]：请输入第二列确认人！');
                if (panel.reviewerInput) panel.reviewerInput.focus();
                return;
            }
            if (!methodVal && !reasonVal) {
                fail('[校验失败]：“条件覆盖方法”与“无条件覆盖原因”必须填写其中之一！');
                if (panel.methodInput) panel.methodInput.focus();
                return;
            }
            if (panel.saveBtn) {
                panel.saveBtn.innerText = 'Saving...';
                panel.saveBtn.className = 'coverage-analysis-btn saving';
            }
            return saveReviewBlocksBatch(this.filePath, [{
                line_start: panel.block.startLine,
                line_end: panel.block.endLine,
                reviewer: reviewerVal,
                status: statusVal,
                coverage_method: methodVal,
                uncovered_reason: reasonVal
            }], 'confirm').then(() => {
                setStoredPanelValues(panel, {
                    status: statusVal, reviewerInput: reviewerVal,
                    methodInput: methodVal, reasonInput: reasonVal,
                    isDraft: false, isDirty: false, _origSavedConfirmed: true
                });
                clearPanelDirty(panel.lineNum, false);
                if (panel.saveBtn) {
                    panel.saveBtn.innerText = '已确认';
                    panel.saveBtn.className = 'coverage-analysis-btn saved';
                }
                notifyProgressChanged();
                updateHeaderStatistics();
                showToast(`第 ${panel.block.startLine}-${panel.block.endLine} 行分析已确认保存`);
            }).catch(err => {
                console.error('[CoverageEnhance] Single block save failed:', err);
                if (panel.saveBtn) {
                    panel.saveBtn.innerText = 'Error';
                    panel.saveBtn.className = 'coverage-analysis-btn error';
                    panel.saveBtn.title = `保存失败: ${err.message}`;
                }
                window.setTimeout(() => markPanelDirty(panel.lineNum), 3000);
            });
        },

        async findServerInheritanceCandidate(panel) {
            if (REPORT_MODE !== 'VNEXT_ARTIFACT_READY' ||
                    !panel || !currentScanId || !this.filePath) return null;
            const query = new URLSearchParams({
                repository_name: currentRepositoryName || '',
                file_path: this.filePath,
                line_number: String(panel.lineNum)
            });
            const payload = await requestCoverageApi(
                `/scans/${encodeURIComponent(currentScanId)}/inheritance/relation?${query.toString()}`,
                { method: 'GET' }
            );
            return payload && payload.item ? payload.item : null;
        },

        async confirmServerInheritance(panel, candidate) {
            if (REPORT_MODE !== 'VNEXT_ARTIFACT_READY') return false;
            const lineId = Number(candidate && candidate.candidate_line_id);
            const revision = Number(candidate && candidate.relation_revision);
            if (!lineId || !revision) return false;
            await requestCoverageApi(
                `/scans/${encodeURIComponent(currentScanId)}/inheritance/confirm`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        selected_line_ids: [lineId],
                        expected_relation_revisions: { [String(lineId)]: revision }
                    })
                }
            );
            const reviewer = candidate.reviewed_by ||
                getStoredPanelValue(panel, 'reviewerInput') || '';
            setStoredPanelValues(panel, {
                status: candidate.conclusion_status || '未确认',
                reviewerInput: reviewer,
                methodInput: candidate.coverage_method || '',
                reasonInput: candidate.uncovered_reason || '',
                isDirty: false,
                isDraft: false,
                _origSavedConfirmed: true
            });
            panel.inheritance = {
                lineId: lineId,
                relationRevision: revision + 1,
                state: 'MANUAL_CONFIRMED',
                relationActive: true,
                rejectionId: 0,
                rejectionRevision: 0,
                inheritedPending: false,
                rejected: false
            };
            updateInheritanceControls(panel);
            clearPanelDirty(panel.lineNum, true);
            notifyProgressChanged();
            updateHeaderStatistics();
            return true;
        },

        async rejectServerInheritance(panel) {
            if (REPORT_MODE !== 'VNEXT_ARTIFACT_READY') return false;
            const meta = panel && panel.inheritance;
            if (!meta || !meta.inheritedPending || !meta.lineId || !meta.relationRevision) return false;
            if (typeof window !== 'undefined' && typeof window.confirm === 'function' &&
                    !window.confirm('拒绝后，该旧结论不会继续自动传递到后续版本。确定拒绝吗？')) {
                return false;
            }
            const payload = await requestCoverageApi(
                `/scans/${encodeURIComponent(currentScanId)}/inheritance/reject`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        line_id: meta.lineId,
                        expected_relation_revision: meta.relationRevision
                    })
                }
            );
            const rejection = payload && payload.rejection ? payload.rejection : {};
            panel.inheritance = {
                lineId: meta.lineId,
                relationRevision: meta.relationRevision + 1,
                state: 'INHERITANCE_REJECTED',
                relationActive: false,
                rejectionId: Number(rejection.id || meta.rejectionId || 0),
                rejectionRevision: Number(rejection.rejection_revision || 1),
                inheritedPending: false,
                rejected: true
            };
            setStoredPanelValues(panel, {
                status: '未确认', reviewerInput: '', methodInput: '', reasonInput: '',
                isDraft: false, isDirty: false, _origSavedConfirmed: false
            });
            updateInheritanceControls(panel);
            clearPanelDirty(panel.lineNum, true);
            notifyProgressChanged();
            updateHeaderStatistics();
            showToast(`第 ${panel.lineNum} 行已拒绝自动继承`);
            return true;
        },

        async undoServerInheritance(panel) {
            if (REPORT_MODE !== 'VNEXT_ARTIFACT_READY') return false;
            const meta = panel && panel.inheritance;
            if (!meta || !meta.rejected || !meta.lineId || !meta.rejectionId ||
                    !meta.rejectionRevision || !meta.relationRevision) return false;
            const payload = await requestCoverageApi(
                `/scans/${encodeURIComponent(currentScanId)}/inheritance/rejections/` +
                    `${encodeURIComponent(meta.rejectionId)}/undo`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        line_id: meta.lineId,
                        rejection_id: meta.rejectionId,
                        expected_rejection_revision: meta.rejectionRevision,
                        expected_relation_revision: meta.relationRevision
                    })
                }
            );
            const candidate = await this.findServerInheritanceCandidate(panel);
            panel.inheritance = {
                lineId: meta.lineId,
                relationRevision: meta.relationRevision + 1,
                state: 'INHERITED_PENDING',
                relationActive: true,
                rejectionId: 0,
                rejectionRevision: 0,
                inheritedPending: true,
                rejected: false
            };
            if (candidate) {
                setStoredPanelValues(panel, {
                    status: candidate.conclusion_status || '未确认',
                    reviewerInput: candidate.reviewed_by || '',
                    methodInput: candidate.coverage_method || '',
                    reasonInput: candidate.uncovered_reason || '',
                    isDraft: true, isDirty: false, _origSavedConfirmed: false
                });
            }
            updateInheritanceControls(panel);
            clearPanelDirty(panel.lineNum, true);
            notifyProgressChanged();
            updateHeaderStatistics();
            showToast(`第 ${panel.lineNum} 行已撤销拒绝，恢复待复核继承`);
            return Boolean(payload);
        },

        async inheritPanel(panel) {
            let serverCandidate = null;
            try {
                serverCandidate = await this.findServerInheritanceCandidate(panel);
            } catch (error) {
                if (typeof alert === 'function') {
                    alert(`无法读取服务器继承关系；当前面板未改变。${error.message ? `\n${error.message}` : ''}`);
                }
                return;
            }
            if (serverCandidate) {
                try {
                    if (await this.confirmServerInheritance(panel, serverCandidate)) {
                        showToast(`第 ${panel.lineNum} 行继承结果已确认`);
                        return;
                    }
                } catch (error) {
                    if (typeof alert === 'function') {
                        alert(`继承确认失败: ${error.message}`);
                    }
                    return;
                }
            }
            if (typeof alert === 'function') {
                alert('当前行没有服务器提供的自动继承关系。请使用“手工复制上一条”创建明确的 MANUAL 草稿。');
            }
        },

        batchInheritPanel(panel) {
            const sourceEntry = findPreviousFilledPanelEntry(panel.lineNum);
            if (!sourceEntry) {
                if (typeof alert === 'function') alert('没有找到可作为批量继承来源的上一条已填写结果。');
                return;
            }
            const sourceLineNum = sourceEntry[0];
            const sourcePanel = sourceEntry[1];
            const inheritedValues = {
                status: getStoredPanelValue(sourcePanel, 'status') || '未确认',
                reviewerInput: getStoredPanelValue(sourcePanel, 'reviewerInput'),
                methodInput: getStoredPanelValue(sourcePanel, 'methodInput'),
                reasonInput: getStoredPanelValue(sourcePanel, 'reasonInput'),
                isDraft: true,
                isDirty: true
            };
            const targetEntries = panelLineNumbers
                .filter(lineNum => lineNum > sourceLineNum && lineNum <= panel.lineNum)
                .map(lineNum => [lineNum, panelsMap.get(lineNum)]);
            targetEntries.forEach(([lineNum, targetPanel]) => {
                setStoredPanelValues(targetPanel, inheritedValues);
                if (targetPanel.saveBtn) {
                    targetPanel.saveBtn.innerText = 'Save';
                    targetPanel.saveBtn.className = 'coverage-analysis-btn';
                }
                markPanelDirty(lineNum);
            });
            if (typeof alert === 'function') {
                alert(`已从第 ${sourceLineNum} 行批量继承到 ${targetEntries.length} 个控件，请点击“暂存草稿”或“确认提交”写入数据库。`);
            }
        },

        async init(layoutData, preSource) {
            const layoutStart = typeof performance !== 'undefined' && performance.now
                ? performance.now() : 0;
            this.filePath = layoutData.file_path || '';
            if (layoutData.scan_id) currentScanId = String(layoutData.scan_id);
            if (layoutData.report_id) currentReportId = String(layoutData.report_id);
            if (layoutData.repository_name !== undefined) {
                currentRepositoryName = String(layoutData.repository_name || '');
            }
            this.container = preSource;
            this.installPanelDelegation(preSource);
            this.installVirtualScrollListener();
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
                        if (reg.loaded && reg.currentState === 'expanded-loaded') {
                            await this.renderRegionLines(reg);
                        } else if (reg.currentState === 'collapsed-unloaded') {
                            // A partial/empty batch response is a recoverable
                            // contract mismatch.  Keep the missing region
                            // interactive but collapsed; expanding it here
                            // would immediately reintroduce the per-region
                            // fallback request that batching was meant to
                            // avoid.
                            this.updatePlaceholderState(reg);
                        } else {
                            await this.expandRegion(reg);
                        }
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
            if (layoutStart) {
                PerformanceTelemetry.layoutStart = layoutStart;
                PerformanceTelemetry.layoutMs = Number((performance.now() - layoutStart).toFixed(3));
            }
        },

        createPlaceholderElement(region) {
            const el = document.createElement('div');
            el.className = 'coverage-region-placeholder';
            // Make the visible default explicit for lightweight DOM shims as
            // well as real browsers (where an unset display resolves to '').
            el.style.display = '';
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

            if (CodeRegionLoader._inflightPromises.has(region.id)) {
                // In-flight load already in progress, await it without re-preparing DOM
                await CodeRegionLoader._inflightPromises.get(region.id);
                if (region.loaded && (!region.linesEl || !region.linesEl.children.length)) {
                    await this.renderRegionLines(region);
                }
                return;
            }

            region.loadGeneration = (region.loadGeneration || 0) + 1;
            const currentGen = region.loadGeneration;

            // Load lines via Loader with chunk streaming - clear any partially loaded DOM lines from prior failed attempt
            this.updatePlaceholderState(region);
            try {
                this.prepareRegionLinesContainer(region, { reset: true });
                region.lines = [];

                await CodeRegionLoader.loadRegion(this.filePath, region, async (chunkLines, start, end, loaded, total) => {
                    if (region.loadGeneration !== currentGen) return;
                    region.progressText = `正在展开 ${loaded} / ${total} 行…`;
                    this.updatePlaceholderState(region);
                    await this.appendChunkLines(region, chunkLines);
                });

                if (region.loadGeneration === currentGen && region.currentState !== 'collapsed-unloaded' && region.currentState !== 'collapsed-loaded') {
                    // Non-chunked ranges are loaded directly by the loader and
                    // therefore have no streaming callback to populate the DOM.
                    if (region.loaded && !region.virtualized && region.linesEl && !region.linesEl.children.length) {
                        await this.renderRegionLines(region);
                    }
                    await this.finalizeRegionLoaded(region);
                }
            } catch (err) {
                console.error(`[CodeRegionController] Failed to expand region ${region.id}:`, err);
                CodeRegionStore.setError(region.id, err.message || String(err));
                this.updatePlaceholderState(region);
                throw err;
            }
        },

        prepareRegionLinesContainer(region, options = {}) {
            const container = region.domContainer;
            if (!container) return;

            if (!region.headerEl) {
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
                    if (region.loading) return; // Prevent collapse race during active chunk loading
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
            } else if (options.reset) {
                region.linesEl.innerHTML = '';
                region.virtualTopSpacer = null;
                region.virtualContent = null;
                region.virtualBottomSpacer = null;
            }
            region.virtualized = (Number(region.lineCount) || 0) >= VIRTUAL_SCROLL_THRESHOLD;
            if (region.virtualized) this.ensureVirtualScaffold(region);
        },

        async appendChunkLines(region, chunkLines) {
            if (!region.linesEl || !chunkLines || !chunkLines.length) return;
            if (region.virtualized) {
                if (!region.virtualContent || !region.virtualContent.children.length) {
                    this.renderVirtualWindow(region);
                }
                await yieldToBrowser();
                return;
            }
            const fragment = document.createDocumentFragment();
            for (const line of chunkLines) {
                fragment.appendChild(CodeLineRenderer.renderCodeLine(line, this.filePath));
            }
            region.linesEl.appendChild(fragment);
            await yieldToBrowser();
        },

        async finalizeRegionLoaded(region) {
            if (region.currentState === 'collapsed-loaded' || region.currentState === 'collapsed-unloaded') {
                return;
            }
            if (region.placeholderEl) {
                region.placeholderEl.style.display = 'none';
            }
            CodeRegionStore.setExpanded(region.id);
            if (region.virtualized) this.renderVirtualWindow(region);
            updateReviewNavigation();
            updateHeaderStatistics();
        },

        collapseRegion(regionId, force = false) {
            const region = typeof regionId === 'string' ? CodeRegionStore.get(regionId) : regionId;
            if (!region || !region.domContainer) return;
            if (region.loading && !force) return; // Prevent manual collapse during chunk stream loading

            CodeRegionLoader.cancelInitialBatch();
            region.loadGeneration = (region.loadGeneration || 0) + 1;

            // Free DOM memory while preserving DraftStore edits
            if (region.linesEl) {
                region.linesEl.remove();
                region.linesEl = null;
            }
            if (region.headerEl) {
                region.headerEl.remove();
                region.headerEl = null;
            }
            region.virtualTopSpacer = null;
            region.virtualContent = null;
            region.virtualBottomSpacer = null;
            region.virtualRenderPending = false;

            CodeRegionStore.setCollapsed(region.id);

            // Re-display placeholder
            if (!region.placeholderEl) {
                region.placeholderEl = this.createPlaceholderElement(region);
            }
            region.placeholderEl.style.display = '';
            this.updatePlaceholderState(region);
            if (!region.placeholderEl.parentNode) {
                region.domContainer.appendChild(region.placeholderEl);
            }

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
            if (region.virtualized) {
                this.renderVirtualWindow(region);
            } else {
                await this.renderLinesInBatches(region.lines, region.linesEl);
                PerformanceTelemetry.recordDomLineCount(region.linesEl.children.length, false);
            }
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

        operationGeneration: 0,
        expandAllBtnEl: null,
        toolbarStatusEl: null,

        createTopToolbar() {
            const existing = document.querySelector('.coverage-lazy-toolbar');
            if (existing) existing.remove();

            const toolbar = document.createElement('div');
            toolbar.className = 'coverage-lazy-toolbar';
            toolbar.setAttribute('contenteditable', 'false');

            const statusSpan = document.createElement('span');
            statusSpan.className = 'coverage-lazy-toolbar-status';
            this.toolbarStatusEl = statusSpan;

            const expandAllBtn = document.createElement('button');
            expandAllBtn.type = 'button';
            expandAllBtn.className = 'coverage-lazy-toolbar-btn primary';
            expandAllBtn.innerText = '📖 展开全部';
            expandAllBtn.title = '分批逐步展开当前文件的全部代码行';
            this.expandAllBtnEl = expandAllBtn;

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
            this.operationGeneration = (this.operationGeneration || 0) + 1;
            const currentOpGen = this.operationGeneration;

            btn.disabled = true;
            btn.innerText = '展开中…';
            const allRegions = CodeRegionStore.getAll();
            const totalLines = CodeRegionStore._fileMeta.totalLines || 0;
            let loadedLinesCount = 0;
            let failedCount = 0;

            for (let i = 0; i < allRegions.length; i++) {
                if (this.operationGeneration !== currentOpGen) {
                    break;
                }
                const reg = allRegions[i];
                statusEl.innerText = `正在展开 ${loadedLinesCount} / ${totalLines} 行 (${i + 1}/${allRegions.length})…`;
                try {
                    if (reg.currentState === 'expanded-loaded' && reg.linesEl) {
                        loadedLinesCount += reg.lineCount;
                        continue;
                    }
                    await this.expandRegion(reg);
                    if (this.operationGeneration !== currentOpGen) {
                        break;
                    }
                    loadedLinesCount += reg.lineCount;
                } catch (err) {
                    console.error(`[CodeRegionController] Expand all failed on ${reg.id}:`, err);
                    failedCount++;
                }
                await yieldToBrowser();
            }

            if (this.operationGeneration === currentOpGen) {
                statusEl.innerText = '';
                btn.disabled = false;
                btn.innerText = '📖 展开全部';

                if (failedCount > 0) {
                    showToast(`已展开 ${allRegions.length - failedCount} 个区域，${failedCount} 个区域加载失败`);
                } else {
                    showToast('已全部展开');
                }
            }
        },

        restoreDefault() {
            this.operationGeneration = (this.operationGeneration || 0) + 1;

            if (this.expandAllBtnEl) {
                this.expandAllBtnEl.disabled = false;
                this.expandAllBtnEl.innerText = '📖 展开全部';
            }
            if (this.toolbarStatusEl) {
                this.toolbarStatusEl.innerText = '';
            }

            const allRegions = CodeRegionStore.getAll();
            allRegions.forEach(reg => {
                if (reg.defaultState === 'expanded') {
                    if (reg.currentState !== 'expanded-loaded') {
                        this.expandRegion(reg.id);
                    }
                } else {
                    this.collapseRegion(reg.id, true);
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
                } else if (region.virtualized) {
                    this.revealLine(region, lineNum);
                }
            } else if (region.placeholderEl) {
                if (region.placeholderEl.scrollIntoView) {
                    region.placeholderEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
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
        const metaScanId = document.querySelector('meta[name="coverage-scan-id"]');
        const metaRepositoryName = document.querySelector('meta[name="coverage-repository-name"]');
        currentScanId = (metaScanId && metaScanId.content)
            || URL_PARAMS.get('scan_id')
            || DEFAULT_SCAN_ID
            || '';
        currentRepositoryName = (metaRepositoryName && metaRepositoryName.content)
            || URL_PARAMS.get('repository_name')
            || DEFAULT_REPOSITORY_NAME
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
        CodeRegionController.filePath = filePath;

        console.log('[CoverageEnhance] Current file path:', filePath);
        console.log('[CoverageEnhance] Report ID:', currentReportId);
        console.log('[CoverageEnhance] Scan ID:', currentScanId);
        console.log('[CoverageEnhance] Repository:', currentRepositoryName);
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
        if (REPORT_MODE === 'VNEXT_ARTIFACT_READY') {
            createBatchToolbar(filePath);
        }
        CodeRegionController.installPanelDelegation(preSource);

        // Branch by ACTIVE_MODE
        if (REPORT_MODE === 'VNEXT_ARTIFACT_READY' && ACTIVE_MODE === 'lazy_collapse') {
            try {
                const query = codeDetailQuery(filePath);

                const layoutResp = await requestCoverageApi(`/code-layout?${query.toString()}`, { method: 'GET' });
                if (layoutResp && layoutResp.regions) {
                    await CodeRegionController.init(layoutResp, preSource);
                    return;
                }
            } catch (err) {
                console.warn('[CoverageEnhance] Failed to initialize via backend layout:', err.message);
                // The canonical lazy-collapse mode is VNext-only. Do not
                // silently switch to a legacy API when identity or the VNext
                // contract is unavailable.
                const errBanner = document.createElement('div');
                errBanner.className = 'coverage-region-placeholder error';
                const left = document.createElement('div');
                left.className = 'placeholder-left';
                const icon = document.createElement('span');
                icon.className = 'placeholder-icon';
                icon.textContent = '⚠️';
                const message = document.createElement('span');
                message.className = 'placeholder-text';
                message.textContent = '无法加载代码布局与源码数据：' + String(err.message || err);
                left.appendChild(icon);
                left.appendChild(message);
                errBanner.appendChild(left);
                preSource.innerHTML = '';
                preSource.appendChild(errBanner);
                return;
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

        function getSuggestedReviewer(item) {
            if (!item || !item.span) return '';
            return item.span.getAttribute('data-coverage-reviewer') ||
                (item.span.querySelector && item.span.querySelector('[data-coverage-reviewer]')
                    ? item.span.querySelector('[data-coverage-reviewer]').getAttribute('data-coverage-reviewer')
                    : '') || '';
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
            const startReviewer = getSuggestedReviewer(start);
            let consumedUntil = startIndex;

            for (let j = startIndex + 1; j < sourceLines.length; j++) {
                const next = sourceLines.get(j);
                if (!next) continue;
                if (isCoveredLine(next)) break;

                if (isUncoveredLine(next)) {
                    if (getSuggestedReviewer(next) !== startReviewer) break;
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
                block_type: 'single',
                suggested_reviewer: getSuggestedReviewer(startItem),
                reviewer: getSuggestedReviewer(startItem)
            };
            const panel = CodeLineRenderer.createReviewPanel(lineDto, filePath);
            startItem.span.appendChild(panel);
        });

        // Static legacy display modes remain usable offline, but optional
        // overlays use only the canonical paged VNext endpoint.
        if (REPORT_MODE === 'LEGACY_STATIC' || !currentScanId) {
            reviewControlsReady = true;
            updateReviewNavigation();
            updateHeaderStatistics();
            updateBatchToolbar();
            return;
        }
        const query = new URLSearchParams({
            project: DEFAULT_PROJECT,
            scan_id: currentScanId,
            file: filePath,
            repository_name: currentRepositoryName,
            page: '1',
            page_size: '200'
        });

        requestCoverageApi(`/progress/details?${query.toString()}`, { method: 'GET' })
            .then(data => {
                if (data && data.rows) {
                    const dbMap = new Map();
                    data.rows.forEach(rec => dbMap.set(rec.line_number, rec));
                    panelsMap.forEach((pState, sLine) => {
                        const rec = dbMap.get(sLine);
                        if (rec) {
                            const draft = ReviewDraftStore.getDraft(sLine);
                            if (draft && draft.isDirty) return;
                            const currentReviewer = getStoredPanelValue(pState, 'reviewerInput');
                            setStoredPanelValues(pState, {
                                status: rec.status || '未确认',
                                isDraft: rec.is_draft === true || rec.is_draft === 1,
                                reviewerInput: rec.reviewer || currentReviewer,
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

    if (typeof window !== 'undefined') {
        window.__COVERAGE_ENHANCE_INTERNALS__ = {
            ReviewDraftStore,
            CodeRegionStore,
            CodeRegionLoader,
            CodeLineRenderer,
            CodeRegionController,
            RegionLineLRUCache,
            PerformanceTelemetry
        };
    }
})();
