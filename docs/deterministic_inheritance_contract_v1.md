# Deterministic Inheritance Contract v1

The machine-readable authority is [`deterministic_inheritance_contract_v1.json`](deterministic_inheritance_contract_v1.json). Rule ownership and the R01–R83 test mapping remain authoritative in [`contracts/inheritance_rules_v1.json`](../contracts/inheritance_rules_v1.json) and [`contracts/inheritance_test_matrix.json`](../contracts/inheritance_test_matrix.json).

VNext evaluates an immutable predecessor/candidate Git snapshot in a fixed order: repository and file identity, line mapping, normalized tokens, function identity, control and preprocessor context, then same-repository macro/constant/callee dependencies. Any missing or ambiguous fact becomes an ordinary `NO_INHERIT` decision with an explicit reason code. Technical failures are recorded separately and block publication.

`AnalysisRecord` owns analysis content. `AnalysisLineLink` owns the current review state. `AnalysisBlock` owns a human-selected range, and `InheritanceGroup` owns automatic inheritance organization. `INHERITED_PENDING` is never treated as confirmed.

Parser and dependency caches are bounded and keyed by repository/commit/file/blob identity. The local deterministic corpus is a regression artifact only: it is synthetic and cannot satisfy the target-host parser, real Git snapshot, database ledger, or production release evidence requirements.
