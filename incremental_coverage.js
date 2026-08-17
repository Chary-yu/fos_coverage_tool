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

    var keyToColumn = {
        repository: 0,
        module: 1,
        team: 2,
        leader: 3,
        ownership: 1,
        file: 4,
        changed: 5,
        covered: 6,
        uncovered: 7,
        ignored: 8,
        missing: 9
    };

    var currentKey = "uncovered";
    var currentDirection = -1;

    function applyFilters() {
        var selRepo = repoFilter ? repoFilter.value : "";
        var selModule = moduleFilter ? moduleFilter.value : "";
        var selTeam = teamFilter ? teamFilter.value : "";
        var selLeader = leaderFilter ? leaderFilter.value : "";
        var kw = fileSearch ? fileSearch.value.trim().toLowerCase() : "";

        var total = allRows.length;
        var visible = 0;

        for (var i = 0; i < total; i++) {
            var row = allRows[i];
            var rRepo = row.getAttribute("data-repo") || "";
            var rModule = row.getAttribute("data-module") || "";
            var rTeam = row.getAttribute("data-team") || "";
            var rLeader = row.getAttribute("data-leader") || "";
            var rOwnership = row.getAttribute("data-ownership") || "";
            var rFile = (row.cells[4] ? row.cells[4].getAttribute("data-sort-value") || row.cells[4].textContent || "" : "");
            var fullSearchText = (rRepo + " " + rModule + " " + rTeam + " " + rLeader + " " + rOwnership + " " + rFile).toLowerCase();

            var mRepo = !selRepo || rRepo === selRepo;
            var mModule = !selModule || rModule === selModule;
            var mTeam = !selTeam || rTeam === selTeam;
            var mLeader = !selLeader || rLeader === selLeader;
            var mKw = !kw || fullSearchText.indexOf(kw) !== -1;

            if (mRepo && mModule && mTeam && mLeader && mKw) {
                row.style.display = "";
                visible++;
            } else {
                row.style.display = "none";
            }
        }

        if (filterCount) {
            if (selRepo || selModule || selTeam || selLeader || kw) {
                filterCount.textContent = "筛选匹配 " + visible + " / " + total + " 个文件";
            } else {
                filterCount.textContent = "共 " + total + " 个文件";
            }
        }
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
    if (fileSearch) fileSearch.addEventListener("input", applyFilters);
    if (resetBtn) {
        resetBtn.addEventListener("click", function() {
            if (repoFilter) repoFilter.value = "";
            if (moduleFilter) moduleFilter.value = "";
            if (teamFilter) teamFilter.value = "";
            if (leaderFilter) leaderFilter.value = "";
            if (fileSearch) fileSearch.value = "";
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

    sortRows("uncovered", -1);
})();
