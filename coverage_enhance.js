/**
 * 覆盖率 HTML 报告增强脚本 (ES6) - 基本块合并与控制流隔离终极版
 * 自动识别连续未覆盖代码段 (基本块)，只在首行渲染分析控件并显示范围徽章；
 * 强行将 C 语言控制流分支关键字所在的行 (if, else, for, while, do, switch, case, default) 进行物理隔离单行展示，确保科学细致的分析。
 */
(function() {
    const ENHANCE_VERSION = 'dop-lineNum-rowfix2-20260526';
    const SERVER_URL = '/api/coverage';
    const DEFAULT_PROJECT = 'Gemini-NOS';
    const STATUS_OPTIONS = ['未确认', '可覆盖', '无法覆盖'];

    // 控制流分支关键字侦测正则 (边界隔离)
    const CONTROL_FLOW_REGEX = /\b(if|else|for|while|do|switch|case|default)\b/;

    let totalUncovered = 0;       // 物理未覆盖代码行总数
    let blocks = [];              // 合并与隔离切分后的 Block 列表 (元素为 [{span, lineNum}])
    let panelsMap = new Map();     // startLine -> {select, reviewerInput, methodInput, reasonInput, saveBtn, block}

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
        function collectSourceLines(pre) {
            const modernLines = Array.from(pre.querySelectorAll('span[id^="L"]')).map(span => ({
                span,
                lineNum: parseInt(span.id.replace('L', ''), 10),
                legacyInline: false
            })).filter(item => !Number.isNaN(item.lineNum));

            if (modernLines.length > 0) {
                return modernLines;
            }

            function findSameLineCodeSpan(lineNumSpan) {
                let node = lineNumSpan.nextSibling;
                while (node) {
                    if (node.nodeType === Node.TEXT_NODE && node.nodeValue.includes('\n')) {
                        return null;
                    }
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        if (node.matches('.lineNum')) {
                            return null;
                        }
                        if (node.matches('.lineCov, .lineNoCov, .tlaGNC, .tlaUNC, .tlaBgGNC, .tlaBgUNC')) {
                            return node;
                        }
                    }
                    node = node.nextSibling;
                }
                return null;
            }

            return Array.from(pre.querySelectorAll('span.lineNum')).map((lineNumSpan, index) => {
                const lineNum = parseInt(lineNumSpan.innerText, 10);
                const codeSpan = findSameLineCodeSpan(lineNumSpan) || lineNumSpan;
                return {
                    span: codeSpan,
                    lineNumSpan,
                    lineNum: Number.isNaN(lineNum) ? index + 1 : lineNum,
                    legacyInline: true
                };
            });
        }

        const allLines = collectSourceLines(preSource);
        const isLegacyReport = allLines.some(item => item.legacyInline);
        let currentBlock = [];

        function flushCurrentBlock() {
            if (currentBlock.length > 0) {
                blocks.push(currentBlock);
                currentBlock = [];
            }
        }

        function isUncoveredLine(item) {
            return item.span.matches('.tlaUNC, .tlaBgUNC, .lineNoCov') || item.span.querySelector('.tlaUNC, .tlaBgUNC, .lineNoCov') !== null;
        }

        function isCoveredLine(item) {
            return item.span.matches('.tlaGNC, .tlaBgGNC, .lineCov') || item.span.querySelector('.tlaGNC, .tlaBgGNC, .lineCov') !== null;
        }

        function getLineText(item) {
            return item.span.innerText || '';
        }

        function isControlFlowLine(item) {
            return CONTROL_FLOW_REGEX.test(getLineText(item));
        }

        if (isLegacyReport) {
            for (let i = 0; i < allLines.length; i++) {
                const item = allLines[i];
                if (!isUncoveredLine(item)) {
                    continue;
                }

                totalUncovered++;
                if (!isControlFlowLine(item)) {
                    blocks.push([item]);
                    continue;
                }

                const block = [item];
                let consumedUntil = i;
                for (let j = i + 1; j < allLines.length; j++) {
                    const next = allLines[j];
                    if (isCoveredLine(next)) {
                        break;
                    }
                    if (!isUncoveredLine(next)) {
                        const nextText = getLineText(next);
                        if (/^\s*(\{|\})?\s*$/.test(nextText) || /[\{\}]/.test(nextText)) {
                            continue;
                        }
                        break;
                    }
                    if (isControlFlowLine(next)) {
                        break;
                    }
                    block.push(next);
                    totalUncovered++;
                    consumedUntil = j;
                }
                blocks.push(block);
                i = consumedUntil;
            }
        } else {
        allLines.forEach(item => {
            const { span, lineNum, legacyInline } = item;
            const isUncovered = isUncoveredLine(item);
            const isCovered = isCoveredLine(item);

            if (isUncovered) {
                totalUncovered++;
                if (legacyInline) {
                    flushCurrentBlock();
                    blocks.push([{ span, lineNum, legacyInline }]);
                    return;
                }

                const lineText = span.innerText || '';
                const hasControlFlow = CONTROL_FLOW_REGEX.test(lineText);
                if (hasControlFlow) {
                    flushCurrentBlock();
                    blocks.push([{ span, lineNum, legacyInline }]);
                } else {
                    currentBlock.push({ span, lineNum, legacyInline });
                }
                return;
            }

            if (isCovered) {
                flushCurrentBlock();
                return;
            }

            const lineText = span.innerText || '';
            if (/[\{\}]/.test(lineText)) {
                flushCurrentBlock();
            }
        });
        }

        // 最后的结算
        if (currentBlock.length > 0) {
            blocks.push(currentBlock);
        }

        if (totalUncovered === 0) {
            console.log('[CoverageEnhance] No uncovered lines found.');
            return;
        }

        console.log(`[CoverageEnhance] Total uncovered lines: ${totalUncovered}. Consolidated into ${blocks.length} block(s).`);

        // 3. 构建并注入表单 DOM，仅在 Block 的第一行注入
        blocks.forEach(block => {
            const startLineItem = block[0];
            const startLineNum = startLineItem.lineNum;
            const endLineNum = block[block.length - 1].lineNum;
            const isLegacyInline = startLineItem.legacyInline === true;
            const isMultiLine = !isLegacyInline && block.length > 1;

            // 动态为当前 Block 的所有物理行 span 设置 padding-right，以防止其内容与悬浮的表单面板重叠
            block.forEach(item => {
                if (!isLegacyInline) {
                    item.span.style.setProperty('padding-right', '550px', 'important');
                }
            });

            const panel = document.createElement('span');
            panel.className = 'coverage-analysis-panel' + (isMultiLine ? ' multiline' : '') + (isLegacyInline ? ' legacy-overlay' : '');
            panel.setAttribute('contenteditable', 'false');

            // 提取前缀及实际 C 代码列数信息的辅助函数
            function getLineCodeInfo(span) {
                const lineText = span.innerText || '';
                const colonIndex = lineText.indexOf(':');
                if (colonIndex === -1) {
                    return { prefixLen: 0, codeLen: lineText.length };
                }
                const prefixLen = colonIndex + 2; // 包含冒号以及冒号后面的一个空格
                const codeText = lineText.substring(prefixLen);
                return { prefixLen, codeLen: codeText.length };
            }

            // 扫描计算当前 Block 范围内的最大代码列数以及前缀列数
            let maxCodeLen = 0;
            let commonPrefixLen = 0;
            block.forEach(item => {
                const info = getLineCodeInfo(item.span);
                if (info.codeLen > maxCodeLen) {
                    maxCodeLen = info.codeLen;
                }
                if (info.prefixLen > 0) {
                    commonPrefixLen = info.prefixLen;
                }
            });

            // 核心对齐对位算法：默认从 121 列开始对齐；若最大代码列数超过了 120 列，则自动从最大代码行结束位置 +2 字符间距开始
            const targetCodeCol = maxCodeLen <= 120 ? 121 : (maxCodeLen + 2);
            const absoluteCol = commonPrefixLen + targetCodeCol;

            // 完美等宽字体对位
            if (!isLegacyInline) {
                panel.style.left = `${absoluteCol}ch`;
            }

            if (isMultiLine) {
                // 完美契合 Block 的多行物理高度 (每行 24px)
                panel.style.height = `${block.length * 24 - 4}px`;
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

            // 条件覆盖方法 (多行时使用 textarea，单行时使用 input)
            const methodInput = document.createElement(isMultiLine ? 'textarea' : 'input');
            if (!isMultiLine) {
                methodInput.type = 'text';
            }
            methodInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            methodInput.placeholder = '条件覆盖方法';

            // 无条件覆盖原因 (多行时使用 textarea，单行时使用 input)
            const reasonInput = document.createElement(isMultiLine ? 'textarea' : 'input');
            if (!isMultiLine) {
                reasonInput.type = 'text';
            }
            reasonInput.className = 'coverage-analysis-input' + (isMultiLine ? ' multiline' : '');
            reasonInput.placeholder = '无条件覆盖原因';

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

            // 组合面板
            panel.appendChild(select);
            panel.appendChild(reviewerInput);
            panel.appendChild(methodInput);
            panel.appendChild(reasonInput);
            if (badgeSpan) {
                panel.appendChild(badgeSpan);
            }
            panel.appendChild(inheritBtn);
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

            // 追加控件。旧 genhtml 的源码区是 <pre> 文本流，不能把控件插入其中，否则会破坏原始排版。
            if (isLegacyInline) {
                panel.style.setProperty('visibility', 'hidden', 'important');
                document.body.appendChild(panel);
                requestAnimationFrame(positionLegacyPanel);
                window.addEventListener('load', positionLegacyPanel);
                window.addEventListener('resize', positionLegacyPanel);
            } else {
                startLineItem.span.appendChild(panel);
            }

            // 存储该 Block 面板的映射，key 设为首行行号
            panelsMap.set(startLineNum, {
                select,
                reviewerInput,
                methodInput,
                reasonInput,
                saveBtn,
                block
            });

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

                select.value = previous.select.value;
                reviewerInput.value = previous.reviewerInput.value;
                methodInput.value = previous.methodInput.value;
                reasonInput.value = previous.reasonInput.value;
                saveBtn.innerText = 'Save';
                saveBtn.className = 'coverage-analysis-btn';
            });

            select.addEventListener('change', function() {
                if (saveBtn.className.includes('saved')) {
                    saveBtn.innerText = 'Save';
                    saveBtn.className = 'coverage-analysis-btn';
                }
            });
        });

        // 4. 异步拉取并回显已有数据
        fetchLineAnalysis(filePath);
    });

    function findPreviousFilledPanel(currentLineNum) {
        const candidates = Array.from(panelsMap.entries())
            .filter(([lineNum, panel]) => lineNum < currentLineNum && panel.select.value !== '未确认')
            .sort((a, b) => b[0] - a[0]);

        for (const [, panel] of candidates) {
            const hasContent = panel.reviewerInput.value.trim() ||
                panel.methodInput.value.trim() ||
                panel.reasonInput.value.trim();
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
            const status = panel.select.value;
            if (status === '可覆盖' || status === '无法覆盖') {
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
    function fetchLineAnalysis(filePath) {
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
                    if (rec) {
                        panel.select.value = rec.status || '未确认';
                        panel.reviewerInput.value = rec.reviewer || '';
                        panel.methodInput.value = rec.coverage_method || '';
                        panel.reasonInput.value = rec.uncovered_reason || '';
                        
                        panel.saveBtn.innerText = 'Saved';
                        panel.saveBtn.classList.add('saved');
                    }
                });
            }
            updateHeaderStatistics();
        })
        .catch(err => {
            console.warn('[CoverageEnhance] Failed to fetch existing records:', err.message);
            panelsMap.forEach(panel => {
                panel.saveBtn.innerText = 'Offline';
                panel.saveBtn.classList.add('error');
                panel.saveBtn.title = '无法连接到本地持久化服务，请检查 enhance_coverage.py 服务是否在运行。';
            });
            updateHeaderStatistics();
        });
    }

    /**
     * 批量并发持久化该 Block 包含的所有代码行
     */
    function saveBlockLineAnalysis(filePath, block, reviewer, status, method, reason, btn) {
        btn.innerText = 'Saving...';
        btn.className = 'coverage-analysis-btn saving';

        const requests = block.map(item => {
            const payload = {
                project_name: DEFAULT_PROJECT,
                file_path: filePath,
                line_number: item.lineNum,
                reviewer: reviewer,
                status: status,
                coverage_method: method,
                uncovered_reason: reason
            };

            return fetch(SERVER_URL, {
                method: 'POST',
                mode: 'cors',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            }).then(response => {
                if (!response.ok) {
                    throw new Error(`Line ${item.lineNum} save failed with HTTP status: ${response.status}`);
                }
                return response.json();
            });
        });

        Promise.all(requests)
        .then(results => {
            const allSuccess = results.every(res => res.status === 'success');
            if (allSuccess) {
                btn.innerText = 'Saved';
                btn.className = 'coverage-analysis-btn saved';
                console.log(`[CoverageEnhance] Successfully saved block range L${block[0].lineNum}-${block[block.length - 1].lineNum}`);
                
                updateHeaderStatistics();
                
                setTimeout(() => {
                    if (btn.className.includes('saved')) {
                        btn.innerText = 'Save';
                    }
                }, 2000);
            } else {
                const failMsg = results.find(res => res.status !== 'success')?.message || 'Unknown server error';
                throw new Error(failMsg);
            }
        })
        .catch(err => {
            console.error('[CoverageEnhance] Batch save failed:', err);
            btn.innerText = 'Error';
            btn.className = 'coverage-analysis-btn error';
            btn.title = `保存失败: ${err.message}. 请重试。`;
            
            setTimeout(() => {
                btn.innerText = 'Save';
                btn.className = 'coverage-analysis-btn';
            }, 3000);
        });
    }
})();
