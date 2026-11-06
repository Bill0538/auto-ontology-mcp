"""Deterministic relationship inference.

Auto Ontology finds undeclared foreign keys with an LLM (which columns look
like FKs), a vector search (which primary key they point at) and a
sample-value SQL probe (do the values actually match). Here the first two
steps are name heuristics and the third is kept as is: a containment probe
against the live database. Every candidate keeps its evidence so the host
model can review it with ``list_relationships`` / ``set_relationship``.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .naming import compact, compact_singular, id_base, split_words
from .profiling import is_numeric_type, is_text_type

logger = logging.getLogger(__name__)

# Containment ≥ this and a good name match → accepted automatically.
AUTO_ACCEPT_CONTAINMENT = 0.98
AUTO_ACCEPT_NAME = 0.75
# Below this the values do not support a join at all.
MIN_CONTAINMENT = 0.6

_SELF_REFERENCE = {"parent", "manager", "reportsto", "supervisor", "boss", "lead", "mentor", "predecessor", "successor", "referrer", "referredby"}

_MEASURE_HINTS = (
    "amount", "price", "cost", "total", "qty", "quantity", "weight", "height", "length",
    "width", "size", "volume", "rate", "pct", "percent", "ratio", "score", "count",
)


@dataclass
class Candidate:
    source: dict[str, Any]
    target: dict[str, Any]
    name_score: float
    role: str | None
    reasons: list[str] = field(default_factory=list)
    containment: float | None = None
    probe_size: int | None = None


def _type_family(data_type: str | None) -> str:
    if is_numeric_type(data_type):
        t = (data_type or "").lower()
        return "int" if "int" in t or "serial" in t or t.startswith("number") else "num"
    if is_text_type(data_type):
        return "text"
    return (data_type or "").lower() or "unknown"


def _types_compatible(a: str | None, b: str | None) -> bool:
    fa, fb = _type_family(a), _type_family(b)
    if fa == fb:
        return True
    numeric = {"int", "num"}
    if fa in numeric and fb in numeric:
        return True
    # A text code never references an integer key (state_code → state_id).
    return "unknown" in (fa, fb)


def infer_primary_key(table: dict[str, Any], columns: list[dict[str, Any]]) -> list[str]:
    """Pick an undeclared single-column PK: ``id`` / ``<table>_id`` that is unique and non-null."""
    tname = compact(table["name"])
    tsing = compact_singular(table["name"])
    best: tuple[int, str] | None = None
    for col in columns:
        if col.get("is_unique") is not True or (col.get("null_fraction") or 0) > 0:
            continue
        name = compact(col["name"])
        rank = None
        if name in ("id", "uuid", "guid"):
            rank = 0
        elif name in (f"{tsing}id", f"{tname}id", f"{tsing}key", f"{tsing}code", f"{tsing}uuid", f"{tsing}no"):
            rank = 1
        elif id_base(col["name"]) is not None and col.get("ordinal", 99) == 0:
            rank = 2
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, col["name"])
    return [best[1]] if best else []


def names_own_table(column: str, table: str) -> bool:
    """``states.state_code`` / ``orders.order_no``: the table's own key or code, not a reference."""
    return id_base(column) in (compact(table), compact_singular(table))


def _looks_like_measure(column: str) -> bool:
    words = [w.lower() for w in split_words(column)]
    return any(w in _MEASURE_HINTS for w in words)


