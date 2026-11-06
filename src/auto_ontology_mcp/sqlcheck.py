"""SQL parsing helpers: the read-only guard, column lineage and predicate extraction.

Everything here is static analysis with ``sqlglot``; the database probes that
build on it live in :mod:`auto_ontology_mcp.validation`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope


class SqlGuardError(ValueError):
    """The statement is not a single read-only query."""


_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "AlterTable",
        "TruncateTable",
        "Command",
        "Pragma",
        "Attach",
        "Detach",
        "Copy",
        "Grant",
        "Revoke",
        "Set",
        "Use",
        "LoadData",
        "Transaction",
        "Commit",
        "Rollback",
        "Lock",
        "Analyze",
        "Kill",
        "Cache",
        "Uncache",
    )
    if hasattr(exp, name)
)

# Functions that read or write outside the query's tables, or change server state.
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "lo_from_bytea",
        "lo_put",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "set_config",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_advisory_lock",
        "nextval",
        "setval",
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "sleep",
        "pg_sleep",
        "benchmark",
        "xp_cmdshell",
        "openrowset",
        "opendatasource",
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_text",
        "read_blob",
        "glob",
    }
)


# Name prefixes / suffixes of functions that touch files, other servers or server state.
_FORBIDDEN_PREFIXES = (
    "pg_read", "pg_write", "pg_file", "pg_ls", "pg_stat_file", "pg_terminate", "pg_cancel", "pg_reload",
    "pg_rotate", "pg_advisory", "pg_sleep", "pg_logical", "pg_replication", "pg_create", "pg_drop",
    "pg_switch", "pg_promote", "pg_backup", "pg_start", "pg_stop", "lo_", "dblink", "load_file", "utl_",
    "dbms_", "xp_", "sp_", "openquery", "openrowset", "opendatasource", "read_", "system", "shell",
)
_FORBIDDEN_SUFFIXES = ("_scan", "_attach")
_FORBIDDEN_EXACT = frozenset({"query", "query_table", "glob", "sniff_csv", "copy", "outfile", "dumpfile"})


def _forbidden_function(name: str) -> bool:
    n = name.lower().rsplit(".", 1)[-1]
    return (
        n in _FORBIDDEN_FUNCTIONS
        or n in _FORBIDDEN_EXACT
        or n.startswith(_FORBIDDEN_PREFIXES)
        or n.endswith(_FORBIDDEN_SUFFIXES)
    )


def guard_sources(tree: exp.Expression, catalog_tables: Iterable[tuple[str, str]]) -> None:
    """Only cataloged tables, CTEs, subqueries, VALUES and UNNEST may appear as row sources.

    A deny-list of functions cannot keep up with every engine's table functions
    (DuckDB reads files from ``FROM '/path'``, SQL Server has ``OPENQUERY``),
    so row sources are allow-listed against the scanned catalog instead.
    """
    known = {((schema or "").casefold(), name.casefold()) for schema, name in catalog_tables}
    names = {name for _, name in known}
    ctes = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlGuardError(
                f"Only tables from the catalog can be read; {table.this.sql()[:60]} is a table function."
            )
        name, db = table.name.casefold(), (table.db or "").casefold()
        if not db and name in ctes:
            continue
        if (db, name) in known or (not db and name in names):
            continue
        raise SqlGuardError(f"Table {table.sql()} is not in the catalog of this datasource.")


def parse_one(sql: str, dialect: str | None) -> exp.Expression:
    statements = [s for s in sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE) if s is not None]
    if len(statements) != 1:
        raise SqlGuardError(f"Expected exactly one statement, got {len(statements)}.")
    return statements[0]


def guard_readonly(sql: str, dialect: str | None) -> exp.Expression:
    """Parse *sql* and refuse anything but a single read-only query."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise SqlGuardError("Empty SQL.")
    try:
        tree = parse_one(text, dialect)
    except SqlGuardError:
        raise
    except Exception as exc:  # sqlglot.ParseError, TokenError
        raise SqlGuardError(
            f"Could not parse the SQL for dialect {dialect or 'generic'}: {str(exc).splitlines()[0]}"
        ) from exc

    if not isinstance(tree, (exp.Select, exp.SetOperation, exp.Subquery)):
        raise SqlGuardError(
            f"Only SELECT queries are allowed (got {type(tree).__name__.upper()})."
        )
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SqlGuardError(f"{type(node).__name__.upper()} is not allowed in a read-only query.")
        if isinstance(node, exp.Select) and node.args.get("into") is not None:
            raise SqlGuardError("SELECT … INTO creates a table and is not allowed.")
        if isinstance(node, exp.Func):
            name = (node.sql_name() if not isinstance(node, exp.Anonymous) else str(node.this)).lower()
            if _forbidden_function(name):
                raise SqlGuardError(f"Function {name}() is not allowed.")
        if isinstance(node, exp.Lock) or (isinstance(node, exp.Select) and node.args.get("locks")):
            raise SqlGuardError("Row locking clauses (FOR UPDATE/SHARE) are not allowed.")
    return tree


