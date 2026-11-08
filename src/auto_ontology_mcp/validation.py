"""Validate a query against the ontology and, with read-only probes, the data.

Ports the deterministic checks of Auto Ontology's text-to-SQL repair path
(``retrieval/text_to_sql/db_probe``, Apache-2.0, NVIDIA): near-miss string
literals, percent-vs-fraction thresholds, joins that the semantic model does
not know, plus a fan-out check for aggregates over a one-to-many join.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Callable

import sqlglot
from sqlglot import exp

from .naming import compact
from .sqlcheck import Analysis, PhysicalColumn, SqlGuardError, analyze, guard_readonly, guard_sources

CLOSE_MATCH_CUTOFF = 0.6
LOW_CARD_THRESHOLD = 200


def qtable(schema: str | None, name: str) -> exp.Table:
    return exp.table_(exp.to_identifier(name, quoted=True), db=exp.to_identifier(schema, quoted=True) if schema else None)


def qcol(name: str) -> exp.Column:
    return exp.column(exp.to_identifier(name, quoted=True))


def literal(value: Any) -> exp.Expression:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


class Validator:
    def __init__(
        self,
        *,
        dialect: str | None,
        catalog_columns: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        attributes: list[dict[str, Any]],
        sql_attributes: list[dict[str, Any]],
        run: Callable[[str], list[list[Any]]],
    ) -> None:
        self.dialect = dialect
        self.catalog_columns = catalog_columns
        self.cols = {
            ((c.get("schema_name") or "").casefold(), c["table_name"].casefold(), c["name"].casefold()): c
            for c in catalog_columns
        }
        self.tables = {}
        for c in catalog_columns:
            self.tables[((c.get("schema_name") or "").casefold(), c["table_name"].casefold())] = (
                c.get("schema_name") or "",
                c["table_name"],
            )
        self.rels = relationships
        self.attributes = attributes
        self.rel_columns = {r["source_column_id"] for r in relationships} | {r["target_column_id"] for r in relationships}
        self.attr_by_column: dict[str, list[dict[str, Any]]] = {}
        for a in attributes:
            self.attr_by_column.setdefault(a["column_id"], []).append(a)
        self.sql_attributes = sql_attributes
        self.run = run

    # ---------------------------------------------------------------- helpers
    def _col(self, p: PhysicalColumn) -> dict[str, Any] | None:
        key = (p.schema.casefold(), p.table.casefold(), p.column.casefold())
        hit = self.cols.get(key)
        if hit is None and not p.schema:
            matches = [c for k, c in self.cols.items() if k[1:] == key[1:]]
            hit = matches[0] if len(matches) == 1 else None
        return hit

    def _table_expr(self, c: dict[str, Any]) -> exp.Table:
        return qtable(c.get("schema_name") or None, c["table_name"])

    def _probe(self, query: exp.Expression) -> list[list[Any]]:
        return self.run(query.sql(dialect=self.dialect))

    def _rel_between(self, a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
        """Relationship joining columns a and b; bool says whether a is the FK source."""
        for r in self.rels:
            if r["source_column_id"] == a["id"] and r["target_column_id"] == b["id"]:
                return r, True
            if r["source_column_id"] == b["id"] and r["target_column_id"] == a["id"]:
                return r, False
        return None

    # ------------------------------------------------------------------- main
    def validate(self, sql: str, *, probe: bool = True) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        notes: list[str] = []
        try:
            tree = guard_readonly(sql, self.dialect)
            guard_sources(tree, self.tables.values())
        except SqlGuardError as exc:
            return {"valid": False, "errors": [str(exc)], "warnings": [], "notes": [], "grounding": {}}

        analysis = analyze(tree, self.catalog_columns, self.dialect)
        if analysis.error:
            errors.append(self._explain_qualify_error(analysis.error))

        for schema, table in analysis.tables:
            if (schema.casefold(), table.casefold()) not in self.tables and not any(
                k[1] == table.casefold() for k in self.tables
            ):
                errors.append(f"Table {schema + '.' if schema else ''}{table} is not in the catalog.")

        fanned = self._check_joins(analysis, errors, warnings, notes, probe)
        self._check_fanout(analysis, fanned, warnings)
        self._check_branch_fanout(analysis, fanned, warnings)
        if probe:
            self._check_string_literals(analysis, warnings)
            self._check_numeric_scale(analysis, warnings)
            if not errors:
                self._dry_run(tree, errors)

        grounding = self.grounding(analysis, sql)
        if grounding["draft_terms"]:
            notes.append(
                "Uses Terms that are not certified yet: " + ", ".join(grounding["draft_terms"]) + ". Mention this when you answer."
            )
        if grounding["unmodeled_columns"]:
            notes.append(
                "Columns outside the semantic layer (no Term maps them): " + ", ".join(grounding["unmodeled_columns"][:10])
            )
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "notes": notes,
            "grounding": grounding,
        }

    def _explain_qualify_error(self, message: str) -> str:
        names = sorted({c["name"] for c in self.catalog_columns})
        hint = ""
        if "Column '" in message:
            missing = message.split("Column '")[1].split("'")[0]
            close = difflib.get_close_matches(missing, names, n=4, cutoff=0.6)
            if close:
                hint = f" Did you mean: {', '.join(close)}?"
            key = compact(missing)
            mapped = [
                a for a in self.attributes
                if compact(a["display_name"]) == key or compact(a["display_name"]).endswith(key) and len(key) >= 4
            ]
            if mapped:
                hint += " In the ontology, " + "; ".join(
                    f"'{a['display_name']}' ({a['term_name']}) is {a['table_name']}.{a['column_name']}" for a in mapped[:4]
                ) + "."
        elif "Table" in message or "table" in message:
            tables = sorted({t for _, t in self.tables.values()})
            hint = f" Known tables: {', '.join(tables[:40])}."
        return f"{message}.{hint}"

    # ------------------------------------------------------------------ joins
    def _check_joins(
        self, analysis: Analysis, errors: list[str], warnings: list[str], notes: list[str], probe: bool
    ) -> set[tuple[str, str]]:
        """Check join predicates; return (one_side_table_key, many_side_table_key) fan-out pairs."""
        fanned: set[tuple[str, str]] = set()
        seen: set[frozenset] = set()
        for a, b, _origin in analysis.joins:
            key = frozenset({(a.table, a.column), (b.table, b.column)})
            if key in seen:
                continue
            seen.add(key)
            ca, cb = self._col(a), self._col(b)
            label = f"{a.table}.{a.column} = {b.table}.{b.column}"
            if not ca or not cb:
                continue
            found = self._rel_between(ca, cb)
            if found:
                rel, a_is_source = found
                src, tgt = (ca, cb) if a_is_source else (cb, ca)
                if rel["status"] == "rejected":
                    errors.append(f"Join {label} was reviewed and rejected in the ontology.")
                elif rel["status"] == "proposed":
                    warnings.append(f"Join {label} is only a proposed (unreviewed) relationship.")
                if rel.get("role") and not rel.get("is_default"):
                    notes.append(f"Join {label} uses the '{rel['role']}' role, not the default relationship.")
                if rel.get("cardinality") != "one_to_one":
                    fanned.add(((tgt.get("schema_name") or "", tgt["table_name"]), (src.get("schema_name") or "", src["table_name"])))
                continue

            # Unknown join: is there a known relationship between these tables on other columns?
            known = [
                r for r in self.rels
                if r["status"] == "accepted"
                and {r["source_table_id"], r["target_table_id"]} == {ca["table_id"], cb["table_id"]}
            ]
            if known:
                alts = "; ".join(f"{r['source_table']}.{r['source_column']} = {r['target_table']}.{r['target_column']}" for r in known)
                errors.append(
                    f"Join {label} is not how the ontology joins these tables ({alts}). Matching values between "
                    "unrelated surrogate keys are a coincidence; use the known relationship unless the user asked for this one."
                )
            else:
                warnings.append(f"Join {label} is not a relationship in the ontology.")
            if self._family(ca) != self._family(cb):
                errors.append(f"Join {label} compares {ca.get('data_type')} with {cb.get('data_type')}.")
                continue
            if probe:
                ratio = self._containment(ca, cb)
                if ratio is not None:
                    if ratio < 0.5:
                        errors.append(f"Join {label}: only {ratio:.0%} of {a.table}.{a.column} values exist in {b.table}.{b.column}; this join is very likely wrong.")
                    else:
                        notes.append(f"Join {label}: {ratio:.0%} of sampled values match.")
            if ca.get("is_unique") and not cb.get("is_unique"):
                fanned.add(((ca.get("schema_name") or "", ca["table_name"]), (cb.get("schema_name") or "", cb["table_name"])))
            elif cb.get("is_unique") and not ca.get("is_unique"):
                fanned.add(((cb.get("schema_name") or "", cb["table_name"]), (ca.get("schema_name") or "", ca["table_name"])))
        return fanned

    @staticmethod
    def _family(c: dict[str, Any]) -> str:
        from .sqlcheck import _coarse_type

        t = _coarse_type(c.get("data_type"))
        return "number" if t in ("BIGINT", "DOUBLE") else ("text" if t == "TEXT" else t)

    def _containment(self, a: dict[str, Any], b: dict[str, Any]) -> float | None:
        src = (
            exp.select(qcol(a["name"]).as_("v"))
            .distinct()
            .from_(self._table_expr(a))
            .where(exp.Not(this=exp.Is(this=qcol(a["name"]), expression=exp.Null())))
            .limit(300)
            .subquery("s")
        )
        tgt = self._table_expr(b).as_("t")
        query = (
            exp.select(exp.Count(this=exp.Star()).as_("total"), exp.Count(this=exp.column(exp.to_identifier(b["name"], quoted=True), table="t")).as_("hits"))
            .from_(src)
            .join(tgt, on=exp.EQ(this=exp.column(exp.to_identifier(b["name"], quoted=True), table="t"), expression=exp.column("v", table="s")), join_type="left")
        )
        try:
            rows = self._probe(query)
        except Exception:  # noqa: BLE001
            return None
        total, hits = rows[0][0] or 0, rows[0][1] or 0
        return (hits / total) if total else None

    # ---------------------------------------------------------------- fan-out
    def _check_fanout(self, analysis: Analysis, fanned: set[tuple[tuple[str, str], tuple[str, str]]], warnings: list[str]) -> None:
        if not fanned:
            return
        for agg in analysis.aggregates:
            if agg["distinct"] or not agg["columns"]:
                continue
            scope = {(s.casefold(), t.casefold()) for s, t in agg["scope_tables"]}
            # What is summed or averaged; with only constants (COUNT(CASE WHEN … THEN 1 END)) the
            # condition columns decide which rows are counted, so they stand in for the value.
            values = agg.get("value_columns") or agg["columns"]
            for col in values:
                for one_side, many_side in fanned:
                    if any(v.table.casefold() == many_side[1].casefold() for v in values):
                        continue  # the value is computed per row of the many side (qty * unit_cost): no fan-out
                    one_key = (one_side[0].casefold(), one_side[1].casefold())
                    many_key = (many_side[0].casefold(), many_side[1].casefold())
                    same_table = col.table.casefold() == one_side[1].casefold() and (
                        not col.schema or col.schema.casefold() == one_side[0].casefold()
                    )
                    in_scope = (one_key in scope or one_side[1].casefold() in {t for _, t in scope}) and (
                        many_key in scope or many_side[1].casefold() in {t for _, t in scope}
                    )
                    if same_table and in_scope:
                        warnings.append(
                            f"Fan-out: {agg['sql']} aggregates {one_side[1]}.{col.column}, but {one_side[1]} is joined "
                            f"one-to-many to {many_side[1]}, so each {one_side[1]} row is counted once per matching "
                            f"{many_side[1]} row. Aggregate {many_side[1]} first (subquery/CTE) or aggregate at "
                            f"{one_side[1]} grain."
                        )

    def _check_branch_fanout(
        self, analysis: Analysis, fanned: set[tuple[tuple[str, str], tuple[str, str]]], warnings: list[str]
    ) -> None:
        """Fan-out from a sibling branch: rows aggregated from one table repeat once per row of
        another table that hangs one-to-many from a table they share (a chasm trap). Summing a
        product's order lines while also joining its product tags repeats each line once per tag.

        _check_fanout covers aggregating the one side itself; this walks from the aggregated
        table up its many-to-one joins and flags every one-to-many join that leaves the walk.
        """
        if not fanned:
            return
        one_to_many = {(one[1].casefold(), many[1].casefold()) for one, many in fanned}
        names = {t.casefold(): t for _, t in analysis.tables}
        edges: dict[str, set[str]] = {}
        for a, b, _origin in analysis.joins:
            ta, tb = a.table.casefold(), b.table.casefold()
            if ta != tb:
                edges.setdefault(ta, set()).add(tb)
                edges.setdefault(tb, set()).add(ta)
        reported: set[str] = set()
        for agg in analysis.aggregates:
            if agg["distinct"] or not agg["columns"] or agg["sql"] in reported:
                continue
            scope = {t.casefold() for _, t in agg["scope_tables"]}
            start = {v.table.casefold() for v in (agg.get("value_columns") or agg["columns"])}
            if not start or not start <= scope:
                continue
            grouped = {t.casefold() for _, t in agg.get("group_tables") or []}
            branches: dict[str, list[str]] = {}
            reached, queue = set(start), list(start)
            while queue:
                u = queue.pop()
                for v in sorted(edges.get(u, ())):
                    if v not in scope or v in reached:
                        continue
                    if (u, v) not in one_to_many:
                        reached.add(v)  # many-to-one (or one-to-one): one row per aggregated row
                        queue.append(v)
                        continue
                    if u in start:
                        continue  # aggregating the one side itself: _check_fanout reports it
                    if self._branch(edges, v, u, scope) & grouped:
                        continue  # grouped by the branch (revenue by tag): one row per pair is the point
                    if v not in branches.setdefault(u, []):
                        branches[u].append(v)
            if not branches:
                continue
            reported.add(agg["sql"])
            agg_tables = ", ".join(sorted(names.get(t, t) for t in start))
            for u, vs in branches.items():
                parent = names.get(u, u)
                listed = " and ".join(names.get(v, v) for v in vs)
                warnings.append(
                    f"Fan-out: {agg['sql']} aggregates {agg_tables} rows, but {parent} is also joined one-to-many "
                    f"to {listed}, so each {agg_tables} row is repeated for every matching row of {listed}. "
                    f"{agg_tables} and {listed} are related only through {parent}: aggregate each one-to-many "
                    f"branch in its own subquery or CTE, then join the results on {parent}."
                )

    @staticmethod
    def _branch(edges: dict[str, set[str]], start: str, parent: str, scope: set[str]) -> set[str]:
        """Tables reachable from *start* without going back through *parent*."""
        seen, queue = {start}, [start]
        while queue:
            for nxt in edges.get(queue.pop(), ()):
                if nxt != parent and nxt in scope and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen

    # --------------------------------------------------------------- literals
    def _check_string_literals(self, analysis: Analysis, warnings: list[str]) -> None:
        checked: set[tuple[str, str, str]] = set()
        for f in analysis.string_filters:
            col = self._col(f["column"])
            if not col:
                continue
            for value in f["values"][:10]:
                key = (col["id"], f["op"], value)
                if key in checked:
                    continue
                checked.add(key)
                if f["op"] in ("LIKE", "ILIKE"):
                    self._check_like(col, value, f["op"], warnings)
                elif f["op"] in ("EQ", "IN"):
                    self._check_equals(col, value, warnings)

    def _check_equals(self, col: dict[str, Any], value: str, warnings: list[str]) -> None:
        where = exp.EQ(this=qcol(col["name"]), expression=literal(value))
        try:
            if self._probe(exp.select(exp.Literal.number(1)).from_(self._table_expr(col)).where(where).limit(1)):
                return
        except Exception:  # noqa: BLE001
            return
        suggestions = self._near_values(col, value)
        name = f"{col['table_name']}.{col['name']}"
        if suggestions:
            warnings.append(f"No row has {name} = '{value}'. Close real values: {', '.join(repr(s) for s in suggestions)}.")
        else:
            warnings.append(f"No row has {name} = '{value}', and no similar value was found. An empty result may be correct; use lookup_values to check.")

    def _check_like(self, col: dict[str, Any], pattern: str, op: str, warnings: list[str]) -> None:
        node = exp.Like if op == "LIKE" else exp.ILike
        where = node(this=qcol(col["name"]), expression=literal(pattern))
        try:
            if self._probe(exp.select(exp.Literal.number(1)).from_(self._table_expr(col)).where(where).limit(1)):
                return
        except Exception:  # noqa: BLE001
            return
        lowered = exp.Like(
            this=exp.Lower(this=exp.cast(qcol(col["name"]), "TEXT")), expression=literal(pattern.lower())
        )
        try:
            rows = self._probe(exp.select(qcol(col["name"])).distinct().from_(self._table_expr(col)).where(lowered).limit(5))
        except Exception:  # noqa: BLE001
            rows = []
        name = f"{col['table_name']}.{col['name']}"
        if rows:
            warnings.append(f"{name} {op} '{pattern}' matches nothing, but it matches case-insensitively: {', '.join(repr(r[0]) for r in rows)}.")
        else:
            warnings.append(f"{name} {op} '{pattern}' matches no rows.")

    def _near_values(self, col: dict[str, Any], value: str) -> list[Any]:
        table = self._table_expr(col)
        try:
            rows = self._probe(
                exp.select(qcol(col["name"])).distinct().from_(table)
                .where(exp.Not(this=exp.Is(this=qcol(col["name"]), expression=exp.Null())))
                .limit(LOW_CARD_THRESHOLD + 1)
            )
        except Exception:  # noqa: BLE001
            rows = []
        values = [r[0] for r in rows if isinstance(r[0], str)]
        if values and len(rows) <= LOW_CARD_THRESHOLD:
            lower_map = {v.casefold().strip(): v for v in values}
            if value.casefold().strip() in lower_map:
                return [lower_map[value.casefold().strip()]]
            words = re.findall(r"\w+", value.casefold())
            if words:
                containing = [v for v in values if all(w in v.casefold() for w in words)]
                if containing:
                    return containing[:3]
            close = difflib.get_close_matches(value, values, n=3, cutoff=CLOSE_MATCH_CUTOFF)
            if close:
                return close
            contains = [v for v in values if value.casefold() in v.casefold()]
            return contains[:3]
        # High cardinality: point-probe case/whitespace variants only.
        normalized = exp.EQ(
            this=exp.Lower(this=exp.Trim(this=exp.cast(qcol(col["name"]), "TEXT"))),
            expression=literal(value.strip().lower()),
        )
        try:
            rows = self._probe(exp.select(qcol(col["name"])).distinct().from_(table).where(normalized).limit(3))
            if rows:
                return [r[0] for r in rows]
            like = exp.Like(
                this=exp.Lower(this=exp.cast(qcol(col["name"]), "TEXT")), expression=literal(f"%{value.strip().lower()}%")
            )
            rows = self._probe(exp.select(qcol(col["name"])).distinct().from_(table).where(like).limit(3))
            return [r[0] for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def _check_numeric_scale(self, analysis: Analysis, warnings: list[str]) -> None:
        seen: set[str] = set()
        for f in analysis.numeric_filters:
            if f["op"] not in ("GT", "GTE", "LT", "LTE", "BETWEEN"):
                continue
            col = self._col(f["column"])
            if not col or col["id"] in seen:
                continue
            seen.add(col["id"])
            try:
                rows = self._probe(
                    exp.select(exp.Min(this=qcol(col["name"])), exp.Max(this=qcol(col["name"]))).from_(self._table_expr(col))
                )
            except Exception:  # noqa: BLE001
                continue
            lo, hi = rows[0] if rows else (None, None)
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
                continue
            for v in f["values"]:
                if lo <= v <= hi:
                    continue
                if lo <= v / 100 <= hi and hi <= 1.5:
                    warnings.append(
                        f"{f['sql']}: {col['table_name']}.{col['name']} ranges {lo}–{hi}, so {v} looks like a percent "
                        f"against a 0–1 fraction. Did you mean {v / 100:g}?"
                    )
                elif lo <= v * 100 <= hi and hi > 1.5 and v < 1:
                    warnings.append(
                        f"{f['sql']}: {col['table_name']}.{col['name']} ranges {lo}–{hi}, so {v} looks like a fraction "
                        f"against a percent column. Did you mean {v * 100:g}?"
                    )
                else:
                    warnings.append(f"{f['sql']}: the value {v} is outside the column's range {lo}–{hi}.")

    def _dry_run(self, tree: exp.Expression, errors: list[str]) -> None:
        try:
            wrapped = exp.select("*").from_(tree.copy().subquery("_aoq")).limit(0)
            self.run(wrapped.sql(dialect=self.dialect))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"The database rejected the query: {str(getattr(exc, 'orig', None) or exc).splitlines()[0][:400]}")

    # -------------------------------------------------------------- grounding
    def grounding(self, analysis: Analysis, sql: str) -> dict[str, Any]:
        terms: dict[str, dict[str, Any]] = {}
        unmodeled = []
        for p in analysis.columns:
            col = self._col(p)
            if not col or col.get("pseudo"):
                continue
            attrs = self.attr_by_column.get(col["id"], [])
            if not attrs and col["id"] not in self.rel_columns and not col["name"].startswith("_"):
                unmodeled.append(f"{col['table_name']}.{col['name']}")
            for a in attrs:
                t = terms.setdefault(a["term_name"], {"term": a["term_name"], "status": a["term_status"], "attributes": []})
                if a["display_name"] not in t["attributes"]:
                    t["attributes"].append(a["display_name"])
        normalized = " ".join(sql.split()).casefold()
        used_sql_attrs = []
        for s in self.sql_attributes:
            expr = " ".join((s["expression"] or "").split()).casefold()
            if s["kind"] in ("measure", "filter", "dimension") and expr and expr in normalized:
                used_sql_attrs.append({"term": s["term_name"], "name": s["name"], "status": s["status"]})
            elif s["kind"] in ("measure", "filter", "dimension") and expr:
                bare = _strip_qualifiers(s["expression"], self.dialect)
                if bare and bare in _strip_qualifiers(sql, self.dialect, whole=True):
                    used_sql_attrs.append({"term": s["term_name"], "name": s["name"], "status": s["status"]})
        return {
            "terms": list(terms.values()),
            "draft_terms": sorted(t["term"] for t in terms.values() if t["status"] != "certified"),
            "sql_attributes": used_sql_attrs,
            "unmodeled_columns": sorted(set(unmodeled)),
        }


def _strip_qualifiers(sql: str, dialect: str | None, whole: bool = False) -> str:
    """Expression text with table qualifiers removed, for loose matching."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect) if whole else sqlglot.parse_one(f"SELECT {sql}", read=dialect)
    except Exception:
        return ""
    tree = tree.copy()
    for col in tree.find_all(exp.Column):
        col.set("table", None)
    text = tree.sql(dialect=dialect)
    if not whole and text.upper().startswith("SELECT "):
        text = text[7:]
    return " ".join(text.split()).casefold()
