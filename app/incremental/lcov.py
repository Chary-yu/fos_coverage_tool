"""LCOV DA/FN parser including the official 2.2 FNL/FNA function format."""

import os


def _legacy_fn(fields):
    payload = fields[1:] if fields and fields[0] == "FN" else fields
    if len(payload) != 2:
        return None
    location, name = payload
    numbers = location.split(",")
    if len(numbers) not in (1, 2) or not all(item.isdigit() for item in numbers):
        return None
    start = int(numbers[0])
    end = int(numbers[1]) if len(numbers) == 2 else None
    return {"start_line": start, "end_line": end, "name": name, "format": "legacy"}


def parse_function_records(lines):
    modern_ranges = {}
    modern_names = {}
    modern_counts = {}
    legacy = []
    modern_seen = False
    modern_invalid = False
    for raw in lines:
        if raw.startswith("FN:"):
            fields = raw[3:].split(",", 2)
            if len(fields) == 2:
                item = _legacy_fn(["FN", fields[0], fields[1]])
            elif len(fields) == 3:
                item = _legacy_fn(["FN", fields[0] + "," + fields[1], fields[2]])
            else:
                item = None
            if item:
                legacy.append(item)
            continue
        if raw.startswith("FNL:"):
            modern_seen = True
            fields = raw[4:].split(",")
            if len(fields) not in (2, 3) or not fields[0].isdigit() or not fields[1].isdigit():
                modern_invalid = True
                continue
            if len(fields) == 3 and not fields[2].isdigit():
                modern_invalid = True
                continue
            index = int(fields[0])
            candidate = {
                "start_line": int(fields[1]),
                "end_line": int(fields[2]) if len(fields) == 3 else None,
                "name": "",
                "format": "modern",
            }
            if candidate["start_line"] < 1 or (
                    candidate["end_line"] is not None and
                    candidate["end_line"] < candidate["start_line"]):
                modern_invalid = True
            if index in modern_ranges and modern_ranges[index] != candidate:
                modern_invalid = True
            modern_ranges[index] = candidate
            continue
        if raw.startswith("FNA:"):
            modern_seen = True
            fields = raw[4:].split(",", 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                modern_invalid = True
                continue
            index = int(fields[0])
            count = int(fields[1])
            candidate = (count, fields[2])
            if index in modern_names and (
                    modern_counts.get(index), modern_names.get(index)
            ) != candidate:
                modern_invalid = True
            modern_counts[index] = count
            modern_names[index] = fields[2]
    if modern_seen:
        ranges = []
        for index in sorted(modern_ranges):
            item = dict(modern_ranges[index])
            item["name"] = modern_names.get(index, "")
            if index not in modern_names:
                modern_invalid = True
            if item["end_line"] is None or item["end_line"] < item["start_line"]:
                modern_invalid = True
            ranges.append(item)
        if set(modern_names) - set(modern_ranges):
            modern_invalid = True
        if modern_invalid or not ranges:
            return ranges, True
        return ranges, False
    return legacy, False


def iter_info_records(path):
    """Yield one LCOV file record at ``end_of_record``.

    The compatibility ``parse_info`` API still aggregates these records for
    callers that need a mapping.  Scan import uses this iterator directly so
    the input file is never represented as one process-wide list/dictionary.
    """
    current = None
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        function_lines = []
        for raw in stream:
            line = raw.rstrip("\r\n")
            if line == "end_of_record":
                if current is not None:
                    ranges, fallback = parse_function_records(function_lines)
                    current["function_ranges"] = ranges
                    current["function_range_fallback"] = fallback or any(
                        item.get("end_line") is None for item in ranges
                    )
                    yield current
                current = None
                function_lines = []
                continue
            if line.startswith("SF:"):
                # A malformed record without an end marker must not retain
                # the previous file's facts or context.
                if current is not None:
                    ranges, fallback = parse_function_records(function_lines)
                    current["function_ranges"] = ranges
                    current["function_range_fallback"] = fallback or any(
                        item.get("end_line") is None for item in ranges
                    )
                    yield current
                current = {
                    "file_path": line[3:],
                    "lines": {},
                    "function_ranges": [],
                    "function_range_fallback": False,
                }
                function_lines = []
                continue
            if current is None:
                continue
            if line.startswith("DA:"):
                fields = line[3:].split(",")
                if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
                    current["lines"][int(fields[0])] = int(fields[1])
            elif line.startswith(("FN:", "FNL:", "FNA:")):
                function_lines.append(line)


def parse_info(path):
    return {item["file_path"]: item for item in iter_info_records(path)}


def load_info(path):
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    return parse_info(path)
