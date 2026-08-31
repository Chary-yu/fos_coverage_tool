(function(root) {
    "use strict";

    function fileKey(repositoryName, filePath) {
        var repository = String(repositoryName || "").trim();
        var path = String(filePath || "").replace(/\\/g, "/")
            .replace(/^\.\//, "");
        return repository ? repository + "::" + path : path;
    }

    function staleError(message) {
        var error = new Error(message || "PAGINATION_CURSOR_STALE");
        error.code = "PAGINATION_CURSOR_STALE";
        return error;
    }

    function requestPage(url) {
        return fetch(url).then(function(response) {
            return response.json().catch(function() { return {}; }).then(function(payload) {
                if (!response.ok) {
                    var error = new Error(payload.message || payload.error ||
                        ("HTTP " + response.status));
                    error.code = payload.error || (response.status === 409 ?
                        "PAGINATION_CURSOR_STALE" : "HTTP_ERROR");
                    throw error;
                }
                if (payload && payload.status === "error") {
                    var apiError = new Error(payload.message || payload.error || "API error");
                    apiError.code = payload.error || "API_ERROR";
                    throw apiError;
                }
                // VNext owns the top-level envelope.  A nested ``data``
                // fallback would allow a legacy response to be mixed into a
                // supposedly exact Scan/DataVersion snapshot.
                return payload || {};
            });
        });
    }

    function tokenIsCurrent(token) {
        return typeof token !== "function" || Boolean(token());
    }

    function boundedInteger(value, fallback, maximum) {
        if (value == null || value === "") return fallback;
        var number = Number(value);
        if (!Number.isFinite(number) || number <= 0) return fallback;
        return Math.min(maximum, Math.floor(number));
    }

    function nonNegativeInteger(value, fallback) {
        if (value == null || value === "") return fallback;
        var number = Number(value);
        if (!Number.isFinite(number) || number < 0) return fallback;
        return Math.floor(number);
    }

    function snapshotNumber(value) {
        if (value == null) return null;
        if (typeof value !== "number" && typeof value !== "string") return null;
        var text = String(value).trim();
        if (!/^\d+$/.test(text)) return null;
        var number = Number(text);
        return Number.isSafeInteger(number) ? String(number) : null;
    }

    function contractError(message) {
        var error = new Error(message || "pending snapshot contract is invalid");
        error.code = "PENDING_SNAPSHOT_CONTRACT_ERROR";
        return error;
    }

    function fetchComplete(options) {
        options = options || {};
        var base = String(options.apiBase || "").replace(/\/+$/, "");
        var project = String(options.projectName || "");
        var scanId = options.scanId == null ? "" : String(options.scanId);
        var requestedScan = scanId ? snapshotNumber(scanId) : null;
        var repositoryName = options.repositoryName == null ? "" :
            String(options.repositoryName);
        var pageSize = boundedInteger(options.pageSize, 200, 200);
        var endpoint = options.endpoint || "/incremental/unanalyzed";
        var maxRestarts = nonNegativeInteger(options.maxRestarts, 2);
        var token = options.requestToken;

        if (scanId && requestedScan === null) {
            return Promise.reject(contractError("requested pending snapshot scan identity is invalid"));
        }

        function attempt(restarts) {
            if (!tokenIsCurrent(token)) {
                var cancelled = new Error("已取消过期的 pending snapshot");
                cancelled.code = "REQUEST_GENERATION_STALE";
                return Promise.reject(cancelled);
            }
            var map = Object.create(null);
            var pendingLineMap = Object.create(null);
            var cursor = null;
            var expectedScan = null;
            var expectedVersion = null;
            var pages = 0;
            var seenCursors = Object.create(null);
            var pathIdentities = Object.create(null);

            function consume() {
                if (!tokenIsCurrent(token)) {
                    var cancelled = new Error("已取消过期的 pending snapshot");
                    cancelled.code = "REQUEST_GENERATION_STALE";
                    return Promise.reject(cancelled);
                }
                if (pages > 100000) return Promise.reject(staleError("pending snapshot is too large"));
                var params = new URLSearchParams();
                params.set("project", project);
                params.set("page_size", String(pageSize));
                if (scanId) params.set("scan_id", scanId);
                if (repositoryName) params.set("repository_name", repositoryName);
                if (cursor) params.set("cursor", cursor);
                return requestPage(base + endpoint + "?" + params.toString()).then(function(payload) {
                    if (!tokenIsCurrent(token)) {
                        var cancelled = new Error("已取消过期的 pending snapshot");
                        cancelled.code = "REQUEST_GENERATION_STALE";
                        throw cancelled;
                    }
                    pages += 1;
                    var pageScan = snapshotNumber(payload.scan_id);
                    var pageVersion = snapshotNumber(payload.data_version);
                    if (!Object.prototype.hasOwnProperty.call(payload, "scan_id") ||
                            !Object.prototype.hasOwnProperty.call(payload, "data_version") ||
                            !Object.prototype.hasOwnProperty.call(payload, "repository_name") ||
                            !Object.prototype.hasOwnProperty.call(payload, "has_more") ||
                            !Object.prototype.hasOwnProperty.call(payload, "files") ||
                            !Object.prototype.hasOwnProperty.call(payload, "next_cursor")) {
                        throw contractError("pending snapshot identity is incomplete");
                    }
                    if (typeof payload.has_more !== "boolean" || !Array.isArray(payload.files)) {
                        throw contractError("pending snapshot envelope is invalid");
                    }
                    if ((payload.scan_id != null && pageScan === null) ||
                            payload.data_version == null || pageVersion === null) {
                        throw contractError("pending snapshot version identity is invalid");
                    }
                    if (requestedScan !== null && pageScan !== requestedScan) {
                        throw staleError("pending snapshot scan identity does not match request");
                    }
                    var pageRepository = payload.repository_name == null
                        ? "" : String(payload.repository_name);
                    if (pageRepository !== repositoryName) {
                        throw staleError("pending snapshot repository changed during pagination");
                    }
                    if (expectedScan === null) expectedScan = pageScan;
                    if (expectedVersion === null) expectedVersion = pageVersion;
                    if (pageScan !== expectedScan || pageVersion !== expectedVersion) {
                        throw staleError("pending snapshot changed during pagination");
                    }
                    payload.files.forEach(function(file) {
                        if (!file || typeof file !== "object") {
                            throw contractError("pending snapshot file identity is invalid");
                        }
                        var normalizedPath = String(file.file_path || "").replace(/\\/g, "/")
                            .replace(/^\.\//, "");
                        if (!normalizedPath) {
                            throw contractError("pending snapshot file path is missing");
                        }
                        var fileRepository = String(file.repository_name || "");
                        if (repositoryName && fileRepository !== repositoryName) {
                            throw staleError("pending snapshot file repository changed");
                        }
                        var key = fileKey(fileRepository, normalizedPath);
                        var pending = [];
                        if (Object.prototype.hasOwnProperty.call(file, "pending_line_numbers")) {
                            if (!Array.isArray(file.pending_line_numbers)) {
                                throw contractError("pending snapshot line numbers are invalid");
                            }
                            pending = file.pending_line_numbers.map(Number);
                            if (pending.some(function(value) {
                                return !Number.isSafeInteger(value) || value <= 0;
                            })) {
                                throw contractError("pending snapshot line numbers are invalid");
                            }
                        }
                        var count = file.unanalyzed == null ? pending.length : Number(file.unanalyzed);
                        if (!Number.isSafeInteger(count) || count < 0) {
                            throw contractError("pending snapshot count is invalid");
                        }
                        if (!key) {
                            throw contractError("pending snapshot file identity is missing");
                        }
                        if (Object.prototype.hasOwnProperty.call(map, key)) {
                            throw contractError("pending snapshot contains a duplicate file identity");
                        }
                        var pathIdentity = pathIdentities[normalizedPath] || {
                            unscoped: false, repositories: Object.create(null), count: 0,
                        };
                        var pathConflict = fileRepository
                            ? pathIdentity.unscoped ||
                                Object.prototype.hasOwnProperty.call(
                                    pathIdentity.repositories, fileRepository
                                )
                            : pathIdentity.count > 0;
                        if (pathConflict) {
                            var ambiguous = new Error("pending snapshot file identity is ambiguous");
                            ambiguous.code = "PENDING_SNAPSHOT_AMBIGUOUS_IDENTITY";
                            throw ambiguous;
                        }
                        if (fileRepository) {
                            pathIdentity.repositories[fileRepository] = true;
                        } else {
                            pathIdentity.unscoped = true;
                        }
                        pathIdentity.count += 1;
                        pathIdentities[normalizedPath] = pathIdentity;
                        map[key] = {
                            key: key, repository_name: fileRepository,
                            file_path: normalizedPath, unanalyzed: count,
                        };
                        pendingLineMap[key] = pending;
                        if (!fileRepository) {
                            map[normalizedPath] = map[key];
                            pendingLineMap[normalizedPath] = pending;
                        }
                    });
                    var next = payload.next_cursor;
                    if (next !== null && (typeof next !== "string" || !next)) {
                        throw contractError("pending snapshot cursor is invalid");
                    }
                    if (payload.has_more && !next) {
                        throw contractError("pending snapshot cursor is missing");
                    }
                    if (!payload.has_more && next) {
                        throw contractError("pending snapshot has an unexpected cursor");
                    }
                    if (!payload.has_more) {
                        return {
                            files: Object.keys(map).filter(function(key) {
                                return !map[key].key || map[key].key === key;
                            }).map(function(key) { return map[key]; }),
                            map: map, pendingLineMap: pendingLineMap,
                            scan_id: expectedScan, data_version: expectedVersion,
                            pages: pages, restarts: restarts,
                        };
                    }
                    if (seenCursors[next]) throw staleError("cursor repeated");
                    seenCursors[next] = true;
                    cursor = next;
                    return consume();
                });
            }

            return consume().catch(function(error) {
                if (error && error.code === "PAGINATION_CURSOR_STALE" &&
                        restarts < maxRestarts) {
                    return attempt(restarts + 1);
                }
                if (error && error.code === "PAGINATION_CURSOR_STALE" &&
                        restarts >= maxRestarts) {
                    var closed = new Error("数据正在变化，请稍后刷新");
                    closed.code = "PENDING_SNAPSHOT_STALE";
                    throw closed;
                }
                throw error;
            });
        }

        return attempt(0);
    }

    root.CoveragePendingSnapshot = {
        fileKey: fileKey,
        fetchComplete: fetchComplete,
    };
}(window));
