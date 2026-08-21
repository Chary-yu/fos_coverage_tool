"""Pure request validation for the VNext inheritance API."""

from __future__ import absolute_import


MAX_SELECTED_LINES = 500


def confirm(body):
    values = body or {}
    selected = values.get("selected_line_ids") or [values.get("line_id")]
    selected = [int(item) for item in selected if item]
    if not selected:
        raise ValueError("line_id and expected_relation_revision are required")
    if len(selected) > MAX_SELECTED_LINES:
        raise ValueError("selected_line_ids exceeds limit")
    return (
        selected,
        values.get("expected_relation_revisions") or {},
        values.get("expected_relation_revision"),
    )


def edit_confirm(body):
    values = body or {}
    records = values.get("records") or []
    if not records:
        raise ValueError("records are required")
    expected_revision = values.get("expected_record_revision")
    expected_relation = values.get("expected_relation_revision")
    expected_relation_map = values.get("expected_relation_revisions") or {}
    if expected_revision is None and any(
            item.get("expected_record_revision") is None
            for item in records if isinstance(item, dict)):
        raise ValueError("EXPECTED_RECORD_REVISION_REQUIRED")
    if expected_relation is None and not expected_relation_map and any(
            item.get("expected_relation_revision") is None
            for item in records if isinstance(item, dict)):
        raise ValueError("STALE_RELATION_REVISION")
    if expected_revision is not None:
        records = [
            dict(item, expected_record_revision=item.get(
                "expected_record_revision", expected_revision
            ))
            for item in records
        ]
    if expected_relation is not None or expected_relation_map:
        records = [
            dict(item, expected_relation_revision=item.get(
                "expected_relation_revision",
                expected_relation_map.get(str(item.get("line_id")),
                                          expected_relation_map.get(
                                              item.get("line_id"), expected_relation)),
            ))
            for item in records
        ]
    return records


def reject(body):
    values = body or {}
    return int(values.get("line_id") or 0), int(
        values.get("expected_relation_revision") or 0
    )


def undo(body, rejection_id=None):
    values = dict(body or {})
    if rejection_id is not None:
        values["rejection_id"] = int(rejection_id)
    return (
        int(values.get("line_id") or 0),
        int(values.get("rejection_id") or 0),
        int(values.get("expected_rejection_revision") or 0),
        int(values.get("expected_relation_revision") or 0),
    )
