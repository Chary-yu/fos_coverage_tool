"""Bounded, deterministic read-set fingerprints for inheritance publication."""

from __future__ import absolute_import

import hashlib


READ_SET_FORMAT = "inheritance-read-set-v1"
READ_SET_HASH_ALGORITHM = "sha256-sum-xor-v1"
_MASK = (1 << 256) - 1


def _item_digest(kind, item_id, revision):
    payload = "{}:{}:{}".format(str(kind), int(item_id), int(revision))
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest(), 16)


class ReadSetAccumulator(object):
    """Accumulate source relation/record observations in constant memory.

    Records are observations rather than a de-duplicated set: a record used by
    several relations is added once per relation.  Publication uses the same
    query shape, so this preserves the exact consulted input while avoiding a
    resident relation-id/record-id collection and a large checkpoint payload.
    """

    def __init__(self):
        self.relation_count = 0
        self.relation_sum = 0
        self.relation_xor = 0
        self.record_count = 0
        self.record_sum = 0
        self.record_xor = 0

    def add_relation(self, relation_id, relation_revision):
        value = _item_digest("relation", relation_id, relation_revision)
        self.relation_count += 1
        self.relation_sum = (self.relation_sum + value) & _MASK
        self.relation_xor ^= value

    def add_record(self, record_id, content_revision):
        value = _item_digest("record", record_id, content_revision)
        self.record_count += 1
        self.record_sum = (self.record_sum + value) & _MASK
        self.record_xor ^= value

    def to_payload(self, candidate_scan_id=None, predecessor_scan_id=None):
        return {
            "format": READ_SET_FORMAT,
            "hash_algorithm": READ_SET_HASH_ALGORITHM,
            "candidate_scan_id": (
                int(candidate_scan_id) if candidate_scan_id is not None else None
            ),
            "predecessor_scan_id": (
                int(predecessor_scan_id) if predecessor_scan_id is not None else None
            ),
            "relations": {
                "count": int(self.relation_count),
                "sum": format(self.relation_sum, "064x"),
                "xor": format(self.relation_xor, "064x"),
            },
            "records": {
                "count": int(self.record_count),
                "sum": format(self.record_sum, "064x"),
                "xor": format(self.record_xor, "064x"),
            },
        }

    def matches(self, payload):
        if not isinstance(payload, dict):
            return False
        expected = self.to_payload(
            payload.get("candidate_scan_id"),
            payload.get("predecessor_scan_id"),
        )
        return (
            isinstance(payload, dict)
            and payload.get("format") == READ_SET_FORMAT
            and payload.get("hash_algorithm") == READ_SET_HASH_ALGORITHM
            and payload.get("relations") == expected["relations"]
            and payload.get("records") == expected["records"]
        )
