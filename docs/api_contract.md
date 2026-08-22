# VNext API Contract v1

The frozen machine-readable contract is [`api_contract.json`](api_contract.json). The VNext application is the canonical owner of these routes; compatibility shims may translate legacy calls but may not implement a second review state machine.

All inheritance reads are explicitly bound to `scan_id` and use opaque cursors tied to `scan_id`, `data_version` and filter. The review button uses the exact `GET /api/coverage/scans/{scan_id}/inheritance/relation` query with repository, file, and line/relation identity; it never searches a global pending page. Mutations are CURRENT-only and carry relation/content/rejection revisions as applicable. The authenticated operator is the persisted reviewer; a client-provided suggestion is never used as a credential.

Progress exposes ordinary, inherited-pending and manual-draft counts separately. They are mutually exclusive and must conserve to pending_total. Missing identity, stale revisions, repository busy state and stale cursors fail closed with the frozen error codes in the JSON contract.

Progress Details is a canonical top-level pagination envelope. Its response
fields are page, page_size, total, total_pages, and rows; clients must
consume these fields directly and must not unwrap a legacy data member. The
pending-file homepage endpoint is likewise bounded and may return has_more
and next_cursor. `GET /api/coverage/progress/files` is the canonical bounded
keyset window for the Progress file table; it returns only the current window
and binds its cursor to scan/data-version/filter. Physical line detail is
loaded only through the explicit file detail route.