def _score(source_col: dict[str, Any], source_table: dict[str, Any], target_col: dict[str, Any], target_table: dict[str, Any]) -> tuple[float, str | None, str] | None:
    """Name evidence that *source_col* references *target_col* (a PK)."""
    s_name = compact(source_col["name"])
    t_name = compact(target_col["name"])
    t_table = compact(target_table["name"])
    t_table_sing = compact_singular(target_table["name"])
    base = id_base(source_col["name"])

    # Same column name on both sides, and it is not a bare "id": customerid ↔ customers.customerid.
    if s_name == t_name and t_name not in ("id", "uuid", "guid", "key", "code"):
        return 0.95, None, f"same column name as {target_table['name']}.{target_col['name']}"

    if base is None:
        return None

    # Within one table only genuine self references count (parent_id, manager_id).
    if source_table["id"] == target_table["id"]:
        if base in _SELF_REFERENCE or base.startswith("parent"):
            return 0.7, base, f"self-reference '{source_col['name']}'"
        return None

    # Target PK is a bare id: customer_id → customers.id
    if t_name in ("id", "uuid", "guid", "key"):
        if base in (t_table_sing, t_table):
            return 0.9, None, f"'{source_col['name']}' names table {target_table['name']}"
        for stem in (t_table_sing, t_table):
            if stem and len(stem) >= 3 and base.endswith(stem):
                role = base[: -len(stem)] or None
                return 0.8, role, f"'{source_col['name']}' ends with {target_table['name']} (role '{role}')"
        ratio = difflib.SequenceMatcher(None, base, t_table_sing).ratio()
        if ratio >= 0.85:
            return 0.6, None, f"'{base}' resembles {target_table['name']} ({ratio:.2f})"
        return None

    # Target PK is entity-named: delivery_city_id → cities.city_id, billingcustomerid → customerid
    if len(t_name) >= 4 and s_name.endswith(t_name):
        role = s_name[: -len(t_name)] or None
        return 0.8, role, f"'{source_col['name']}' ends with {target_col['name']} (role '{role}')"
    t_base = id_base(target_col["name"])
    if t_base and base == t_base:
        return 0.85, None, f"same entity stem '{base}'"
    if t_base and len(t_base) >= 3 and base.endswith(t_base):
        role = base[: -len(t_base)] or None
        return 0.75, role, f"'{source_col['name']}' ends with entity '{t_base}' (role '{role}')"
    return None


