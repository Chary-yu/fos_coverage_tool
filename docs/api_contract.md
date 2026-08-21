# VNext API Contract v1

The frozen machine-readable contract is [`api_contract.json`](api_contract.json). The VNext application is the canonical owner of these routes; compatibility shims may translate legacy calls but may not implement a second review state machine.

All inheritance reads are explicitly bound to `scan_id` and use opaque cursors tied to `scan_id`, `data_version` and filter. Mutations are CURRENT-only and carry relation/content/rejection revisions as applicable. The authenticated operator is the persisted reviewer; a client-provided suggestion is never used as a credential.

Progress exposes ordinary, inherited-pending and manual-draft counts separately. They are mutually exclusive and must conserve to `pending_total`. Missing identity, stale revisions, repository busy state and stale cursors fail closed with the frozen error codes in the JSON contract.