# --------------------------------------------------------------------- lineage


@dataclass(frozen=True)
class PhysicalColumn:
    schema: str
    table: str
    column: str

    @property
    def table_key(self) -> tuple[str, str]:
        return (self.schema.casefold(), self.table.casefold())


@dataclass
class Analysis:
    tree: exp.Expression
    qualified: bool
    error: str | None = None
    tables: list[tuple[str, str]] = field(default_factory=list)
    columns: list[PhysicalColumn] = field(default_factory=list)
    joins: list[tuple[PhysicalColumn, PhysicalColumn, str]] = field(default_factory=list)
    string_filters: list[dict[str, Any]] = field(default_factory=list)
    numeric_filters: list[dict[str, Any]] = field(default_factory=list)
    aggregates: list[dict[str, Any]] = field(default_factory=list)


def _coarse_type(data_type: str | None) -> str:
    t = (data_type or "").lower()
    if any(k in t for k in ("int", "serial")):
        return "BIGINT"
    if any(k in t for k in ("real", "float", "double", "numeric", "decimal", "money", "number")):
        return "DOUBLE"
    if "bool" in t:
        return "BOOLEAN"
    if "timestamp" in t or "datetime" in t:
        return "TIMESTAMP"
    if t.startswith("date"):
        return "DATE"
    return "TEXT"


