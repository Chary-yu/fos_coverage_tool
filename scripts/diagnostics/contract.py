"""Shared machine-readable contract metadata for repository diagnostics."""

CONTRACT_VERSION = "vnext-audit-20260820.2"


def with_contract(result):
    payload = dict(result or {})
    payload.setdefault("contract_version", CONTRACT_VERSION)
    return payload
