# Gate evidence output

Evidence is generated for an exact checkout by `scripts/diagnostics/build_gate_evidence.py` and `scripts/diagnostics/gate_matrix.py`. The default output is `.artifacts/gates/gate-a` through `gate-f`; production evidence must be supplied by the target environment and validated with Evidence Manifest v2. Synthetic local fixtures are never release-eligible.

When an operator supplies a legacy flat JSON artifact through a `COVERAGE_GATE_*_EVIDENCE` variable, a `PASSED` result must include the matching `gate` (`gate-a` through `gate-f`), exact `candidate_revision`, `release_identity.commit_sha`, `evidence_class`, non-empty `host_identity`, `command_or_action`, `started_at`, `finished_at`, integer `exit_code=0`, `synthetic=false`, and an `artifact_path` whose `artifact_sha256` matches the referenced file. Missing, replayed, or unverifiable provenance remains `INCOMPLETE`; it is never promoted by the gate assembler.
