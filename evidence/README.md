# Gate evidence output

Evidence is generated for an exact checkout by `scripts/diagnostics/build_gate_evidence.py` and `scripts/diagnostics/gate_matrix.py`. The default output is `.artifacts/gates/gate-a` through `gate-f`; production evidence must be supplied by the target environment and validated with Evidence Manifest v2. Synthetic local fixtures are never release-eligible.
