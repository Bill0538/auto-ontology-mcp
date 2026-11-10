"""Column profiling from a bounded row sample.

Follows Auto Ontology's ``calculate_columns_profiling`` (Apache-2.0, NVIDIA):
sample up to N rows, record uniqueness and the most common values per column,
probe the full distinct set of small text enums, and infer date notations.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable

TOP_N = 5
LOW_CARDINALITY_MAX = 25
MAX_SAMPLE_LEN = 40
_EXCLUDED_SAMPLE_TYPES = ("uuid", "blob", "bytea", "binary", "image")
_TEXT_TYPES = ("char", "text", "string", "clob", "enum", "varchar")
_NUMERIC_TYPES = ("int", "real", "float", "double", "numeric", "decimal", "number", "money")
_DATE_TYPES = ("date", "time")

# (strptime pattern, notation shown to the model) in descending specificity.
_DATE_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("%Y-%m-%d %H:%M:%S.%f", "YYYY-MM-DD HH:MM:SS.f"),
    ("%Y-%m-%dT%H:%M:%S.%f", "YYYY-MM-DDTHH:MM:SS.f"),
    ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH:MM:SS"),
    ("%Y-%m-%dT%H:%M:%S", "YYYY-MM-DDTHH:MM:SS"),
    ("%Y-%m-%d %H:%M", "YYYY-MM-DD HH:MM"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%Y/%m/%d", "YYYY/MM/DD"),
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%d-%m-%Y", "DD-MM-YYYY"),
    ("%m-%d-%Y", "MM-DD-YYYY"),
    ("%d.%m.%Y", "DD.MM.YYYY"),
    ("%Y%m%d", "YYYYMMDD"),
    ("%H:%M:%S", "HH:MM:SS"),
)
_INDISTINGUISHABLE = (
    frozenset({"DD/MM/YYYY", "MM/DD/YYYY"}),
    frozenset({"DD-MM-YYYY", "MM-DD-YYYY"}),
)


def is_text_type(data_type: str | None) -> bool:
    t = (data_type or "").lower()
    return any(k in t for k in _TEXT_TYPES) or t == ""


def is_numeric_type(data_type: str | None) -> bool:
    t = (data_type or "").lower()
    return any(k in t for k in _NUMERIC_TYPES)


def is_date_type(data_type: str | None) -> bool:
    t = (data_type or "").lower()
    return any(k in t for k in _DATE_TYPES)


def _sample_key(value: Any) -> str:
    return repr(value)


def _parses_all(pattern: str, values: list[str]) -> bool:
    for v in values:
        try:
            parsed = datetime.strptime(v, pattern)
        except ValueError:
            return False
        if "%Y" in pattern and not (1900 <= parsed.year <= 2099):
            return False
    return True


def infer_date_format(values: Iterable[Any]) -> str | None:
    """Notation shared by every sampled value, or None when there is none or it is ambiguous."""
    strings = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return None
        s = str(v).strip()
        if not s or len(s) > 40:
            return None
        strings.append(s)
        if len(strings) >= 200:
            break
    if len(strings) < 3:
        return None
    matches = [label for pattern, label in _DATE_CANDIDATES if _parses_all(pattern, strings)]
    if not matches:
        return None
    first = matches[0]
    for pair in _INDISTINGUISHABLE:
        if first in pair and len(pair & set(matches)) == 2:
            return None
    # Pure digit strings that only match YYYYMMDD are often ids, not dates.
    if first == "YYYYMMDD" and not all(len(s) == 8 for s in strings):
        return None
    return first


_LIST_SEPARATORS = (",", ";", "|")


def infer_list_separator(values: list[Any]) -> str | None:
    """The separator of a text column that stores lists ('SLE, SjS'), or None.

    A column counts as a list column when at least two distinct values contain the
    separator and most of the items those values split into also occur on their own
    ('SLE' and 'SjS' are values too). Free text that happens to contain commas
    (addresses, names) fails the second test, because its pieces never stand alone.
    """
    strings = {v.strip() for v in values if isinstance(v, str) and v.strip()}
    if len(strings) < 3:
        return None
    standalone = {s.casefold() for s in strings}
    for sep in _LIST_SEPARATORS:
        multi = [s for s in strings if sep in s]
        if len(multi) < 2:
            continue
        items = [i.strip().casefold() for s in multi for i in s.split(sep) if i.strip()]
        if items and sum(1 for i in items if i in standalone) / len(items) >= 0.6:
            return sep
    return None


def _json_ready(value: Any) -> Any:
    from .datasources import json_value

    return json_value(value)


def profile_rows(
    columns: list[dict[str, Any]], column_names: list[str], rows: list[list[Any]]
) -> dict[str, dict[str, Any]]:
    """Profile each column of a row sample.

    Returns ``{column: {sample_values, is_unique, null_fraction,
    distinct_in_sample, min_value, max_value, format, needs_distinct_probe}}``.
    """
    type_by_name = {c["name"]: c.get("data_type") for c in columns}
    total = len(rows)
    out: dict[str, dict[str, Any]] = {}
    for idx, name in enumerate(column_names):
        raw = [row[idx] for row in rows]
        values = [_json_ready(v) for v in raw if v is not None]
        keys = [_sample_key(v) for v in values]
        counter = Counter(keys)
        first_by_key: dict[str, Any] = {}
        for k, v in zip(keys, values):
            first_by_key.setdefault(k, v)
        declared = type_by_name.get(name)
        is_unique = bool(keys) and len(counter) == len(keys)
        top = [first_by_key[k] for k, _ in counter.most_common(TOP_N)]

        lowered = (declared or "").lower()
        keep_samples = not any(k in lowered for k in _EXCLUDED_SAMPLE_TYPES)
        samples = [
            v
            for v in top
            if keep_samples and not isinstance(v, (dict, list)) and not (isinstance(v, str) and len(v) > MAX_SAMPLE_LEN)
        ]

        minimum = maximum = None
        comparable = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if comparable and (is_numeric_type(declared) or len(comparable) == len(values)):
            minimum, maximum = min(comparable), max(comparable)
        elif values and (is_date_type(declared) or all(isinstance(v, str) for v in values)):
            strings = [v for v in values if isinstance(v, str)]
            if strings and is_date_type(declared):
                minimum, maximum = min(strings), max(strings)

        fmt = None
        if is_date_type(declared) or is_text_type(declared):
            fmt = infer_date_format(values)
            if fmt and not minimum and all(isinstance(v, str) for v in values):
                minimum, maximum = min(values), max(values)

        list_sep = infer_list_separator(values) if is_text_type(declared) and fmt is None else None
        if list_sep:
            # Show a list-shaped value among the samples so the model sees the shape.
            example = next((first_by_key[k] for k, _ in counter.most_common() if list_sep in str(first_by_key[k])), None)
            if example is not None and example not in samples and len(str(example)) <= MAX_SAMPLE_LEN:
                samples = samples[: TOP_N - 1] + [example]

        out[name] = {
            "sample_values": samples,
            "list_separator": list_sep,
            "is_unique": is_unique,
            "null_fraction": round((total - len(values)) / total, 4) if total else None,
            "distinct_in_sample": len(counter),
            "min_value": None if minimum is None else str(minimum),
            "max_value": None if maximum is None else str(maximum),
            "format": fmt,
            # Small text enums get a DISTINCT probe so rare values (e.g. a
            # 'Cancelled' status hidden behind a dominant one) are not missed.
            "needs_distinct_probe": (
                not is_unique
                and is_text_type(declared)
                and fmt is None
                and 0 < len(counter) <= LOW_CARDINALITY_MAX
            ),
        }
    return out
