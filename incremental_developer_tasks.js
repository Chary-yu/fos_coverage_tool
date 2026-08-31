(function() {
    "use strict";

    var mainEl = document.querySelector("main[data-project]");
    var projectName = mainEl ? mainEl.getAttribute("data-project") : "";
    if (!projectName && window.location && window.location.search) {
        try {
            var params = new URLSearchParams(window.location.search);
            projectName = params.get("project") || "";
        } catch (e) {}
    }

    var resolvedApiBase = "";
    var lastRefreshTime = 0;
    var refreshGeneration = 0;
    var urlParams = null;
    try {
        urlParams = new URLSearchParams(window.location.search || "");
    } catch (e) {}
    var scanId = urlParams ? (urlParams.get("scan_id") || "") : "";
    var repositoryName = urlParams ? (urlParams.get("repository_name") || "") : "";

    function getApiBaseCandidates() {
        return ["/api/coverage"];
    }
    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function parseLineNumbers(value) {
        if (Array.isArray(value)) {
            return value.map(function(item) { return parseInt(item, 10); })
                .filter(function(item) { return Number.isFinite(item) && item > 0; });
        }
        return String(value || "").split(",")
            .map(function(item) { return parseInt(item.trim(), 10); })
            .filter(function(item) { return Number.isFinite(item) && item > 0; });
    }

    function countOwnedPendingLines(row, pendingLines) {
        var ownedLines = parseLineNumbers(row.getAttribute("data-owned-lines"));
        if (!ownedLines.length || !Array.isArray(pendingLines)) return 0;
        var pendingSet = new Set(pendingLines);
        var count = 0;
        for (var i = 0; i < ownedLines.length; i++) {
            if (pendingSet.has(ownedLines[i])) count += 1;
        }
        return count;
    }

    function refreshDeveloperTasks() {
        if (!projectName) return;
        var now = Date.now();
        if (now - lastRefreshTime < 2000) return;
        lastRefreshTime = now;
        var generation = ++refreshGeneration;

        var rawCandidates = resolvedApiBase ? [resolvedApiBase].concat(getApiBaseCandidates()) : getApiBaseCandidates();
        var candidates = [];
        for (var k = 0; k < rawCandidates.length; k++) {
            var candidateItem = rawCandidates[k];
            if (candidateItem && candidates.indexOf(candidateItem) === -1) {
                candidates.push(candidateItem);
            }
        }

        var requests = candidates.map(function(base) {
            return function() {
                resolvedApiBase = base;
                return window.CoveragePendingSnapshot.fetchComplete({
                    apiBase: base, projectName: projectName, scanId: scanId,
                    repositoryName: repositoryName, pageSize: 200,
                    requestToken: function() { return generation === refreshGeneration; }
                });
            };
        });
        function tryCandidate(index) {
            if (index >= requests.length) return Promise.reject(new Error("Canonical API endpoint unavailable"));
            return requests[index]().catch(function() { return tryCandidate(index + 1); });
        }
        tryCandidate(0)
            .then(function(snapshot) {
                if (generation !== refreshGeneration) return;
                var unanalyzedMap = snapshot.map || {};
                var pendingLineMap = snapshot.pendingLineMap || {};

                var devSections = document.querySelectorAll("section[id]");
                for (var s = 0; s < devSections.length; s++) {
                    var section = devSections[s];
                    var sectionId = section.id;
                    var fileRows = section.querySelectorAll("tbody tr[data-file-key]");
                    var devReviewFiles = 0;
                    var devTotalUncovered = 0;

                    for (var r = 0; r < fileRows.length; r++) {
                        var row = fileRows[r];
                        var rowFileKey = row.getAttribute("data-file-key");
                        var pageLink = row.getAttribute("data-page-link");
                        var changed = parseInt(row.getAttribute("data-changed") || "0", 10);
                        var ownerSpecific = row.getAttribute("data-owner-specific") === "true";
                        var unanalyzedCell = row.querySelector(".js-task-unanalyzed");
                        var actionCell = row.querySelector(".js-task-action");

                        var count = 0;
                        if (ownerSpecific && rowFileKey && Object.prototype.hasOwnProperty.call(pendingLineMap, rowFileKey)) {
                            // A file can belong to several developers.  The
                            // API returns current pending line numbers; only
                            // the intersection with this row's owned lines is
                            // the developer's live task count.
                            count = countOwnedPendingLines(row, pendingLineMap[rowFileKey]);
                        } else if (rowFileKey && Object.prototype.hasOwnProperty.call(unanalyzedMap, rowFileKey)) {
                            count = Number(unanalyzedMap[rowFileKey].unanalyzed);
                        }
                        if (!Number.isFinite(count)) count = 0;

                        if (unanalyzedCell) {
                            unanalyzedCell.textContent = count;
                            unanalyzedCell.setAttribute("data-sort-value", count);
                        }

                        if (actionCell) {
                            if (count > 0) {
                                if (pageLink) {
                                    actionCell.innerHTML = '<a class="fill-link" href="' + escapeHtml(pageLink) + '">填写 ' + count + ' 行</a>';
                                } else {
                                    actionCell.innerHTML = '<span class="todo">待填写 ' + count + ' 行（未找到源码页）</span>';
                                }
                            } else {
                                if (changed > 0) {
                                    actionCell.innerHTML = '<span class="done">已全部填写完成</span>';
                                } else {
                                    actionCell.innerHTML = '<span class="muted">本次提交未产生新增代码行</span>';
                                }
                            }
                        }

                        if (count > 0) {
                            devReviewFiles += 1;
                        }
                        devTotalUncovered += count;
                    }

                    // Update section statistics pills
                    var reviewFilesEl = section.querySelector(".js-dev-review-files");
                    if (reviewFilesEl) {
                        reviewFilesEl.textContent = devReviewFiles;
                    }
                    var uncoveredLinesEl = section.querySelector(".js-dev-uncovered-lines");
                    if (uncoveredLinesEl) {
                        uncoveredLinesEl.textContent = devTotalUncovered;
                    }
                    var warnPillEl = section.querySelector(".js-dev-uncovered-pill");
                    if (warnPillEl) {
                        if (devTotalUncovered === 0) {
                            warnPillEl.classList.remove("warn");
                            warnPillEl.classList.add("done-pill");
                        } else {
                            warnPillEl.classList.remove("done-pill");
                            warnPillEl.classList.add("warn");
                        }
                    }

                    // Update summary overview row
                    var summaryRow = document.querySelector('tr[data-dev-anchor="' + sectionId + '"]');
                    if (summaryRow) {
                        var sumReviewFiles = summaryRow.querySelector(".js-summary-review-files");
                        if (sumReviewFiles) {
                            sumReviewFiles.textContent = devReviewFiles;
                        }
                        var sumUncoveredLines = summaryRow.querySelector(".js-summary-uncovered-lines");
                        if (sumUncoveredLines) {
                            sumUncoveredLines.textContent = devTotalUncovered;
                        }
                    }
                }
            })
            .catch(function(err) {
                if (generation !== refreshGeneration) return;
                var failedSections = document.querySelectorAll("section[id]");
                for (var fs = 0; fs < failedSections.length; fs++) {
                    var failedRows = failedSections[fs].querySelectorAll("tbody tr[data-file-key]");
                    for (var fr = 0; fr < failedRows.length; fr++) {
                        var failedCell = failedRows[fr].querySelector(".js-task-unanalyzed");
                        if (failedCell) {
                            // Do not retain the generated HTML value after a
                            // failed snapshot.  A complete response is needed
                            // before the task count is trustworthy.
                            failedCell.textContent = "--";
                            failedCell.setAttribute("data-sort-value", "");
                        }
                        var failedAction = failedRows[fr].querySelector(".js-task-action");
                        if (failedAction) {
                            failedAction.innerHTML = '<span class="todo">待分析快照暂不可用</span>';
                        }
                    }
                    var failedReviewFiles = failedSections[fs].querySelector(".js-dev-review-files");
                    var failedUncovered = failedSections[fs].querySelector(".js-dev-uncovered-lines");
                    if (failedReviewFiles) failedReviewFiles.textContent = "--";
                    if (failedUncovered) failedUncovered.textContent = "--";
                }
                var failedSummaryRows = document.querySelectorAll("tr[data-dev-anchor]");
                for (var sr = 0; sr < failedSummaryRows.length; sr++) {
                    var summaryReviewFiles = failedSummaryRows[sr].querySelector(".js-summary-review-files");
                    var summaryUncovered = failedSummaryRows[sr].querySelector(".js-summary-uncovered-lines");
                    if (summaryReviewFiles) summaryReviewFiles.textContent = "--";
                    if (summaryUncovered) summaryUncovered.textContent = "--";
                }
                console.warn("[Developer Tasks Refresh] API failed:", err);
            });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function() {
            refreshDeveloperTasks();
        });
    } else {
        refreshDeveloperTasks();
    }

    if (window.addEventListener) {
        window.addEventListener("pageshow", function() { refreshDeveloperTasks(); });
        window.addEventListener("focus", function() { refreshDeveloperTasks(); });
    }
    if (document.addEventListener) {
        document.addEventListener("visibilitychange", function() {
            if (document.visibilityState === "visible") {
                refreshDeveloperTasks();
            }
        });
    }
})();
