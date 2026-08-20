"""Single JSON DTO conversion boundary for the HTTP API."""

import json
import os
from datetime import date, datetime
from decimal import Decimal


def to_jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (list,)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if hasattr(value, "to_dict"):
        return to_jsonable(value.to_dict())
    raise TypeError("value is not JSON serializable: {}".format(type(value).__name__))


def dumps(value):
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def loads(payload):
    if payload in (None, "", b""):
        return {}
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    value = json.loads(payload)
    if not isinstance(value, (dict, list)):
        raise ValueError("JSON request body must be an object or array")
    return value