def candidate_pairs(
    tables: list[dict[str, Any]],
    columns_by_table: dict[str, list[dict[str, Any]]],
    declared: set[tuple[str, str]],
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Name-based FK candidates, plus identifier-like columns left without one.

    ``declared`` holds ``(source_column_id, target_column_id)`` already known.
    """
    declared_sources = {s for s, _ in declared}
    targets: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for table in tables:
        pk = table.get("pk") or []
        if len(pk) != 1:
            continue
        for col in columns_by_table.get(table["id"], []):
            if col["name"] == pk[0]:
                targets.append((table, col))

    candidates: list[Candidate] = []
    unlinked: list[dict[str, Any]] = []
    for table in tables:
        pk = set(table.get("pk") or [])
        for col in columns_by_table.get(table["id"], []):
            if col["id"] in declared_sources:
                continue
            is_own_pk = col["name"] in pk and len(pk) == 1
            id_like = id_base(col["name"]) is not None
            best: list[Candidate] = []
            for t_table, t_col in targets:
                if t_col["id"] == col["id"]:
                    continue
                if is_own_pk and t_table["id"] == table["id"]:
                    continue
                if _looks_like_measure(col["name"]) and not id_like:
                    continue
                if not _types_compatible(col.get("data_type"), t_col.get("data_type")):
                    continue
                scored = _score(col, table, t_col, t_table)
                if scored is None:
                    continue
                score, role, reason = scored
                # A table's own single PK only references another table in 1:1 extension tables.
                if is_own_pk:
                    score -= 0.2
                best.append(Candidate(source={**col, "table": table}, target={**t_col, "table": t_table}, name_score=score, role=role, reasons=[reason]))
            if best:
                best.sort(key=lambda c: c.name_score, reverse=True)
                top = best[0].name_score
                candidates.extend(c for c in best if c.name_score >= top - 0.1)
            elif id_like and not is_own_pk and col["name"] not in pk and not names_own_table(col["name"], table["name"]):
                unlinked.append({"table": table, "column": col})
    return candidates, unlinked


def probe_containment(
    candidate: Candidate,
    run: Callable[[str], list[list[Any]]],
    qualify: Callable[[dict[str, Any]], str],
    quote: Callable[[str], str],
    limit: int = 500,
) -> None:
    """Fraction of distinct source values found in the target column (sampled)."""
    s_tab, t_tab = candidate.source["table"], candidate.target["table"]
    s_col, t_col = quote(candidate.source["name"]), quote(candidate.target["name"])
    sql = (
        f"SELECT COUNT(*) AS total, COUNT(t.{t_col}) AS hits "
        f"FROM (SELECT DISTINCT {s_col} AS v FROM {qualify(s_tab)} WHERE {s_col} IS NOT NULL LIMIT {limit}) s "
        f"LEFT JOIN {qualify(t_tab)} t ON t.{t_col} = s.v"
    )
    try:
        rows = run(sql)
    except Exception as exc:  # noqa: BLE001
        candidate.reasons.append(f"value probe failed: {str(exc).splitlines()[0][:160]}")
        return
    if not rows:
        return
    total, hits = rows[0][0] or 0, rows[0][1] or 0
    candidate.probe_size = int(total)
    if total:
        candidate.containment = round(float(hits) / float(total), 4)
        candidate.reasons.append(f"{hits}/{total} sampled distinct values exist in the target")
    else:
        candidate.reasons.append("source column has no non-null values to probe")


def decide(candidate: Candidate) -> tuple[str, float] | None:
    """Status and confidence for a probed candidate, or None to drop it."""
    c = candidate.containment
    if c is None:
        if candidate.probe_size == 0:
            return "proposed", round(candidate.name_score * 0.6, 3)
        return "proposed", round(candidate.name_score * 0.5, 3)
    if c < MIN_CONTAINMENT:
        return None
    confidence = round(0.5 * candidate.name_score + 0.5 * c, 3)
    if c >= AUTO_ACCEPT_CONTAINMENT and candidate.name_score >= AUTO_ACCEPT_NAME:
        return "accepted", confidence
    return "proposed", confidence


def junction_verdict(table: dict[str, Any], columns: list[dict[str, Any]], fk_columns: set[str]) -> tuple[bool, str] | None:
    """Pure bridge tables are certain; payload-bearing ones are only candidates.

    Returns ``(is_certain, reason)`` or None when the table is not a junction.
    """
    if len(fk_columns) < 2:
        return None
    pk = set(table.get("pk") or [])
    others = [c for c in columns if c["name"] not in fk_columns and c["name"] not in pk]
    if not others:
        return True, f"every non-key column is a foreign key ({', '.join(sorted(fk_columns))})"
    payload_words = ("qty", "quantity", "role", "rank", "order", "position", "seq", "sort", "status", "date", "from", "to", "start", "end", "created", "updated", "weight", "is", "flag", "primary")
    if len(others) <= 2 and all(any(w in [x.lower() for x in split_words(c["name"])] for w in payload_words) for c in others):
        return False, (
            f"{len(fk_columns)} foreign keys plus association properties ({', '.join(c['name'] for c in others)})"
        )
    return None


def default_join_choice(group: list[dict[str, Any]]) -> str:
    """Among several FKs from one table to the same target, pick the unqualified role.

    ``orders.customer_id`` beats ``orders.billing_customer_id``; otherwise the
    alphabetically first column wins (Auto Ontology's deterministic fallback).
    """
    def key(rel: dict[str, Any]) -> tuple[int, str]:
        target_stem = compact_singular(rel["target_table"])
        base = id_base(rel["source_column"]) or compact(rel["source_column"])
        exact = base in (target_stem, compact(rel["target_table"]), compact(rel["target_column"]))
        return (0 if exact else 1, rel["source_column"].casefold())

    return min(group, key=key)["id"]

