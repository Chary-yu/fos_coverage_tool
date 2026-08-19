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

    function getApiBaseCandidates() {
        var candidates = [];
        if (window.location && window.location.search) {
            try {
                var p = new URLSearchParams(window.location.search);
                var explicit = p.get("api");
                if (explicit) candidates.push(explicit.replace(/\/+$/, ""));
            } catch (e) {}
        }
        var origin = (window.location && window.location.origin && window.location.origin !== "null") ? window.location.origin : "";
        if (origin) {
            candidates.push(origin + "/api/coverage");
            if (window.location.pathname && window.location.pathname.indexOf("/coverage/") === 0) {
                candidates.push(origin + "/coverage/api/coverage");
            }
            if (window.location.hostname && window.location.port !== "9528") {
                candidates.push(window.location.protocol + "//" + window.location.hostname + ":9528/api/coverage");
            }
        }
        candidates.push("http://127.0.0.1:9528/api/coverage");
        candidates.push("/api/coverage");
        var result = [];
        for (var c = 0; c < candidates.length; c++) {
            var item = (candidates[c] || "").replace(/\/+$/, "");
            if (item && result.indexOf(item) === -1) {
                result.push(item);
            }
        }
        return result;
    }

    function fetchUnanalyzedFromCandidates(candidates, index) {
        if (index >= candidates.length) {
            return Promise.reject(new Error("All API endpoints unavailable"));
        }
        var base = candidates[index];
        var url = base + "/incremental/unanalyzed?project=" + encodeURIComponent(projectName);
        return fetch(url).then(function(res) {
            if (!res.ok) throw new Error("HTTP " + res.status);
            resolvedApiBase = base;
            return res.json();
        }).catch(function(err) {
            return fetchUnanalyzedFromCandidates(candidates, index + 1);
        });
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
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
                if (!resData || resData.status !== "success" || !resData.data) return;
                var files = resData.data.files || [];
                var unanalyzedMap = {};
                for (var i = 0; i < files.length; i++) {
                    unanalyzedMap[files[i].file_path] = files[i].unanalyzed;
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
                        var fileKey = row.getAttribute("data-file-key");
                        var pageLink = row.getAttribute("data-page-link");
                        var changed = parseInt(row.getAttribute("data-changed") || "0", 10);
                        var unanalyzedCell = row.querySelector(".js-task-unanalyzed");
                        var actionCell = row.querySelector(".js-task-action");

                        var count = (fileKey && (fileKey in unanalyzedMap))
                            ? unanalyzedMap[fileKey]
                            : parseInt((unanalyzedCell && unanalyzedCell.textContent) ? unanalyzedCell.textContent.trim() : "0", 10);

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
