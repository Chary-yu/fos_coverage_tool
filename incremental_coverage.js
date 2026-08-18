(function() {
    var table = document.getElementById("incremental-file-table");
    if (!table || !table.tBodies.length) return;
    var body = table.tBodies[0];

    var repoFilter = document.getElementById("repo-filter");
    var moduleFilter = document.getElementById("module-filter");
    var teamFilter = document.getElementById("team-filter");
    var leaderFilter = document.getElementById("leader-filter");
    var fileSearch = document.getElementById("file-search");
    var resetBtn = document.getElementById("reset-filters-btn");
    var filterCount = document.getElementById("filter-count");

    var repos = [];
    var modules = [];
    var teams = [];
    var leaders = [];

    function addUnique(arr, val) {
        val = (val || "").trim();
        if (val && arr.indexOf(val) === -1) {
            arr.push(val);
        }
    }

    var allRows = Array.prototype.slice.call(body.rows);
    for (var i = 0; i < allRows.length; i++) {
        var r = allRows[i];
        addUnique(repos, r.getAttribute("data-repo"));
        addUnique(modules, r.getAttribute("data-module"));
        addUnique(teams, r.getAttribute("data-team"));
        addUnique(leaders, r.getAttribute("data-leader"));
    }

    function populateSelect(selectEl, list) {
        if (!selectEl) return;
        list.sort(function(a, b) { return a.localeCompare(b, "zh-CN"); });
        for (var j = 0; j < list.length; j++) {
            var opt = document.createElement("option");
            opt.value = list[j];
            opt.textContent = list[j];
            selectEl.appendChild(opt);
        }
    }

    populateSelect(repoFilter, repos);
    populateSelect(moduleFilter, modules);
    populateSelect(teamFilter, teams);
    populateSelect(leaderFilter, leaders);

    if (window.location && window.location.search) {
        try {
            var params = new URLSearchParams(window.location.search);
            var qRepo = params.get("repo");
            var qModule = params.get("module");
            var qTeam = params.get("team");
            var qLeader = params.get("leader");

            if (qRepo && repoFilter) repoFilter.value = qRepo;
            if (qModule && moduleFilter) moduleFilter.value = qModule;
            if (qTeam && teamFilter) teamFilter.value = qTeam;
            if (qLeader && leaderFilter) leaderFilter.value = qLeader;
        } catch (e) {}
    }

    var keyToColumn = {
        repository: 0,
        team: 1,
        leader: 2,
        module: 3,
        ownership: 1,
        file: 4,
        changed: 5,
        covered: 6,
        uncovered: 7,
        ignored: 8,
        missing: 9,
        unanalyzed: 9
    };

    var currentKey = "uncovered";
    var currentDirection = -1;

    function updateSelectOptions(selectEl, validList, currentVal) {
        if (!selectEl) return;
        validList.sort(function(a, b) { return a.localeCompare(b, "zh-CN"); });

        var existingOptions = [];
        for (var k = 1; k < selectEl.options.length; k++) {
            existingOptions.push(selectEl.options[k].value);
        }

        var isSameOptions = existingOptions.length === validList.length;
        if (isSameOptions) {
            for (var m = 0; m < validList.length; m++) {
                if (existingOptions[m] !== validList[m]) {
                    isSameOptions = false;
                    break;
                }
            }
        }

        if (!isSameOptions) {
            while (selectEl.options.length > 1) {
                selectEl.remove(1);
            }

            for (var j = 0; j < validList.length; j++) {
                var val = validList[j];
                var opt = document.createElement("option");
                opt.value = val;
                opt.textContent = val;
                selectEl.appendChild(opt);
            }
        }

        if (currentVal && validList.indexOf(currentVal) !== -1) {
            selectEl.value = currentVal;
        } else {
            selectEl.value = "";
        }
    }

    function updateFilterDropdowns() {
        var validRepos = [];
        for (var i = 0; i < allRows.length; i++) {
            var rRepo = allRows[i].getAttribute("data-repo") || "";
            addUnique(validRepos, rRepo);
        }
        var curRepo = repoFilter ? repoFilter.value : "";
        updateSelectOptions(repoFilter, validRepos, curRepo);
        var activeRepo = repoFilter ? repoFilter.value : "";

        var validTeams = [];
        for (var i = 0; i < allRows.length; i++) {
            var rRepo = allRows[i].getAttribute("data-repo") || "";
            var rTeam = allRows[i].getAttribute("data-team") || "";
            var mRepo = !activeRepo || rRepo === activeRepo;
            if (mRepo) {
                addUnique(validTeams, rTeam);
            }
        }
        var curTeam = teamFilter ? teamFilter.value : "";
        updateSelectOptions(teamFilter, validTeams, curTeam);
        var activeTeam = teamFilter ? teamFilter.value : "";

        var validLeaders = [];
        for (var i = 0; i < allRows.length; i++) {
            var rRepo = allRows[i].getAttribute("data-repo") || "";
            var rTeam = allRows[i].getAttribute("data-team") || "";
            var rLeader = allRows[i].getAttribute("data-leader") || "";
            var mRepo = !activeRepo || rRepo === activeRepo;
            var mTeam = !activeTeam || rTeam === activeTeam;
            if (mRepo && mTeam) {
                addUnique(validLeaders, rLeader);
            }
        }
        var curLeader = leaderFilter ? leaderFilter.value : "";
        updateSelectOptions(leaderFilter, validLeaders, curLeader);
        var activeLeader = leaderFilter ? leaderFilter.value : "";

        var validModules = [];
        for (var i = 0; i < allRows.length; i++) {
            var rRepo = allRows[i].getAttribute("data-repo") || "";
            var rTeam = allRows[i].getAttribute("data-team") || "";
            var rLeader = allRows[i].getAttribute("data-leader") || "";
            var rModule = allRows[i].getAttribute("data-module") || "";
            var mRepo = !activeRepo || rRepo === activeRepo;
            var mTeam = !activeTeam || rTeam === activeTeam;
            var mLeader = !activeLeader || rLeader === activeLeader;
            if (mRepo && mTeam && mLeader) {
                addUnique(validModules, rModule);
            }
        }
        var curModule = moduleFilter ? moduleFilter.value : "";
        updateSelectOptions(moduleFilter, validModules, curModule);
    }

    function applyFilters() {
        updateFilterDropdowns();

        var selRepo = repoFilter ? repoFilter.value : "";
        var selTeam = teamFilter ? teamFilter.value : "";
        var selLeader = leaderFilter ? leaderFilter.value : "";
        var selModule = moduleFilter ? moduleFilter.value : "";
        var kw = fileSearch ? fileSearch.value.trim().toLowerCase() : "";

        var total = allRows.length;
        var visible = 0;

        for (var i = 0; i < total; i++) {
            var row = allRows[i];
            var rRepo = row.getAttribute("data-repo") || "";
            var rTeam = row.getAttribute("data-team") || "";
            var rLeader = row.getAttribute("data-leader") || "";
            var rModule = row.getAttribute("data-module") || "";
            var rOwnership = row.getAttribute("data-ownership") || "";
            var rFile = (row.cells[4] ? row.cells[4].getAttribute("data-sort-value") || row.cells[4].textContent || "" : "");
            var fullSearchText = (rRepo + " " + rModule + " " + rTeam + " " + rLeader + " " + rOwnership + " " + rFile).toLowerCase();

            var mRepo = !selRepo || rRepo === selRepo;
            var mTeam = !selTeam || rTeam === selTeam;
            var mLeader = !selLeader || rLeader === selLeader;
            var mModule = !selModule || rModule === selModule;
            var mKw = !kw || fullSearchText.indexOf(kw) !== -1;

            if (mRepo && mTeam && mLeader && mModule && mKw) {
                row.style.display = "";
                visible++;
            } else {
                row.style.display = "none";
            }
        }

        if (filterCount) {
            if (selRepo || selTeam || selLeader || selModule || kw) {
                filterCount.textContent = "已筛选出 " + visible + " / " + total + " 个文件";
            } else {
                filterCount.textContent = "共 " + total + " 个文件";
            }
        }
    }

    var searchTimer = null;
    function debouncedApplyFilters() {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(function() {
            applyFilters();
        }, 120);
    }

    function updateIndicators() {
        var buttons = table.querySelectorAll(".sort-button");
        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            var key = btn.getAttribute("data-sort-key");
            var active = (key === currentKey);
            var indicator = btn.querySelector(".sort-indicator");
            if (indicator) {
                indicator.textContent = active ? (currentDirection < 0 ? "↓" : "↑") : "↕";
            }
            if (btn.parentNode && btn.parentNode.tagName === "TH") {
                btn.parentNode.setAttribute("aria-sort", active ? (currentDirection < 0 ? "descending" : "ascending") : "none");
            }
        }
    }

    function sortRows(key, direction) {
        var column = keyToColumn[key];
        if (column === undefined) return;

        currentKey = key;
        currentDirection = direction;

        allRows.sort(function(left, right) {
            var leftCell = left.cells[column];
            var rightCell = right.cells[column];
            var leftValue = leftCell ? (leftCell.getAttribute("data-sort-value") || leftCell.textContent.trim()) : "";
            var rightValue = rightCell ? (rightCell.getAttribute("data-sort-value") || rightCell.textContent.trim()) : "";
            var btn = table.querySelector('.sort-button[data-sort-key="' + key + '"]');
            var numeric = btn ? (btn.getAttribute("data-sort-type") === "number") : false;

            var comparison = 0;
            if (numeric) {
                var nLeft = parseFloat(leftValue);
                if (isNaN(nLeft)) nLeft = 0;
                var nRight = parseFloat(rightValue);
                if (isNaN(nRight)) nRight = 0;
                comparison = nLeft - nRight;
            } else {
                comparison = leftValue.localeCompare(rightValue, "zh-CN");
            }
            return comparison !== 0 ? comparison * direction : 0;
        });

        for (var i = 0; i < allRows.length; i++) {
            body.appendChild(allRows[i]);
        }

        updateIndicators();
        applyFilters();
    }

    if (repoFilter) repoFilter.addEventListener("change", applyFilters);
    if (moduleFilter) moduleFilter.addEventListener("change", applyFilters);
    if (teamFilter) teamFilter.addEventListener("change", applyFilters);
    if (leaderFilter) leaderFilter.addEventListener("change", applyFilters);
    if (fileSearch) fileSearch.addEventListener("input", debouncedApplyFilters);
    if (resetBtn) {
        resetBtn.addEventListener("click", function() {
            if (repoFilter) repoFilter.value = "";
            if (moduleFilter) moduleFilter.value = "";
            if (teamFilter) teamFilter.value = "";
            if (leaderFilter) leaderFilter.value = "";
            if (fileSearch) fileSearch.value = "";
            if (window.history && window.history.replaceState) {
                var cleanUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
                window.history.replaceState({ path: cleanUrl }, "", cleanUrl);
            }
            applyFilters();
        });
    }

    var thead = table.querySelector("thead");
    if (thead) {
        thead.addEventListener("click", function(e) {
            var el = e.target;
            while (el && el !== thead) {
                if (el.classList && el.classList.contains("sort-button")) {
                    var key = el.getAttribute("data-sort-key");
                    var type = el.getAttribute("data-sort-type");
                    var direction = (key === currentKey) ? -currentDirection : (type === "number" ? -1 : 1);
                    sortRows(key, direction);
                    break;
                }
                if (el.tagName === "TH" && el.getAttribute("data-sort-key")) {
                    var btn = el.querySelector(".sort-button");
                    if (btn) {
                        var key = btn.getAttribute("data-sort-key");
                        var type = btn.getAttribute("data-sort-type");
                        var direction = (key === currentKey) ? -currentDirection : (type === "number" ? -1 : 1);
                        sortRows(key, direction);
                    }
                    break;
                }
                el = el.parentElement;
            }
        });
    }

    var mainEl = document.querySelector("main[data-project]");
    var projectName = mainEl ? mainEl.getAttribute("data-project") : "";
    if (!projectName && window.location && window.location.search) {
        try {
            var params = new URLSearchParams(window.location.search);
            projectName = params.get("project") || "";
        } catch (e) {}
    }

    var isRefreshing = false;
    var lastRefreshTime = 0;

    function refreshUnanalyzedCounts() {
        if (!projectName || isRefreshing) return;
        var now = Date.now();
        if (now - lastRefreshTime < 2000) return;
        isRefreshing = true;
        lastRefreshTime = now;

        var url = "/api/coverage/incremental/unanalyzed?project=" + encodeURIComponent(projectName);
        fetch(url)
            .then(function(res) {
                if (!res.ok) throw new Error("HTTP " + res.status);
                return res.json();
            })
            .then(function(resData) {
                isRefreshing = false;
                if (!resData || resData.status !== "success" || !resData.data) return;
                var files = resData.data.files || [];
                var map = {};
                for (var i = 0; i < files.length; i++) {
                    map[files[i].file_path] = files[i].unanalyzed;
                }

                var total = 0;
                var rows = body.rows;
                for (var j = 0; j < rows.length; j++) {
                    var row = rows[j];
                    var key = row.getAttribute("data-file-key");
                    var cell = row.querySelector(".js-unanalyzed-count") || row.cells[9];
                    if (cell) {
                        var count = (key && key in map) ? map[key] : parseInt(cell.getAttribute("data-sort-value") || cell.textContent.trim() || "0", 10);
                        if (key && key in map) {
                            cell.textContent = count;
                            cell.setAttribute("data-sort-value", count);
                        }
                        total += count;
                    }
                }

                var totalEl = document.getElementById("incremental-unanalyzed-total");
                if (totalEl) {
                    totalEl.textContent = total;
                    totalEl.removeAttribute("title");
                    if (totalEl.classList) totalEl.classList.remove("refresh-failed");
                }

                if (currentKey === "unanalyzed" || currentKey === "missing") {
                    sortRows(currentKey, currentDirection);
                }
            })
            .catch(function(err) {
                isRefreshing = false;
                var totalEl = document.getElementById("incremental-unanalyzed-total");
                if (totalEl) {
                    totalEl.setAttribute("title", "待分析动态刷新失败，正在显示当前快照数值 (" + (err.message || err) + ")");
                    if (totalEl.classList) totalEl.classList.add("refresh-failed");
                }
                console.warn("[Unanalyzed Refresh] API failed:", err);
            });
    }

    if (window.addEventListener) {
        window.addEventListener("pageshow", function() { refreshUnanalyzedCounts(); });
        window.addEventListener("focus", function() { refreshUnanalyzedCounts(); });
    }
    if (document.addEventListener) {
        document.addEventListener("visibilitychange", function() {
            if (document.visibilityState === "visible") {
                refreshUnanalyzedCounts();
            }
        });
    }

    sortRows("uncovered", -1);
    refreshUnanalyzedCounts();
})();
