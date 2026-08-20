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
    var isRefreshing = false;
    var lastRefreshTime = 0;
    var urlParams = null;
    try {
        urlParams = new URLSearchParams(window.location.search || "");
    } catch (e) {}
    var scanId = urlParams ? (urlParams.get("scan_id") || "") : "";
    var repositoryName = urlParams ? (urlParams.get("repository_name") || "") : "";

    function getApiBaseCandidates() {
        return ["/api/coverage"];
    }
    function fileKey(repositoryName, filePath) {
        var repository = String(repositoryName || "").trim();
        var path = String(filePath || "").replace(/\\/g, "/").replace(/^\.\//, "");
        return repository ? repository + "::" + path : path;
    }
    function fetchUnanalyzedFromCandidates(candidates, index) {
        if (!candidates.length || index >= candidates.length) {
            return Promise.reject(new Error("Canonical API endpoint unavailable"));
        }
        var base = candidates[index];
        var url = base + "/incremental/unanalyzed?project=" + encodeURIComponent(projectName);
        if (scanId) url += "&scan_id=" + encodeURIComponent(scanId);
        if (repositoryName) url += "&repository_name=" + encodeURIComponent(repositoryName);
        return fetch(url).then(function(res) {
            if (!res.ok) throw new Error("HTTP " + res.status);
            resolvedApiBase = base;
            return res.json();
        });
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
        if (!projectName || isRefreshing) return;
        var now = Date.now();
        if (now - lastRefreshTime < 2000) return;
        isRefreshing = true;
        lastRefreshTime = now;

        var rawCandidates = resolvedApiBase ? [resolvedApiBase].concat(getApiBaseCandidates()) : getApiBaseCandidates();
        var candidates = [];
        for (var k = 0; k < rawCandidates.length; k++) {
            var candidateItem = rawCandidates[k];
            if (candidateItem && candidates.indexOf(candidateItem) === -1) {
                candidates.push(candidateItem);
            }
        }

        fetchUnanalyzedFromCandidates(candidates, 0)
            .then(function(resData) {
                isRefreshing = false;
                if (!resData || (resData.status && resData.status !== "success")) return;
                var payload = resData.data || resData;
                var files = payload.files || [];
                var unanalyzedMap = {};
                var pendingLineMap = {};
                for (var i = 0; i < files.length; i++) {
                    var key = fileKey(files[i].repository_name, files[i].file_path);
                    unanalyzedMap[key] = files[i].unanalyzed;
                    pendingLineMap[key] = parseLineNumbers(files[i].pending_line_numbers);
                    // Legacy responses did not carry repository identity;
                    // retain a path-only alias only for those responses.
                    if (!files[i].repository_name) {
                        unanalyzedMap[files[i].file_path] = files[i].unanalyzed;
                        pendingLineMap[files[i].file_path] = parseLineNumbers(files[i].pending_line_numbers);
                    }
                }

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

                        var count;
                        if (ownerSpecific && rowFileKey && Object.prototype.hasOwnProperty.call(pendingLineMap, rowFileKey)) {
                            // A file can belong to several developers.  The
                            // API returns current pending line numbers; only
                            // the intersection with this row's owned lines is
                            // the developer's live task count.
                            count = countOwnedPendingLines(row, pendingLineMap[rowFileKey]);
                        } else if (rowFileKey && (rowFileKey in unanalyzedMap)) {
                            count = unanalyzedMap[rowFileKey];
                        } else {
                            count = parseInt((unanalyzedCell && unanalyzedCell.textContent) ? unanalyzedCell.textContent.trim() : "0", 10);
                        }

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
                isRefreshing = false;
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