def schema_mapping(columns: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    """Build a sqlglot schema mapping from catalog column rows.

    Returns ``(mapping, nested)`` where ``nested`` tells whether tables are
    keyed by schema (``{schema: {table: {col: type}}}``).
    """
    by_schema: dict[str, dict[str, dict[str, str]]] = {}
    for col in columns:
        by_schema.setdefault(col.get("schema_name") or "", {}).setdefault(col["table_name"], {})[
            col["name"]
        ] = _coarse_type(col.get("data_type"))
    if set(by_schema) == {""}:
        return by_schema[""], False
    if "" in by_schema:
        # Mixed: put schema-less tables under a placeholder schema.
        by_schema["main"] = {**by_schema.get("main", {}), **by_schema.pop("")}
    return by_schema, True


def _source_for(scope: Scope, column: exp.Column) -> Any:
    return scope.sources.get(column.table)


def _trace(scope: Scope, column: exp.Column, depth: int = 0) -> PhysicalColumn | None:
    """Follow a column through CTEs / derived tables down to a physical column."""
    if depth > 8:
        return None
    source = _source_for(scope, column)
    if isinstance(source, exp.Table):
        return PhysicalColumn(schema=source.db or "", table=source.name, column=column.name)
    if isinstance(source, Scope):
        select = source.expression
        if isinstance(select, exp.SetOperation):
            select = select.left
            while isinstance(select, exp.SetOperation):
                select = select.left
        if not isinstance(select, exp.Select):
            return None
        for projection in select.expressions:
            if projection.alias_or_name == column.name:
                inner = projection.this if isinstance(projection, exp.Alias) else projection
                if isinstance(inner, exp.Column):
                    return _trace(source, inner, depth + 1)
                return None
    return None


def _literal_value(node: exp.Expression) -> Any:
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        try:
            return float(node.this) if any(c in node.this for c in ".eE") else int(node.this)
        except ValueError:
            return node.this
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        value = _literal_value(node.this)
        return -value if isinstance(value, (int, float)) else None
    return None


def _column_side(node: exp.Expression) -> exp.Column | None:
    """The column inside ``col`` / ``LOWER(col)`` / ``TRIM(col)`` / ``CAST(col …)``."""
    while isinstance(node, (exp.Lower, exp.Upper, exp.Trim, exp.Cast, exp.Paren)):
        node = node.this
    return node if isinstance(node, exp.Column) else None


def analyze(sql_or_tree: str | exp.Expression, columns: list[dict[str, Any]], dialect: str | None) -> Analysis:
    """Qualify the query against the catalog and collect what validation needs."""
    tree = sqlglot.parse_one(sql_or_tree, read=dialect) if isinstance(sql_or_tree, str) else sql_or_tree
    mapping, nested = schema_mapping(columns)
    default_db = None
    if nested:
        schemas = {c.get("schema_name") or "" for c in columns}
        for candidate in ("public", "dbo", "main"):
            if candidate in schemas:
                default_db = candidate
                break
    analysis = Analysis(tree=tree, qualified=False)
    try:
        qualified = qualify(
            tree.copy(),
            schema=mapping,
            dialect=dialect,
            db=default_db,
            validate_qualify_columns=True,
            quote_identifiers=False,
            identify=False,
        )
        analysis.tree = qualified
        analysis.qualified = True
    except Exception as exc:  # OptimizeError and friends
        analysis.error = str(exc).splitlines()[0]
        qualified = tree

    seen_tables: set[tuple[str, str]] = set()
    seen_cols: set[PhysicalColumn] = set()
    try:
        scopes = traverse_scope(qualified)
    except Exception as exc:
        analysis.error = analysis.error or str(exc).splitlines()[0]
        scopes = []

    for scope in scopes:
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                key = (source.db or "", source.name)
                if key not in seen_tables:
                    seen_tables.add(key)
                    analysis.tables.append(key)
        for column in scope.columns:
            phys = _trace(scope, column)
            if phys and phys not in seen_cols:
                seen_cols.add(phys)
                analysis.columns.append(phys)

        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        # Join predicates: col = col across two different physical tables.
        predicate_roots: list[tuple[exp.Expression, str]] = []
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if on is not None:
                predicate_roots.append((on, "join"))
        where = select.args.get("where")
        if where is not None:
            predicate_roots.append((where.this, "where"))
        having = select.args.get("having")
        if having is not None:
            predicate_roots.append((having.this, "having"))

        for root, origin in predicate_roots:
            for node in root.find_all(exp.EQ, exp.NEQ, exp.In, exp.Like, exp.ILike, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between):
                if isinstance(node, exp.EQ) and isinstance(node.left, exp.Column) and isinstance(node.right, exp.Column):
                    a, b = _trace(scope, node.left), _trace(scope, node.right)
                    if a and b and a.table_key != b.table_key:
                        analysis.joins.append((a, b, origin))
                    continue
                if isinstance(node, (exp.EQ, exp.NEQ, exp.Like, exp.ILike)):
                    col = _column_side(node.left) or _column_side(node.right)
                    lit = node.right if _column_side(node.left) else node.left
                    value = _literal_value(lit)
                    if col is not None and value is not None:
                        phys = _trace(scope, col)
                        if phys is None:
                            continue
                        entry = {"column": phys, "op": type(node).__name__.upper(), "values": [value], "sql": node.sql(dialect=dialect)}
                        if isinstance(value, str):
                            analysis.string_filters.append(entry)
                        elif isinstance(node, (exp.EQ, exp.NEQ)):
                            analysis.numeric_filters.append(entry)
                    continue
                if isinstance(node, exp.In):
                    col = _column_side(node.this)
                    values = [_literal_value(v) for v in node.expressions]
                    if col is not None and values and all(v is not None for v in values):
                        phys = _trace(scope, col)
                        if phys is None:
                            continue
                        entry = {"column": phys, "op": "IN", "values": values, "sql": node.sql(dialect=dialect)}
                        if all(isinstance(v, str) for v in values):
                            analysis.string_filters.append(entry)
                        else:
                            analysis.numeric_filters.append(entry)
                    continue
                if isinstance(node, exp.Between):
                    col = _column_side(node.this)
                    low, high = _literal_value(node.args.get("low")), _literal_value(node.args.get("high"))
                    if col is not None and isinstance(low, (int, float)) and isinstance(high, (int, float)):
                        phys = _trace(scope, col)
                        if phys:
                            analysis.numeric_filters.append({"column": phys, "op": "BETWEEN", "values": [low, high], "sql": node.sql(dialect=dialect)})
                    continue
                # GT / GTE / LT / LTE
                col = _column_side(node.left)
                value = _literal_value(node.right)
                if col is None:
                    col = _column_side(node.right)
                    value = _literal_value(node.left)
                if col is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
                    phys = _trace(scope, col)
                    if phys:
                        analysis.numeric_filters.append({"column": phys, "op": type(node).__name__.upper(), "values": [value], "sql": node.sql(dialect=dialect)})

        for agg in select.find_all(exp.Sum, exp.Avg, exp.Count):
            if agg.find_ancestor(exp.Select) is not select:
                continue
            arg = agg.this
            if isinstance(arg, exp.Distinct):
                distinct = True
                arg = arg.expressions[0] if arg.expressions else None
            else:
                distinct = False
            cols = [] if arg is None else list(arg.find_all(exp.Column))
            physical = [p for p in (_trace(scope, c) for c in cols) if p]
            analysis.aggregates.append(
                {
                    "function": type(agg).__name__.upper(),
                    "distinct": distinct,
                    "columns": physical,
                    "sql": agg.sql(dialect=dialect),
                    "scope_tables": [
                        (s.db or "", s.name) for s in scope.sources.values() if isinstance(s, exp.Table)
                    ],
                }
            )
    return analysis


# ------------------------------------------------------------ expression mining
# Port of Auto Ontology's ``_extract_expressions`` (sql_attribute_suggester.py,
# Apache-2.0, NVIDIA): candidate reusable expressions found in executed SQL.

_BARE_KEYWORDS = frozenset({"count(*)", "count(1)", "1 = 1", "true", "false"})


def extract_expressions(sql_text: str, dialect: str | None) -> list[str]:
    try:
        ast = sqlglot.parse_one(sql_text, read=dialect, error_level=ErrorLevel.IGNORE)
    except Exception:
        return []
    if ast is None:
        return []

    seen: set[str] = set()
    results: list[str] = []
    cmp_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between, exp.Like, exp.Is)

    def _is_pure_fk_eq(node: exp.Expression) -> bool:
        return isinstance(node, exp.EQ) and isinstance(node.left, exp.Column) and isinstance(node.right, exp.Column)

    def _add(node: exp.Expression) -> None:
        text = node.sql(dialect=dialect).strip()
        key = text.lower()
        if len(text) < 5 or key in seen:
            return
        if isinstance(node, (exp.Column, exp.Star, exp.Literal)):
            return
        if key in _BARE_KEYWORDS or _is_pure_fk_eq(node):
            return
        seen.add(key)
        results.append(text)

    where = ast.find(exp.Where)
    if where:
        for node in where.walk():
            if isinstance(node, cmp_types):
                _add(node)
    for node in ast.walk():
        if isinstance(node, (exp.AggFunc, exp.Anonymous, exp.Case)):
            _add(node)
    having = ast.find(exp.Having)
    if having and having.this:
        _add(having.this)
    for join in ast.find_all(exp.Join):
        on = join.args.get("on")
        if on:
            _add(on)
            for node in on.walk():
                if isinstance(node, cmp_types):
                    _add(node)
    for set_op in ast.find_all(exp.Union, exp.Intersect, exp.Except):
        _add(set_op)
        for branch in (set_op.left, set_op.right):
            if branch is not None:
                _add(branch)
    return results


def transpile(sql: str, dialect: str | None) -> str:
    """Translate neutral (postgres-style) probe SQL into the target dialect."""
    if not dialect or dialect == "postgres":
        return sql
    return sqlglot.transpile(sql, read="postgres", write=dialect)[0]


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip().rstrip(";").casefold()
